# Intent router + prompt caching (2026-11-12, chat_intent.py)

Golden chat `--runs 3` (`results/golden_chat_20261112_100217.json`): **75/78**.

| | Result |
|---|---|
| 21 earlier cases | 60/63. st_001 and st_002 each lost one run to "Could not connect to the endpoint URL" (Bedrock network, 09:50); ov_003 once wrote `workdone_activity_name LIKE '%FIELD%'` for "mandays of field work" (SQL writer, prompt text unchanged) |
| 5 new intent cases (in_001..in_005: greeting, two definitions, context follow-up, query after a definition) | 15/15 |

Prompt cache over the run window: 64% of input tokens read from cache (sql_generation 71%, chat_intent 83%,
chat_explain 72%, chat_answer 37%). domain_check no longer runs (its decision is part of chat_intent).
A 10-turn live chat on a cold cache read 48%.

---

# Nova, before and after the chat harness fixes (2026-11-11, buildplan_chat_harness.md)

Nova 2 Lite (main) / Nova Pro (reasoning, corrections, planner) / Nova Lite (routing).
Golden chat = `evals/run_golden_chat.py --runs 3`; sql_eval = `sql_eval.py`, 36 live cases, 3 runs each, median.

| | Before | After |
|---|---|---|
| Golden chat, 16 original cases | 47/48 (97.9%) | 47/48 (97.9%) |
| Golden chat, 5 new overview cases | — | 15/15 |
| Golden chat, all 21 | — | 62/63 (98.4%) |
| Distinct SQL across 3 runs, cases with more than one | 8 of 16 | 2 of 16 |
| sql_eval with examples, old setting (k=2, near-copies of the case allowed) | 0.639 | 0.667 |
| sql_eval with examples, near-copies (similarity >= 0.90) excluded | — | 0.667 |
| sql_eval without examples (code path unchanged: run-to-run variance) | 0.583 | 0.556 |

The one golden miss is st_005 (1 of 3): after the overview answer, the follow-up router once answered
"Total ripe bunches per hectare" from the conversation instead of querying.

Found and fixed while measuring (a mid-build run fell to 68.8%):
- Chroma reports an exact match as distance -0.000; a lower bound of 0 dropped every exact near-copy.
- Deleting ids in place corrupted the Chroma example index ("Error finding id"), silently removing all
  staging examples. The index is now rebuilt from the database at every start and never edited in place.
- A seed whose question names a period, shown for a question that names none, made the writer drop the
  default period (0/3 -> 3/3 with a period-mismatch penalty in example ranking and a no-period seed).

---

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
