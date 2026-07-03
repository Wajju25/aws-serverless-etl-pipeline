"""Alert Lambda: enrich dead-lettered events and notify operators via SNS.

Consumes the SQS dead-letter queue that receives ingestion events after
Lambda's asynchronous retries are exhausted. Each message body is the
original S3 event; Lambda attaches ``RequestID``, ``ErrorCode``, and
``ErrorMessage`` as SQS message attributes. The handler enriches that
context (environment, queue, receive count, failed object list) and
publishes a structured alert to SNS.

The function uses partial batch responses (``ReportBatchItemFailures``), so
one bad message never blocks or re-drives the rest of the batch.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

import boto3

from shared.logger import get_logger

logger = get_logger("alert")

# Client created at import time for reuse across warm invocations.
sns = boto3.client("sns")

SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
PIPELINE_NAME = os.environ.get("PIPELINE_NAME", "serverless-etl")

_SUBJECT_LIMIT = 100  # SNS hard limit on subject length.


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Publish one alert per dead-lettered message, reporting partial failures."""
    if not SNS_TOPIC_ARN:
        raise RuntimeError("SNS_TOPIC_ARN environment variable is not configured")

    request_id = getattr(context, "aws_request_id", "unknown")
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        try:
            _publish_alert(record)
        except Exception:
            logger.exception(
                "failed to publish alert",
                extra={"request_id": request_id, "message_id": record.get("messageId")},
            )
            failures.append({"itemIdentifier": record["messageId"]})

    logger.info(
        "alert batch complete",
        extra={
            "request_id": request_id,
            "messages": len(event.get("Records", [])),
            "failed": len(failures),
        },
    )
    return {"batchItemFailures": failures}


def _publish_alert(record: dict[str, Any]) -> None:
    """Enrich a single DLQ message and publish it to the SNS topic."""
    payload = _parse_body(record.get("body", ""))
    error = _extract_error(record)
    attributes = record.get("attributes", {})

    alert = {
        "alert_type": "etl_ingestion_failure",
        "pipeline": PIPELINE_NAME,
        "environment": ENVIRONMENT,
        "detected_at": datetime.now(UTC).isoformat(),
        "message_id": record.get("messageId"),
        "queue_arn": record.get("eventSourceARN"),
        "receive_count": attributes.get("ApproximateReceiveCount"),
        "first_failed_at": _epoch_ms_to_iso(attributes.get("SentTimestamp")),
        "error": error,
        "failed_objects": _failed_objects(payload),
        "runbook": (
            "Fix the input or code, then redrive the message from the DLQ "
            "via StartMessageMoveTask. Reprocessing is idempotent."
        ),
    }

    subject = _build_subject(error, alert["failed_objects"])
    sns.publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject=subject,
        Message=json.dumps(alert, indent=2, default=str),
    )
    logger.info(
        "alert published",
        extra={"message_id": record.get("messageId"), "subject": subject},
    )


def _parse_body(body: str) -> Any:
    """Decode the message body, tolerating non-JSON payloads."""
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body


def _extract_error(record: dict[str, Any]) -> dict[str, str | None]:
    """Pull the error context Lambda attaches to dead-lettered messages."""
    attributes = record.get("messageAttributes", {})

    def value(name: str) -> str | None:
        return attributes.get(name, {}).get("stringValue")

    return {
        "code": value("ErrorCode"),
        "message": value("ErrorMessage"),
        "failed_request_id": value("RequestID"),
    }


def _failed_objects(payload: Any) -> list[str]:
    """List the S3 URIs referenced by the original (failed) event."""
    if not isinstance(payload, dict):
        return []
    uris: list[str] = []
    for record in payload.get("Records", []):
        s3_section = record.get("s3")
        if not s3_section:
            continue
        bucket = s3_section.get("bucket", {}).get("name", "unknown-bucket")
        key = s3_section.get("object", {}).get("key", "unknown-key")
        uris.append(f"s3://{bucket}/{key}")
    return uris


def _build_subject(error: dict[str, str | None], failed_objects: list[str]) -> str:
    """Compose a short, informative SNS subject within the 100-char limit."""
    detail = failed_objects[0] if failed_objects else (error.get("code") or "unknown cause")
    subject = f"[{ENVIRONMENT}] ETL ingestion failure: {detail}"
    return subject[:_SUBJECT_LIMIT]


def _epoch_ms_to_iso(timestamp_ms: str | None) -> str | None:
    """Convert an epoch-milliseconds string to an ISO 8601 timestamp."""
    if not timestamp_ms:
        return None
    try:
        return datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=UTC).isoformat()
    except (ValueError, OSError):
        return None
