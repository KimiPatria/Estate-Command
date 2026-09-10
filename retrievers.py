"""
Retriever interface (Bedrock migration plan #6).

Standardizes retrieval behind LangChain's BaseRetriever so the LangGraph/agent
layer can be handed any retriever without knowing what backs it:

  * SchemaTableRetriever — TODAY: wraps the existing hybrid pipeline in
    retrieval.py (ChromaDB dense + BM25 + reciprocal rank fusion + FK closure).
    It does NOT reimplement or replace that pipeline; it adapts its output
    (TableCard list) to LangChain Documents.
  * ContractDocumentRetriever — FUTURE: clause-level retrieval over contract
    PDFs/DOCX. Once AWS access lands this becomes a thin wrapper around
    langchain_aws.AmazonKnowledgeBasesRetriever (Bedrock KB backed by the
    S3 bucket + OpenSearch Serverless collection in infra/terraform/).
    Until then it raises with a clear message.
  * route_query — the seam where a router decides which retriever(s) a query
    needs (e.g. "show an active contract covering material X" -> structured
    SQL for the contract row + document retriever for clause detail).

The live request path still calls retrieval.retrieve_tables() directly — this
module adds the interface without changing runtime behavior. New consumers
(the supply-chain use case, eval harness) should code against BaseRetriever.
"""

import logging

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

log = logging.getLogger(__name__)


class SchemaTableRetriever(BaseRetriever):
    """Hybrid ChromaDB+BM25+RRF table retrieval as a LangChain BaseRetriever.

    Each Document is one EPMS table: page_content is its compact DDL (what the
    SQL prompt consumes), metadata carries the table name/description and
    whether the LLM fallback selector was used.
    """

    k: int = 5

    def _get_relevant_documents(self, query: str, *, run_manager=None) -> list[Document]:
        from metadata_loader import compact_ddl_for
        from retrieval import retrieve_tables

        table_cards, used_llm_fallback = retrieve_tables(query, k=self.k)
        docs = []
        for rank, card in enumerate(table_cards):
            try:
                ddl = compact_ddl_for([card.name])
            except Exception:
                ddl = card.description or card.name
            docs.append(Document(
                page_content=ddl,
                metadata={
                    "source": "epms_schema",
                    "table": card.name,
                    "description": card.description,
                    "rank": rank,
                    "used_llm_fallback": used_llm_fallback,
                },
            ))
        return docs


class ContractDocumentRetriever(BaseRetriever):
    """Placeholder for the future contract-document retriever (Bedrock KB).

    Planned implementation once AWS access exists:
        from langchain_aws import AmazonKnowledgeBasesRetriever
        AmazonKnowledgeBasesRetriever(knowledge_base_id=..., retrieval_config=...)
    backed by the resources drafted in infra/terraform/.
    """

    knowledge_base_id: str | None = None

    def _get_relevant_documents(self, query: str, *, run_manager=None) -> list[Document]:
        raise NotImplementedError(
            "Contract-document retrieval requires the Bedrock Knowledge Base "
            "(awaiting AWS access). See infra/terraform/ and doc_ingestion/ "
            "for the prepared ingestion path."
        )


# Keyword heuristic only — replaced by a proper (LLM) router when the
# supply-chain use case ships. Kept deliberately transparent.
_DOCUMENT_HINTS = (
    "contract", "clause", "force majeure", "mitigation", "agreement",
    "terms", "kontrak", "klausul", "perjanjian",
)


def route_query(query: str, k: int = 5) -> dict[str, BaseRetriever]:
    """Decide which retriever(s) a query needs.

    Returns a dict of role -> retriever. Today every query gets the structured
    retriever; queries that look contract/clause-shaped additionally get the
    document retriever (which raises until the Bedrock KB exists — callers
    should treat it as optional enrichment).
    """
    routes: dict[str, BaseRetriever] = {"structured": SchemaTableRetriever(k=k)}
    q = query.lower()
    if any(hint in q for hint in _DOCUMENT_HINTS):
        routes["documents"] = ContractDocumentRetriever()
        log.info("[retriever-router] document retriever routed for query=%r", query[:80])
    return routes
