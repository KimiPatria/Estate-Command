# NOT YET APPLIED — awaiting AWS access.
# The Knowledge Base + its S3 data source. Chunking/parsing choices follow
# doc_ingestion/FINDINGS.md: hierarchical chunking keyed on clause headings;
# switch parsing_configuration to the FM parser for document classes flagged
# by the dry-run harness (nested tables / multi-column / scanned).

resource "aws_bedrockagent_knowledge_base" "contracts" {
  name     = "${local.prefix}-contracts-kb"
  role_arn = aws_iam_role.bedrock_kb.arn

  knowledge_base_configuration {
    type = "VECTOR"
    vector_knowledge_base_configuration {
      embedding_model_arn = "arn:aws:bedrock:${var.aws_region}::foundation-model/${var.embedding_model_id}"
    }
  }

  storage_configuration {
    type = "OPENSEARCH_SERVERLESS"
    opensearch_serverless_configuration {
      collection_arn    = aws_opensearchserverless_collection.kb_vectors.arn
      vector_index_name = "${local.prefix}-contracts-index"
      field_mapping {
        vector_field   = "embedding"
        text_field     = "chunk_text"
        metadata_field = "metadata"
      }
    }
  }

  depends_on = [aws_iam_role_policy.bedrock_kb]
}

resource "aws_bedrockagent_data_source" "contracts_s3" {
  name              = "${local.prefix}-contracts-s3"
  knowledge_base_id = aws_bedrockagent_knowledge_base.contracts.id

  data_source_configuration {
    type = "S3"
    s3_configuration {
      bucket_arn = aws_s3_bucket.kb_documents.arn
      # inclusion_prefixes = ["contracts/"]
    }
  }

  vector_ingestion_configuration {
    chunking_configuration {
      # Hierarchical: parent/child chunks so clause-level hits return with
      # their surrounding article context (see doc_ingestion/FINDINGS.md).
      chunking_strategy = "HIERARCHICAL"
      hierarchical_chunking_configuration {
        overlap_tokens = 60
        level_configuration {
          max_tokens = 1500
        }
        level_configuration {
          max_tokens = 300
        }
      }
    }
    # TODO before apply, per dry-run findings: enable FM parsing for complex
    # layouts (nested tables / multi-column / scanned):
    # parsing_configuration {
    #   parsing_strategy = "BEDROCK_FOUNDATION_MODEL"
    #   bedrock_foundation_model_configuration {
    #     model_arn = "arn:aws:bedrock:${var.aws_region}::foundation-model/amazon.nova-lite-v1:0"
    #   }
    # }
  }
}
