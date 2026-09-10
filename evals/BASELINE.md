# Text-to-SQL Baseline — Groq / openai-gpt-oss-120b

*Run `sqlrun_20260708_151309` · 2026-07-08 · 36 golden cases (`evals/sql_cases.yaml`) · no few-shot examples · full summary in `results/sqlrun_20260708_151309_summary.json`, MLflow experiment `text2sql-evals`*

| Metric | Value |
|---|---|
| **Execution accuracy** (row-level diff vs reference) | **72.2%** (26/36) |
| Executable rate (generated SQL runs without error) | 100% |
| SQL validity rate (passes safety validator) | 100% |
| Self-correction retry used | 2/36 cases |
| Retrieval recall / all-tables-found | 0.944 |
| Retrieval MRR | 0.764 |
| Retrieval precision@5(+FK) | 0.20 |
| Mean retrieval latency | 327 ms |
| Mean generation latency (incl. retry) | 6.8 s |
| Tokens (in / out, whole run) | 72,387 / 14,968 |
| Est. cost per full run (prices in `model_prices.yaml`) | ~$0.022 |

**Run-to-run variance is real:** an immediately preceding run scored 61.1% on
the same cases (Groq backend nondeterminism at temperature 0 — several cases
flip between runs). When comparing providers (Groq vs Nova), run the harness
≥3 times per provider and compare medians, not single runs.

## Failure taxonomy (the 10 misses)

| Class | Cases | Note |
|---|---|---|
| Join fan-out (block_code repeats per estate; model joins on code alone) | 005, 035*, 036 | real model errors, correctly caught; *035 passed in the other run |
| Retrieval miss (needed table not in top-5) | 011 (t_oph for "rejected bunches"), 020 (t_workdone for "pekerja bekerja") | recall=0 — the only retrieval failures |
| Representation strictness (right numbers, different labels/format) | 007 (DATE_TRUNC vs month number), 017 (INTERNAL/EXTERNAL labels vs raw flag) | debatable misses; row diff is strict by design |
| Aggregation shape (single total vs per-entity breakdown) | 024, 027, 033 | 024's Indonesian "total duration pekerja" implies per-worker rows |

## Pipeline bugs this harness already surfaced (fixed)

1. `metadata_loader` stripped `oph_created_date` (audit-column filter) from the
   DDL, so business rule 5 ("use oph_created_date, never oph_approved_date")
   was un-followable — the model silently used the wrong date column on every
   harvest query. Fixed: curated `key_columns` are exempt from the filter.
2. gpt-oss reasoning tokens count against `max_tokens`; the 800-token SQL cap
   occasionally truncated SQL mid-token. Retry logic masks some of these;
   consider raising the cap or setting `reasoning_effort` for SQL generation.
3. Routing-tier JSON calls at `max_tokens=20` intermittently 400'd
   (`json_validate_failed`); bumped to 60.

## Re-running (identical command after a provider swap)

```
python sql_eval.py                 # full scored run -> results/ + MLflow
python sql_eval.py --check-cases   # validate reference SQL only (no LLM)
python sql_eval.py --with-examples # production few-shot config (leaks near-dup cases)
```
