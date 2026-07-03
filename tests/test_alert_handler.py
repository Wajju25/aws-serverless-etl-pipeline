"""Tests for the alert (DLQ consumer) Lambda handler."""

import json

from functions.alert.handler import lambda_handler
from helpers import make_dlq_record, make_s3_event


def _drain_queue(sqs_client, queue_url: str) -> list[dict]:
    """Read every SNS-delivered message from the capture queue."""
    messages = []
    while True:
        response = sqs_client.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10)
        batch = response.get("Messages", [])
        if not batch:
            return messages
        messages.extend(json.loads(msg["Body"]) for msg in batch)


class TestAlertPublishing:
    def test_publishes_enriched_alert(self, alert_topic, lambda_context):
        failed_event = make_s3_event("etl-raw-test", "incoming/broken.csv")
        record = make_dlq_record(failed_event, error_message="IngestionError: 2 of 3 rows invalid")

        result = lambda_handler({"Records": [record]}, lambda_context)

        assert result == {"batchItemFailures": []}
        delivered = _drain_queue(alert_topic["sqs"], alert_topic["queue_url"])
        assert len(delivered) == 1
        alert = json.loads(delivered[0]["Message"])
        assert alert["alert_type"] == "etl_ingestion_failure"
        assert alert["environment"] == "test"
        assert alert["failed_objects"] == ["s3://etl-raw-test/incoming/broken.csv"]
        assert alert["error"]["message"] == "IngestionError: 2 of 3 rows invalid"
        assert alert["receive_count"] == "1"
        assert alert["first_failed_at"].startswith("2026-01-02")

    def test_subject_names_failed_object_and_respects_limit(self, alert_topic, lambda_context):
        long_key = "incoming/" + "x" * 200 + ".csv"
        record = make_dlq_record(make_s3_event("etl-raw-test", long_key))

        lambda_handler({"Records": [record]}, lambda_context)

        delivered = _drain_queue(alert_topic["sqs"], alert_topic["queue_url"])
        subject = delivered[0]["Subject"]
        assert subject.startswith("[test] ETL ingestion failure: s3://etl-raw-test/")
        assert len(subject) <= 100

    def test_non_json_body_still_alerts(self, alert_topic, lambda_context):
        record = make_dlq_record("this is not json")
        record["body"] = "this is not json"

        result = lambda_handler({"Records": [record]}, lambda_context)

        assert result == {"batchItemFailures": []}
        delivered = _drain_queue(alert_topic["sqs"], alert_topic["queue_url"])
        alert = json.loads(delivered[0]["Message"])
        assert alert["failed_objects"] == []
        assert "unknown cause" not in delivered[0]["Subject"]  # error code is present

    def test_multiple_records_publish_multiple_alerts(self, alert_topic, lambda_context):
        records = [
            make_dlq_record(make_s3_event("etl-raw-test", f"file-{i}.csv"), message_id=f"msg-{i}")
            for i in range(3)
        ]

        result = lambda_handler({"Records": records}, lambda_context)

        assert result == {"batchItemFailures": []}
        assert len(_drain_queue(alert_topic["sqs"], alert_topic["queue_url"])) == 3


class TestPartialBatchFailure:
    def test_failed_publish_reports_item_identifier(self, aws, lambda_context):
        # No topic exists inside this moto context, so publishing fails and
        # the handler must report the message for redelivery.
        record = make_dlq_record(make_s3_event("etl-raw-test", "a.csv"), message_id="msg-dead")

        result = lambda_handler({"Records": [record]}, lambda_context)

        assert result == {"batchItemFailures": [{"itemIdentifier": "msg-dead"}]}

    def test_empty_batch(self, alert_topic, lambda_context):
        assert lambda_handler({"Records": []}, lambda_context) == {"batchItemFailures": []}
