# AutoDashboard

A natural-language AI interface for a PostgreSQL plantation management (EPMS) database. Ask questions in plain English across three browser-based interfaces — the system generates SQL, executes it, and presents results as dashboards, reports, or conversational answers.

Built as an internship project to explore practical LLM application development: retrieval-augmented generation, multi-step LLM pipelines, and cost-efficient inference on free-tier APIs.

---

## Four Main Features

### 1. Dashboard
Ask a goal like *"show me revenue by division"* and the app generates a full dashboard layout — 3 to 12 widgets (KPI cards, bar charts, line charts, tables) — each backed by its own SQL query. The layout is structured JSON produced by the LLM, then executed and returned to the browser as a live dashboard.

### 2. Report
Two modes:
- **Preset reports** — fixed SQL templates for common summaries (general, employee, harvest). Zero LLM cost.
- **Generated reports** — ask a freeform question; the full RAG → SQL pipeline runs, potentially generating multiple queries and composing the results into a multi-section report. Supports saved query templates with date parameterization for repeatable business queries.

### 3. Chat
Conversational Q&A. Ask a question in plain English, get a natural-language answer. The pipeline retrieves relevant tables and few-shot examples, generates SQL, executes it with one self-correction retry on error, then uses an LLM to compose an answer adapted to the shape of the result (count, comparison, trend, list, etc.). Returns the SQL, row count, tables used, and the answer.

### 4. Forecast
CSV-based estate FFB (plantation) production forecast — separate from the database-driven text-to-SQL pipeline above. An ensemble of SARIMAX + LightGBM, trained on ~40 months of production, weather, and agronomic-practice data, projects the next 3/6/12 months with a conformal-calibrated sMAPE uncertainty band. Includes:
- **TreeSHAP driver panel** — per-forecast-month feature attributions (rainfall, pruning, fertiliser lag effects, seasonality, etc.) explaining *why* the model predicts what it does
- **Intelligence layer** — an LLM risk-synthesis pass over the ML forecast that pulls live weather/ENSO/fire-hotspot signals and returns a narrative risk level, without altering the underlying numbers
- **Investigator agent** — a LangGraph ReAct agent that runs only when triggered (an actual falling outside the conformal band, or a divergence between the numeric forecast and the risk level), reasoning over live signals, the database, and SHAP tools to explain anomalies

### 5. Estate Command
A full-page GIS decision layer over the estate's own geometry: 291 real block polygons, coloured by any of sixteen metrics, each badged with where its numbers came from — the client's records, a free satellite, or a generator. Six decision panels turn a map into a recorded accept / reject / defer, and a data-readiness register states, per capability, what exists and what it degrades to without it.

Two of its layers are worth calling out because neither came from the client:

- **Real canopy vigour.** `gis/build_ndre.py` searches the free Copernicus archive for the least-cloudy Sentinel-2 scene over the estate, crops the red-edge and near-infrared bands to its bounding box through a public raster service, masks cloud with the scene's own classification band, and averages NDRE inside each block polygon. No GDAL, no rasterio, no credentials — numpy and one HTTP call. On the current EC scene that is 291 of 291 blocks measured. It is the only layer on the page whose numbers are a real measurement the client does not already hold.
- **Metric agreement.** `layers.compare_metrics` computes the rank correlation between any two metrics and returns the blocks in the bottom fifth of both. Satellite vigour and recorded yield turn out to rank this estate almost independently, which is itself the finding: the thirteen blocks weak on both are the corroborated ones.

**The operating rhythm.** Five of the panels stop describing the estate and start running it. Harvesting, pruning, weeding and spraying, pest control and transport each open in their own window rather than the side dock, with a section menu down the left grouped as Tomorrow, Ledger and Why, over one row shape, the work order: what was planned, what was done, by whom, on which block. Anything that names blocks has a Show-on-map action, which outlines them on the full map and shrinks the window to a bar at the foot of the screen until you go back to it. The assumption register and Did-it-work open the same way.

