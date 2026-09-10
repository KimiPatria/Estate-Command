# Document Ingestion Dry Run — Findings

*2026-07-08 · Bedrock migration plan #6 · no AWS dependency*

## What was done

- `generate_samples.py` synthesizes three contracts covering the structures the
  supply-chain use case will face: a simple clause+table DOCX, a DOCX with a
  **nested rate-card table** and force-majeure/mitigation clauses, and a PDF
  with a **two-column article layout** plus a delivery-schedule table.
- `dry_run.py` parses any folder of PDF/DOCX with open-source parsers
  (`pypdf`, `python-docx`; `unstructured` is used automatically if installed)
  and reports structure per document: headings, tables (incl. nesting depth),
  clause-keyword hits, multi-column detection, scanned-page detection.
  Output: `dry_run_report.json`.

**Caveat:** the sample set is synthetic. The harness is the deliverable —
point it at a handful of real (sanitised) contracts with
`python doc_ingestion/dry_run.py --dir <folder>` as soon as any are available,
and re-evaluate the recommendation below.

## Results on the sample set

| Document | Structure found | Extraction quality |
|---|---|---|
| contract_simple.docx | 5 headings, 1 flat 4x4 table, FM + mitigation clauses | clean — headings, clause text, and table cells all recovered |
| contract_nested_tables.docx | 8 headings, **1 nested table (depth 2)** | python-docx exposes the nesting explicitly (`depth1:2x2`, `depth2:3x3`) |
| contract_two_column.pdf | 2 pages, **multi-column layout detected on p.1**, 1 table | text extracts fully but plain extraction **interleaves the two columns**, splitting Article 3 (force majeure) text mid-clause |

## Implications for Bedrock Knowledge Bases

1. **Clause-level retrieval is heading-friendly in DOCX** — clause boundaries
   (Force Majeure, Mitigation) sit under numbered headings. Bedrock KB's
   default chunking (fixed-size) would cut across clauses; **semantic or
   hierarchical chunking** (both supported by Bedrock KB) keyed on headings is
   the right setting.
2. **Nested tables need the FM-based parser.** Bedrock KB's default parser
   flattens tables to text; a nested rate card inside a cell loses its
   row/column meaning. If real contracts contain rate cards like the sample
   (likely for logistics contracts), plan for **Bedrock Data Automation or the
   foundation-model parsing option** on the data source, not the default
   parser.
3. **Two-column PDFs are the biggest risk.** Plain text extraction interleaves
   columns — a force-majeure clause read across columns is garbage for
   retrieval. The default KB parser has the same failure mode. Detection is
   cheap (this harness flags it), so: route flagged documents to FM parsing,
   let simple ones use the default parser (cheaper).
4. **Scanned contracts**: none in the sample set, but `dry_run.py` flags pages
   with no extractable text. Any hit there mandates OCR (FM parser handles
   this in KB; locally that would be Tesseract/Textract).

## Recommendation

Ingest with a **two-tier parser policy**: run this dry-run harness over the
real contract corpus at onboarding time; documents flagged
`multi_column_suspected`, `nested_tables > 0`, or `empty_text_pages > 0` go to
the FM-based parser / Bedrock Data Automation, the rest to the default parser
with semantic chunking. Budget accordingly — if the corpus turns out to be
mostly flat single-column DOCX, the default parser suffices and FM parsing
cost is avoided.

The retrieval seam is already in place: `retrievers.ContractDocumentRetriever`
(LangChain `BaseRetriever`) is the interface the future
`AmazonKnowledgeBasesRetriever` plugs into, next to the existing
`SchemaTableRetriever`, with `retrievers.route_query()` deciding which
retriever(s) serve a given question.
