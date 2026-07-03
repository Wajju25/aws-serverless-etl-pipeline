"""End-to-end tests for the ingest Lambda handler, backed by moto."""

import json
from decimal import Decimal

import pytest

from conftest import PROCESSED_BUCKET, RAW_BUCKET, TABLE_NAME
from functions.ingest.handler import IngestionError, lambda_handler
from helpers import make_s3_event

VALID_CSV = (
    "order_id,customer_id,sku,quantity,unit_price,currency,status,order_date\n"
    "ord-1001,cust-1,SKU-A,2,10.00,USD,paid,2026-06-30\n"
    "ord-1002,cust-2,SKU-B,1,25.50,EUR,shipped,2026-07-01\n"
    "ord-1003,cust-1,SKU-C,4,3.25,USD,pending,2026-06-30\n"
)

MOSTLY_INVALID_CSV = (
    "order_id,customer_id,sku,quantity,unit_price,currency,status,order_date\n"
    "ord-1,cust-1,SKU-A,not-a-number,10.00,USD,paid,2026-06-30\n"
    "ord-2,cust-2,SKU-B,1,free,EUR,shipped,2026-07-01\n"
    "ord-3,cust-3,SKU-C,2,5.00,USD,paid,2026-06-30\n"
)


def _upload(s3_client, key: str, body: str) -> dict:
    s3_client.put_object(Bucket=RAW_BUCKET, Key=key, Body=body.encode())
    return make_s3_event(RAW_BUCKET, key)


def _scan_entities(dynamodb_client, entity: str) -> list[dict]:
    items = dynamodb_client.scan(TableName=TABLE_NAME)["Items"]
    return [item for item in items if item["entity"]["S"] == entity]


class TestHappyPath:
    def test_csv_file_is_loaded(self, etl_stack, lambda_context):
        event = _upload(etl_stack["s3"], "incoming/orders.csv", VALID_CSV)

        result = lambda_handler(event, lambda_context)

        assert result["processed"] == 1
        summary = result["objects"][0]
        assert summary["status"] == "processed"
        assert summary["records_written"] == 3
        assert summary["records_rejected"] == 0
        assert _scan_entities(etl_stack["dynamodb"], "order")
        assert len(_scan_entities(etl_stack["dynamodb"], "order")) == 3
        assert len(_scan_entities(etl_stack["dynamodb"], "file_claim")) == 1

    def test_curated_item_shape(self, etl_stack, lambda_context):
        event = _upload(etl_stack["s3"], "incoming/orders.csv", VALID_CSV)
        lambda_handler(event, lambda_context)

        item = etl_stack["dynamodb"].get_item(
            TableName=TABLE_NAME,
            Key={"pk": {"S": "ORDER#ord-1001"}, "sk": {"S": "CUSTOMER#cust-1"}},
        )["Item"]

        assert Decimal(item["total"]["N"]) == Decimal("20.00")
        assert item["currency"]["S"] == "USD"
        assert item["source_key"]["S"] == f"s3://{RAW_BUCKET}/incoming/orders.csv"
        assert item["source_sha256"]["S"]

    def test_partitioned_output_written(self, etl_stack, lambda_context):
        event = _upload(etl_stack["s3"], "incoming/orders.csv", VALID_CSV)

        result = lambda_handler(event, lambda_context)

        output_keys = result["objects"][0]["output_keys"]
        assert len(output_keys) == 2  # two distinct order dates
        assert all(key.startswith("dataset=orders/dt=") for key in output_keys)
        body = (
            etl_stack["s3"].get_object(Bucket=PROCESSED_BUCKET, Key=output_keys[0])["Body"].read()
        )
        rows = [json.loads(line) for line in body.decode().strip().splitlines()]
        assert {row["order_id"] for row in rows} == {"ord-1001", "ord-1003"}

    def test_json_array_file_is_loaded(self, etl_stack, lambda_context):
        rows = [
            {
                "order_id": "ord-9",
                "customer_id": "cust-9",
                "sku": "SKU-Z",
                "quantity": 1,
                "unit_price": "99.00",
                "currency": "GBP",
                "status": "delivered",
                "order_date": "2026-06-28",
            }
        ]
        event = _upload(etl_stack["s3"], "incoming/orders.json", json.dumps(rows))

        result = lambda_handler(event, lambda_context)

        assert result["objects"][0]["records_written"] == 1


