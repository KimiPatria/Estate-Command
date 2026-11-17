# Build plan: chat harness fixes 2, 3, 5 and 7 (Nova-only)

## Status: built 2026-11-11

All four phases are in. Results are in `evals/BASELINE.md`: golden chat 62/63 over 21 cases, with 15/15 on the 5 new overview cases. sql_eval with examples rose from 0.639 to 0.667, with near-copies now excluded.

Where the build differs from the plan below:
- **Prompts keep their JSON examples.** Stripping them broke the text fallback, so the forced tool simply sits on top of them.
- **The chart planner runs on Nova 2 Lite.** Nova Lite broke off the chart tool call on 9 of 15 tries.
- **Nova 2 Lite reasoning mode is unused.** It returns no tool call when a tool is forced. The overview planner uses Nova Pro instead.
- **The schema check went straight to `enforce`.** The replay found 15/158 flagged and 0 false positives: 12 used the hidden `t_abw` table and 3 used dropped columns.
- **The example distance cap is 0.30, not 0.35.** On this history, unrelated questions start at about 0.26.
- **Verified examples get a 0.05 ranking bonus**, not a strict verified-first order. Examples whose question disagrees on whether a period is named get a 0.06 penalty.
- **Database queries are limited to 2 at a time per engine.** Four overview parts hit the staging SQL Server's 15 s timeout. A query that times out is re-run once as it is.
- **Overview parts are never saved as examples.** Their questions are templates, and the staging scope filter would teach the wrong thing.
- **The purge does not retire empty results from recent periods.** "Yesterday" and "last N days/months" are empty because the data stops earlier.
- **The Chroma example index is rebuilt at every start and never edited in place.** Deleting ids corrupted it.
- **Golden `st_005` is rescored on its second turn.** The first turn is now answered as an overview instead of being asked back.

Operations:
- Restart the server after `evals/seed_examples.py --apply` or `evals/purge_bad_examples.py --apply` so the example index is rebuilt.
- After adding a seed, run `python evals/seed_examples.py --check`.

## Context

An assessment of the `/chat` harness found that the pipeline design suits a weaker model: fixed steps, with arithmetic and scope handled in code. Four weak spots remain:

- **#2: The SQL retry is narrow.** It fires only when every estate fails. There is no pre-run schema check, and nothing checks empty or suspicious results.
- **#3: The example pool treats "executed" as "correct".** Examples are the biggest measured lever: Nova Pro scored 0.25–0.31 without examples and 0.50–0.53 with them in two runs. The third run with examples scored only 0.33, with 4 retries.
- **#5: JSON from Nova is handled loosely.** `json_mode` is a no-op on Bedrock. Three chat call sites use strict `json.loads`, and none has a repair retry.
- **#7: There is no planner for vague multi-measure questions** such as "how is Estate C performing", "operational efficiency" or "kinerja".

**Goal:** fewer wrong-but-plausible answers and fewer "couldn't produce a query" replies, measured on the existing evals.

**Constraint:** only Amazon Nova models: Nova Lite (routing), Nova 2 Lite (main) and Nova Pro (reasoning). Nova 2 Lite's own `reasoningConfig` may be used. No Claude or other providers.

**Decisions taken:**
- **#7 is hybrid.** Overview, performance and efficiency questions go to the planner. Rankings of individuals ("best harvester", "worst block") keep the existing ask-back for the measure.
- **#3 seeds come from a new curated seed file.** The eval YAMLs stay held-out, with a guard against near-duplicate questions.
- **#3 keeps auto-saved examples at a lower rank,** and only those that are lint-clean, schema-valid and non-empty.

**Order:** Phase 0 → 5 → 2 → 3 → 7. #5 provides the structured-output call that the #7 planner needs. #2 provides the schema check and `generate_and_run()` that #3 and #7 reuse.

On approval, this plan is copied into the repo as `buildplan_chat_harness.md`, matching `buildplan_finance.md`.

---

## Phase 0: Baseline and spike (no behaviour change)

1. **Baseline.** Record the scores before any change. Run with Anaconda python:
   - `python evals/run_golden_chat.py --runs 3`
   - `python sql_eval.py --with-examples`, 3 runs
   - the same `sql_eval` command without `--with-examples`, 3 runs
   - Save the medians in `evals/BASELINE.md` under a "Nova, pre-harness-fix" heading.
