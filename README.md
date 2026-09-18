# Estate Command

**A GIS-first, AI-powered decision layer for an oil palm estate — and an FFB production forecast the estate can plan tomorrow's work from.**

Estate Command puts the estate's own geometry on the screen: 291 real block
polygons, coloured by any of thirty-four metrics, each badged with where its
number came from. Over that map sit 47 decision features across six
operational domains, an FFB (fresh fruit bunch) forecasting stack that runs
from the next twelve months down to tomorrow morning's gang assignment, and a
tool-calling AI layer that answers in the estate's own figures and moves the
map to match.

The project began as *AutoDashboard*, a natural-language interface to a
PostgreSQL plantation management (EPMS) database. Those text-to-SQL
surfaces — Dashboard, Report and Chat — are still here and still work
(§5), but the map and the forecast are the product now.

One rule holds everywhere in this app: **a figure never wears a badge it did
not earn.** Real client records, real free satellite and weather measurements,
and generated stand-ins are three different colours on every screen, and the
data-readiness register states, per capability, what exists today and what the
feature degrades to without it.

---

## 1. The map — a GIS decision layer

`/command` is a full-page MapLibre workspace over the estate's ArcGIS block
export. Six domain rails (Harvesting, Upkeep & Maintenance, Pest & Disease,
Transport, Commercial, Governance) open panels and windows against one shared
map; anything that names blocks carries a **Show on map** action that outlines
them on the full map and shrinks the window to a bar at the foot of the screen.

**47 features, 46 of them running** — most on generated feeds, each carrying
the precise list of data entities it needs to run on the client's real
numbers. `gis/features.py` is the manifest those requirements are generated
from, so the ask always sits beside the feature, and `GET /gis/data-request`
is the leave-behind: every unsatisfied requirement, by source system.

### Two layers that came from nobody's database

- **Real canopy vigour.** `gis/build_ndre.py` searches the free Copernicus
  archive for the least-cloudy Sentinel-2 scene over the estate
  (S2B_54MVT_20250215, 7.6% cloud), crops the red-edge and near-infrared bands
  to its bounding box through a public raster service, masks cloud with the
  scene's own classification band, and averages NDRE inside each block
  polygon. No GDAL, no rasterio, no credentials — numpy and one HTTP call.
  291 of 291 blocks measured at 20 m. It is the only layer on the page whose
  numbers are a real measurement the client does not already hold.
- **Terrain and rainfall.** `gis/build_terrain.py` and
  `gis/build_rainfall.py` do the same trick against public DEM and Open-Meteo
  history: real measurements over the client's own blocks, free, badged real,
  and struck off the data request as a result.

**Metric agreement** (`layers.compare_metrics`) computes the rank correlation
between any two metrics and returns the blocks in the bottom fifth of both.
Satellite vigour and recorded yield rank this estate almost independently
(rho 0.10), which is itself the finding: the thirteen blocks weak on both are
the corroborated ones.

### The operating rhythm

Five panels stop describing the estate and start running it. Harvesting,
pruning, weeding and spraying, pest control and transport each open in their
own window with a section menu grouped as **Tomorrow · Ledger · Why**, over
one row shape — the work order: what was planned, what was done, by whom, on
which block.

- **Ledger.** 143 days of orders across six operations, ending 2025-05-23
  where the client's export ends. Harvest actuals sum to the real block-month
  bunch counts to the bunch; the plan is backed out through an adherence model
  driven by real daily rainfall, road condition and crew attendance,
  calibrated to 78%. Upkeep and pest work are simulated crew by crew, day by
  day, so a partial order names the order it became.
- **Tomorrow.** The assignment for 2025-05-24, the first day beyond the
  client's data. `gis/models/scheduler.py` is greedy by deferral value per
  man-day with a swap-improvement pass, a contiguity bonus that produces
  "blocks 14 to 19" rather than a scattered list, a travel penalty, and the
  gap to a relaxed bound so the greedy choice is accountable. Every headcount
  is editable; a re-run reprices in under a second.
- **Why.** The demand ranking with its arithmetic, the objective's terms, the
  binding constraint, and what contiguity cost — measured by running the plan
  again without it.

### Stores

