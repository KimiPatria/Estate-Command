# NOT YET APPLIED — awaiting AWS access.
# Service role Bedrock assumes to run the Knowledge Base: read the S3 data
# source, invoke the embedding model, read/write the AOSS index.

data "aws_iam_policy_document" "kb_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "bedrock_kb" {
  name               = "${local.prefix}-bedrock-kb-role"
  assume_role_policy = data.aws_iam_policy_document.kb_trust.json
}

data "aws_iam_policy_document" "kb_permissions" {
  statement {
    sid       = "S3ListDataSource"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.kb_documents.arn]
  }

  statement {
    sid       = "S3ReadDataSource"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.kb_documents.arn}/*"]
  }

  statement {
    sid     = "InvokeEmbeddingModel"
    actions = ["bedrock:InvokeModel"]
    resources = [
      "arn:aws:bedrock:${var.aws_region}::foundation-model/${var.embedding_model_id}",
    ]
  }

  statement {
    sid       = "AossAccess"
    actions   = ["aoss:APIAccessAll"]
    resources = [aws_opensearchserverless_collection.kb_vectors.arn]
  }
}

resource "aws_iam_role_policy" "bedrock_kb" {
  name   = "${local.prefix}-bedrock-kb-policy"
  role   = aws_iam_role.bedrock_kb.id
  policy = data.aws_iam_policy_document.kb_permissions.json
}
