"""S3 helpers: event parsing, raw-file decoding, and curated output writes."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from typing import Any
from urllib.parse import unquote_plus

#: Hive-style partition column used when laying out curated output.
DEFAULT_PARTITION_FIELD = "order_date"


class UnsupportedFormatError(ValueError):
    """Raised when an object's extension maps to no known parser."""


class MalformedFileError(ValueError):
    """Raised when a file's content can't be decoded or parsed."""


def content_sha256(body: bytes) -> str:
    """Return the hex SHA-256 digest of an object body.

    The digest is the pipeline's idempotency key: re-delivering the same
    bytes (retries, duplicate S3 events, manual re-uploads) never produces
    duplicate output.
    """
    return hashlib.sha256(body).hexdigest()


def keys_from_event(event: dict[str, Any]) -> list[tuple[str, str]]:
    """Extract ``(bucket, key)`` pairs from an S3 event notification.

    Keys arrive URL-encoded (spaces become ``+``), so each key is decoded
    with ``unquote_plus``. Records that don't originate from S3 are ignored
    rather than treated as errors, which keeps the handler safe to wire to
    test events.
    """
    objects: list[tuple[str, str]] = []
    for record in event.get("Records", []):
        s3_section = record.get("s3")
        if record.get("eventSource") != "aws:s3" or not s3_section:
            continue
        bucket = s3_section["bucket"]["name"]
        key = unquote_plus(s3_section["object"]["key"])
        objects.append((bucket, key))
    return objects


def _row_has_data(row: dict[Any, Any]) -> bool:
    """True when any cell in a CSV row holds a non-blank value.

    ``DictReader`` stores overflow cells (rows wider than the header) as a
    list under the ``None`` restkey, so values may be strings or lists.
    """
    for value in row.values():
        if isinstance(value, list):
            if any(cell and cell.strip() for cell in value):
                return True
        elif value and value.strip():
            return True
    return False


def _rows_from_csv(text: str) -> list[dict[str, Any]]:
    """Parse CSV text into dict rows with normalized (lowercase) headers."""
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return []
    reader.fieldnames = [name.strip().lower() for name in reader.fieldnames]
    return [row for row in reader if _row_has_data(row)]


def _rows_from_json(text: str) -> list[dict[str, Any]]:
    """Parse a JSON document: either an array of objects or a single object."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedFileError(f"invalid JSON document: {exc}") from exc
    if isinstance(document, list):
        return document
    if isinstance(document, dict):
        return [document]
    raise MalformedFileError(f"expected a JSON object or array, got {type(document).__name__}")


def _rows_from_jsonl(text: str) -> list[dict[str, Any]]:
    """Parse newline-delimited JSON, skipping blank lines."""
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise MalformedFileError(f"invalid JSON on line {line_number}: {exc}") from exc
    return rows


def load_rows(body: bytes, key: str) -> list[dict[str, Any]]:
    """Decode an object body into raw dict rows based on its extension.

    Supported extensions: ``.csv``, ``.json``, ``.jsonl``, and ``.ndjson``.
    Content is decoded as UTF-8 with BOM tolerance, since exported files from
    spreadsheet tools frequently carry one.

    Raises:
        UnsupportedFormatError: for any other extension.
        MalformedFileError: when the bytes can't be decoded or parsed.
    """
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MalformedFileError(f"object is not valid UTF-8: {exc}") from exc

    suffix = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    if suffix == "csv":
        return _rows_from_csv(text)
    if suffix == "json":
        return _rows_from_json(text)
    if suffix in ("jsonl", "ndjson"):
        return _rows_from_jsonl(text)
    raise UnsupportedFormatError(f"unsupported file extension for key {key!r}")


def write_partitioned(
    client: Any,
    bucket: str,
    dataset: str,
    records: list[dict[str, Any]],
    content_hash: str,
    partition_field: str = DEFAULT_PARTITION_FIELD,
) -> list[str]:
    """Write curated records to S3 in a Hive-partitioned layout.

    Records are grouped by *partition_field* and written as one
    newline-delimited JSON object per partition under::

        dataset=<dataset>/dt=<partition-value>/part-<hash-prefix>.jsonl

    The layout is Athena and Glue friendly, and the deterministic part name
    (derived from the source content hash) makes retried writes overwrite
    themselves instead of accumulating duplicates.

    Returns:
        The list of keys written, in partition order.
    """
    partitions: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        partitions.setdefault(str(record.get(partition_field, "unknown")), []).append(record)

    written: list[str] = []
    for partition_value, group in sorted(partitions.items()):
        key = f"dataset={dataset}/dt={partition_value}/part-{content_hash[:16]}.jsonl"
        body = "\n".join(json.dumps(row, separators=(",", ":"), default=str) for row in group)
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=(body + "\n").encode("utf-8"),
            ContentType="application/x-ndjson",
            Metadata={"source-sha256": content_hash, "record-count": str(len(group))},
        )
        written.append(key)
    return written
