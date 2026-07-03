# Lambda layer (shared library), the two functions, and their event wiring.
# Build the zips first with `make package`.

resource "aws_lambda_layer_version" "shared" {
  layer_name          = "${local.name_prefix}-shared"
  description         = "Shared ETL library: logging, schemas, S3 and DynamoDB helpers"
  filename            = local.layer_zip
  source_code_hash    = filebase64sha256(local.layer_zip)
  compatible_runtimes = ["python3.12"]
}

# Ingest function (S3 -> validate -> DynamoDB + processed bucket)

resource "aws_lambda_function" "ingest" {
  function_name = "${local.name_prefix}-ingest"
  description   = "Validates and loads raw S3 drops into DynamoDB and the processed bucket"

  filename         = local.ingest_zip
  source_code_hash = filebase64sha256(local.ingest_zip)
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  memory_size      = var.lambda_memory_mb
  timeout          = var.lambda_timeout_seconds
  role             = aws_iam_role.ingest.arn
  layers           = [aws_lambda_layer_version.shared.arn]

  environment {
    variables = {
      DYNAMODB_TABLE    = aws_dynamodb_table.curated.name
      PROCESSED_BUCKET  = aws_s3_bucket.processed.bucket
      METRICS_NAMESPACE = "ServerlessEtl"
      DATASET_NAME      = var.dataset_name
      MAX_INVALID_RATIO = var.max_invalid_ratio
      LOG_LEVEL         = "INFO"
    }
  }

  # S3 invokes this function asynchronously; after the retries configured
  # below are exhausted, the original event is sent to the DLQ.
  dead_letter_config {
    target_arn = aws_sqs_queue.ingest_dlq.arn
  }

  depends_on = [
    aws_iam_role_policy.ingest,
    aws_cloudwatch_log_group.ingest,
  ]
}

resource "aws_lambda_function_event_invoke_config" "ingest" {
  function_name                = aws_lambda_function.ingest.function_name
  maximum_retry_attempts       = 2
  maximum_event_age_in_seconds = 3600
}

resource "aws_lambda_permission" "allow_raw_bucket" {
  statement_id   = "AllowExecutionFromRawBucket"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.ingest.function_name
  principal      = "s3.amazonaws.com"
  source_arn     = aws_s3_bucket.raw.arn
  source_account = local.account_id
}

# Alert function (DLQ -> enrich -> SNS)

resource "aws_lambda_function" "alert" {
  function_name = "${local.name_prefix}-alert"
  description   = "Enriches dead-lettered ingestion events and notifies operators via SNS"

  filename         = local.alert_zip
  source_code_hash = filebase64sha256(local.alert_zip)
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  memory_size      = 256
  timeout          = 30
  role             = aws_iam_role.alert.arn
  layers           = [aws_lambda_layer_version.shared.arn]

  environment {
    variables = {
      SNS_TOPIC_ARN = aws_sns_topic.alerts.arn
      ENVIRONMENT   = var.environment
      PIPELINE_NAME = var.project_name
      LOG_LEVEL     = "INFO"
    }
  }

  depends_on = [
    aws_iam_role_policy.alert,
    aws_cloudwatch_log_group.alert,
  ]
}

resource "aws_lambda_event_source_mapping" "dlq_to_alert" {
  event_source_arn        = aws_sqs_queue.ingest_dlq.arn
  function_name           = aws_lambda_function.alert.arn
  batch_size              = 10
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 2
  }
}
