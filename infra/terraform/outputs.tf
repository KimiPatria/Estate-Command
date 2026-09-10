# NOT YET APPLIED — awaiting AWS access.

output "knowledge_base_id" {
  description = "Feed to retrievers.ContractDocumentRetriever / AmazonKnowledgeBasesRetriever."
  value       = aws_bedrockagent_knowledge_base.contracts.id
}

output "kb_documents_bucket" {
  description = "Upload contracts here (then start an ingestion job)."
  value       = aws_s3_bucket.kb_documents.bucket
}

output "aoss_collection_endpoint" {
  value = aws_opensearchserverless_collection.kb_vectors.collection_endpoint
}