- **Ledger.** 143 days of generated orders across six operations, ending on 2025-05-23 where the client's export ends. Harvest actuals sum to the real block-month bunch counts to the bunch; the plan is backed out through an adherence model driven by real daily rainfall (Open-Meteo), road condition and crew attendance, calibrated to 78%. Upkeep and pest work are simulated crew by crew, day by day, so a partial order names the order it became and the round stretches because the simulation stretched it.
- **Tomorrow.** The assignment for 2025-05-24, the first day beyond the client's data. `gis/models/scheduler.py` is greedy by deferral value per man-day with a swap-improvement pass, a contiguity bonus that produces "blocks 14 to 19" rather than a scattered list, a travel penalty, and the gap to a relaxed bound so the greedy choice is accountable. Every headcount is editable; a re-run reprices in under a second.
- **Why.** The demand ranking with its arithmetic, the objective's terms, the binding constraint, and what contiguity cost, measured by running the plan again without it.
- **Forecast.** Four fitted models feed the plan, and every one says what it means in plain words ("a 54% chance of rain heavy enough to wash off spraying", "expect 574 to 616 of 668 people"). Rain is learned from 954 days of real Open-Meteo forecasts issued the evening before, set against the rain that fell, and turns spray-or-hold into a cost decision. Headcount gives each crew a likely range; work done gives each block its expected share done and flags the ones likely to need another day; crew speeds learn who covers more or less ground than the book. Each is checked week by week on days it had not seen against the method it replaces, carries a one-word trust grade, has a switch in the assumption register, and falls back to the old method if it does not beat it. Three of the four learn from the generated ledger, and say so: their checks show they find what the generator put there and invent nothing it did not. Tomorrow's outlook, in the Governance domain, puts all four on one screen.

Every rupiah and tonne rests on `gis/assumptions.py`, a register the client can edit in the Governance panel; the four loop-closing columns on the decision log (`due_date`, `expected_effect`, `order_ref`, `observed_effect`) let the Did-it-work panel read an accepted plan back against the ledger once its date has passed. A scheduled figure never wears a real badge.

**The AI layer.** Six features run on Amazon Nova through the same `llm_client` seam the rest of the app uses — Nova Pro for reasoning, Nova Lite for the short-form writing:

| Feature | What it does | Model |
|---|---|---|
| Ask the map | Tool-calling agent over thirty-four tools spanning every layer, including five that return plans computed server-side and five that return the forecasts in plain words: "G1-03 is four men short tomorrow, replan" is a question it can answer. Answers in figures and moves the map to match. | Nova Pro |
| Duty officer brief | Turns a fire assessment into a posture, a priority order and orders a person can act on | Nova Pro |
| Shift handover | Reads the decision log and the open positions and writes the note someone arriving cold needs | Nova Pro |
| Block brief | The agronomist's read on one block against its planting cohort and its satellite reading | Nova Lite |
| Artifact instructions | The words on a drafted work order, inspection order or requisition | Nova Lite |
| Readiness interview | Turns a red capability row into the questions worth asking, then scores the client's answer against the measurement | Nova Lite |

Three properties hold across all six:

- **The server computes, the model writes.** Every figure is calculated by `layers.py`, `fire.py` or `vegetation.py` and handed over with a provenance tag; the prompts forbid deriving anything new.
- **The guardrail is checked, not trusted.** `reasoning.audit_figures` re-reads every number and block label in the generated text and looks for it in the payload behind it. Anything missing is reported on the response and shown in the UI. The copilot gets one self-correction pass when its own audit fails; whatever survives is still labelled.
- **Nothing generated is load-bearing.** Each feature returns `available: false` with a reason when the model is unreachable, and the panel underneath renders regardless. No model in this app can write to EPMS — both engines are opened read-only.

---

## How It Works

The Dashboard, Report, and Chat features share the same underlying pipeline (the Forecast page is a separate, CSV-based ML pipeline described above — it does not touch the database):

```
User question
     │
     ▼
1. Retrieval (no LLM)       — ChromaDB + BM25 hybrid search finds relevant tables
     │
     ▼
2. SQL Generation (LLM)     — Prompt with schema context, business rules → SQL query
     │
     ▼
3. Execute + Self-Correct   — Run query; on error, feed error back to LLM for one retry
     │
     ▼
4. Presentation (LLM)       — Dashboard layout / report sections / conversational answer
```

**Business grounding:** Every prompt is augmented with `business_rules.txt` and `glossary.yaml` so the LLM understands domain-specific terms, table preferences, and query constraints.

