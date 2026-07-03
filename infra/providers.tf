terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  # Partial configuration: bucket, key, and region are supplied at init time
  # (see .github/workflows/ci.yml) so no state location is hardcoded here.
  backend "s3" {}
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
      Repository  = "aws-serverless-etl-pipeline"
    }
  }
}

data "aws_caller_identity" "current" {}
