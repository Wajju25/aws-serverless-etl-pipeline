"""Factories for AWS event payloads used across the test suite."""

from __future__ import annotations

import json
from typing import Any


def make_s3_event(bucket: str, *keys: str) -> dict[str, Any]:
    """Build an S3 ``ObjectCreated`` notification for one or more keys."""
    return {
        "Records": [
            {
                "eventVersion": "2.1",
                "eventSource": "aws:s3",
                "eventName": "ObjectCreated:Put",
                "s3": {
                    "bucket": {"name": bucket, "arn": f"arn:aws:s3:::{bucket}"},
                    "object": {"key": key},
                },
            }
            for key in keys
        ]
    }


def make_dlq_record(
    body: dict[str, Any] | str,
    *,
    message_id: str = "msg-1",
    error_code: str = "200",
    error_message: str = "IngestionError: schema validation failed",
    request_id: str = "failed-req-1",
    receive_count: str = "1",
) -> dict[str, Any]:
    """Build an SQS record shaped like a Lambda async-failure DLQ message."""
    return {
        "messageId": message_id,
        "receiptHandle": "handle-1",
        "body": body if isinstance(body, str) else json.dumps(body),
        "attributes": {
            "ApproximateReceiveCount": receive_count,
            "SentTimestamp": "1767312000000",
        },
        "messageAttributes": {
            "ErrorCode": {"stringValue": error_code, "dataType": "String"},
            "ErrorMessage": {"stringValue": error_message, "dataType": "String"},
            "RequestID": {"stringValue": request_id, "dataType": "String"},
        },
        "eventSource": "aws:sqs",
        "eventSourceARN": "arn:aws:sqs:us-east-1:123456789012:etl-ingest-dlq",
    }
