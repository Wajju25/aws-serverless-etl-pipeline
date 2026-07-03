"""DynamoDB write helpers: batched writes with backoff and idempotency claims."""

from __future__ import annotations

import random
import time
from collections.abc import Iterator
from typing import Any

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

#: DynamoDB's hard limit on items per BatchWriteItem request.
MAX_BATCH_SIZE = 25

_serializer = TypeSerializer()


class UnprocessedItemsError(RuntimeError):
    """Raised when items remain unprocessed after all retry attempts."""

    def __init__(self, remaining: int, attempts: int) -> None:
        self.remaining = remaining
        self.attempts = attempts
        super().__init__(f"{remaining} item(s) still unprocessed after {attempts} attempt(s)")


def _serialize(item: dict[str, Any]) -> dict[str, Any]:
    """Convert a plain dict into the DynamoDB wire format."""
    return {key: _serializer.serialize(value) for key, value in item.items()}


def _chunks(items: list[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    """Yield *items* in slices of at most *size*."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def batch_write_with_retry(
    client: Any,
    table_name: str,
    items: list[dict[str, Any]],
    *,
    max_attempts: int = 6,
    base_delay: float = 0.2,
    max_delay: float = 8.0,
) -> int:
    """Write *items* with ``BatchWriteItem``, retrying unprocessed items.

    DynamoDB may return ``UnprocessedItems`` under throttling even when the
    request succeeds, so each batch is retried with capped exponential
    backoff and jitter until it drains or *max_attempts* is exhausted.

    Returns:
        The number of items written.

    Raises:
        UnprocessedItemsError: if a batch fails to drain within the attempt
            budget. The caller should let the invocation fail so the event is
            retried and eventually dead-lettered.
    """
    written = 0
    for batch in _chunks(items, MAX_BATCH_SIZE):
        pending = [{"PutRequest": {"Item": _serialize(item)}} for item in batch]
        attempt = 0
        while pending:
            response = client.batch_write_item(RequestItems={table_name: pending})
            unprocessed = response.get("UnprocessedItems", {}).get(table_name, [])
            written += len(pending) - len(unprocessed)
            if not unprocessed:
                break
            attempt += 1
            if attempt >= max_attempts:
                raise UnprocessedItemsError(len(unprocessed), attempt)
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            time.sleep(delay + random.uniform(0, base_delay))
            pending = unprocessed
    return written


def claim_file(client: Any, table_name: str, content_hash: str, source_key: str) -> bool:
    """Atomically claim a file for processing via a conditional put.

    The claim item is keyed on the object's content hash, so the same bytes
    can never be processed twice — regardless of key name, retry, or
    duplicate event delivery.

    Returns:
        ``True`` if this invocation won the claim, ``False`` if the file was
        already claimed (a duplicate).
    """
    item = {
        "pk": f"FILE#{content_hash}",
        "sk": "INGEST",
        "entity": "file_claim",
        "source_key": source_key,
        "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        client.put_item(
            TableName=table_name,
            Item=_serialize(item),
            ConditionExpression="attribute_not_exists(pk)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True


def release_claim(client: Any, table_name: str, content_hash: str) -> None:
    """Delete a file claim so a failed file can be retried cleanly."""
    client.delete_item(
        TableName=table_name,
        Key=_serialize({"pk": f"FILE#{content_hash}", "sk": "INGEST"}),
    )
