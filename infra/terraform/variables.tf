# NOT YET APPLIED — awaiting AWS access.

variable "aws_region" {
  description = "Region for all resources. Must offer Bedrock KB + OpenSearch Serverless."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Resource naming prefix."
  type        = string
  default     = "autodash"
}

variable "embedding_model_id" {
  description = "Bedrock embedding model for the KB vector index."
  type        = string
  default     = "amazon.titan-embed-text-v2:0"
}

variable "embedding_dimensions" {
  description = "Vector dimension of the embedding model (Titan v2: 1024)."
  type        = number
  default     = 1024
}