**SQL safety:** All queries are validated before execution — DDL, DML, and multi-statement queries are blocked. The database connection is read-only with a configurable statement timeout.

---

## Features

- **Hybrid table retrieval** — combines dense vector search (ChromaDB + fastembed) with BM25 keyword matching for robust schema lookup
- **Self-correction loop** — on SQL execution error, the error message is fed back to the LLM for one corrected retry
- **SQL safety validation** — blocks DDL, DML, and multi-statement queries; read-only DB connection with configurable timeout
- **Few-shot examples from history** — successful Chat queries are stored and retrieved as in-context examples for future similar questions
- **Switchable model providers** — toggle between GPT OSS 120B, and GPT OSS 20B at runtime via the UI or API
- **Schema bootstrapping** — `bootstrap_metadata.py` uses Gemini to auto-generate table descriptions, synonyms, and example questions from DDL
- **Hot-reload** — schema metadata and business glossary can be reloaded without restarting the server

---

## Tech Stack

| Layer | Technology |
|---|---|
| Web framework | FastAPI + Uvicorn |
| LLM inference | Provider-agnostic LangChain layer (`llm_client.py`): Groq API today (GPT OSS 120B/20B, Llama 3.1 8B, Qwen3 32B); Amazon Bedrock (Nova) ready via config |
| Schema bootstrapping | Google Gemini 2.5 Flash |
| Vector store | ChromaDB |
| Embeddings | fastembed (BAAI/bge-small-en-v1.5) |
| Keyword search | BM25 (rank-bm25) |
| Database | PostgreSQL via SQLAlchemy |
| Forecasting | SARIMAX + LightGBM ensemble, conformal prediction, TreeSHAP |
| Forecast agents | LangGraph (Investigator ReAct agent), LLM risk-synthesis (Intelligence layer) |
| Frontend | Vanilla HTML/CSS/JS (four static files) |

---

## Project Structure

