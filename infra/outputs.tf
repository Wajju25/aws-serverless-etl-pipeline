output "raw_bucket" {
  description = "Name of the raw (landing) bucket."
  value       = aws_s3_bucket.raw.bucket
}

output "processed_bucket" {
  description = "Name of the processed (curated) bucket."
  value       = aws_s3_bucket.processed.bucket
}

output "dynamodb_table" {
  description = "Name of the curated DynamoDB table."
  value       = aws_dynamodb_table.curated.name
}

output "ingest_function_name" {
  description = "Name of the ingest Lambda function."
  value       = aws_lambda_function.ingest.function_name
}

output "alert_function_name" {
  description = "Name of the alert Lambda function."
  value       = aws_lambda_function.alert.function_name
}

output "ingest_dlq_url" {
  description = "URL of the ingestion dead-letter queue."
  value       = aws_sqs_queue.ingest_dlq.url
}

output "alerts_topic_arn" {
  description = "ARN of the SNS alert topic."
  value       = aws_sns_topic.alerts.arn
}
