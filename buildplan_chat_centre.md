# Build plan: chat as the centre of the app, chat reads the forecast, research agent

Status: Parts A and B built 2026-11-16 (see the build log). Part C: steps 1 (billing trial, run) and 2 (`grounding.py`) done; credit cover still unconfirmed (it can only be seen on the payer account). Step 3 (`brief.py`) is next.

## Build log

**Part C steps 1-2, run 2026-11-16.**
- `forecast/research/grounding.py`: `ask(question, system=)` -> `Grounded(text, citations[{url, domain}], usage, latency_ms)`. boto3 `converse` on `us.amazon.nova-2-lite-v1:0` with the `nova_grounding` system tool. It signs with SigV4 (`Config(signature_version="v4")`): the `AWS_BEARER_TOKEN_BEDROCK` identity (`BedrockAPIKey-jvk4`, AmazonBedrockLimitedAccess) is refused `bedrock:InvokeTool`; the IAM user behind the access keys has it through the `wcstechnical` group, so no IAM change was made. Inline `<thinking>` text is stripped. Each call is logged to `llm_calls.jsonl` with `grounded_queries: 1` and the citation count; the response's usage has tokens only, no query count.
- Trial: `python -m forecast.research.trial` (hard cap 10; answers in `forecast/research/trial_2026-11-16.json`). 11 grounded queries in total (one access test, then 10), all succeeded, 3-10 s each, about 200 input and 220-560 output tokens, 1-20 citations.
- What the answers showed, for step 3: stale facts come back as current (GAPKI's May 2026 report, September's CPO reference price), so the freshness filters are needed; answers drift off the region (the Papua pests query returned Sumatra and generic material), so extraction must drop findings not about the region; claims sometimes come without a date; Papua Selatan's TBS price was not found (as expected; fall back per C.3). Sarawak's MetMalaysia monsoon outlook, MPOB's September figures with Sarawak's own line, the haze emergency, FCPO and the export duty reference all came back dated and cited.
- **Credit cover.** Account 430287291296 is a member of organization `o-vy6xyxv1aq` (payer 393213447518). Its Cost Explorer shows no `Credit` records on any service for Aug-Nov 2026, only gross usage (November so far: $63.38 usage, of which Bedrock Nova is most; plus $192 FlatRateSubscription). So the coupon is applied on the payer's bill and cannot be confirmed from here. Next day: check this account's Cost Explorer (service Amazon Bedrock, by usage type) for the new grounding usage type and its gross cost per query; whoever holds the payer account checks the Credits page for whether it was covered. Caps stay at 20 queries per run and 3 on-demand runs per region per day until then.

