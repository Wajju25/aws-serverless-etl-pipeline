# Single-table design: curated order items plus file-claim (idempotency)
# items share one on-demand table.

resource "aws_dynamodb_table" "curated" {
  name         = "${local.name_prefix}-curated"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  deletion_protection_enabled = var.environment == "prod"
}