`gis/stores.py` answers what to order, when, and why, off six generated SAP MM
extracts (material master, suppliers, purchase orders, material documents,
reservations, closing stock). Four models sit under it: supplier lead time
learned from PO history rather than the quoted MARC-PLIFZ, consumption
modelled three ways (programme / seasonal / per-job) because average use is
right for nothing on this estate, a 2,000-draw reorder-point simulation, and a
replay that decides whether the new number is used at all. Every answer leads
with a sentence — *"Order by 31 July: 250 t"*, *"1 in 10 orders from Pupuk
Kaltim takes 66 days or more"* — and carries its figures beside it.

### Governance

Every rupiah and tonne rests on `gis/assumptions.py`, a register the client
can edit in the Governance panel. Six decision panels turn the map into a
recorded accept / reject / defer, and the four loop-closing columns on the
decision log (`due_date`, `expected_effect`, `order_ref`, `observed_effect`)
let the **Did-it-work** panel read an accepted plan back against the ledger
once its date has passed. A scheduled figure never wears a real badge, and no
model in this app can write to EPMS — both engines are opened read-only.

---

## 2. FFB forecasting

Three horizons, three honest methods. Each one is only as complex as its data
supports, and each says so.

### Estate production forecast — months ahead (`/forecast`)

An ensemble over ~40 months of cleaned monthly production, weather and
agronomic-practice data, projecting 3 / 6 / 12 months with a
conformal-calibrated uncertainty band.

| | |
|---|---|
| Served model | `Ensemble_workdone` — SARIMAX(1,1,1) + month sin/cos, Trailing3, and LightGBM with work-done features |
| Accuracy | **sMAPE 10.1%, MASE 0.572** against a Seasonal Naive benchmark, on 30 expanding-window 1-step-ahead folds |
| Bands | Conformal prediction, adaptive (ACI, alpha 0.1, gamma 0.08), prequentially evaluated |
| Target hygiene | Under-recorded months are seasonally imputed and flagged; sparse months and partial endpoints are excluded from train and score, not day-scaled — naive day-scaling caused ~41% of the previous build's backtest error |
| Coverage | Two estates modelled (K3, EC); the page states its real coverage rather than implying every estate is forecast |

On top of the numbers:

- **TreeSHAP driver panel** — per-forecast-month feature attributions
  (rainfall, pruning, fertiliser lag effects, seasonality) explaining *why*
  the model predicts what it does.
- **Production Risk Intelligence** — an LLM risk-synthesis pass over the ML
  forecast that pulls live weather / ENSO / fire-hotspot signals and returns a
  narrative risk level, **without altering the underlying numbers**.
- **Investigator agent** — a LangGraph ReAct agent that runs only when
  triggered (an actual falling outside the conformal band, or a divergence
  between the numeric forecast and the risk level), reasoning over live
  signals, the database and SHAP tools to explain the anomaly.
- **Model health** (`/model-health`) — accuracy, interval calibration, data
  quality and lifecycle for the served model, read from the cached forecast
  and the training artifact. It never refits to make itself look good.
- **Block Map** — the estate's 291 polygons with a per-block forecast overlay
  and drill-down. Deliberately *not* the estate ensemble run 291 times: each
  block has the same handful of monthly points the whole estate has, so the
  point forecast is a trailing mean of the block's own non-partial months —
  what a ~4-point series can actually support.

### Tomorrow's outlook — the operational forecast (`/command`)

Four fitted models feed tomorrow's plan, and every one says what it means in
plain words.

| Model | What it answers | Example |
|---|---|---|
| `models/rain.py` | Chance of rain heavy enough to wash off spraying, learned from 954 days of real Open-Meteo forecasts issued the evening before, set against the rain that fell | *"a 54% chance of rain heavy enough to wash off spraying"* — turns spray-or-hold into a cost decision |
| `models/headcount.py` | Who turns up, per crew, as a range | *"expect 574 to 616 of 668 people"* |
| `models/slippage.py` | Each block's expected share done, and which orders are likely to need another day | flags the ones that will slip |
| `models/rates.py` | Crew speeds and harvest block pace, learned against the book | who covers more or less ground than the standard |