**Part B, built 2026-11-16.**
- `chat_forecast.py` is the whole of it: detection, scope, months, reading, the facts block, the figure check, the chart, the follow-up buttons and the no-model fallback. `chat_intent.py` gained the `forecast` route (code-first: a forecast word, or a future period plus a production word; after a forecast turn, a short or pointed follow-up stays on it; "how does it work" goes to explain with `EXPLAIN_NOTES` added to the explain prompt). `chat_router._forecast` builds the response and stores the turn with `meta.forecast = {source, estates, every, months, shown}`; the SQL scope in `meta.estates` is left as it was.
- Reading: `forecast_router.page_payload(estate, horizon, source)`, horizon 3 or 6. The chat's database picks the source. Named estates come from a detector over both rosters (K3, EC, EB, EA, BA, "Estate C", "kebun 3"); none named means the page default (staging group; K3 on live, said in the facts); "all estates" reads the roster (plus the group on staging); an estate on the other source is reported, not read.
- Facts per estate: model (with manual pick / fallback / stale tags), last actual month, each month's expected and likely range (80% range where the payload has it, else the band, as the outlook page does) with the same month last year, the total with last year's total and the change, typical error, the track record, the range's coverage, and whether each of the last six one-month-ahead forecasts landed within 10% (so "forecast vs actual last month" is answered honestly; the past forecast figures themselves are not in the payload). A named month beyond the forecast or already in the actuals becomes a NOTE. More than 6 months is capped with a NOTE.
- Figure check: `gis.reasoning.audit_figures(answer, facts_text)`. A stray figure gets one non-streamed retry naming it; a second failure uses `template_answer`, built from the facts. On the first server runs no answer needed the retry.
- Chart: one estate is `type: "forecast"` (12 actual months, the forecast dashed from the last actual month, the likely range as a band), drawn by a new branch in `chat.js`; several estates are a bar chart of the totals with last year beside them. `renderResult` now shows a chart with no rows. "Chart that" after a forecast turn redraws without a model call.
- Every answer ends with `[Open in Forecast](/forecast?db=<source>&estate=<id>)`. The step summary reads "Read the forecast page".
- Prompt `prompts/chat_forecast_system.txt`: plain words ("expected", "likely range"), figures exactly as in FACTS with comma separators even in Indonesian, no arithmetic, no table repeated on a follow-up, a range belongs to one month, a past month's forecast figure is never stated.
- Tests: `test_chat_forecast.py` (46 checks, model and payload faked); the chat suites and `test_navigation.py` pass (303). Golden: `fc_001`-`fc_009` (`expect_route`) on both databases; `--check-cases` passes. Server runs on both databases: outlook, "how sure is it?", "and EB?", "chart that", "forecast vs actual last month", Indonesian on staging, "what did we produce last month?" back to SQL, "how does the forecast work?" to explain. Screenshot of the fan chart on the chat page.
- Not done (v2 in the plan): mixed questions that need fresh SQL actuals plus the forecast; a forecast starter on the welcome screen.

**Part A, built 2026-11-16.**
- `/` is a 307 to `/chat` that keeps the query string; the dashboard is at `/dashboard`. `Request` is now imported in `dashboard_server.py` for the redirect.
- Chat: the top-bar pill nav is gone. `.sb-pages` at the bottom of the sidebar holds Report and Forecast with icons (`report`, `forecast` added to `ICONS`). `.top-pages`, two icon links beside the sidebar toggle, shows only under `sb-closed` on desktop; on mobile the drawer has them. Cache-busters bumped to `?v=4`.
- The Dashboard link is removed from 11 files (the 9 planned plus `command.html` and the built `command_static/dist/index.html`, edited directly rather than rebuilt). Nav order is Chat / Report / Forecast; the forecast and lab brand links go to `/chat`. The dashboard's own nav keeps a Dashboard item at `/dashboard`.
- The dashboard page makes no relative fetches, so it works unchanged at `/dashboard`.
- Tests: `test_navigation.py` (18 checks: redirect, query string kept, `/dashboard` 200, no page links to `/` or `/dashboard`, every page links to the three, chat has no pill nav). `test_forecast_theme.py`, `outlook_page.mjs` and `forecast_page.mjs` still pass. Checked by screenshot with the sidebar open and closed.

---

Parts A and B are ready to build. Part C (the research agent) has its decisions (C.6, answered 2026-11-16) and starts with a capped trial run to confirm billing. Part D lists ideas for other agents; none of them are planned yet.

## What changes for the user

- Opening the app (`/`) lands on the chat.
- The chat's sidebar holds the links to Report and Forecast. The pill nav in the chat's top bar goes away.
- The Dashboard is not linked from anywhere, but still works at `/dashboard`.
- The chat can answer forecast questions ("what's the outlook for EC next 3 months?", "how sure is it?", "which estate drops most?") from the same files the forecast page serves.

---

## Part A: navigation

### A.1 Routes (`dashboard_server.py`)

| Route | Now | After |
|---|---|---|
| `/` | Dashboard (`dashboard_static/index.html`) | 307 redirect to `/chat`, query string kept |
| `/dashboard` | none | Dashboard (same file, `_NO_CACHE`) |
| `/chat` | chat | unchanged |
| everything else | | unchanged (`/report`, `/forecast*`, `/model-health`, `/experiment/*`, `/command`, `/api/generate`) |

