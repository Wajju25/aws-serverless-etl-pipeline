# Failure routing:
#   ingest Lambda (async) -- after retries --> ingest DLQ --> alert Lambda
#   alert Lambda (via its event source mapping) -- after 5 receives --> terminal DLQ

resource "aws_sqs_queue" "ingest_dlq" {
  name                       = "${local.name_prefix}-ingest-dlq"
  message_retention_seconds  = 1209600 # 14 days: maximum, to allow slow triage
  visibility_timeout_seconds = 180     # 6x the alert Lambda timeout
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.terminal_dlq.arn
    maxReceiveCount     = 5
  })
}

# Messages the alert function itself repeatedly fails to handle land here so
# nothing is ever silently dropped.
resource "aws_sqs_queue" "terminal_dlq" {
  name                      = "${local.name_prefix}-terminal-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue_redrive_allow_policy" "terminal_dlq" {
  queue_url = aws_sqs_queue.terminal_dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.ingest_dlq.arn]
  })
}
