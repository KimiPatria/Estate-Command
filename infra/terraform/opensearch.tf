# NOT YET APPLIED — awaiting AWS access.
# OpenSearch Serverless vector collection backing the Knowledge Base index.
# (Alternative: skip AOSS and use S3 Vectors / Aurora pgvector — revisit cost
# before applying; AOSS has a nontrivial floor cost for small corpora.)

resource "aws_opensearchserverless_security_policy" "kb_encryption" {
  name = "${local.prefix}-kb-enc"
  type = "encryption"
  policy = jsonencode({
    Rules = [{
      Resource     = ["collection/${local.prefix}-kb-vectors"]
      ResourceType = "collection"
    }]
    AWSOwnedKey = true
  })
}

resource "aws_opensearchserverless_security_policy" "kb_network" {
  name = "${local.prefix}-kb-net"
  type = "network"
  # Public endpoint to start (Bedrock KB accesses via AWS network anyway);
  # tighten to VPC endpoints when the account's network layout is known.
  policy = jsonencode([{
    Rules = [
      {
        Resource     = ["collection/${local.prefix}-kb-vectors"]
        ResourceType = "collection"
      },
      {
        Resource     = ["collection/${local.prefix}-kb-vectors"]
        ResourceType = "dashboard"
      }
    ]
    AllowFromPublic = true
  }])
}

resource "aws_opensearchserverless_collection" "kb_vectors" {
  name = "${local.prefix}-kb-vectors"
  type = "VECTORSEARCH"
  depends_on = [
    aws_opensearchserverless_security_policy.kb_encryption,
    aws_opensearchserverless_security_policy.kb_network,
  ]
}

resource "aws_opensearchserverless_access_policy" "kb_data_access" {
  name = "${local.prefix}-kb-data"
  type = "data"
  policy = jsonencode([{
    Rules = [
      {
        Resource     = ["collection/${local.prefix}-kb-vectors"]
        ResourceType = "collection"
        Permission = [
          "aoss:CreateCollectionItems",
          "aoss:DescribeCollectionItems",
          "aoss:UpdateCollectionItems",
        ]
      },
      {
        Resource     = ["index/${local.prefix}-kb-vectors/*"]
        ResourceType = "index"
        Permission = [
          "aoss:CreateIndex",
          "aoss:DescribeIndex",
          "aoss:UpdateIndex",
          "aoss:ReadDocument",
          "aoss:WriteDocument",
        ]
      }
    ]
    Principal = [aws_iam_role.bedrock_kb.arn]
  }])
}
