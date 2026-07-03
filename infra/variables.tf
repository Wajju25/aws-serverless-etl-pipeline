variable "project_name" {
  description = "Short name used as a prefix for all resources."
  type        = string
  default     = "serverless-etl"
}

variable "environment" {
  description = "Deployment environment (dev, staging, prod)."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "dataset_name" {
  description = "Logical dataset name used for partitioning and metrics dimensions."
  type        = string
  default     = "orders"
}

variable "alert_email" {
  description = "Optional email address subscribed to the alert topic. Leave empty to skip."
  type        = string
  default     = ""
}

variable "lambda_memory_mb" {
  description = "Memory allocation for the ingest Lambda."
  type        = number
  default     = 512
}

variable "lambda_timeout_seconds" {
  description = "Timeout for the ingest Lambda."
  type        = number
  default     = 120
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for both functions."
  type        = number
  default     = 30
}

variable "raw_expiration_days" {
  description = "Days before raw objects expire (0 disables expiration)."
  type        = number
  default     = 90
}

variable "max_invalid_ratio" {
  description = "Fraction of invalid rows above which a whole file is failed."
  type        = string
  default     = "0.5"
}

locals {
  name_prefix = "${var.project_name}-${var.environment}"
  account_id  = data.aws_caller_identity.current.account_id

  raw_bucket_name       = "${local.name_prefix}-raw-${local.account_id}"
  processed_bucket_name = "${local.name_prefix}-processed-${local.account_id}"

  ingest_zip = "${path.module}/../dist/ingest.zip"
  alert_zip  = "${path.module}/../dist/alert.zip"
  layer_zip  = "${path.module}/../dist/layer.zip"
}