Each is checked week by week on days it had not seen, against the method it
replaces; carries a one-word trust grade; has a switch in the assumption
register; and **falls back to the old method if it does not beat it**. Three
of the four learn from the generated ledger and say so — their checks show
they find what the generator planted and invent nothing it did not. The
answer-key columns in the generated feeds (`skill`, `delay_cause`) are
stripped before any model can read them.

Other estate models in the same family: `productivity.py` (adjusted daily
target from terrain, age, density), `shrinkage.py` (field-to-mill anomaly
detection, scored against planted answers), `clusters.py` (agronomic
underperformance grouping), `lagged_forecast.py` (yield against rainfall at
the agronomic lags).

---

## 3. The AI layer

Six features run on Amazon Nova through the same `llm_client` seam the rest of
the app uses — Nova Pro for reasoning, Nova Lite for short-form writing.

| Feature | What it does | Model |
|---|---|---|
| **Ask the map** | Tool-calling agent over **44 tools** spanning every layer, including ones that return plans computed server-side and the forecasts in plain words. *"G1-03 is four men short tomorrow, replan"* is a question it can answer. Answers in figures and moves the map to match. | Nova Pro |
| Duty officer brief | Turns a fire assessment into a posture, a priority order, and orders a person can act on | Nova Pro |
| Shift handover | Reads the decision log and the open positions and writes the note someone arriving cold needs | Nova Pro |
| Block brief | The agronomist's read on one block against its planting cohort and its satellite reading | Nova Lite |
| Artifact instructions | The words on a drafted work order, inspection order or requisition | Nova Lite |
| Readiness interview | Turns a red capability row into the questions worth asking, then scores the client's answer against the measurement | Nova Lite |

Three properties hold across all six:

- **The server computes, the model writes.** Every figure is calculated by
  `layers.py`, `ops.py`, `stores.py`, `fire.py` or `vegetation.py` and handed
  over with a provenance tag; the prompts forbid deriving anything new.
- **The guardrail is checked, not trusted.** `reasoning.audit_figures`
  re-reads every number and block label in the generated text and looks for it
  in the payload behind it. Anything missing is reported on the response and
  shown in the UI. The copilot gets one self-correction pass when its own
  audit fails; whatever survives is still labelled.
- **Nothing generated is load-bearing.** Each feature returns
  `available: false` with a reason when the model is unreachable, and the
  panel underneath renders regardless.

---

## 4. Fire and force majeure

`gis/fire.py` triages live FIRMS hotspots against the estate's own polygons:
an Open-Meteo-driven spread cone, per-block exposure, and a mobilisation
recommendation, with the real-versus-invented split carried in every line
(the hotspots and weather are real; the response assets are generated,
because EPMS records none).

---

## 5. The text-to-SQL surfaces

The original AutoDashboard pipeline, still mounted and still useful. Ask a
question in plain English against the EPMS PostgreSQL database and get a
dashboard, a report, or a conversational answer.

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

- **Dashboard** — *"show me revenue by division"* generates a full layout of
  3–12 widgets (KPI cards, bar charts, line charts, tables), each backed by
  its own SQL query, as structured JSON produced by the LLM.
- **Report** — preset SQL templates (zero LLM cost) or freeform generated
  reports with saved, date-parameterized query templates.
- **Chat** — conversational Q&A with per-estate fan-out across the two live
  estate databases (BA, K3). Returns the SQL, row count, tables used, and the
  answer.

Supporting machinery: hybrid dense + BM25 table retrieval, a one-shot
self-correction loop on SQL error, few-shot examples mined from successful
history, SQL safety validation (DDL, DML and multi-statement queries blocked;
read-only connection with a statement timeout), runtime model switching, and
hot-reload of schema metadata and glossary. Every prompt is grounded in
`business_rules.txt` and `glossary.yaml`.

---

## Tech stack