A redirect, not serving `chat.html` at `/`. That keeps one address for the chat, so existing `/chat` links, `?theme=` and the session URLs keep working. `/api/generate` and the dashboard's other API calls are not moved: the dashboard page calls them by absolute path, so it works from `/dashboard` without changes. Check for relative fetches in `dashboard_static/index.html` while doing this.

### A.2 Chat sidebar (`chat_static/chat.html`, `chat.css`, `chat.js`)

- Remove `<nav class="app-nav">` from the top bar.
- Turn `.sb-foot` (today mobile-only, `display:none` on desktop) into a short "Pages" group that is always visible at the bottom of the sidebar. It holds Report and Forecast, each with an icon (`data-ico`), styled like the session rows. Dashboard is removed from it.
- When the sidebar is closed (`sb-closed`), Report and Forecast must still be reachable. Show two icon buttons next to `#sb-open` in the top bar, only while the sidebar is closed. On mobile the sidebar is a drawer, so the group inside it is enough.
- Bump `chat.css?v=` and `chat.js?v=`.

### A.3 Every other page's nav

Remove the Dashboard link from the nav on 9 pages: `report.html`, `outlook.html`, `forecast.html`, `compare.html`, `model_health.html`, `forecast_lab.html`, `evals.html`, `command_static/index.html` and `command.html`, then rebuild `command_static/dist`.
- New order: Chat, Report, Forecast.
- The brand link (`<a class="brand" href="/" aria-label="EPMS AI, dashboard">`) on the forecast and lab pages goes to `/chat`, and its label becomes "EPMS AI, chat".
- `dashboard_static/index.html` keeps its own nav, with its Dashboard item pointing to `/dashboard`. The sliding pill code (`nav-active`) keeps working because it reads the active link, not the href.
- `forecast_static/theme.css` is a copy kept in sync by a drift test (`test_forecast_theme.py`). If the nav CSS changes there, update the source too.

### A.4 Tests

- New `test_navigation.py` (FastAPI TestClient):
  - `/` returns 307 to `/chat`, and keeps the query string.
  - `/dashboard` returns 200 with the dashboard markup.
  - No served page except the dashboard has a nav link to `/` or `/dashboard`.
- Re-run the browser suites that read the nav (`outlook_page.mjs`, `forecast_page.mjs`, `forecast_compare.mjs`) and the chat CDP driver.

Size: about half a day.

---

## Part B: the chat reads the forecast

### B.1 Principles

- **No new numbers.** The chat reads the served forecast from the same functions the page uses: `forecast_router.page_payload(estate, horizon, source)`, which calls `forecast_sources.payload` for staging. Nothing is fitted and no SQL runs for a forecast answer. The chat and the page therefore always agree, including manual picks (`serving_overrides.json`), the K3 ensemble fallback, stale flags, and "unavailable" estates.
- **Numbers are quoted, never computed by the model.** The answer model gets a facts block, and the answer is checked against it. This is the same contract as `forecast_intelligence.py` and the chat's `_derived_facts`.
- **Both sources** (standing rule, see `dual-source-forecast`). The chat's selected database picks the forecast source: Live estates goes to `live`, Staging goes to `staging`. Every test and golden case runs on both.
- **The language matches `/forecast`, not `/forecast/analyst`.** Plain wording, a range rather than metrics, and the same N/A placeholders for tonnes and budget. Accuracy figures are given only when asked ("how accurate has it been?").

### B.2 Routing (`chat_intent.py`)

- Add a sixth route, `FORECAST = "forecast"`.
- Code-first detection, so most forecast questions need no LLM call. A message is sent to the forecast route when it names the future plus production:
  - English: forecast, outlook, projection, predict, expected, next month, next quarter, coming months
  - Indonesian: ramalan, perkiraan, proyeksi, bulan depan, prediksi
  - or a month after the last actual month
