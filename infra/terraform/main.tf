# NOT YET APPLIED — awaiting AWS access. See README.md in this directory.

terraform {
  required_version = ">= 1.7"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }
  # TODO once the account exists: S3 backend + DynamoDB lock table.
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = local.tags
  }
}

data "aws_caller_identity" "current" {}

locals {
  prefix = var.name_prefix
  tags = {
    Project   = "autodashboard"
    Component = "bedrock-knowledge-base"
    ManagedBy = "terraform"
  }
}