| Layer | Technology |
|---|---|
| Web framework | FastAPI + Uvicorn (single server, port 8001) |
| Map | MapLibre GL, ES modules built by Vite |
| Geospatial | ArcGIS block export → GeoJSON; Sentinel-2 L2A via STAC + COG range requests (numpy only, no GDAL/rasterio) |
| Estate forecasting | SARIMAX + LightGBM + Trailing3 ensemble, conformal prediction (ACI), TreeSHAP |
| Operational models | Ridge / logistic fits with backtests, trust grades and fallbacks (`gis/models/learn.py`) |
| Scheduling | Greedy value-density assignment with swap improvement and a relaxed bound |
| LLM inference | Provider-agnostic LangChain layer (`llm_client.py`): Groq today (GPT OSS 120B/20B, Llama 3.1 8B, Qwen3 32B); Amazon Bedrock (Nova Pro / Nova Lite) ready via config |
| Agents | LangGraph (forecast investigator), tool-calling copilot (`gis/copilot.py`) |
| Vector store / retrieval | ChromaDB + fastembed (BAAI/bge-small-en-v1.5), BM25 (rank-bm25) |
| Database | PostgreSQL via SQLAlchemy (read-only) |
| Decision log / assumptions | SQLite — never EPMS |
| Public feeds | Copernicus / Sentinel-2, Open-Meteo (history, forecast, previous runs), NASA FIRMS, NASA POWER |
| Experiment tracking | MLflow |

---

## Project structure

```
├── dashboard_server.py     # Main entry point (port 8001) — mounts every app
│
├── gis_router.py           # Estate Command endpoints: map, panels, ops, stores, AI
├── gis/
│   ├── features.py         # The feature manifest: 47 features, 6 domains, and what each needs
│   ├── ontology.py         # Estate/division/block geometry from the ArcGIS export
│   ├── layers.py           # Derived per-block metrics, panels, metric agreement
│   ├── vegetation.py       # Real Sentinel-2 NDRE per block (read side)
│   ├── build_ndre.py       # Offline: STAC search + COG crop + per-polygon NDRE
│   ├── environment.py      # Real terrain and rainfall (read side)
│   ├── fire.py             # Hotspot triage: FIRMS + Open-Meteo + spread screen
│   ├── ops.py              # The work-order ledger: adherence, capacity, demand, outcomes
│   ├── forecasts.py        # Tomorrow's outlook: four forecasts in plain words, with grades
│   ├── stores.py           # What to order, when, and why
│   ├── assumptions.py      # The register every plan is priced from
│   ├── decisions.py        # Decision log and drafted artifacts (SQLite, never EPMS)
│   ├── readiness.py        # Capability register and the interview store
│   ├── reasoning.py        # Shared LLM plumbing: JSON repair, TTL cache, figure audit
│   ├── briefings.py        # Block / fire / handover / artifact / interview narratives
│   ├── copilot.py          # Ask the map: tool-calling agent over 44 tools
│   ├── loose_fruit.py      # Loose fruit recovery — real, from the client's own counts
│   ├── cutting_interval.py # Realised round, measured visit to visit
│   ├── herbicide.py        # Spray-round adherence: MM issues joined to spray orders
│   ├── ffa.py              # Free fatty acid against dispatch delay
│   ├── abw_trend.py        # Average bunch weight trend off the weighbridge ledger
│   ├── pest_warning.py     # Canopy anomaly against census — the early-warning list
│   ├── collection.py       # Collection point coverage and scheduling
│   ├── models/
│   │   ├── scheduler.py    # Tomorrow's assignment: greedy value density, swap pass, bound
│   │   ├── learn.py        # Shared kit: fits, scoring, grades, answer-key stripping
│   │   ├── rain.py         # Wash-off, heavy and stopping rain from real day-ahead forecasts
│   │   ├── headcount.py    # Who turns up per crew, as a range
│   │   ├── slippage.py     # How much of each order gets done, and what may slip
│   │   ├── rates.py        # Crew speeds and harvest pace, learned against the book
│   │   ├── productivity.py # Adjusted daily target from terrain, age, density
│   │   ├── shrinkage.py    # Field-to-mill anomaly detection
│   │   ├── clusters.py     # Agronomic underperformance grouping
│   │   ├── lagged_forecast.py # Yield against rainfall at the agronomic lags
│   │   ├── mm.py           # The SAP MM extracts, read once, answer key stripped
│   │   ├── leadtime.py     # What a supplier really takes, from PO history
│   │   ├── consumption.py  # Use between now and delivery: programme / seasonal / jobs
│   │   └── safety_stock.py # Reorder points by simulation, and the replay that gates them
│   └── build_*.py          # Offline generators: synthetic feeds, operations, materials,
│                           #   rainfall, day-ahead rain forecasts, terrain, footprints
│
├── command_static/         # Estate Command frontend (Vite + ES modules)
│   ├── src/
│   │   ├── main.js         # Boot and wiring
│   │   ├── map/            # MapLibre instance, choropleth, pins, fire, hover, highlight
│   │   ├── shell/          # Context bar, domain rail, workbench dock, feature window, tray
│   │   ├── panels/         # One module per domain plus per-feature panels; the registry
│   │   ├── state/          # The store and the working set of blocks
│   │   └── ai/             # Ask the map, briefs, the readiness interview
│   ├── test/               # undefined.mjs (AST lint), smoke, ops, models, stores,
│   │                       #   selection, compare, blocks, and one per feature
│   └── dist/               # Built bundle, what /command serves (gitignored)
│
├── forecast_router.py      # Forecast endpoints: data, model health, blocks, intelligence
├── forecast/
│   ├── monthly_model.py    # SARIMAX + Trailing3 + LightGBM ensemble, walk-forward backtest
│   ├── conformal.py        # Conformal / ACI uncertainty bands
│   ├── blocks.py           # Block-polygon GeoJSON for the Block Map
│   ├── build_block_forecast.py # Per-block trailing-mean forecast
│   ├── serving.py          # Monthly + daily/weekly serving layer
│   ├── monthly_model.joblib, EC/, experiments/
├── forecast_intelligence.py # LLM risk synthesis over live weather/ENSO/fire signals
├── forecast_investigator.py # LangGraph ReAct agent for anomaly investigation
│
├── chat_router.py          # Chat: RAG → SQL → conversational answer
├── report_router.py        # Reports: preset and generated
├── retrieval.py            # Hybrid ChromaDB + BM25 retrieval
├── prompt_builder.py       # Prompt assembly and SQL extraction
├── metadata_loader.py      # Schema catalog loader (schema_metadata.json)
├── sql_validator.py        # SQL safety checks
├── layout_prompt.py / layout_schema.py  # Dashboard layout prompt and widget schema
├── query_history.py        # Few-shot example storage
│
├── llm_client.py           # Provider-agnostic LangChain chat layer (Groq/Bedrock) + call log
├── prompts/                # Externalized prompt templates (per-provider overrides)
├── retrievers.py           # LangChain BaseRetriever seam (schema now, Bedrock KB later)
├── guardrails.py           # No-op guardrails seam (future Bedrock Guardrails call site)
├── tool_schemas.py         # Provider-neutral tool schemas + OpenAI/Bedrock translators
├── config.py               # Env-var config and the per-estate SQLAlchemy engines
│
├── sql_eval.py             # Golden-set text-to-SQL eval harness
├── eval_harness.py         # Investigator-agent eval harness
├── infra/terraform/        # Bedrock KB IaC skeletons — NOT YET APPLIED
├── doc_ingestion/          # PDF/DOCX parsing dry run for the contracts use case
│
├── dashboard_static/ report_static/ chat_static/ forecast_static/   # the four single-file UIs
├── business_rules.txt      # Domain rules and query constraints for the LLM
├── glossary.yaml           # Domain term → table/column mappings
├── schema_metadata.json    # Table descriptions and embeddings
└── requirements.txt
```