- Otherwise the routing-tier call gets `forecast` as a sixth label, with three examples added to `prompts/` (with `CACHE_POINT` kept where it is).
- Edge cases the router must handle:
  - "forecast vs actual last month" goes to forecast (it is answered from the backtest/shadow rows in the payload).
  - "what did we produce last month" goes to data.
  - "explain how the forecast works" goes to explain, using the forecast page's reference text added to the explain sources.
  - A forecast message with no estate uses the page's default scope: the group view, which is K3 on live. The answer says so, like the default period does.

### B.3 New module `chat_forecast.py`

`answer(message, turns, db) -> ChatResponse` does six things in order:

1. **Scope.** Estates from `detect_estate_mentions`, carried scope from the history (`carried_estates`), or the default. The horizon comes from the message ("next 3 months", "Q1") and is capped at the served horizon: steps 1 to 6 for Chronos. Asked for more, the answer says how far the forecast reaches.
2. **Read.** `page_payload` for each estate in scope, through the existing cache. The warm-up after a reload applies here too.
3. **Facts.** A compact block for each estate:
   - source and estate
   - last actual month
   - for each forecast month: point, typical-error band, 80% range
   - the served model's plain label, plus "manual pick" or "fallback" if either applies
   - the stale note, if any
   - headline accuracy and band coverage
   - the same months last year (from the payload's history)
   - unavailable estates with their reason, e.g. "live EA: only 1 complete month"

   Group answers use the payload's group figures. The model never sums anything.
4. **Answer.** One `chat_json`/answer call on the main tier (Nova 2 Lite). The prompt is `prompts/chat_forecast_system.txt`, with the facts block after a cache point.
5. **Check.** Every number in the answer must appear in the facts block, after the existing rounding and format normalisation. On failure there is one retry with the offending numbers named, then a template answer built from the facts. Reuse the lint machinery from the golden set.
6. **Extras.**
   - A forecast chart: actuals plus forecast line plus band, as a new `chart.kind = "forecast"` drawn with the Chart.js already vendored.
   - An "Open in Forecast" link to `/forecast?db=<source>&estate=<id>`.
   - Follow-up suggestions ("How sure is it?", "Compare with last year", "Other estates").
   - The turn is stored with `route="forecast"` and the facts in `meta`, so "chart that", "and EB?" and "why lower?" work as context follow-ups.

### B.4 Questions v1 answers

| Question | Answered from |
|---|---|
| Outlook for an estate or the group, N months | forecast points and bands |
| How sure is it / what's the range | band, 80% range, coverage ("held 17 of 21") |
| Compare estates | each estate's payload, side by side (no summing across sources) |
| Compared with last year | payload history, same months |
| How accurate has it been | headline accuracy and backtest rows |
| Which model / why this model | served model label, manual pick or rule, in plain words |
| Forecast vs budget, in tonnes | stays N/A, as on the outlook page: "not available yet" |

**Not in v1:** mixed questions that need fresh SQL actuals plus the forecast, e.g. "forecast next quarter vs the last 3 months by division". v2 runs them through the overview planner (`chat_overview.py`), with the forecast as one part.

### B.5 Tests and evals

- `test_chat_forecast.py`:
  - routing cases in English and Indonesian, including "forecast vs actual" and "how does the forecast work"
  - the scope and horizon cap
  - the number check rejects an invented figure
  - the payload is read for the right source
  - unavailable and stale estates are worded honestly
- `evals/golden_chat.yaml`: about 12 forecast cases on each source, including multi-turn ones (`turns`): outlook, then "and EB?", then "chart that". Run `run_golden_chat.py`. The 62/63 baseline must hold.
- The chat CDP driver checks that the forecast chart renders.

Size: about 3-4 days.

---

## Part C: research agent for the forecast page

### C.1 What it is for

The forecast model only sees the estates' own history; the K3 covariate arm sees a little more. A research agent would look outside the data: things that will move production but are not in it yet, plus the context a manager needs to trust the number.

**Hard rule, same as `forecast_intelligence.py`:** the agent never changes a forecast number. It writes a dated, cited brief. A research finding can only reach the numbers by becoming a covariate that the Forecast Lab tests, and that goes through the promotion rule like any other change (C.4).

### C.2 Building blocks already here

- `forecast_intelligence.py` already pulls a 14-day Open-Meteo outlook, NOAA ENSO status and NASA FIRMS hotspots, and writes a risk narrative.
- `forecast_investigator.py` is a LangGraph ReAct agent that explains band breaches. Its header still says Groq; check that it now runs through `llm_client` on Nova.
- **Nova 2 Web Grounding.** A built-in Bedrock system tool (`toolConfig.tools = [{"systemTool": {"name": "nova_grounding"}}]`) on `us.amazon.nova-2-lite-v1:0`. It searches the web and returns text with `citationsContent` URLs, so internet research stays Nova-only. Constraints:
  - It works in US regions only. `AWS_REGION` is already `us-east-1`.
  - It needs `bedrock:InvokeTool` on `amazon.nova_grounding`.
  - It costs extra beyond inference. **Check whether the AWS coupon covers it.**
  - `ChatBedrockConverse` may not pass `systemTool` through, so call `boto3` `converse` directly in a small wrapper.

### C.3 What to research, in order of value

1. **Climate with a lag.** Drought cuts oil palm yield 6-24 months later (lower female flowering and more bunch abortion). This is the strongest outside signal for a 6-month forecast.
   - Research: ENSO and IOD outlooks (NOAA CPC, BoM, BMKG), and BMKG seasonal rainfall outlooks for the estates' provinces.
   - Record: rainfall deficits 6-24 months back.
   - Output: "BMKG expects a below-normal dry season in <province> from <month>; past dry spells cut output here N months later."
2. **Calendar effects.** Lebaran (Idul Fitri) moves about 11 days earlier each year, and harvesting drops in that month and shifts into the next. This is deterministic, so it needs no web search: compute it directly and make it a covariate candidate. Also school holidays and regional elections.
3. **Industry production signals.**
   - Sources: GAPKI monthly production and stock reports for Indonesia, MPOB for Malaysia (a regional seasonal check), and published analyst outlooks for Indonesian output.
   - Use: flag when our forecast direction disagrees with the industry's (e.g. ours +8% while the industry expects -5%). This is a sanity check, not a correction.
4. **Events.** Haze and fire beyond the FIRMS counts, floods or road closures near the estates, mill outages, and pest or disease outbreaks (Ganoderma, rhinoceros beetle, bagworm) reported in the region. Local news is the source, so citations matter most here.
5. **Inputs with a lag.** Fertilizer prices and supply in Indonesia (urea, MOP), subsidy changes, and labour (UMP changes, harvester shortages). Under-fertilising shows up in yield about 12-24 months later.
6. **Prices and policy.** CPO price (FCPO), the provincial TBS price set by Disbun, export levy and duty, B40/B50, EUDR. These do not move production, but they turn a tonnes forecast into IDR, which serves the client's "where are we losing money" question. This topic is optional and fits the Cost efficiency work better.

**v1 scope (decided 2026-11-16): topics 1, 3, 4 and 6.** The Lebaran calendar (2) is not a research topic. It goes to the Forecast Lab as a covariate experiment, because it can be computed without any search.

**The two regions need different sources.** The estates are in two countries, so each region gets its own query templates and languages.

| | EA, EB, EC: South Papua (Indonesia) | K3: Sarawak (Malaysia) |
|---|---|---|
| Season | Dry season about Jul-Nov, strongly ENSO-driven. Output peaks Apr-Jun. | Output peaks Sep-Nov. |
| 1. Climate | BMKG seasonal outlook for Papua Selatan, plus NOAA/BoM ENSO and IOD | MetMalaysia outlook, plus NOAA/BoM ENSO and IOD |
| 3. Industry | GAPKI (Indonesia), with MPOB as a regional check | MPOB monthly data, including Sarawak's own figures |
| 4. Events | Local news for Merauke and Boven Digoel: fires and haze (including land clearing for the Merauke food estate), floods, roads, river transport, mill outages, disruptions to operations | Sarawak news: floods, haze, labour shortages (foreign worker supply), mill outages |
| 6. Prices and policy | Provincial TBS price (Disbun Papua Selatan; fall back to Papua or national if not published), CPO reference price, export levy and duty, B40/B50, EUDR | FCPO (Bursa Malaysia), Malaysian export duty and windfall levy, EUDR |
| Query language | Indonesian, then English | English, then Malay |

- **Expect thin news for South Papua.** Coverage of the area is sparse. When nothing is found, the brief says "nothing new found" and does not pad.
- **Prices and policy (topic 6) never carry a production direction.** It is always `direction: none` for production. On the page it is its own line ("Prices"), apart from the production risks.

Each finding is:

```
{claim, source_url, published, region, forecast_months_affected, direction: up|down|none, confidence}
```

The agent drops anything without a citation, or older than its freshness window.

### C.4 How it reaches the user

- **Forecast page (the audience is the client).** The card sits on `/forecast` (outlook), for the estate or group in view:
  - Label: "Outside the data". Subtitle: "What could move this forecast?"
  - 3-5 plain lines, each with an up/down/none marker, the months it touches and a source link.
  - A "Prices" line underneath.
  - "Checked <date>" and a "Check again" button for on-demand runs.
  - No method or provenance text (UI copy rule). The full brief, with every finding and query, goes on model health for the team.
- **On demand.**
  - `POST /forecast/research?source=&estate=` runs the pipeline for that region.
  - A result less than 6 hours old is returned from the cache, not re-run.
  - At most one run per region at a time, and a daily run cap, both in `config.py`.
  - The button shows progress, and "Checked just now" when it finishes.
- **Chat:** a tool for the forecast route.
  - "Is there anything that could affect next quarter?" reads the latest brief, with its date.
  - "Check the latest news" or "search now" starts an on-demand run, under the same cache and caps, and answers when it finishes.
  - Every claim in a chat answer keeps its source link.
- **Forecast Lab:** findings with a measurable series become covariate candidates for the Chronos covariate arms: ENSO index, lagged rainfall deficit, the Lebaran month flag. They are backtested and promoted only through the dated rule.
- **Audit:** each brief is saved as `forecast/research/<date>/<source>_<region>.json`, next to the refresh run. When the months it flagged resolve, score it: did "down" months come in below the forecast? Without that score, there is no evidence the agent helps.

### C.5 Shape of the agent

Keep it small at first: a scripted pipeline, not an open-ended ReAct agent.

1. For each region and topic, run a fixed query template through Nova grounding.
2. Pull out findings with `chat_json` against the C.3 schema.
3. Drop uncited or stale findings, and drop duplicates.
4. Pass the existing `forecast_intelligence` signals in as facts.
5. Write the brief.

It runs in two ways:
- **Scheduled:** at the end of `refresh.py` (monthly), for both regions.
- **On demand:** from the page button or the chat (C.4). It runs as a background task, so a page request never waits on a search.

The brief is per region, not per estate. EA, EB and EC share one South Papua brief, filtered to each estate's months. Live and staging read the same brief: the outside world does not depend on which database is selected. Move to a ReAct loop only if the fixed templates miss things.

### C.6 Decisions (answered 2026-11-16)

1. **Audience:** the client. Short card on `/forecast`. The full brief goes on model health.
2. **Topics:** 1 (climate), 3 (industry), 4 (events) and 6 (prices and policy). The Lebaran calendar goes to the Lab as a covariate experiment.
3. **Cadence:** monthly with the refresh, plus on demand from the page and the chat.
4. **Credit:** the credit list names "Amazon Bedrock", "Amazon Bedrock Service" and "AmazonBedrockFoundationModels". Web Grounding is billed under Bedrock but is not listed by name, so it is probably covered but not confirmed. **First build step:**
   - one capped trial run of about 10 grounded queries
   - after the charge has posted (about 24h), check Cost Explorer (service Amazon Bedrock, grouped by usage type) and confirm that the grounding line went against the credit
   - until confirmed, keep the cap at 20 queries per run and 3 on-demand runs per region per day
5. **Regions:** EA, EB and EC in South Papua (Papua Selatan, Indonesia). K3 in Sarawak (Malaysia). Sources and languages are in C.3.

### C.7 Build steps

1. Billing trial (C.6 #4). Add `bedrock:InvokeTool` on `amazon.nova_grounding` to the IAM policy if the trial is refused.
2. `forecast/research/grounding.py`: a boto3 `converse` wrapper with `nova_grounding`. It returns text with its citations. Calls are logged to `llm_calls.jsonl` with a query count.
3. `forecast/research/brief.py`:
   - the query templates per region and topic, from the C.3 table
   - extraction to the finding schema
   - filters: no citation, too old (climate 60 days, events 30, industry and prices 45), duplicates
   - the brief JSON is written under `forecast/research/<date>/`
4. Hook into `refresh.py`. Add `POST /forecast/research` and `GET /forecast/research` with the cache and caps.
5. The card on `outlook.html` and the full list on model health. Add page tests to `outlook_page.mjs`.
6. The chat tool on the forecast route, plus golden cases ("anything that could affect next quarter?", "check the latest news").
7. Scoring: when the flagged months resolve, `shadow.py record` marks each up/down finding right or wrong. Model health shows the hit rate.
8. Tests: `test_forecast_research.py`, with grounding mocked:
   - schema, filters and caps
   - the cache hit
   - a region with no findings gives "nothing new found"
   - the brief never changes a payload number. Check it with a byte-equal payload before and after.

Size: about 4 days for v1, plus 1 day of waiting for the billing check. The Lebaran covariate experiment in the Lab is separate.

---

## Part D: other agent ideas (not planned)

| Agent | What it does | Why it fits here | Effort |
|---|---|---|---|
| **Cost leakage investigator** | Drills from estate to division, block and activity to find where IDR is lost. Uses the chat's SQL pipeline as its tool and writes ranked findings, each with the query behind it. | It is the client's own question (Cost efficiency pivot) and reuses `generate_and_run` and the lint. | Medium-high |
| **Forecast miss explainer** | When a shadow row resolves outside the band, it explains why, using weather, data gaps, the history of a block, and the research brief. | `forecast_investigator.py` already does most of this. It needs Nova, staging and a trigger from `shadow.py record`. | Low-medium |
| **Monthly report writer** | Drafts the monthly production report: actual vs forecast vs last year, the biggest movers, open risks from the research brief. It writes into `/report`. | It joins the chat, the forecast and the research into one thing a manager forwards. | Medium |
| **Data-quality watchdog** | After each staging extract or refresh, it checks for missing days, FDN weight duplicates and history-table gotchas, then explains in plain words what it found and what it affects. | It turns the existing monitoring alerts into something readable, and protects the dual-source comparison. | Low |
| **Weekly digest** | A scheduled "3 things that changed": a yield drop on a block, a rain deficit, a forecast revision. It is sent by email or Slack, or shown on chat open. | It makes the chat the start page in a real sense: the chat greets you with what changed. | Medium |
| **Golden-set curator** (dev-side) | Turns thumbs-down feedback into candidate golden cases and examples to purge, for review. | It closes the loop on `feedback` and `purge_bad_examples.py`. | Low |

Suggested order after A and B:
1. The forecast miss explainer. It is cheap, and it gives the research agent a measured job.
2. The research agent v1.
3. The cost leakage investigator.

## Build order

1. Part A, the navigation (half a day).
2. Part B, the chat reads the forecast (3-4 days), with golden cases on both sources.
3. Part C, the research agent. Its billing trial (C.7 step 1) can run in parallel with A and B, so the answer is in before C starts.
4. Part D: pick the next agent.
