"""Unit tests for shared.s3_utils."""

import json

import pytest

from helpers import make_s3_event
from shared.s3_utils import (
    MalformedFileError,
    UnsupportedFormatError,
    content_sha256,
    keys_from_event,
    load_rows,
    write_partitioned,
)


class TestContentSha256:
    def test_is_deterministic(self):
        assert content_sha256(b"hello") == content_sha256(b"hello")

    def test_differs_for_different_content(self):
        assert content_sha256(b"a") != content_sha256(b"b")


class TestKeysFromEvent:
    def test_extracts_bucket_and_key(self):
        event = make_s3_event("raw-bucket", "incoming/orders.csv")
        assert keys_from_event(event) == [("raw-bucket", "incoming/orders.csv")]

    def test_url_decodes_keys(self):
        event = make_s3_event("raw-bucket", "incoming/june+orders+%282026%29.csv")
        assert keys_from_event(event) == [("raw-bucket", "incoming/june orders (2026).csv")]

    def test_ignores_non_s3_records(self):
        event = {"Records": [{"eventSource": "aws:sqs", "body": "{}"}]}
        assert keys_from_event(event) == []

    def test_empty_event(self):
        assert keys_from_event({}) == []


class TestLoadRows:
    def test_csv_with_normalized_headers(self):
        body = b"Order_ID,Quantity\nord-1,2\nord-2,5\n"
        rows = load_rows(body, "drop/orders.csv")
        assert rows == [
            {"order_id": "ord-1", "quantity": "2"},
            {"order_id": "ord-2", "quantity": "5"},
        ]

    def test_csv_skips_blank_lines(self):
        body = b"order_id\nord-1\n,\n\n"
        assert load_rows(body, "orders.csv") == [{"order_id": "ord-1"}]

    def test_csv_tolerates_utf8_bom(self):
        body = "﻿order_id\nord-1\n".encode()
        assert load_rows(body, "orders.csv") == [{"order_id": "ord-1"}]

    def test_json_array(self):
        body = json.dumps([{"order_id": "ord-1"}, {"order_id": "ord-2"}]).encode()
        assert len(load_rows(body, "orders.json")) == 2

    def test_json_single_object(self):
        body = json.dumps({"order_id": "ord-1"}).encode()
        assert load_rows(body, "orders.json") == [{"order_id": "ord-1"}]

    def test_jsonl(self):
        body = b'{"order_id": "ord-1"}\n\n{"order_id": "ord-2"}\n'
        assert len(load_rows(body, "orders.jsonl")) == 2

    def test_invalid_json_raises(self):
        with pytest.raises(MalformedFileError):
            load_rows(b"{not json", "orders.json")

    def test_invalid_jsonl_line_reports_line_number(self):
        with pytest.raises(MalformedFileError, match="line 2"):
            load_rows(b'{"a": 1}\n{broken\n', "orders.ndjson")

    def test_json_scalar_raises(self):
        with pytest.raises(MalformedFileError):
            load_rows(b'"just a string"', "orders.json")

    def test_unsupported_extension_raises(self):
        with pytest.raises(UnsupportedFormatError):
            load_rows(b"data", "orders.parquet")

    def test_non_utf8_raises(self):
        with pytest.raises(MalformedFileError):
            load_rows(b"\xff\xfe\x00bad", "orders.csv")


class TestWritePartitioned:
    def test_groups_records_by_partition_field(self, s3_client):
        s3_client.create_bucket(Bucket="processed")
        records = [
            {"order_id": "a", "order_date": "2026-06-30"},
            {"order_id": "b", "order_date": "2026-07-01"},
            {"order_id": "c", "order_date": "2026-06-30"},
        ]

        keys = write_partitioned(s3_client, "processed", "orders", records, "f" * 64)

        assert keys == [
            f"dataset=orders/dt=2026-06-30/part-{'f' * 16}.jsonl",
            f"dataset=orders/dt=2026-07-01/part-{'f' * 16}.jsonl",
        ]
        first = s3_client.get_object(Bucket="processed", Key=keys[0])
        lines = first["Body"].read().decode().strip().splitlines()
        assert [json.loads(line)["order_id"] for line in lines] == ["a", "c"]
        assert first["Metadata"]["record-count"] == "2"

    def test_rewrite_overwrites_same_key(self, s3_client):
        s3_client.create_bucket(Bucket="processed")
        records = [{"order_id": "a", "order_date": "2026-06-30"}]

        write_partitioned(s3_client, "processed", "orders", records, "a" * 64)
        write_partitioned(s3_client, "processed", "orders", records, "a" * 64)

        listing = s3_client.list_objects_v2(Bucket="processed")
        assert listing["KeyCount"] == 1