class TestIdempotency:
    def test_duplicate_content_is_skipped(self, etl_stack, lambda_context):
        first = _upload(etl_stack["s3"], "incoming/orders.csv", VALID_CSV)
        lambda_handler(first, lambda_context)

        duplicate = _upload(etl_stack["s3"], "incoming/orders-copy.csv", VALID_CSV)
        result = lambda_handler(duplicate, lambda_context)

        assert result["objects"][0]["status"] == "skipped_duplicate"
        assert len(_scan_entities(etl_stack["dynamodb"], "order")) == 3

    def test_failed_file_releases_claim(self, etl_stack, lambda_context):
        event = _upload(etl_stack["s3"], "incoming/bad.csv", MOSTLY_INVALID_CSV)

        with pytest.raises(IngestionError):
            lambda_handler(event, lambda_context)

        assert _scan_entities(etl_stack["dynamodb"], "file_claim") == []


class TestFailureModes:
    def test_mostly_invalid_file_fails_invocation(self, etl_stack, lambda_context):
        event = _upload(etl_stack["s3"], "incoming/bad.csv", MOSTLY_INVALID_CSV)

        with pytest.raises(IngestionError, match="rows invalid"):
            lambda_handler(event, lambda_context)

        assert _scan_entities(etl_stack["dynamodb"], "order") == []

    def test_minority_invalid_rows_still_load(self, etl_stack, lambda_context):
        csv_body = VALID_CSV + "ord-bad,cust-9,SKU-D,-5,1.00,USD,paid,2026-06-30\n"
        event = _upload(etl_stack["s3"], "incoming/mixed.csv", csv_body)

        result = lambda_handler(event, lambda_context)

        summary = result["objects"][0]
        assert summary["records_written"] == 3
        assert summary["records_rejected"] == 1

    def test_unsupported_extension_fails(self, etl_stack, lambda_context):
        event = _upload(etl_stack["s3"], "incoming/orders.parquet", "binary-ish")

        with pytest.raises(IngestionError, match="unsupported file extension"):
            lambda_handler(event, lambda_context)

    def test_missing_object_fails(self, etl_stack, lambda_context):
        event = make_s3_event(RAW_BUCKET, "incoming/never-uploaded.csv")

        with pytest.raises(IngestionError):
            lambda_handler(event, lambda_context)

    def test_empty_file_processes_without_records(self, etl_stack, lambda_context):
        event = _upload(etl_stack["s3"], "incoming/empty.csv", "order_id\n")

        result = lambda_handler(event, lambda_context)

        summary = result["objects"][0]
        assert summary["status"] == "processed"
        assert summary["records_written"] == 0
        assert summary["output_keys"] == []


class TestEventEdgeCases:
    def test_event_without_records(self, etl_stack, lambda_context):
        assert lambda_handler({}, lambda_context) == {"processed": 0, "objects": []}

    def test_multiple_objects_in_one_event(self, etl_stack, lambda_context):
        s3_client = etl_stack["s3"]
        s3_client.put_object(Bucket=RAW_BUCKET, Key="a.csv", Body=VALID_CSV.encode())
        other_csv = VALID_CSV.replace("ord-100", "ord-200")
        s3_client.put_object(Bucket=RAW_BUCKET, Key="b.csv", Body=other_csv.encode())
        event = make_s3_event(RAW_BUCKET, "a.csv", "b.csv")

        result = lambda_handler(event, lambda_context)

        assert result["processed"] == 2
        assert len(_scan_entities(etl_stack["dynamodb"], "order")) == 6
