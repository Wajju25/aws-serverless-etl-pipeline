"""Unit tests for shared.dynamo."""

from decimal import Decimal

import pytest

from conftest import TABLE_NAME
from shared.dynamo import (
    UnprocessedItemsError,
    batch_write_with_retry,
    claim_file,
    release_claim,
)


def _items(count: int) -> list[dict]:
    return [
        {"pk": f"ORDER#{i}", "sk": "CUSTOMER#c", "quantity": i, "unit_price": Decimal("9.99")}
        for i in range(count)
    ]


class TestBatchWriteWithRetry:
    def test_writes_more_than_one_batch(self, etl_stack):
        client = etl_stack["dynamodb"]

        written = batch_write_with_retry(client, TABLE_NAME, _items(60))

        assert written == 60
        scan = client.scan(TableName=TABLE_NAME, Select="COUNT")
        assert scan["Count"] == 60

    def test_empty_input_writes_nothing(self, etl_stack):
        assert batch_write_with_retry(etl_stack["dynamodb"], TABLE_NAME, []) == 0

    def test_retries_unprocessed_items(self, monkeypatch):
        monkeypatch.setattr("shared.dynamo.time.sleep", lambda _: None)

        class FlakyClient:
            calls = 0

            def batch_write_item(self, RequestItems):  # noqa: N803 - boto3 API shape
                type(self).calls += 1
                table, requests = next(iter(RequestItems.items()))
                if type(self).calls == 1:
                    return {"UnprocessedItems": {table: requests[:2]}}
                return {"UnprocessedItems": {}}

        written = batch_write_with_retry(FlakyClient(), "any-table", _items(5))

        assert written == 5
        assert FlakyClient.calls == 2

    def test_raises_after_exhausting_attempts(self, monkeypatch):
        monkeypatch.setattr("shared.dynamo.time.sleep", lambda _: None)

        class ThrottledClient:
            def batch_write_item(self, RequestItems):  # noqa: N803 - boto3 API shape
                return {"UnprocessedItems": dict(RequestItems)}

        with pytest.raises(UnprocessedItemsError) as excinfo:
            batch_write_with_retry(ThrottledClient(), "any-table", _items(3), max_attempts=3)

        assert excinfo.value.remaining == 3
        assert excinfo.value.attempts == 3


class TestFileClaims:
    def test_first_claim_wins(self, etl_stack):
        client = etl_stack["dynamodb"]

        assert claim_file(client, TABLE_NAME, "hash-1", "s3://raw/a.csv") is True
        assert claim_file(client, TABLE_NAME, "hash-1", "s3://raw/a-copy.csv") is False

    def test_different_hashes_claim_independently(self, etl_stack):
        client = etl_stack["dynamodb"]

        assert claim_file(client, TABLE_NAME, "hash-1", "s3://raw/a.csv") is True
        assert claim_file(client, TABLE_NAME, "hash-2", "s3://raw/b.csv") is True

    def test_release_allows_reclaim(self, etl_stack):
        client = etl_stack["dynamodb"]
        claim_file(client, TABLE_NAME, "hash-1", "s3://raw/a.csv")

        release_claim(client, TABLE_NAME, "hash-1")

        assert claim_file(client, TABLE_NAME, "hash-1", "s3://raw/a.csv") is True