---

## Getting started

### 1. Clone and install

```bash
git clone https://github.com/KimiPatria/Text-to-SQL-Chatbot.git
cd Text-to-SQL-Chatbot
python -m venv .venv
.venv\Scripts\activate       # Windows
# source .venv/bin/activate  # macOS / Linux
pip install -r requirements.txt
```

### 2. Configure environment

Copy `.env.example` to `.env` and fill it in. The minimum:

```env
GROQ_API_KEY=your_groq_api_key
DATABASE_URL=postgresql://username:password@host:5432/k3_db
DATABASE_URL_BA=postgresql://username:password@host:5432/ba_db
DB_SCHEMA=public
FIRMS_MAP_KEY=your_firms_key     # live fire hotspots; the panel degrades without it
```

Get a free Groq API key at [console.groq.com](https://console.groq.com) and a
free FIRMS map key at [firms.modaps.eosdis.nasa.gov](https://firms.modaps.eosdis.nasa.gov/api/).

### 3. Set up domain context files

Edit `business_rules.txt` to describe query constraints, and `glossary.yaml`
to map domain terms to table/column names. The LLM uses both as grounding
context in every text-to-SQL prompt.

### 4. Bootstrap schema metadata

Run once to generate `schema_metadata.json` — natural-language descriptions
for every table in your database:

```bash
# Requires GEMINI_API_KEY in .env
python bootstrap_metadata.py
```

### 5. Build the Estate Command frontend

Estate Command is an ES module tree built by Vite. The four single-file
interfaces need no build.

```bash
cd command_static
npm install
npm run build          # writes command_static/dist, which /command serves
cd ..
```

`/command` falls back to the pre-build single file when `dist/` is absent, so
a missing build degrades rather than serving a blank page. Rebuild after any
change under `command_static/src`.

For frontend work, `npx vite --port 5173` serves `src` with hot reload at
`http://localhost:5173/command-static/` and proxies the API to 8001, so both
processes run side by side.

`npm run build` runs a gate before bundling: `test/undefined.mjs` walks each
module's AST and reports any identifier that is used but bound nowhere and is
not a browser global — Rollup treats such a name as a global the browser will
supply, so a forgotten import builds perfectly and throws the moment the line
runs. Then Vite resolves every import and fails on a missing export.

### 6. Run

```bash
uvicorn dashboard_server:app --reload --port 8001
```

| Interface | URL |
|---|---|
| **Estate Command** | [http://localhost:8001/command](http://localhost:8001/command) |
| **Forecast** | [http://localhost:8001/forecast](http://localhost:8001/forecast) |
| Dashboard | [http://localhost:8001](http://localhost:8001) |
| Report | [http://localhost:8001/report](http://localhost:8001/report) |
| Chat | [http://localhost:8001/chat](http://localhost:8001/chat) |

The operational models and the stores replay fit in background threads when
the server starts, so the first outlook, plan or stores call after a restart
can take half a minute. Wait for `GET /gis/forecast/accuracy` to return 200
before running the frontend tests.

### 7. Regenerate the feeds (optional)

```bash
python gis/build_synthetic.py       # all demand-side feeds, then build_operations.py
python gis/build_materials.py       # the six SAP MM feeds, on their own seed
python gis/build_rainfall.py        # daily rainfall the ledger reads
python gis/build_rain_forecast.py   # what the forecast said the evening before, daily
python gis/build_ndre.py            # the real Sentinel-2 canopy layer
```

### 8. Tests

`npm test` needs the server running. It loads the real page in Chrome, walks
every panel in every domain, builds a selection, drafts a document, and fails
on any console error. Then:

```bash
node test/ops.mjs      # drives the operation windows: edit, re-run, show on map, draft, filter
node test/models.mjs   # the four forecasts: grades, planted-truth checks, switches, replay
node test/stores.mjs   # the stores models and window
```

`test/models.mjs` checks that each model beats the method it replaces, passes
its planted-truth checks, that the generator's answer key never reaches a
payload, that switching a forecast off puts the plan back on the old method,
and that a replayed spray day decides on the forecast rather than the rain
that fell.

---

## API endpoints

### Estate Command — map and features

| Method | Path | Description |
|---|---|---|
| `GET` | `/command` | Estate Command UI |
| `GET` | `/gis/features` | The feature manifest, grouped by domain, with coverage |
| `GET` | `/gis/features/panel` | One feature's panel payload and its requirements |
| `GET` | `/gis/data-request` | The leave-behind: every unsatisfied requirement, by source system |
| `GET` | `/gis/estates` · `/gis/estates.geojson` | Estate index: geometry provenance, block count, harvest window |
| `GET` | `/gis/blocks` · `/gis/divisions` | Block and division polygons for one estate |
| `GET` | `/gis/blocks/table` | Merged per-block values: real attributes plus derived |
| `GET` | `/gis/metrics` | Per-block values for one metric, optionally one month |
| `GET` | `/gis/metrics/catalogue` | All 34 map metrics with provenance (real / derived / synthetic) |
| `GET` | `/gis/metrics/compare` | Rank correlation between two metrics, plus the blocks weak on both |
| `GET` | `/gis/vegetation` | Sentinel-2 canopy scene behind the NDRE layer, and its coverage |
| `GET` | `/gis/terrain` · `/gis/rainfall` | The two other real public feeds |
| `GET` | `/gis/readiness` | Per-capability data readiness, with measured evidence |

### Harvesting, upkeep, pest, transport

| Method | Path | Description |
|---|---|---|
| `GET` | `/gis/rotation` | Blocks due for harvest, by ripeness pressure |
| `GET` | `/gis/cutting-interval` | Realised round per block, measured visit to visit (real) |
| `GET` | `/gis/loose-fruit` | Loose fruit recovery per block-month, from the client's own counts (real) |
| `GET` | `/gis/abw-trend` | Average bunch weight movement per block |
| `GET` | `/gis/labour` | Harvester supply against demand |
| `GET` | `/gis/productivity` | Adjusted daily target per block |
| `GET` | `/gis/herbicide` | Spray-round adherence and application rate |
| `GET` | `/gis/nutrition` · `/gis/roads` · `/gis/clusters` | Nutrient gap, road backlog, underperformance clusters |
| `GET` | `/gis/replant` | Replant schedule from real planting years |
| `GET` | `/gis/pest` · `/gis/pest-warning` | Census, and canopy anomaly against census |
| `GET` | `/gis/transport` · `/gis/shrinkage` | Trip ledger; field-to-mill anomaly detection |
| `GET` | `/gis/ffa` | Free fatty acid against dispatch delay |
| `GET` | `/gis/collection` | Collection point coverage and scheduling |

### Operations, forecasts, stores

| Method | Path | Description |
|---|---|---|
| `GET` | `/gis/ops` | Every operation's headline: orders, adherence, carried forward |
| `GET` | `/gis/ops/{operation}/ledger` | The work-order ledger, filterable; adherence by week, crew, driver |
| `GET` | `/gis/ops/{operation}/adherence` | Planned against actual by crew, block, division, month |
| `GET` | `/gis/ops/{operation}/demand` | What is due on a date, how urgent, what deferring it costs |
| `GET` | `/gis/ops/{operation}/plan` | Tomorrow's assignment, every figure pre-computed |
| `POST` | `/gis/ops/{operation}/plan` | Re-run with edited constraints: headcount, a crew out, rain, a block held |
| `GET` | `/gis/ops/{operation}/deferral` | What waiting costs on one block |
| `GET` | `/gis/ops/capacity` · `/gis/ops/outcomes` | Who is expected; accepted plans read back against the ledger |
| `GET` | `/gis/forecast/outlook` | Tomorrow's outlook: all four forecasts on one screen |
| `GET` | `/gis/forecast/rain` · `/headcount` · `/work-done` · `/speeds` | Each forecast on its own |
| `GET` | `/gis/forecast/accuracy` | Every model's backtest, grade, and the method it beat |
| `GET` | `/gis/forecast/lagged` | Yield against rainfall at the agronomic lags |
| `GET` | `/gis/stores` | What to order, when, and why |
| `GET` | `/gis/stores/material` · `/document` | One material's detail; a drafted requisition |
| `GET` | `/gis/assumptions` | The register: value, unit, source, what uses it |
| `POST` / `DELETE` | `/gis/assumptions` | Set one value / back to the defaults |

### Fire, decisions, AI

| Method | Path | Description |
|---|---|---|
| `GET` | `/gis/fire` · `/fire/assets` · `/fire/scenarios` | Live hotspots, spread cone, exposure, mobilisation |
| `POST` / `GET` | `/gis/decisions` | Log an accept/reject/defer and draft its artifact; the audit view |
| `GET` | `/gis/artifact-kinds` | The documents a selection can be turned into |
| `POST` | `/gis/ask` | **Ask the map** — tool-calling agent over 44 tools, with trace and figure audit |
| `GET` | `/gis/ask/examples` | Seeded questions for the empty state |
| `GET` | `/gis/blocks/brief` · `/gis/fire/brief` · `/gis/handover` | The three generated briefs |
| `GET` / `POST` | `/gis/readiness/interview` | The questions worth asking; scoring the client's answer |
| `GET` | `/gis/ai/status` | Which model serves each generated feature |

### Forecasting and text-to-SQL

| Method | Path | Description |
|---|---|---|
| `GET` | `/forecast` | Forecast UI |
| `GET` | `/forecast/data` | Historical + 3/6/12-month production forecast with uncertainty band |
| `GET` | `/forecast/model-health` | Served-model accuracy, interval calibration, data quality, lifecycle |
| `GET` | `/forecast/blocks` | Block-polygon GeoJSON for the Block Map |
| `GET` | `/forecast/block-forecast` | Per-block trailing-mean forecast overlay |
| `GET` | `/forecast/intelligence` | LLM risk-synthesis narrative over the ML forecast |
| `GET` | `/forecast/investigate` | Investigator agent output (runs only when triggered, or `?force=1`) |
| `GET` / `POST` | `/` · `/api/generate` | Dashboard UI; generate a dashboard layout |
| `GET` / `POST` | `/report` · `/report/generate` | Report UI; generate a freeform report |
| `GET` / `POST` | `/chat` · `/chat/message` | Chat UI; send a chat message |
| `GET` | `/health` | Health check |
| `POST` | `/admin/reload-schema` · `/admin/reload-glossary` | Hot-reload schema metadata / business rules |

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `GROQ_API_KEY` | — | Groq API key (required while `LLM_PROVIDER=groq`) |
| `DATABASE_URL` | — | PostgreSQL connection string for estate K3 (required) |
| `DATABASE_URL_BA` | — | PostgreSQL connection string for estate BA (required) |
| `DB_SCHEMA` | `public` | PostgreSQL schema to introspect |
| `FIRMS_MAP_KEY` | — | NASA FIRMS key for live fire hotspots |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Main SQL-generation model |
| `MAX_RESULT_ROWS` | `100` | Row cap injected into every query |
| `STATEMENT_TIMEOUT_MS` | `15000` | PostgreSQL statement timeout (ms) |
| `LLM_PROVIDER` | `groq` | Infrastructure provider: `groq` or `bedrock` |
| `BEDROCK_MODEL_ID` | `amazon.nova-pro-v1:0` | Main model when `LLM_PROVIDER=bedrock` |
| `BEDROCK_ROUTING_MODEL_ID` | `amazon.nova-lite-v1:0` | Cheap routing/short-form model under Bedrock |
| `AWS_REGION` | `us-east-1` | Bedrock region |
| `LLM_LOG_PATH` | `./llm_calls.jsonl` | Structured per-LLM-call log (task, provider, model, tokens, latency) |

---

## Switching LLM providers (Groq → Amazon Bedrock)

The pipeline never talks to a provider SDK directly: every call goes through
`llm_client.py`, which builds a LangChain `BaseChatModel` from config
(`ChatGroq` today, `ChatBedrockConverse` for Bedrock). The Bedrock keys in
`.env` are **inert until AWS credentials exist** — nothing in the repo needs
them to run, test or demo.

When AWS access lands, the swap is config-only:

1. **Model access first** — in the AWS console, request Bedrock model access
   for the chosen Nova tiers *and* `amazon.titan-embed-text-v2:0` (approval
   can lag account access).
2. Make AWS credentials visible to the process (SSO profile or standard
   `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` env vars) with
   `bedrock:InvokeModel` permission.
3. In `.env`: set `LLM_PROVIDER=bedrock`, pick the tier in
   `BEDROCK_MODEL_ID`, set `AWS_REGION`. Restart the server.
4. Re-run the eval baselines and compare against the committed Groq numbers
   (same harness, zero changes): `python sql_eval.py` and
   `python eval_harness.py`. Per-call latency/tokens land in
   `llm_calls.jsonl`.
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
  tokens and cost estimates (`evals/model_prices.yaml`). Results land in
  `evals/results/` and MLflow (`text2sql-evals`). `--check-cases` validates
  the reference SQL without any LLM calls; `--with-examples` measures the
  production few-shot configuration.
- `python eval_harness.py` — investigator-agent harness (LLM-judged, MLflow
  experiment `investigator-evals`).
- `python forecast/monthly_model.py` — the walk-forward backtest behind the
  scoreboard in `forecast/monthly_results.txt`.
- `GET /gis/forecast/accuracy` — every operational model's backtest, its trust
  grade, and the method it has to beat to be used at all.

---

## License

MIT
