# Bedrock Knowledge Base — Terraform skeleton

> **NOT YET APPLIED — awaiting AWS access.** Nothing here has been planned or
> applied against any AWS account. It exists so that the moment account/model
> access lands, the path is `terraform init && terraform plan`, not "figure
> out what resources are needed."

## What this provisions (once applied)

| File | Resources |
|---|---|
| `main.tf` | provider + version pins, common tags/locals |
| `s3.tf` | the contract-documents data-source bucket (versioned, SSE, private) |
| `opensearch.tf` | OpenSearch Serverless (AOSS) vector collection + encryption/network/data-access policies |
| `iam.tf` | the Bedrock KB service role: S3 read on the data source, AOSS API access, model invoke for the embedding model |
| `bedrock_kb.tf` | the Knowledge Base itself + its S3 data source (chunking strategy per doc_ingestion/FINDINGS.md) |
| `variables.tf` / `outputs.tf` | region, naming prefix, embedding model id; KB id/ARN outputs |

## Before first `terraform apply`

1. AWS account access + credentials (SSO profile or env vars).
2. **Bedrock model access approved in the console** for: the chosen Nova tier
   (`amazon.nova-pro-v1:0` placeholder) *and* the embedding model
   (`amazon.titan-embed-text-v2:0`). Model-access approval can lag account
   access — request it first (this is the human task in the migration plan).
3. Pick the region (default `us-east-1`; `var.aws_region`) and confirm both
   Bedrock KB and AOSS are offered there.
4. Decide the parsing strategy per document class (see
   `doc_ingestion/FINDINGS.md`): default parser vs. FM-based parsing is set on
   the data source's `vector_ingestion_configuration`.
5. Review the AOSS data-access policy principals — it currently grants only
   the KB service role; add human/CI roles as needed.

## Deliberately out of scope here

- No state backend is configured (add S3+DynamoDB backend when the account
  exists).
- No Bedrock Guardrails resource yet — the application seam is
  `guardrails.py`; add an `aws_bedrock_guardrail` resource when policy
  requirements are known.
- Nothing for the runtime chat path: switching the app to Nova needs no infra,
  only IAM credentials with `bedrock:InvokeModel` and `LLM_PROVIDER=bedrock`
  in `.env`.
