"""Ingestion Lambda: validate, transform, and load raw S3 drops.

Triggered asynchronously by ``s3:ObjectCreated:*`` notifications on the raw
bucket. For each object the handler:

1. Downloads the body and computes its SHA-256 digest.
2. Claims the digest in DynamoDB (conditional put) so duplicate deliveries
   and re-uploads of identical content are skipped.
3. Parses CSV or JSON rows and validates them against the order schema.
4. Batch-writes curated items to DynamoDB with backoff on throttling.
5. Writes Hive-partitioned JSONL output to the processed bucket.
6. Publishes custom CloudWatch metrics.

Any unhandled exception fails the invocation; after Lambda's async retries
the event lands on the SQS dead-letter queue, where the alert function picks
it up. Failed files release their idempotency claim first so a retry can
reprocess them from scratch.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError

from shared.dynamo import batch_write_with_retry, claim_file, release_claim
from shared.logger import get_logger
from shared.s3_utils import content_sha256, keys_from_event, load_rows, write_partitioned
from shared.schemas import ValidationError, validate_batch

logger = get_logger("ingest")

# Clients are created at import time so they are reused across warm
# invocations instead of being rebuilt on every event.
s3 = boto3.client("s3")
dynamodb = boto3.client("dynamodb")
cloudwatch = boto3.client("cloudwatch")

TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "")
PROCESSED_BUCKET = os.environ.get("PROCESSED_BUCKET", "")
METRICS_NAMESPACE = os.environ.get("METRICS_NAMESPACE", "ServerlessEtl")
DATASET_NAME = os.environ.get("DATASET_NAME", "orders")
#: Fail the whole file when more than this share of rows is invalid; below
#: the threshold, bad rows are logged and counted while good rows load.
MAX_INVALID_RATIO = float(os.environ.get("MAX_INVALID_RATIO", "0.5"))


class IngestionError(RuntimeError):
    """Raised when one or more objects in an event fail processing."""


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Process every S3 object referenced by the incoming event."""
    request_id = getattr(context, "aws_request_id", "unknown")
    objects = keys_from_event(event)
    if not objects:
        logger.warning("event contained no S3 records", extra={"request_id": request_id})
        return {"processed": 0, "objects": []}

    summaries: list[dict[str, Any]] = []
    failures: list[str] = []
    for bucket, key in objects:
        try:
            summaries.append(_process_object(bucket, key))
        except Exception as exc:
            logger.exception(
                "object processing failed",
                extra={"request_id": request_id, "bucket": bucket, "key": key},
            )
            failures.append(f"s3://{bucket}/{key}: {exc}")

    if failures:
        raise IngestionError("; ".join(failures))

    logger.info(
        "invocation complete",
        extra={
            "request_id": request_id,
            "objects_processed": len(summaries),
            "records_written": sum(item.get("records_written", 0) for item in summaries),
        },
    )
    return {"processed": len(summaries), "objects": summaries}


def _process_object(bucket: str, key: str) -> dict[str, Any]:
    """Run the full ETL flow for a single raw object."""
    started = time.perf_counter()
    source_uri = f"s3://{bucket}/{key}"

    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    digest = content_sha256(body)

    if not claim_file(dynamodb, TABLE_NAME, digest, source_uri):
        logger.info("duplicate content skipped", extra={"key": key, "sha256": digest})
        _emit_metrics(duplicates=1)
        return {"key": key, "status": "skipped_duplicate", "sha256": digest}

    try:
        summary = _transform_and_load(bucket, key, body, digest)
    except Exception:
        # Release the claim so Lambda's async retry (and any manual redrive
        # from the DLQ) can reprocess this file from scratch.
        release_claim(dynamodb, TABLE_NAME, digest)
        raise

    summary["seconds"] = round(time.perf_counter() - started, 3)
    _emit_metrics(
        written=summary["records_written"],
        rejected=summary["records_rejected"],
        files=1,
        seconds=summary["seconds"],
    )
    logger.info("object processed", extra={"source": source_uri, **summary})
    return summary


def _transform_and_load(bucket: str, key: str, body: bytes, digest: str) -> dict[str, Any]:
    """Parse, validate, and persist one object's rows."""
    source_uri = f"s3://{bucket}/{key}"
    rows = load_rows(body, key)
    records, rejects = validate_batch(rows)

    for reject in rejects:
        logger.warning("row rejected", extra={"key": key, **reject})

    if rows and len(rejects) / len(rows) > MAX_INVALID_RATIO:
        raise ValidationError(
            [f"{len(rejects)} of {len(rows)} rows invalid, above threshold {MAX_INVALID_RATIO}"]
        )

    output_keys: list[str] = []
    if records:
        ingested_at = datetime.now(UTC).isoformat()
        items = [
            record.to_item(source_key=source_uri, content_hash=digest, ingested_at=ingested_at)
            for record in records
        ]
        batch_write_with_retry(dynamodb, TABLE_NAME, items)
        output_keys = write_partitioned(
            s3, PROCESSED_BUCKET, DATASET_NAME, [record.to_output() for record in records], digest
        )
    else:
        logger.warning("object yielded no valid records", extra={"key": key})

    return {
        "key": key,
        "status": "processed",
        "sha256": digest,
        "rows_read": len(rows),
        "records_written": len(records),
        "records_rejected": len(rejects),
        "output_keys": output_keys,
    }


def _emit_metrics(
    *,
    written: int = 0,
    rejected: int = 0,
    files: int = 0,
    duplicates: int = 0,
    seconds: float | None = None,
) -> None:
    """Publish pipeline metrics; metric failures never fail the pipeline."""
    dimensions = [{"Name": "Dataset", "Value": DATASET_NAME}]
    metric_data: list[dict[str, Any]] = [
        {"MetricName": name, "Dimensions": dimensions, "Value": value, "Unit": "Count"}
        for name, value in (
            ("RecordsWritten", written),
            ("RecordsRejected", rejected),
            ("FilesProcessed", files),
            ("FilesSkippedDuplicate", duplicates),
        )
        if value
    ]
    if seconds is not None:
        metric_data.append(
            {
                "MetricName": "ProcessingSeconds",
                "Dimensions": dimensions,
                "Value": seconds,
                "Unit": "Seconds",
            }
        )
    if not metric_data:
        return
    try:
        cloudwatch.put_metric_data(Namespace=METRICS_NAMESPACE, MetricData=metric_data)
    except ClientError:
        logger.warning("failed to publish CloudWatch metrics", exc_info=True)
