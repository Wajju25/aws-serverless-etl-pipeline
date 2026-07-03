"""Shared pytest fixtures.

Environment variables are set at import time — before any handler module is
imported — because the handlers read their configuration at module level,
exactly as they do in Lambda.
"""

import os

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

os.environ["DYNAMODB_TABLE"] = "etl-curated-test"
os.environ["PROCESSED_BUCKET"] = "etl-processed-test"
os.environ["METRICS_NAMESPACE"] = "ServerlessEtlTest"
os.environ["DATASET_NAME"] = "orders"
os.environ["MAX_INVALID_RATIO"] = "0.5"
os.environ["SNS_TOPIC_ARN"] = "arn:aws:sns:us-east-1:123456789012:etl-alerts-test"
os.environ["ENVIRONMENT"] = "test"
os.environ["PIPELINE_NAME"] = "serverless-etl-test"

from types import SimpleNamespace  # noqa: E402

import boto3  # noqa: E402
import pytest  # noqa: E402
from moto import mock_aws  # noqa: E402

RAW_BUCKET = "etl-raw-test"
PROCESSED_BUCKET = os.environ["PROCESSED_BUCKET"]
TABLE_NAME = os.environ["DYNAMODB_TABLE"]
REGION = "us-east-1"


@pytest.fixture()
def aws():
    """Activate moto's AWS mock for the duration of a test."""
    with mock_aws():
        yield


@pytest.fixture()
def s3_client(aws):
    return boto3.client("s3", region_name=REGION)


@pytest.fixture()
def dynamodb_client(aws):
    return boto3.client("dynamodb", region_name=REGION)


@pytest.fixture()
def sns_client(aws):
    return boto3.client("sns", region_name=REGION)


@pytest.fixture()
def sqs_client(aws):
    return boto3.client("sqs", region_name=REGION)


@pytest.fixture()
def etl_stack(s3_client, dynamodb_client):
    """Provision the buckets and table the ingest handler depends on."""
    s3_client.create_bucket(Bucket=RAW_BUCKET)
    s3_client.create_bucket(Bucket=PROCESSED_BUCKET)
    dynamodb_client.create_table(
        TableName=TABLE_NAME,
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return {"s3": s3_client, "dynamodb": dynamodb_client}


@pytest.fixture()
def alert_topic(sns_client, sqs_client):
    """Create the alert topic with an SQS subscription to capture messages."""
    topic_arn = sns_client.create_topic(Name="etl-alerts-test")["TopicArn"]
    queue_url = sqs_client.create_queue(QueueName="alert-capture")["QueueUrl"]
    queue_arn = sqs_client.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])[
        "Attributes"
    ]["QueueArn"]
    sns_client.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=queue_arn)
    return {"topic_arn": topic_arn, "queue_url": queue_url, "sqs": sqs_client}


@pytest.fixture()
def lambda_context():
    """A minimal stand-in for the Lambda context object."""
    return SimpleNamespace(
        aws_request_id="test-request-id",
        function_name="test-function",
        memory_limit_in_mb=256,
    )