2. **Spike** with a throwaway script in the scratchpad, not the repo. Confirm on all three Nova tiers that LangChain `bind_tools(..., tool_choice="<tool name>")` returns `message.tool_calls[0]["args"]`.
   - langchain-aws 1.6.2 infers `supports_tool_choice_values=("auto","any","tool")` for Nova.
   - Also test Nova 2 Lite with `additional_model_request_fields={"reasoningConfig": {"type": "enabled", "maxReasoningEffort": "low"}}`, with and without a forced tool. Record which combinations work.
   - The result decides #5's primary path and the planner model choice in #7.

---

## Phase 1, fix #5: structured output from Nova

**New: `llm_client.chat_json(messages, *, name, schema, tier, max_tokens, task, temperature=0) -> dict | None`** in [llm_client.py](llm_client.py).

1. **Bedrock path.**
   - Bind one tool, `{name, description, input_schema: schema}`, built with `tool_schemas.make_tool` and `to_openai_tools` ([tool_schemas.py](tool_schemas.py)).
   - Force it with `tool_choice=name`, then read `result.message.tool_calls[0]["args"]`. The pattern for reading tool calls is already in [gis/copilot.py:1841](gis/copilot.py#L1841).
2. **Validate** the result with a small built-in checker in the same module: required keys, types, and enums. Don't add a jsonschema dependency.
3. **Repair once on failure.** Append the bad output and a specific error such as `"route" must be one of context|sql` to the messages, then call again.
4. **Text fallback.** Use `gis.reasoning.parse_json` on the text ([gis/reasoning.py:78](gis/reasoning.py#L78)) when the tool path is unsupported (spike result), and on the Groq provider with `json_mode`.
5. **Telemetry.** Tag the JSONL record with `json_via` = `tool` | `repair` | `text` | `fail`. `_log_call` already writes the record, so success rates can be read from `llm_calls.jsonl`.

**Migrate these chat-path call sites.** Each keeps its current safe default when `chat_json` returns None.

| Call site | Schema |
|---|---|
| `_route_followup` [chat_router.py:485](chat_router.py#L485), currently strict `json.loads` | `{route: enum[context, sql]}` |
| `is_offdomain` [error_handler.py:83](error_handler.py#L83), currently strict `json.loads` | `{related: bool}` |
| `call_routing_llm` [llm.py:164](llm.py#L164), currently strict `json.loads` | `{tables: [string]}` |
| `clarify_gate` [chat_conversation.py:155](chat_conversation.py#L155) | `{clarify: bool, question?: string, options?: [string]}` |
| `suggest_followups` [chat_conversation.py:353](chat_conversation.py#L353) | `{suggestions: [string]}` |
| `chat_charts.llm_plan` [chat_charts.py:210](chat_charts.py#L210) | chart spec: the existing `validate()` keeps checking the columns |

- **Prompts.** Drop the "Reply with JSON only" boilerplate from these prompts and let the tool schema carry the shape. The decision rules in the prompts stay.
- **Out of scope:** `call_normalization_llm`, because it is not on the chat path.

**Tests** in a new `test_llm_json.py`, using a fake chat model in the same monkeypatch style as [test_chat_conversation.py](test_chat_conversation.py). Cover:
- a tool call parsed
- an enum violation repaired
- text-only output parsed through the fallback
- both attempts failing, which returns None and leaves each caller on its default (route=`sql`, on-domain, no clarify)

---

## Phase 2, fix #2: verification loop

**Refactor first.** Lift steps 3–5 of `_run_sql_pipeline` ([chat_router.py:943-1050](chat_router.py#L943-L1050)) into `generate_and_run(sql_question, table_cards, examples, history_block, estate_ids, question) -> RunResult`.
- `RunResult` holds `sql`, `exec_results`, `ok_estates`, `failed_estates`, `attempts` and `outcome`.
- `_run_sql_pipeline` calls it unchanged at first. Phase 4 reuses it per sub-question.
- Its `chat_events` steps keep the same names, so [chat_static/chat.js](chat_static/chat.js) needs no change.

**New checks inside `generate_and_run`, in this order:**

1. **Pre-run schema check:** new `sql_schema_check.py`.
   - Add the `sqlglot` dependency; it is not installed today.
   - Parse with `read="postgres"` or `"tsql"` from `src.dialect`. Build the schema dict from `metadata_loader.cards(src.id)`, which uses `TableCard.columns` ([metadata_loader.py:66](metadata_loader.py#L66)).
   - Run `sqlglot.optimizer.qualify` with column validation. On an unknown table or column, return a writer-facing message that names the bad identifier and its closest real columns from the referenced tables (`difflib.get_close_matches`).
   - **Fails open:** a parse or qualify exception means "no finding", and only a log line.
   - **Shadow mode first** (`CHAT_SCHEMA_CHECK=log`): before enforcing, replay every stored `query_history` SQL through it and confirm the false-positive rate is about 0 on queries that executed. Then set it to `enforce`.
2. **Existing checks,** unchanged: `validate()` and `src.lint()`.
3. **Execute,** as today.
4. **Partial estate failure** (live fan-out, where some estates are OK and some failed): re-run the same SQL once on the failed estates only. The schemas are identical, so this is usually a timeout or connection error, not a SQL error, and nothing is regenerated. If they still fail, the existing `failed_estates` note covers it.
5. **Result sanity.** New optional `DataSource.check_result(columns, rows, question, sql) -> str | None` hook next to `lint` in [data_sources.py:77](data_sources.py#L77), plus generic checks:
   - zero rows, while `data_through()` is not earlier than the period asked about
   - a single aggregate row where every measure is NULL or 0
   - a negative count or sum, when the column name contains bunches, tonnes, ha, mandays or count
   - Source-specific bounds, for example bunches/ha above about 10,000 or below 1, catch the area fan-out class (1.11 bunches/ha) after the fact.
   - **Hitting the 200-row cap is not a retry.** The flag is passed to the answer prompt instead.
6. **Retry budget.** Allow up to 2 corrective regenerations (today: 1), each fed the specific finding through the existing `prior_error`/`prior_sql` path of `build_sql_messages` ([prompt_builder.py:72](prompt_builder.py#L72)).
   - Attempt 2 goes to the reasoning tier, as now. Attempt 3 also goes to the reasoning tier, with the full error history.
   - An empty or suspicious result is replaced only if the retry returns a non-suspicious result. Otherwise the original result is kept and the answer is told why it may be empty.
   - SQL that is still lint- or schema-flagged is still refused, which is the current design.
7. **Telemetry.** `RunResult.outcome` = `first_ok` | `fixed_schema` | `fixed_lint` | `fixed_exec` | `fixed_empty` | `kept_empty` | `failed`. It goes into the turn meta and the `rag_calls.jsonl` summary.

**Tests:** extend [test_data_sources.py](test_data_sources.py) and add `test_sql_schema_check.py`, with no DB needed. Cover:
- an unknown column in each dialect gives a finding, and an alias, CTE column or select-list alias gives none
- a partial-estate re-run
- an empty result triggering exactly one regeneration
- the retry budget capped at 2

---

## Phase 3, fix #3: example pool provenance

1. **Schema.** Add `origin TEXT DEFAULT 'auto'` (`auto` | `seed` | `rated`), `verified INTEGER DEFAULT 0` and `row_count INTEGER` to `queries` in [query_history.py](query_history.py), using the same migrate-on-connect pattern as `init_db`.
2. **Recording rule** (`record_query` call at [chat_router.py:1110](chat_router.py#L1110)): `success=1` only when the final SQL is lint-clean, schema-clean, non-empty and not flagged by `check_result`. Everything else is recorded with `success=0`, so the history is kept but never used as an example.
3. **Feedback** ([chat_router.py:1166](chat_router.py#L1166)): a thumbs-up sets `verified=1, origin='rated'`. A thumbs-down keeps today's behaviour (`set_success(False)` and `forget_example`).
4. **Seed set.**
   - New files `examples/seed_live.yaml` and `examples/seed_staging.yaml`, each with about 20–30 hand-written `{question, sql, notes}` pairs.
   - Bilingual phrasing. They cover the rule-sensitive shapes: deleted guard, created date, FDN weight once per SPB, BHA in its own subquery, WERKS/ESTNR joins, plasma PRFNR, default period, per-estate grouping.
   - New loader `evals/seed_examples.py`, with `--check` and `--apply` modes:
     - runs each seed and requires lint-clean, schema-clean and non-empty results
     - rejects any seed whose question has cosine similarity ≥ 0.90 (`retrieval._embed`) with a question in `golden_chat.yaml`, `sql_cases.yaml` or `sql_cases_multi.yaml`, which is the leakage guard
     - upserts into the database with `origin='seed', verified=1`, idempotently by a hash of question and SQL
5. **Retrieval** (`retrieve_examples` [retrieval.py:370](retrieval.py#L370)):
   - Store `verified` and `origin` in the Chroma metadata, in `_refresh_history_collection` and `refresh_query_history_index`.
   - Query 8 candidates and drop any whose cosine distance is above a threshold (start at 0.35, tune on the evals). Keep the existing lint filter, plus the schema check.
   - Rank verified examples first, then by similarity. Dedupe near-identical SQL. Return k=3 (today 2).
   - When a source has fewer than 3 history rows, seeds still apply; today retrieval returns nothing in that case.
6. **sql_eval leakage.** In `sql_eval.py --with-examples`, drop any example whose question has similarity ≥ 0.90 with the case being scored, to fix the "leaks near-dup cases" warning in `BASELINE.md`.
7. **Clean-up.** Extend [evals/purge_bad_examples.py](evals/purge_bad_examples.py) so it also retires auto examples that fail the schema check or that return zero rows when re-run. Its existing `--apply` mode stays.

**Tests:** ranking order (verified before auto), the distance threshold, the leakage guard rejecting a near-duplicate, and `record_query` with `success=0` for an empty result.

---

## Phase 4, fix #7: overview planner

1. **Trigger,** a deterministic regex in [chat_conversation.py](chat_conversation.py): new `is_overview(message)`.
   - It matches words such as performance, perform(ing), efficiency, efficient, overview, "how is X doing", productivity, operational, summary, kinerja, performa, efisiensi, produktivitas, gambaran and ringkasan.
   - It matches only when no catalog measure is named and the question is not a ranking of individuals (best/worst/top + harvester, worker, block, mandor, pemanen).
   - **Clarify gate change:** `_MAY_NEED_ASKING` ([chat_conversation.py:75](chat_conversation.py#L75)) skips the gate when `is_overview()` is true. Rankings still reach the gate, which is the hybrid decision.
2. **Measure catalog.** New `DataSource.measures: list[Measure]` in [data_sources.py](data_sources.py), where `Measure` has `id`, `label`, a `question` template with `{grain}` and `{period}` slots, and the allowed grains.
   - Live: ripe bunches, total bunches, ripeness rate, overripe %, bunches/ha (mature), mandays, absence, overtime hours.
   - Staging: ripe bunches, tonnes delivered, loose fruit, bunches/ha, mandays, absence (mangkir), overtime.
   - Sub-questions are written by the template, not the model, so they are phrased the way the seed examples and lint expect.
3. **Plan.** One `chat_json` call (Phase 1), `name="plan_overview"`, with this schema:
   - `{measures: [enum of catalog ids], 2..4 items}`
   - `grain: enum[estate, division, block, month]`
   - `period_note: string`, optional
   - Tier: Nova Pro, or Nova 2 Lite with reasoning if the spike shows that works with a forced tool.
   - The enum means the planner cannot invent a measure. The grain comes from the message: "per estate" or several estates means estate, otherwise the default is estate.
   - The existing scope logic (`detect_entities`, `widens_scope`, `carried_estates`) still decides which estates.
4. **Run.** Each sub-question goes through `generate_and_run()` (Phase 2) in parallel, using a `ThreadPoolExecutor` with `contextvars.copy_context()` as `_start_chart` already does ([chat_router.py:364](chat_router.py#L364)).
   - Table retrieval runs per sub-question.
   - At most 4 sub-queries, each with its own retry budget.
   - The whole request has a 60 s ceiling. Measures that fail are named in the answer, not hidden.
5. **Merge.** Join the sub-results on the grain key in code, into one wide table (estate | measure A | measure B ...).
   - Estate key normalisation reuses `src.entities` and labels, so WERKS, ESTNR and PRFNR all map to one estate label.
   - If the keys don't line up, fall back to the first result as the table and list the others in `parts`.
6. **Answer.** Use the reasoning tier with the merged table, `_derived_facts` per measure column (it already handles up to 3 measures; raise the limit to 4), and a short new prompt `chat_overview_system.txt`.
   - The first sentence names the measures chosen ("I looked at ripe bunches, tonnes delivered and mandays").
   - The same "no own arithmetic" rule applies as in [chat_answer_system.txt](prompts/chat_answer_system.txt).
   - The chart comes from the merged table through the existing `chat_charts.plan_chart`.
7. **Response and UI.**
   - `ChatResponse` gains `parts: list[{measure, sql, row_count, outcome}]`.
   - The merged table is the turn's `rows`, so follow-ups ("which one is worst?") go through the existing context route.
   - `chat.js` shows each part's SQL in the steps panel. That is the only UI change.
8. **Route label.** `route="overview"` in the response and the history, kept out of `pipeline_turns` filtering so it carries context like `sql`.

**Evals:**
- Add 4–6 `golden_chat.yaml` cases with `kind: overview`: live all-estates, staging all-estates, one estate, one Bahasa case, and one ranking that must still ask back.
- Each overview case gets `reference_sql` per measure, keyed by measure id. Extend `judge()` in [evals/run_golden_chat.py](evals/run_golden_chat.py) to look up each measure's figures in its column of the merged rows.
- Add the trigger and merge tests to [test_chat_conversation.py](test_chat_conversation.py).

---

## Verification

**After each phase:**
1. Run the unit tests with Anaconda python: `python -m pytest test_chat_conversation.py test_data_sources.py test_model_routing.py test_domain_check.py test_llm_json.py test_sql_schema_check.py`.
2. `python evals/run_golden_chat.py --check-cases`, then `--runs 3`. The pass rate must not drop below the Phase 0 baseline, and the "distinct SQL" count per case should not rise.
3. `python sql_eval.py --with-examples` and the same without, 3 runs each, comparing medians with the baseline. The baseline file notes run-to-run variance.
4. **Phase 1 only:** compare the `json_via` counts in `llm_calls.jsonl` from a golden run. Target: fewer than 2% `fail`, and zero follow-up router fallbacks caused by a parse error.
5. **Phase 2 only:** the schema-check replay over `query_history.db` in log mode reports about 0 false positives before switching to enforce. `RunResult.outcome` counts from the golden run show where the retries landed.
6. **Manual check** on `/chat`, with uvicorn `--reload` on 8001 and warm-up per the dev-loop memory:
   - "how is Estate C performing?" on staging returns one merged table and a multi-measure answer
   - "who is the best harvester?" still asks back
   - a question naming a non-existent column ends in a fixed query, not an error

**Exit criteria:**
- golden chat ≥ baseline
- `sql_eval` with examples improves on the 0.33 median for Nova 2 Lite
- no new test failures
- overview cases ≥ 80% over 3 runs

## Critical files

[chat_router.py](chat_router.py), [llm_client.py](llm_client.py), [llm.py](llm.py), [error_handler.py](error_handler.py), [chat_conversation.py](chat_conversation.py), [chat_charts.py](chat_charts.py), [data_sources.py](data_sources.py), [retrieval.py](retrieval.py), [query_history.py](query_history.py), [prompt_builder.py](prompt_builder.py), [sql_eval.py](sql_eval.py), [evals/run_golden_chat.py](evals/run_golden_chat.py), [evals/purge_bad_examples.py](evals/purge_bad_examples.py), [chat_static/chat.js](chat_static/chat.js), plus the prompts listed under each phase.

New files: `sql_schema_check.py`, `examples/seed_live.yaml`, `examples/seed_staging.yaml`, `evals/seed_examples.py`, `prompts/chat_overview_system.txt`, `test_llm_json.py`, `test_sql_schema_check.py`.