```
├── dashboard_server.py     # Main entry point (port 8001) — mounts all apps
├── chat_router.py          # Chat endpoint: RAG → SQL → conversational answer
├── report_router.py        # Report endpoints: preset and generated reports
├── forecast_router.py      # Forecast endpoints: data, intelligence, investigate
├── forecast/
│   ├── monthly_model.py    # SARIMAX + LightGBM ensemble training/inference
│   ├── monthly_model.joblib
│   └── conformal.py        # Conformal-calibrated uncertainty bands
├── forecast_intelligence.py # LLM risk-synthesis over live weather/ENSO/fire signals
├── forecast_investigator.py # LangGraph ReAct agent for anomaly investigation
├── gis_router.py           # Estate Command endpoints: map, panels, readiness, AI layer
├── command_static/         # Estate Command frontend (Vite + ES modules)
│   ├── src/
│   │   ├── main.js         # Entry: boot, wiring, the DOMContentLoaded hook
│   │   ├── state/
│   │   │   ├── store.js    # The S store
│   │   │   └── selection.js # The working set of blocks, and what it totals
│   │   ├── map/            # MapLibre instance, layers, choropleth, pins, fire
│   │   │   ├── blocks.js   # Resolves four block-id spellings to map ids
│   │   │   ├── highlight.js # The five highlight layers and who owns each
│   │   │   ├── hover.js    # The readout under the cursor
│   │   │   ├── mapctl.js   # Metric picker and legend, on the map
│   │   │   └── legend-filter.js # Drag a band to select every block in it
│   │   ├── shell/
│   │   │   ├── context-bar.js # Estate, view, coverage, the data request
│   │   │   ├── rail.js     # Six domain icons and the panel flyout
│   │   │   ├── workbench.js # The tabbed dock panels render into
│   │   │   ├── popup.js    # The feature window: section menu, Show on map, minimise to a bar
│   │   │   └── tray.js     # What a selection can be turned into
│   │   ├── panels/         # The panel registry and one module per domain
│   │   │   ├── _shared.js  # Finds block references and wires them to the map
│   │   │   ├── ops.js      # Window definitions for the five operation menus, the register, Did-it-work
│   │   │   └── forecasts.js # Tomorrow's outlook, the plan strip and each window's Forecast section
│   │   └── ai/             # Ask the map, briefs, the readiness interview
│   ├── styles/command.css
│   ├── test/
│   │   ├── undefined.mjs   # AST check: identifiers used but bound nowhere
│   │   ├── smoke.mjs       # Loads the real page, walks all 42 panels and every window section
│   │   ├── ops.mjs         # Drives the windows: edit, re-run, show on map and back, draft, filter
│   │   ├── models.mjs      # The forecasts: grades, planted-truth checks, switches, replay, the outlook
│   │   ├── selection.mjs   # Builds a set, drafts a document, clears it
│   │   ├── compare.mjs     # Metric agreement
│   │   └── blocks.mjs      # Reports which panels drive the map
│   └── dist/               # Built bundle, what /command serves (gitignored)
├── gis/
│   ├── ontology.py         # Estate/division/block geometry from the ArcGIS export
│   ├── layers.py           # Derived per-block metrics, panels, metric agreement
│   ├── fire.py             # UC-06 hotspot triage: FIRMS + Open-Meteo + spread screen
│   ├── decisions.py        # Decision log and drafted artifacts (SQLite, never EPMS)
│   ├── ops.py              # The work-order ledger: adherence, capacity, demand, outcomes
│   ├── assumptions.py      # The assumption register every plan is priced from
│   ├── build_synthetic.py  # Offline: the sixteen demand-side feeds, one latent field
│   ├── build_operations.py # Offline: crews, attendance and six operations' work orders
│   ├── build_rain_forecast.py # Offline: what the weather forecast said the evening before, daily
│   ├── forecasts.py        # Tomorrow's outlook: the four forecasts in plain words, with trust grades
│   ├── models/
│   │   ├── scheduler.py    # Tomorrow's assignment: greedy by value density, swap pass, bound
│   │   ├── learn.py        # Shared kit: ridge and logistic fits, scoring, grades, plain wording
│   │   ├── rain.py         # Chance of wash-off, heavy and stopping rain from real forecasts
│   │   ├── headcount.py    # Who turns up per crew, as a likely range
│   │   ├── slippage.py     # How much of each planned order gets done, and which may slip
│   │   ├── rates.py        # Crew speeds and harvest block pace, learned against the book
│   │   ├── productivity.py # Adjusted daily target per block from terrain, age, density
│   │   ├── shrinkage.py    # Field-to-mill anomaly detection, scored against planted answers
│   │   ├── clusters.py     # Agronomic underperformance grouping
│   │   └── lagged_forecast.py # Yield against rainfall at the agronomic lags
│   ├── readiness.py        # UC-15 capability register and the interview store
│   ├── vegetation.py       # Real Sentinel-2 NDRE per block (read side)
│   ├── build_ndre.py       # Offline: STAC search + COG crop + per-polygon NDRE
│   ├── reasoning.py        # Shared LLM plumbing: JSON repair, TTL cache, figure audit
│   ├── briefings.py        # Block / fire / handover / artifact / interview narratives
│   └── copilot.py          # Ask the map: tool-calling agent over every layer above
├── main.py                 # Original standalone text-to-SQL server (port 8000)
├── llm.py                  # Model-toggle entry points (call_llm etc.)
├── llm_client.py           # Provider-agnostic LangChain chat layer (Groq/Bedrock) + call log
├── prompts/                # Externalized prompt templates (per-provider overrides)
├── retrievers.py           # LangChain BaseRetriever seam (schema now, Bedrock KB later)
├── guardrails.py           # No-op guardrails seam (future Bedrock Guardrails call site)
├── tool_schemas.py         # Provider-neutral tool schemas + OpenAI/Bedrock translators
├── sql_eval.py             # Golden-set text-to-SQL eval harness (evals/sql_cases.yaml)
├── eval_harness.py         # Investigator-agent eval harness (evals/anomaly_cases.jsonl)
├── doc_ingestion/          # PDF/DOCX parsing dry run for the contracts use case
├── infra/terraform/        # Bedrock KB IaC skeletons — NOT YET APPLIED
├── retrieval.py            # Hybrid ChromaDB + BM25 retrieval
├── prompt_builder.py       # Prompt assembly and SQL extraction
├── metadata_loader.py      # Schema catalog loader (schema_metadata.json)
├── sql_validator.py        # SQL safety checks
├── query_history.py        # Few-shot example storage
├── layout_prompt.py        # Dashboard layout prompt builder
├── layout_schema.py        # Dashboard widget JSON schema
├── error_handler.py        # Error classification
├── query_normalizer.py     # Query standardization
├── config.py               # Env-var config and SQLAlchemy engine
├── bootstrap_metadata.py   # One-time Gemini-powered schema annotation script
├── dashboard_static/
│   └── index.html          # Dashboard UI
├── report_static/
│   └── report.html         # Report UI
├── chat_static/
│   └── chat.html           # Chat UI
├── forecast_static/
│   └── forecast.html       # Forecast UI
├── business_rules.txt      # Domain rules and query constraints for the LLM
├── glossary.yaml           # Domain term → table/column mappings
├── schema_metadata.json    # Table descriptions and embeddings
└── requirements.txt
```

---

## Getting Started

### 1. Clone and install

```bash
git clone https://github.com/KimiPatria/Text-to-SQL-Chatbot.git
cd AutoDashboard
python -m venv .venv
.venv\Scripts\activate       # Windows
# source .venv/bin/activate  # macOS / Linux
pip install -r requirements.txt
```

### 2. Configure environment

Create a `.env` file:

```env
GROQ_API_KEY=your_groq_api_key
DATABASE_URL=postgresql://username:password@host:5432/dbname
DB_SCHEMA=public
```

Get a free Groq API key at [console.groq.com](https://console.groq.com).

### 3. Set up domain context files

Edit `business_rules.txt` to describe query constraints, and `glossary.yaml` to map domain terms to table/column names. The LLM uses both as grounding context in every prompt.

### 4. Bootstrap schema metadata

Run this once to generate `schema_metadata.json` — natural-language descriptions for every table in your database:

```bash
# Requires GEMINI_API_KEY in .env
python bootstrap_metadata.py
```

### 5. Build the Estate Command frontend

Estate Command is an ES module tree built by Vite. The other four interfaces
are still single files and need no build.

```bash
cd command_static
npm install
npm run build          # writes command_static/dist, which /command serves
cd ..
```

`/command` falls back to the pre-build single file when `dist/` is absent, so a
missing build degrades rather than serving a blank page. Rebuild after any
change under `command_static/src`.

For frontend work, `npm run dev` serves the page on 5173 with hot reload and
proxies the API to 8001, so both processes run side by side.

`npm run build` runs two gates before bundling. `test/undefined.mjs` walks each
module's AST and reports any identifier that is used but bound nowhere and is
not a browser global: Rollup treats such a name as a global the browser will
supply, so a forgotten import builds perfectly and throws the moment the line
runs. Then Vite resolves every import and fails on a missing export.

`npm test` needs the server running. It loads the real page in Chrome, walks
every panel in every domain, builds a selection and drafts a document from it,
and fails on any console error. `node test/ops.mjs` drives the operation
windows: edits a gang, re-runs the plan, sends a gang's blocks to the map and
restores the window, filters the ledger across sections, drafts the
assignment, checks the decision carries its due date and order references,
switches weeding to spraying, and edits an assumption. `node test/models.mjs`
checks the forecasts: each beats the method it replaces, each passes its
planted-truth checks, the generator's answer key never reaches a payload,
switching a forecast off puts the plan back on the old method, a replayed spray
day decides on the forecast rather than the rain that fell, and the outlook
window explains every section in words. The models fit in the background when
the server starts; the first outlook after a restart can take half a minute.

The synthetic feeds are regenerated with `python gis/build_synthetic.py`, which
now ends by running `gis/build_operations.py` for the crews, attendance and
work orders; `python gis/build_rainfall.py` keeps the daily series the ledger
reads rainfall on the day from, and `python gis/build_rain_forecast.py` pulls
what the forecast said the evening before each day, for the rain model.

### 6. Run

```bash
uvicorn dashboard_server:app --reload --port 8001
```

Then open the four interfaces:

| Interface | URL |
|---|---|
| Dashboard | [http://localhost:8001](http://localhost:8001) |
| Report | [http://localhost:8001/report](http://localhost:8001/report) |
| Chat | [http://localhost:8001/chat](http://localhost:8001/chat) |
| Forecast | [http://localhost:8001/forecast](http://localhost:8001/forecast) |
| Estate Command | [http://localhost:8001/command](http://localhost:8001/command) |

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Dashboard UI |
| `POST` | `/api/generate` | Generate a dashboard layout |
| `GET` | `/report` | Report UI |
| `POST` | `/report/generate` | Generate a freeform report |
| `GET` | `/chat` | Chat UI |
| `POST` | `/chat/message` | Send a chat message |
| `GET` | `/command` | Estate Command UI — full-page GIS decision layer |
| `GET` | `/gis/estates` | Estate index: geometry provenance, block count, harvest window |
| `GET` | `/gis/blocks` | Block polygons for one estate (404 when it has no shapefile) |
| `GET` | `/gis/metrics` | Per-block values for one metric, optionally one month |
| `GET` | `/gis/metrics/catalogue` | Every map metric with its provenance (real / derived / synthetic) |
| `GET` | `/gis/blocks/table` | Merged per-block values: real attributes plus derived |
| `GET` | `/gis/contract` | UC-08 production and forecast against committed volume |
| `GET` | `/gis/vendors` | UC-09 vendors ranked by landed cost, with allocation |
| `GET` | `/gis/rotation` | UC-01 blocks due for harvest, by ripeness pressure |
| `GET` | `/gis/labour` | UC-12 harvester supply against demand |
| `GET` | `/gis/replant` | UC-11 replant schedule from real planting years |
| `GET` | `/gis/fire` | Fire triage: live hotspots, spread cone, exposure, mobilisation |
| `GET` | `/gis/fire/assets` | Fire-response assets (synthetic — EPMS records none) |
| `GET` | `/gis/fire/scenarios` | Available fire scenarios |
| `GET` | `/gis/vegetation` | Sentinel-2 canopy scene behind the NDRE layer, and its coverage |
| `GET` | `/gis/metrics/compare` | Rank correlation between two metrics, plus the blocks weak on both |
| `GET` | `/gis/readiness` | Per-capability data readiness, with measured evidence |
| `POST` | `/gis/decisions` | Log an accept/reject/defer and draft its artifact |
| `GET` | `/gis/decisions` | The audit view: every proposal and what a human did |
| `POST` | `/gis/ask` | Ask the map — tool-calling agent over every layer, with trace and figure audit |
| `GET` | `/gis/ask/examples` | Seeded questions for the empty state |
| `GET` | `/gis/blocks/brief` | Agronomist's read on one block, grounded in its own figures |
| `GET` | `/gis/fire/brief` | Duty officer's brief over the live fire assessment |
| `GET` | `/gis/handover` | Shift handover note over the decision log and open positions |
| `GET` | `/gis/readiness/interview` | The questions worth asking the client about one capability |
| `POST` | `/gis/readiness/interview` | Score the client's answer against the measurement, and record it |
| `GET` | `/gis/ai/status` | Which model serves each generated feature |
| `GET` | `/gis/ops` | Every operation's headline: orders, adherence, carried forward |
| `GET` | `/gis/ops/{operation}/ledger` | The work-order ledger, filterable; adherence by week, crew and driver; the chains |
| `GET` | `/gis/ops/{operation}/adherence` | Planned against actual by crew, block, division, month |
| `GET` | `/gis/ops/{operation}/demand` | What is due on a date, how urgent, what deferring it costs |
| `GET` | `/gis/ops/{operation}/plan` | Tomorrow's assignment, every figure pre-computed |
| `POST` | `/gis/ops/{operation}/plan` | Re-run with edited constraints: headcount, a crew out, rain, a block held |
| `GET` | `/gis/ops/{operation}/deferral` | What waiting costs on one block |
| `GET` | `/gis/ops/capacity` | Who is on the roll, who is expected, what they can do |
| `GET` | `/gis/ops/outcomes` | Did it work: accepted plans read back against the ledger |
| `GET` | `/gis/assumptions` | The assumption register: value, unit, source, what uses it |
| `POST` | `/gis/assumptions` | Set one value; the next plan is priced at it |
| `DELETE` | `/gis/assumptions` | Back to the defaults |
| `GET` | `/forecast` | Forecast UI |
| `GET` | `/forecast/data` | Historical + 3/6/12-month production forecast with uncertainty band |
| `GET` | `/forecast/intelligence` | LLM risk-synthesis narrative over the ML forecast |
| `GET` | `/forecast/investigate` | Investigator agent output (runs only when triggered, or with `?force=1`) |
| `GET` | `/health` | Health check |
| `POST` | `/admin/reload-schema` | Hot-reload schema metadata |
| `POST` | `/admin/reload-glossary` | Hot-reload business rules |

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `GROQ_API_KEY` | — | Groq API key (required while `LLM_PROVIDER=groq`) |
| `DATABASE_URL` | — | PostgreSQL connection string (required) |
| `DB_SCHEMA` | `public` | PostgreSQL schema to introspect |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Main SQL-generation model |
| `MAX_RESULT_ROWS` | `100` | Row cap injected into every query |
| `STATEMENT_TIMEOUT_MS` | `15000` | PostgreSQL statement timeout (ms) |
| `LLM_PROVIDER` | `groq` | Infrastructure provider: `groq` or `bedrock` (inert until AWS access exists) |
| `BEDROCK_MODEL_ID` | `amazon.nova-pro-v1:0` | Main model when `LLM_PROVIDER=bedrock` (placeholder Nova tier) |
| `BEDROCK_ROUTING_MODEL_ID` | `amazon.nova-lite-v1:0` | Cheap routing/classifier model under Bedrock |
| `AWS_REGION` | `us-east-1` | Bedrock region (inert until credentials exist) |
| `LLM_LOG_PATH` | `./llm_calls.jsonl` | Structured per-LLM-call log (task, provider, model, tokens, latency); empty disables |

---

## Switching LLM providers (Groq → Amazon Bedrock)

The pipeline never talks to a provider SDK directly: every call goes through
`llm_client.py`, which builds a LangChain `BaseChatModel` from config
(`ChatGroq` today, `ChatBedrockConverse` for Bedrock). The Bedrock keys in
`.env` are **inert until AWS credentials exist** — nothing in the repo needs
them to run, test, or demo.

When AWS access lands, the swap is config-only:

1. **Model access first** — in the AWS console, request Bedrock model access
   for the chosen Nova tier *and* `amazon.titan-embed-text-v2:0` (approval can
   lag account access).
2. Make AWS credentials visible to the process (SSO profile or standard
   `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` env vars) with
   `bedrock:InvokeModel` permission.
3. In `.env`: set `LLM_PROVIDER=bedrock`, pick the tier in `BEDROCK_MODEL_ID`
   (e.g. `amazon.nova-pro-v1:0`), set `AWS_REGION`. Restart the server.
4. Re-run the eval baselines and compare against the committed Groq numbers
   (same harness, zero changes):
   `python sql_eval.py` (text-to-SQL) and `python eval_harness.py`
   (investigator agent). Per-call latency/tokens land in `llm_calls.jsonl`.
5. Prompt tuning per model family, if needed, goes in `prompts/` as
   `<name>.bedrock.txt` overrides — no pipeline code changes.

Notes:
- The in-UI model toggle (GPT OSS 120B/20B) selects among Groq-hosted models
  and is inert under Bedrock.
- Nova has no JSON response-format mode; `llm_client.chat(json_mode=True)`
  becomes a no-op on Bedrock (prompts already demand JSON and all callers
  parse defensively).
- Contract-document retrieval (Bedrock Knowledge Base) has its own runway:
  `infra/terraform/` (unapplied IaC), `doc_ingestion/` (parser findings), and
  `retrievers.ContractDocumentRetriever` (the code seam).

---

## Evaluation

- `python sql_eval.py` — golden-set text-to-SQL harness (36 labeled cases in
  `evals/sql_cases.yaml`): execution accuracy (row-level diff against
  reference SQL, not string match), retrieval precision/recall/MRR, latency,
  tokens, and cost estimates (`evals/model_prices.yaml`). Results land in
  `evals/results/` and MLflow (`text2sql-evals`).
  `--check-cases` validates the reference SQL without any LLM calls;
  `--with-examples` measures the production few-shot configuration.
- `python eval_harness.py` — Stage-3 investigator-agent harness (LLM-judged,
  MLflow experiment `investigator-evals`).

---

## License

MIT
