# EPMS AI

This repository holds two products that share one server, one set of estate
data and one rule: **a figure never wears a badge it did not earn.** Real client
records, real free satellite and weather measurements, and generated stand-ins
are three different things on every screen, and each says which it is.

| | What it is | Where |
|---|---|---|
| **[Estate Command](#estate-command)** | A GIS-first decision layer over the estate's own block polygons: where money leaks, what to do tomorrow, what to order, what is burning. | `/command` |
| **[The Chatbot](#the-chatbot)** | Ask the EPMS data in plain English and get a table, a chart and an answer; read the FFB production forecast; generate reports. Chat, Forecast and Report are one family of pages. | `/` (opens on `/chat`), `/forecast`, `/report` |

The app started as *AutoDashboard*, a natural-language interface to a
PostgreSQL plantation management (EPMS) database. That text-to-SQL pipeline is
the chatbot's engine; Estate Command was built on top of the same estate data.
Both run from one FastAPI server on port 8001 and read the same two data
sources: the per-estate **live** databases and the central **EPMS staging**
SQL Server database.

[Setup and reference](#setup-and-reference) at the end covers install,
configuration, running, tests and the LLM provider switch for both products.

---

# Estate Command

**A GIS-first, AI-powered decision layer for an oil palm estate.**

Estate Command puts the estate's own geometry on the screen: 291 real block
polygons, coloured by any of thirty-four metrics, each badged with where its
number came from. Over that map sit **50 decision features across seven
domains**, and a tool-calling AI layer that answers in the estate's own figures
and moves the map to match.

`/command` is a full-page MapLibre workspace over the estate's ArcGIS block
export. Seven domain rails open panels and windows against one shared map:
**Cost efficiency**, Harvesting, Upkeep, Pest & Disease, Transport, Commercial
and Governance. Anything that names blocks carries a **Show on map** action
that outlines them on the full map and shrinks the window to a bar at the foot
of the screen.

49 of the 50 features are running, most on generated feeds, each carrying the
precise list of data entities it needs to run on the client's real numbers.
`gis/features.py` is the manifest those requirements are generated from, so the
ask always sits beside the feature, and `GET /gis/data-request` is the
leave-behind: every unsatisfied requirement, by source system. At the moment 8
features run on data the client already holds and the rest wait on 42 extracts.

## Where the estate loses money (Cost efficiency)

The client's review asked for one thing above all: a system that says where
they are losing money and how to stop it. **Cost efficiency** is the first
domain on the rail, with four entries: *Where money leaks*, *Efficiency map*,
*Budget vs actual* and *Block margin*.

- The estate is broken into cost nodes (block, vehicle, road segment, crew,
  supplier), and every node gets an efficiency score and a rupiah leakage.
  The page opens on the ten places money is being lost, ranked by IDR, and
  what to do about each.
- Costs are posted from synthetic SAP FI/CO records generated from the work in
  the ledger, so every line names its source document. The records are a
  stand-in for the client's real postings and badged as such.
- A diagnosis step (`gis/diagnosis.py`, `gis/finance_playbook.py`) turns a
  leak into a cause and a drafted action, and a closed action is read back
  against the ledger (`/gis/finance/outcomes`). Eleven endpoints sit under
  `/gis/finance/*`.
- Built for one estate (EC) on synthetic records; see `buildplan_finance.md`.

## Two layers that came from nobody's database

- **Real canopy vigour.** `gis/build_ndre.py` searches the free Copernicus
  archive for the least-cloudy Sentinel-2 scene over the estate
  (S2B_54MVT_20250215, 7.6% cloud), crops the red-edge and near-infrared bands
  to its bounding box through a public raster service, masks cloud with the
  scene's own classification band, and averages NDRE inside each block
  polygon. No GDAL, no rasterio, no credentials: numpy and one HTTP call.
  291 of 291 blocks measured at 20 m. It is the only layer on the page whose
  numbers are a real measurement the client does not already hold.
- **Terrain and rainfall.** `gis/build_terrain.py` and `gis/build_rainfall.py`
  do the same trick against public DEM and Open-Meteo history: real
  measurements over the client's own blocks, free, badged real, and struck off
  the data request as a result.

**Metric agreement** (`layers.compare_metrics`) computes the rank correlation
between any two metrics and returns the blocks in the bottom fifth of both.
Satellite vigour and recorded yield rank this estate almost independently
(rho 0.10), which is itself the finding: the thirteen blocks weak on both are
the corroborated ones.

## The operating rhythm

Five panels stop describing the estate and start running it. Harvesting,
pruning, weeding and spraying, pest control and transport each open in their
own window with a section menu grouped as **Tomorrow, Ledger, Why**, over one
row shape: the work order. What was planned, what was done, by whom, on which
block.

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
  binding constraint, and what contiguity cost, measured by running the plan
  again without it.

## Tomorrow's outlook: four fitted models

Four models feed tomorrow's plan, and every one says what it means in plain
words.

| Model | What it answers | Example |
|---|---|---|
| `models/rain.py` | Chance of rain heavy enough to wash off spraying, learned from 954 days of real Open-Meteo forecasts issued the evening before, set against the rain that fell | *"a 54% chance of rain heavy enough to wash off spraying"* |
| `models/headcount.py` | Who turns up, per crew, as a range | *"expect 574 to 616 of 668 people"* |
| `models/slippage.py` | Each block's expected share done, and which orders are likely to need another day | flags the ones that will slip |
| `models/rates.py` | Crew speeds and harvest block pace, learned against the book | who covers more or less ground than the standard |

Each is checked week by week on days it had not seen, against the method it
replaces; carries a one-word trust grade; has a switch in the assumption
register; and **falls back to the old method if it does not beat it**. Three of
the four learn from the generated ledger and say so: their checks show they
find what the generator planted and invent nothing it did not. The answer-key
columns in the generated feeds (`skill`, `delay_cause`) are stripped before any
model can read them.

Other estate models in the same family: `productivity.py` (adjusted daily
target from terrain, age, density), `shrinkage.py` (field-to-mill anomaly
detection, scored against planted answers), `clusters.py` (agronomic
underperformance grouping), `lagged_forecast.py` (yield against rainfall at the
agronomic lags).

## Stores

`gis/stores.py` answers what to order, when, and why, off six generated SAP MM
extracts (material master, suppliers, purchase orders, material documents,
reservations, closing stock). Four models sit under it: supplier lead time
learned from PO history rather than the quoted MARC-PLIFZ, consumption modelled
three ways (programme / seasonal / per-job) because average use is right for
nothing on this estate, a 2,000-draw reorder-point simulation, and a replay
that decides whether the new number is used at all. Every answer leads with a
sentence (*"Order by 31 July: 250 t"*, *"1 in 10 orders from Pupuk Kaltim takes
66 days or more"*) and carries its figures beside it.

## Fire and force majeure

`gis/fire.py` triages live NASA FIRMS hotspots against the estate's own
polygons: an Open-Meteo-driven spread cone, per-block exposure, and a
mobilisation recommendation, with the real-versus-invented split carried in
every line. The hotspots and weather are real; the response assets are
generated, because EPMS records none.

## Governance

Every rupiah and tonne rests on `gis/assumptions.py`, a register the client can
edit in the Governance panel. Decision panels turn the map into a recorded
accept / reject / defer, and the four loop-closing columns on the decision log
(`due_date`, `expected_effect`, `order_ref`, `observed_effect`) let the
**Did-it-work** panel read an accepted plan back against the ledger once its
date has passed. A scheduled figure never wears a real badge, and no model in
this app can write to EPMS: both engines are opened read-only.

## The AI layer

Generated features run on Amazon Nova through the same `llm_client` seam the
rest of the app uses: Nova Pro for reasoning, Nova Lite for short-form writing.

| Feature | What it does | Model |
|---|---|---|
| **Ask the map** | Tool-calling agent over **48 tools** spanning every layer, including ones that return plans computed server-side and the leak diagnosis. *"G1-03 is four men short tomorrow, replan"* is a question it can answer. Answers in figures and moves the map to match. | Nova Pro |
| Duty officer brief | Turns a fire assessment into a posture, a priority order, and orders a person can act on | Nova Pro |
| Shift handover | Reads the decision log and the open positions and writes the note someone arriving cold needs | Nova Pro |
| Leak diagnosis | Explains a cost leak in terms of its causes and proposes the action | Nova Pro |
| Block brief | The agronomist's read on one block against its planting cohort and its satellite reading | Nova Lite |
| Artifact instructions | The words on a drafted work order, inspection order or requisition | Nova Lite |
| Readiness interview | Turns a red capability row into the questions worth asking, then scores the client's answer against the measurement | Nova Lite |

Three properties hold across all of them:

- **The server computes, the model writes.** Every figure is calculated by
  `layers.py`, `ops.py`, `stores.py`, `finance.py`, `fire.py` or
  `vegetation.py` and handed over with a provenance tag; the prompts forbid
  deriving anything new.
- **The guardrail is checked, not trusted.** `reasoning.audit_figures`
  re-reads every number and block label in the generated text and looks for it
  in the payload behind it. Anything missing is reported on the response and
  shown in the UI. The copilot gets one self-correction pass when its own audit
  fails; whatever survives is still labelled.
- **Nothing generated is load-bearing.** Each feature returns
  `available: false` with a reason when the model is unreachable, and the panel
  underneath renders regardless.

## Estate Command: structure

```
├── gis_router.py           # Estate Command endpoints: map, panels, ops, stores, finance, AI
├── gis/
│   ├── features.py         # The feature manifest: 50 features, 7 domains, and what each needs
│   ├── ontology.py         # Estate/division/block geometry from the ArcGIS export
│   ├── layers.py           # Derived per-block metrics, panels, metric agreement
│   ├── vegetation.py       # Real Sentinel-2 NDRE per block (read side)
│   ├── build_ndre.py       # Offline: STAC search + COG crop + per-polygon NDRE
│   ├── environment.py      # Real terrain and rainfall (read side)
│   ├── fire.py             # Hotspot triage: FIRMS + Open-Meteo + spread screen
│   ├── ops.py              # The work-order ledger: adherence, capacity, demand, outcomes
│   ├── forecasts.py        # Tomorrow's outlook: four forecasts in plain words, with grades
│   ├── stores.py           # What to order, when, and why
│   ├── finance.py          # Cost nodes, leakage in IDR, budget vs actual, block margin
│   ├── diagnosis.py, finance_playbook.py  # Leak causes and the actions that stop them
│   ├── assumptions.py      # The register every plan is priced from
│   ├── decisions.py        # Decision log and drafted artifacts (SQLite, never EPMS)
│   ├── readiness.py        # Capability register and the interview store
│   ├── reasoning.py        # Shared LLM plumbing: JSON repair, TTL cache, figure audit
│   ├── briefings.py        # Block / fire / handover / artifact / interview narratives
│   ├── copilot.py          # Ask the map: tool-calling agent over 48 tools
│   ├── loose_fruit.py, cutting_interval.py, herbicide.py, ffa.py, abw_trend.py,
│   │   pest_warning.py, collection.py   # Per-feature read models
│   ├── models/             # scheduler, learn (shared kit), rain, headcount, slippage, rates,
│   │                       #   productivity, shrinkage, clusters, lagged_forecast,
│   │                       #   mm, leadtime, consumption, safety_stock
│   └── build_*.py          # Offline generators: synthetic feeds, operations, materials, finance,
│                           #   rainfall, day-ahead rain forecasts, terrain, estate snapshots
│
└── command_static/         # Estate Command frontend (Vite + ES modules)
    ├── src/
    │   ├── main.js         # Boot and wiring
    │   ├── map/            # MapLibre instance, choropleth, pins, fire, hover, highlight
    │   ├── shell/          # Context bar, domain rail, workbench dock, feature window, tray
    │   ├── panels/         # One module per domain plus per-feature panels; the registry
    │   ├── state/          # The store and the working set of blocks
    │   └── ai/             # Ask the map, briefs, the readiness interview
    ├── test/               # undefined.mjs (AST lint), smoke, ops, models, stores, finance,
    │                       #   selection, compare, blocks, ask, and one per feature
    └── dist/               # Built bundle, what /command serves (gitignored)
```

Estate snapshots (block polygons and harvest per estate EC, EA, EB) are cut
from the restored estate databases by `gis/build_estate_data.py` into
`gis/data/estates/<CODE>/`.

## Estate Command: API

| Method | Path | Description |
|---|---|---|
| `GET` | `/command` | Estate Command UI |
| `GET` | `/gis/features` · `/gis/features/panel` | The feature manifest, grouped by domain, with coverage; one feature's panel payload and requirements |
| `GET` | `/gis/data-request` | The leave-behind: every unsatisfied requirement, by source system |
| `GET` | `/gis/estates` · `/gis/estates.geojson` | Estate index: geometry provenance, block count, harvest window |
| `GET` | `/gis/blocks` · `/gis/divisions` · `/gis/blocks/table` | Block and division polygons for one estate; merged per-block values |
| `GET` | `/gis/metrics` · `/gis/metrics/catalogue` · `/gis/metrics/compare` | One metric per block; all 34 with provenance; rank correlation between two |
| `GET` | `/gis/vegetation` · `/gis/terrain` · `/gis/rainfall` | The three real public feeds |
| `GET` | `/gis/readiness` | Per-capability data readiness, with measured evidence |
| `GET` | `/gis/rotation` · `/cutting-interval` · `/loose-fruit` · `/abw-trend` · `/labour` · `/productivity` | Harvesting read models |
| `GET` | `/gis/herbicide` · `/nutrition` · `/roads` · `/clusters` · `/replant` | Upkeep read models |
| `GET` | `/gis/pest` · `/pest-warning` · `/transport` · `/shrinkage` · `/ffa` · `/collection` | Pest, transport and commercial read models |
| `GET` | `/gis/ops` · `/gis/ops/{operation}/ledger` · `/adherence` · `/demand` · `/deferral` | Orders, adherence, what is due and what waiting costs |
| `GET` / `POST` | `/gis/ops/{operation}/plan` | Tomorrow's assignment; re-run with edited constraints |
| `GET` | `/gis/ops/capacity` · `/gis/ops/outcomes` | Who is expected; accepted plans read back against the ledger |
| `GET` | `/gis/forecast/outlook` · `/rain` · `/headcount` · `/work-done` · `/speeds` · `/accuracy` · `/lagged` | Tomorrow's operational forecasts, each model's backtest and grade |
| `GET` | `/gis/stores` · `/stores/material` · `/stores/document` | What to order, when, and why; one material; a drafted requisition |
| `GET` | `/gis/finance/summary` · `/nodes` · `/node` · `/diagnosis` · `/checks` · `/ledger` · `/budget` · `/accuracy` · `/document` · `/outcomes` | Cost efficiency |
| `POST` | `/gis/finance/actions/close` | Close a leak action so it can be read back |
| `GET` / `POST` / `DELETE` | `/gis/assumptions` | The register: read, set one value, back to defaults |
| `GET` | `/gis/fire` · `/fire/assets` · `/fire/scenarios` | Live hotspots, spread cone, exposure, mobilisation |
| `POST` / `GET` | `/gis/decisions` · `/gis/artifact-kinds` | Log an accept/reject/defer and draft its artifact; the audit view |
| `POST` | `/gis/ask` | **Ask the map**: tool-calling agent over 48 tools, with trace and figure audit |
| `GET` | `/gis/blocks/brief` · `/gis/fire/brief` · `/gis/handover` · `/gis/ai/status` | The generated briefs; which model serves each |
| `GET` / `POST` | `/gis/readiness/interview` | The questions worth asking; scoring the client's answer |

---

# The Chatbot

**Ask the plantation data in plain English, see the forecast, generate a
report.** The chatbot is three pages in one family: **Chat** (`/chat`), the
**Forecast** pages (`/forecast` and its analyst, comparison, health and lab
views) and **Report** (`/report`). Opening the app at `/` lands on the chat;
its sidebar links to Report and Forecast. The original **Dashboard** generator
still works at `/dashboard` but is no longer linked from anywhere.

All three pages ask the same question of the data (*which database?*) and share
one answer: a **Database** chip, stored as the same `chat-db` setting, so one
choice of source holds across Chat and Forecast. `?db=live|staging` and
`?theme=light|dark` work as deep links on every page.

| Database | What it is |
|---|---|
| **Live** (default) | The Postgres estate databases the app was built on, one database per estate, readable table names. K3 is a file export. |
| **EPMS staging** | The central SQL Server `EPMSStagingDB`: EA, EB, EC and the plasma schemes P1/P2 in one database, SAP-style table codes (`OPH`, `FDN`, `BLK`...), split by `WERKS`. Optional; set `DATABASE_URL_STAGING`. |

## Chat (`/chat`)

A sidebar of saved chats, a message thread and a composer, in plain HTML, CSS
and JS with no build step (`chat_static/chat.html`, `chat.css`, `chat.js`;
marked, DOMPurify and Chart.js are vendored in `chat_static/vendor/`).

### What happens to a message

```
User message
     │
     ▼
0. Intent router (one cheap call, skipped whenever code can tell)
     │   data · context · explain · chat · off_domain · forecast
     ▼
1. Retrieval (no LLM)       ChromaDB + BM25 hybrid search finds the relevant tables
     │
     ▼
2. SQL generation           Schema context + business rules + similar past examples
     │
     ▼
3. Check, run, self-correct Lint, schema check, run; on error one or two corrected retries
     │
     ▼
4. Answer                   Derived facts computed in code; the model writes around them
```

- **`chat_intent.py` decides what a message wants before anything is queried.**
  A greeting, "what does X mean?", "chart that", a follow-up on rows already
  fetched, an off-topic question and a forecast question are each handled
  without writing SQL. The call is skipped when code can already tell, and a
  failed call means *data*, which is what happened before the router existed.
- **Broad questions are answered with several measures.** *"How is Estate C
  performing?"* has no single figure. `chat_overview.py` has the model pick two
  to four measures from the source's catalogue (an enum in a forced tool call,
  so it cannot invent one), writes each as a plain question from a template,
  runs them in parallel through the normal pipeline, and joins the results in
  code into one table. Nothing is computed across measures.
- **Forecast questions are answered from the Forecast page.**
  *"What's the outlook for EC next 3 months?"*, *"how sure is it?"*, *"which
  estate drops most?"* read the payload `/forecast` draws
  (`chat_forecast.py`): nothing is fitted and no query runs, so the chat and
  the page always agree, including manual picks, fallbacks and stale flags. The
  model gets a facts block and may only quote it; every number in its reply is
  checked against the facts, with one retry and then a reply built from the
  facts without a model. One estate draws a forecast chart (twelve actual
  months, the forecast dashed, the likely range as a band); several draw a bar
  chart beside last year. Every forecast answer ends with a link to the page.

### What you see

- **Live progress.** `POST /chat/stream` sends server-sent events while the
  question runs: a row per pipeline step, the result table as soon as it
  exists, a chart, then the answer. A **Stop** button cancels the request
  server-side and nothing is stored for a stopped message.
- **Charts.** After a query runs, the model is asked which chart shows the
  result (a type and a choice of columns, nothing else). `chat_charts.py`
  checks that against the real columns and takes every number from the rows, so
  a chart cannot show a figure the database did not return. If the model is
  unavailable a plain rule picks the chart. Time becomes a line, comparison
  bars, rankings horizontal bars; the reader can switch type.
- **Chats you can keep.** Rename, pin, search and delete; regenerate the last
  answer or edit the last question; thumbs up or down on any answer. Light and
  dark themes. Deep links: `/chat?c=<session>` and
  `/chat?q=<question>&db=staging&theme=dark`.
- **The tools menu** in the composer picks the **database**, the **answer
  source** (the data, the Bedrock knowledge base of documents, or both), and the
  **model** (Automatic, which uses the fast model and steps up for analysis, or
  a named one).

### Getting the right number, not just a valid query

- **Derived facts.** Gaps, ratios and shares are computed in code and handed to
  the answer model, because it does the arithmetic wrong.
- **Lint before run.** `DataSource.lint` rejects SQL that is valid but wrong
  and gives the model one corrected retry; if it is still flagged the user is
  told no query could be produced, and it is never saved. Live: the approval
  date rule, the deleted-row guard, area summed at a level that reads a
  transaction table. Staging: trip weight counted once per trip, area fan-out,
  `WERKS` compared with an estate code, and `EST.NAME1 = PRFNR` dropping both
  plasmas.
- **Schema check.** `sql_schema_check.py` verifies that every table and column
  a query names exists before it runs, and gives the model the real names to
  correct with.
- **Examples can be poisoned.** Successful queries are mined as few-shot
  examples, so a wrong one that ran cleanly gets copied. Lint also screens
  stored examples at retrieval, and a thumbs-down retires the answer's SQL as an
  example (`/chat/feedback`). A seeded example pool covers the cases that matter
  (`evals/seed_examples.py`).
- **A default period.** On staging, a question with no period means the last
  twelve full months, and the answer states the window it used.
- **Cheaper to repeat.** Conversation history grows only between fixed cut
  points, so each prompt is the previous one plus a turn and Bedrock reads the
  earlier part from its prompt cache.

### Two databases

`data_sources.py` describes each database once (dialect, schema, curated
metadata, business rules, prompt preamble, estate scoping, retrieval index,
measure catalogue, lint rules) and the pipeline asks for whichever is in force
for the request, so concurrent requests on different databases never see each
other.

Staging is read through a login with `db_datareader` only: SQL Server has no
read-only transaction mode, so permissions are the only write protection. Its
schema has traps a model walks into, and they are written into
`business_rules_staging.txt` and `schema_metadata_staging.json` (12 curated
tables; the rest are deliberately not indexed): tonnage lives in `FDN` and
repeats on every row of a weighbridge trip, so summing rows overcounts about
twenty-fold; block, division and employee tables are history tables where the
current row has `KDATE = '9999-12-31'`; and area summed after joining to harvest
tickets fans out.

### Conversation around the pipeline

`chat_conversation.py` sits around the SQL pipeline, never inside it, and every
model call in it is optional: a failure means no question back or no buttons,
never a lost answer. Whatever the model writes is checked before it reaches the
page, so a question or a button cannot carry a figure nobody retrieved.

- **Ask back.** A vague question gets a clarifying question with answer
  buttons; the reply is folded into the original question for retrieval. The
  language to ask in (English or Indonesian) is detected in code and stated to
  the model, which got it wrong when asked to match the message itself.
- **Follow-up buttons** after an answer, and "run it" or "yes" resolved to the
  previous question plus the sentence the answer offered.
- **Scope that carries.** A follow-up keeps the estate the conversation was
  about, and "other estates", "the rest" or "without the filter" widens it to
  every estate.
- **Small talk** (greetings, thanks, "what can you do?") answers directly with
  no query.

## Forecast (`/forecast`)

The forecast pages are an FFB (fresh fruit bunch) production forecast built for
two audiences: everyone gets a plain-language outlook, and the forecasting team
gets everything behind it.

| Page | URL | For |
|---|---|---|
| **Production outlook** | `/forecast` | Everyone. Expected production and a likely range per month, in plain words, no metrics. Tonnes and budget are shown as "not available yet": the data carries bunches, not weight. |
| Analyst view | `/forecast/analyst` | The forecasting team. Accuracy, bands, model comparison, SHAP drivers, risk intelligence, Block Map. |
| **Compare sources** | `/forecast/compare` | The client exhibit: live databases and staging side by side. |
| **Model health** | `/model-health` | Every forecast at a glance; `?db=&estate=` for one's detail. |
| Forecast Lab | `/experiment/forecast` | Models side by side before one is served. |

### What is served

- **Live and staging are two sources of the same forecast.** `forecast_sources.py`
  returns the same payload shape for either, so one render path draws both.
  Live covers **K3, EC, EB and EA** (K3 from its export file, the others from
  their estate databases); staging covers **EA, EB, EC and All** (a true sum of
  the three over the months all three have).
- **The served model is Chronos-2, zero-shot.** A dated rule
  (`forecast/lab/select_source.py`) picks the Chronos-2 arm per source from
  walk-forward backtests: estate weekly, monthly, monthly with weather, or a
  block sum. K3 is a recorded manual promotion (`promotion.py`,
  `MANUAL_PROMOTIONS`) because the history is short and a zero-shot model needs
  no training on it; the SARIMAX + LightGBM ensemble stays computed as the
  fallback and both stay in the shadow log, so the promotion can still be
  overturned. Where a served model does not beat the Naive last-3 mean on an
  estate, it is still served and that estate is flagged.
- **Manual picks.** Click a candidate on the Model health scoreboard to serve
  it (`POST /forecast/serve`). A pick survives refreshes, is logged to an
  append-only file, and falls back to the rule's pick, saying so, if the
  overridden model can no longer build a forecast.
- **A seasonally adjusted challenger** (`chronos2_weekly_seasadj`, in
  `forecast/lab/seasonal_profile.py`) divides the series by a month-of-year
  shape pooled across a region. It is reported beside the rule's picks and is
  not in force: switching to it is the user's call.
- **The series is bunches, not tonnes.** OPH carries a ripe-bunch count and no
  weight. The band is the typical error measured at each step on the
  walk-forward backtest, with the row count behind every step; it is a rough
  band, not a prediction interval. History is short (17 to 22 months on
  staging), and the page says how weak that makes it: staging group sMAPE is
  19.3% over steps 1 to 3, EC alone 30.6%. Staging EC agrees with the live EC
  database to the bunch from May 2025 on.

### Analyst view

The original ensemble page, kept for the forecasting team:

- **Ensemble** (`Ensemble_workdone`): SARIMAX(1,1,1) + month sin/cos,
  Trailing3, and LightGBM with work-done features, over ~40 months of cleaned
  monthly production, weather and agronomic-practice data. sMAPE 10.1% and
  MASE 0.572 against a Seasonal Naive benchmark on 30 expanding-window folds,
  with adaptive conformal bands (ACI, alpha 0.1, gamma 0.08). Under-recorded
  months are seasonally imputed and flagged; sparse months and partial
  endpoints are excluded from train and score, not day-scaled.
- **TreeSHAP driver panel**: per-forecast-month feature attributions
  (rainfall, pruning, fertiliser lag effects, seasonality).
- **Production Risk Intelligence**: an LLM risk-synthesis pass that pulls live
  weather, ENSO and fire-hotspot signals and returns a narrative risk level,
  without altering the underlying numbers.
- **Investigator agent**: a LangGraph ReAct agent that runs only when triggered
  (an actual outside the band, or a divergence between the numeric forecast
  and the risk level).
- **Block Map**: the estate's 291 polygons with a per-block forecast overlay.
  Deliberately not the ensemble run 291 times: each block has the same handful
  of monthly points, so the forecast is a trailing mean of its own non-partial
  months.

Panels that read the live estate databases are hidden on staging rather than
shown empty.

### Model health (`/model-health`)

- **The overview** is one card per forecast (each estate of each source, plus
  the staging group) with a status (*Healthy, Watch, Action*) and a matrix of
  checks: accuracy (MASE), error (sMAPE), bias, evidence, track record, live
  errors, drift, serving and freshness. Built only from what is cached or on
  disk, so it never refits.
- **The detail view** (`?db=&estate=`) reports the served model's accuracy,
  interval calibration, data quality and lifecycle, the candidate scoreboard
  and the manual-pick control.
- **The data-quality watchdog** (`forecast/watchdog.py`) says in plain words
  what is wrong with the data under a forecast and what that does to it:
  freshness, the extract's reconciliation gate, history depth, harvest gaps,
  short months, daily spikes, block coverage, duplicates, ticket ratio, target
  fill, weight per trip, and agreement between live and staging.
  `forecast/monitor.py` watches the model itself: realised error against the
  band, Chronos against naive, volume, column drift and seasonal shape.

### Forecast pipeline: refresh, contract, sources

Neither source touches a database inside a request. Everything a page serves
comes from files cut offline.

```
python forecast/refresh.py --source all            # or live, or staging
python forecast/refresh.py --source all --remote   # Chronos on SageMaker
python forecast/refresh.py --list                  # runs, newest first
python forecast/refresh.py --rollback <run_id>     # serve an earlier run again
```

A run is a complete snapshot under `forecast/runs/<run_id>/`: extract,
inference, selection, a health check, then an atomic publish that the pages
pick up on their next request. A failure in any step before publish leaves the
served run untouched and keeps the unfinished folder as `<run_id>.failed`.

- **Adapters** (`forecast/adapters/`): `live_postgres`, `live_k3_file`,
  `staging_sqlserver` and `file_drop`. Which estates each source covers is one
  list, `forecast/estates.yaml`; adding an estate is an entry there, not a code
  change.
- **The contract** (`forecast/contract.py`): every source's extract must
  validate and pass a reconciliation gate. Core columns are required from every
  source; native columns are declared per adapter and never renamed into each
  other. A `columns` block in `meta.json` records, per column, whether the
  source has it and how full it is, and the Compare page's "what data does each
  source give us" table is read from that block alone.
- **Compare sources** puts the two ingestion paths side by side: data
  available (with each source's own column header), history, agreement where
  both hold the same month, accuracy on the window both share, the forward
  forecast, and what a refresh costs to run.
- **CI** (`.github/workflows/forecast.yml`) runs the whole refresh end to end
  on synthetic estates with a stub in place of Chronos, and fails if any data
  folder is tracked in git.
- Chronos itself runs in its own Python environment (`CHRONOS_PYTHON`);
  anything that trains runs as a SageMaker job, not on a laptop.

### Forecast Lab: comparing models before one is served

An experiment surface kept off the client-facing page, reached from Model
health. It puts models side by side by estate, grain (months or weeks) and a
1 to 12 step horizon, in two views:

- **Forecast**: each model's forward forecast on one chart. Only real intervals
  are drawn: the block sum, and the estate-weekly model at month grain, have
  none, and no band is invented for them.
- **Backtest**: error by horizon, a leaderboard, and a replay slider. It scores
  only the (origin, step) rows every selected model produced, so a model that
  skipped hard origins cannot win by omission. It also shows the promotion gate
  card.

**Promotion is a rule, not a judgement call.** `forecast/lab/promotion.py`
fixes the champion/challenger test before the first result was seen. Gate A is
the backtest (lower pooled MAPE, a Diebold-Mariano test with the HLN
correction at p < 0.05, and no higher MAE at each step). Gate B is a shadow log
of forecasts recorded before their outcome was known. Gate A passed for K3;
Gate B has not, so the formal verdict is **HOLD** and K3 is served on the
recorded manual promotion. Thresholds are never edited after the fact: a
changed rule is a new dated rule beside the old one. After each data refresh,
`python forecast/lab/shadow.py record --rebuild` adds to the log.

Fine-tuning has been tried and failed its gate: a LoRA adapter trained on EC +
EB weekly blocks took K3 step-1 MASE from 0.553 to 0.904. It learned the shared
late-2025 decline rather than anything transferable, so it is not served, and
the rule going forward is that no estate may regress.

## Report (`/report`)

Preset reports and freeform generated ones, with saved, date-parameterised
query templates.

- **Presets** run fixed SQL with zero LLM cost (general, employee, harvest).
- **Generate** writes a report from a plain-English request through the same
  retrieval, SQL and self-correction pipeline the chat uses.
- **Saved templates** can be normalised (`query_normalizer.py` turns a query's
  dates into parameters), re-run for a new period and managed from the page.

## Dashboard (`/dashboard`) and the eval view (`/evals`)

- **Dashboard.** *"Show me revenue by division"* generates a layout of 3 to 12
  widgets (KPI cards, bar and line charts, tables), each backed by its own SQL
  query, as structured JSON from the LLM. No longer linked from the nav.
- **Evals.** `/evals` is a read-only view of the evaluation runs in
  `evals/results/`.

## The Chatbot: structure

```
├── chat_router.py          # Chat: sessions, /chat/stream, RAG → SQL → answer, feedback
├── chat_intent.py          # Intent router + the shared conversation window / prompt cache
├── chat_conversation.py    # Ask-back, follow-up buttons, scope carry, small talk
├── chat_overview.py        # Broad questions answered with several measures
├── chat_forecast.py        # Forecast questions answered from the Forecast page's payload
├── chat_charts.py          # Chart choice from the result; numbers always from the rows
├── chat_events.py          # Request-scoped progress events and cancellation
├── chat_history.py         # Saved chats and per-turn metadata
├── data_sources.py         # Live vs staging: dialect, metadata, rules, lint, measures, scoping
├── rag_sources.py          # Answer source: SQL, Bedrock knowledge base, or both
├── retrieval.py            # Hybrid ChromaDB + BM25 retrieval
├── prompt_builder.py       # Prompt assembly and SQL extraction
├── sql_validator.py, sql_schema_check.py   # SQL safety checks; every name must exist
├── query_history.py        # Few-shot example storage
├── report_router.py        # Reports: preset, generated, saved templates
├── layout_prompt.py / layout_schema.py     # Dashboard layout prompt and widget schema
│
├── forecast_router.py      # Forecast endpoints: data, sources, health, fleet, serve, compare
├── forecast_sources.py     # The page payload for either source, from lab artifacts
├── forecast_fleet.py       # Model health overview: one card and one check row per forecast
├── forecast_compare.py     # Compare sources payload
├── forecast_intelligence.py / forecast_investigator.py   # Risk synthesis; anomaly agent
├── experiment_router.py    # Forecast Lab endpoints (/experiment/forecast/*)
├── forecast/
│   ├── estates.yaml        # Which estates each source covers
│   ├── refresh.py          # One command: extract → inference → select → health → publish
│   ├── contract.py, adapters/   # The data contract and one adapter per kind of source
│   ├── build_series.py, build_staging_series.py   # Offline extracts, gated
│   ├── watchdog.py, monitor.py  # Data-quality and model monitoring
│   ├── overrides.py        # Manual serving picks
│   ├── monthly_model.py    # SARIMAX + Trailing3 + LightGBM ensemble, walk-forward backtest
│   ├── conformal.py        # Conformal / ACI uncertainty bands
│   ├── block_weekly.py, blocks.py, build_block_forecast.py   # Block series and Block Map
│   ├── chronos_*.py, chronos2_blocks_finetune.py   # Chronos-2 runs, incl. the SageMaker job
│   ├── lab/                # registry, build_artifacts, evaluate, promotion, shadow,
│   │                       #   select_source, select_staging, seasonal_profile
│   ├── runs/               # Published runs and current.json (gitignored data)
│   └── fixtures/           # Synthetic estates for CI
│
├── llm_client.py           # Provider-agnostic LangChain chat layer (Groq/Bedrock) + call log
├── prompts/                # Externalised prompt templates (per-provider overrides)
├── retrievers.py, guardrails.py, tool_schemas.py   # Seams: KB retrieval, guardrails, tool schemas
├── config.py               # Env-var config and the per-estate SQLAlchemy engines
│
├── chat_static/            # Chat page: chat.html / chat.css / chat.js + vendor/
├── forecast_static/        # outlook, forecast (analyst), compare, model_health, theme.css/js
├── experiment_static/      # forecast_lab.html
├── report_static/ dashboard_static/ eval_static/   # single-file UIs, no build
├── business_rules.txt, business_rules_staging.txt  # Domain rules for the LLM
├── glossary.yaml           # Domain term → table/column mappings
├── schema_metadata.json, schema_metadata_staging.json   # Table descriptions (staging: 12 curated)
│
├── sql_eval.py, eval_harness.py, evals/   # Evaluation (see below)
├── infra/terraform/        # Bedrock KB IaC skeletons, NOT YET APPLIED
├── doc_ingestion/          # PDF/DOCX parsing dry run for the contracts use case
└── test_*.py               # pytest: chat, intent, overview, forecast, data sources, navigation
```

## The Chatbot: API

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | 307 redirect to `/chat`, query string kept |
| `GET` | `/chat` | Chat UI |
| `POST` | `/chat/stream` | Send a message; server-sent events `step`, `table`, `chart`, `delta`, `final` / `error` |
| `POST` | `/chat/message` | The same pipeline without streaming (used by the evals) |
| `GET` | `/chat/databases` · `/chat/models` | What the tools menu offers; a database that isn't configured is listed as unavailable |
| `GET` | `/chat/sessions` · `/chat/sessions/{id}` | Saved chats; one chat's turns |
| `PATCH` / `DELETE` | `/chat/sessions/{id}` | Rename or pin; delete |
| `POST` | `/chat/feedback` | Thumbs up/down; a thumbs-down retires that answer's SQL as an example |
| `GET` | `/forecast` · `/forecast/analyst` · `/forecast/compare` · `/model-health` | Outlook, analyst view, compare sources, model health |
| `GET` | `/forecast/sources` | The databases the forecast pages can read, and whether each is built |
| `GET` | `/forecast/data` | Historical + forecast with a band. `source=live\|staging`, `estate`, horizon 3 or 6 |
| `GET` | `/forecast/model-health` | Served-model accuracy, calibration, data quality, lifecycle for one estate |
| `GET` | `/forecast/fleet` | Every forecast at a glance: status and the check matrix |
| `GET` / `POST` | `/forecast/watchdog` · `/forecast/watchdog/run` | Data-quality findings; re-run them now |
| `POST` | `/forecast/serve` | Serve a scoreboard candidate (or `model: null` to return to the rule's pick) |
| `GET` | `/forecast/compare-data` | Both sources side by side |
| `GET` | `/forecast/blocks` · `/forecast/block-forecast` | Block Map polygons; per-block trailing-mean overlay |
| `GET` | `/forecast/intelligence` · `/forecast/investigate` | Risk-synthesis narrative; investigator agent (only when triggered, or `?force=1`) |
| `GET` | `/experiment/forecast` and `/sources` · `/catalog` · `/data` · `/backtest` · `/promotion` | Forecast Lab UI and data |
| `GET` | `/report` | Report UI |
| `POST` | `/report/preset` · `/report/generate` · `/report/run-saved` · `/report/run-template` | Run a preset, a freeform request, or a saved template |
| `GET` / `PATCH` / `DELETE` | `/report/saved-templates` | Manage saved templates |
| `GET` / `POST` | `/dashboard` · `/api/generate` | Dashboard UI; generate a dashboard layout |
| `GET` | `/evals` · `/evals/data` | Evaluation runs |
| `GET` | `/health` | Health check |
| `POST` | `/admin/reload-schema` · `/admin/reload-glossary` | Hot-reload schema metadata / business rules |

## The Chatbot: evaluation

- `python evals/run_golden_chat.py --runs 3`: the whole chat pipeline, scored
  on whether the **number** is right, including multi-turn conversations, the
  overview route, the intent route and forecast answers. Expected figures come
  from hand-written reference SQL, never from the model; `--check-cases`
  validates that SQL with no LLM calls. Runs are not saved as few-shot
  examples. Every fix so far came from a real wrong answer (a missing
  deleted-row filter; a tonnage query that summed trip weight per row; a
  conversation that stayed scoped to one estate). Rerun it after any change to
  prompts, rules, metadata or the example pool, and turn real bad
  conversations into new cases. Results and history are in
  `evals/BASELINE.md`.
- `python sql_eval.py`: golden-set text-to-SQL harness (36 labelled cases in
  `evals/sql_cases.yaml`): execution accuracy by row-level diff against
  reference SQL, retrieval precision/recall/MRR, latency, tokens and cost.
  Results land in `evals/results/` and MLflow (`text2sql-evals`).
  `--check-cases` validates the reference SQL; `--with-examples` measures the
  production few-shot configuration.
- `python eval_harness.py`: investigator-agent harness (LLM-judged).
- `python forecast/monthly_model.py`: the walk-forward backtest behind the
  ensemble scoreboard.

---

# Setup and reference

Shared by both products.

## 1. Clone and install

```bash
git clone https://github.com/KimiPatria/Text-to-SQL-Chatbot.git
cd Text-to-SQL-Chatbot
python -m venv .venv
.venv\Scripts\activate       # Windows
# source .venv/bin/activate  # macOS / Linux
pip install -r requirements.txt
```

Chronos-2 runs in a separate Python environment
(`requirements-chronos.txt`); point `CHRONOS_PYTHON` at it.

## 2. Configure environment

Copy `.env.example` to `.env` and fill it in. The minimum for the Postgres
estates and the chat:

```env
DATABASE_URL=postgresql://username:password@host:5432/k3_db
DATABASE_URL_BA=postgresql://username:password@host:5432/ba_db
DB_SCHEMA=public
LLM_PROVIDER=bedrock             # Amazon Nova; see "Switching LLM providers"
AWS_REGION=us-east-1
FIRMS_MAP_KEY=your_firms_key     # live fire hotspots; the panel degrades without it
```

A free FIRMS key comes from
[firms.modaps.eosdis.nasa.gov](https://firms.modaps.eosdis.nasa.gov/api/).
Estate Command's snapshots (`gis/build_estate_data.py`) read one database per
estate through `DATABASE_URL_EC`, `DATABASE_URL_EA` and `DATABASE_URL_EB`.

**Optional: the EPMS staging database.** To offer "EPMS staging" on the chat
and forecast pages, install Microsoft's *ODBC Driver 18 for SQL Server* (the
`pyodbc` package needs it) and add a connection string:

```env
DATABASE_URL_STAGING=mssql+pyodbc://epms_ro:password@localhost/EPMSStagingDB?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes
```

Use a login with `db_datareader` only. Without the variable the database is
listed as unavailable and everything else runs as before.

## 3. Domain context files and schema metadata

Edit `business_rules.txt` and `glossary.yaml` to describe query constraints and
map domain terms to table and column names; the LLM uses both as grounding in
every text-to-SQL prompt. Then generate `schema_metadata.json` once:

```bash
python bootstrap_metadata.py     # requires GEMINI_API_KEY in .env
```

## 4. Build the Estate Command frontend

Estate Command is an ES module tree built by Vite. The other interfaces need no
build.

```bash
cd command_static
npm install
npm run build          # writes command_static/dist, which /command serves
cd ..
```

`/command` falls back to the pre-build single file when `dist/` is absent. For
frontend work, `npx vite --port 5173` serves `src` with hot reload and proxies
the API to 8001. `npm run build` runs a gate before bundling: `test/undefined.mjs`
walks each module's AST and reports any identifier that is used but bound
nowhere, because Rollup treats such a name as a browser global and a forgotten
import would otherwise build and then throw at runtime.

## 5. Run

```bash
uvicorn dashboard_server:app --reload --port 8001
```

| Interface | URL |
|---|---|
| **Chat** (the app opens here) | [http://localhost:8001](http://localhost:8001) |
| **Forecast** (outlook) | [http://localhost:8001/forecast](http://localhost:8001/forecast) |
| Analyst view | [http://localhost:8001/forecast/analyst](http://localhost:8001/forecast/analyst) |
| Compare sources | [http://localhost:8001/forecast/compare](http://localhost:8001/forecast/compare) |
| Model health | [http://localhost:8001/model-health](http://localhost:8001/model-health) |
| Forecast Lab | [http://localhost:8001/experiment/forecast](http://localhost:8001/experiment/forecast) |
| **Report** | [http://localhost:8001/report](http://localhost:8001/report) |
| Dashboard | [http://localhost:8001/dashboard](http://localhost:8001/dashboard) |
| **Estate Command** | [http://localhost:8001/command](http://localhost:8001/command) |

The operational models and the stores replay fit in background threads when the
server starts, and the model health overview is built on first request, so the
first outlook, plan, stores or fleet call after a restart can take half a
minute. Wait for `GET /gis/forecast/accuracy` to return 200 before running the
frontend tests.

## 6. Regenerate data (optional)

Estate Command feeds:

```bash
python gis/build_synthetic.py       # all demand-side feeds, then build_operations.py
python gis/build_materials.py       # the six SAP MM feeds, on their own seed
python gis/build_finance.py         # the SAP FI/CO cost records
python gis/build_rainfall.py        # daily rainfall the ledger reads
python gis/build_rain_forecast.py   # what the forecast said the evening before, daily
python gis/build_ndre.py            # the real Sentinel-2 canopy layer
```

The forecast, for both sources, after new data lands:

```bash
python forecast/refresh.py --source all          # extract, Chronos inference, selection, health, publish
python forecast/lab/shadow.py record --rebuild   # log the month's shadow forecasts for the promotion rule
```

Add `--remote` to run Chronos as a SageMaker job (`--recover-job <name>`
collects one whose local poll died). The full K3/EC lab build takes about six
minutes.

## 7. Tests

Estate Command's `npm test` needs the server running. It loads the real page in
Chrome, walks every panel in every domain, builds a selection, drafts a
document, and fails on any console error. Then:

```bash
node test/ops.mjs       # the operation windows: edit, re-run, show on map, draft, filter
node test/models.mjs    # the four forecasts: grades, planted-truth checks, switches, replay
node test/stores.mjs    # the stores models and window
node test/finance.mjs   # cost efficiency
```

`test/models.mjs` checks that each model beats the method it replaces, passes
its planted-truth checks, that the generator's answer key never reaches a
payload, and that switching a forecast off puts the plan back on the old
method.

The chatbot's tests are pytest plus browser drivers:

```bash
pytest test_chat_experience.py test_chat_conversation.py test_chat_intent.py test_chat_forecast.py test_overview.py test_data_sources.py test_navigation.py
pytest test_forecast_*.py
node command_static/test/outlook_page.mjs http://127.0.0.1:8001          # /forecast in a real browser
node command_static/test/forecast_page.mjs http://127.0.0.1:8001         # the analyst view
node command_static/test/forecast_compare.mjs http://127.0.0.1:8001      # compare sources
node command_static/test/model_health_fleet.mjs http://127.0.0.1:8001    # the health overview
node command_static/test/forecast_lab.mjs http://127.0.0.1:8001         # the Lab
```

`test_forecast_theme.py` fails if `forecast_static/theme.css` drifts from the
chat tokens in `chat_static/chat.css`: the forecast pages carry a copy so they
look like the chat page, and the test is what keeps it a copy. The Lab test
includes a leakage check, so a model cannot be scored on data it had already
seen. Anything that goes through `/chat` should be tested against the running
server, because a standalone process has no retrieval index.

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `groq` | Infrastructure provider: `groq` or `bedrock` |
| `BEDROCK_MODEL_ID` | `us.amazon.nova-2-lite-v1:0` | Main model under Bedrock |
| `BEDROCK_REASONING_MODEL_ID` | `amazon.nova-pro-v1:0` | Reasoning, corrections, planner |
| `BEDROCK_ROUTING_MODEL_ID` | `amazon.nova-lite-v1:0` | Cheap routing / short-form model |
| `AWS_REGION` | `us-east-1` | Bedrock region |
| `CHAT_DEFAULT_MODEL` | `nova-2-lite` | Model the chat page's "Automatic" starts from |
| `CHAT_ESCALATE_TO_REASONING` | `1` | Let "Automatic" step up to the reasoning model for analysis |
| `CHAT_SCHEMA_CHECK` | `enforce` | Pre-run check that every table and column exists |
| `CHAT_SQL_FIX_ATTEMPTS` | `2` | Corrected retries after a failed or flagged query |
| `PROMPT_CACHE` | `1` | Bedrock prompt-cache markers |
| `DATABASE_URL` | none | PostgreSQL connection string for estate K3 |
| `DATABASE_URL_BA` | none | PostgreSQL connection string for estate BA |
| `DATABASE_URL_EC` / `_EA` / `_EB` | none | Estate databases read by `gis/build_estate_data.py` and the forecast extracts |
| `DATABASE_URL_STAGING` | none | Optional SQL Server connection string (read-only login). Without it, staging is listed as unavailable |
| `DB_SCHEMA` | `public` | PostgreSQL schema to introspect |
| `MAX_RESULT_ROWS` | `5000` | Row cap injected into every query |
| `STATEMENT_TIMEOUT_MS` | `15000` | PostgreSQL statement timeout (ms) |
| `RAG_SOURCE_DEFAULT` | `sql` | `sql`, `knowledge_base` or `both` |
| `BEDROCK_KB_ID` | none | Bedrock Knowledge Base for the documents source |
| `FIRMS_MAP_KEY` | none | NASA FIRMS key for live fire hotspots |
| `CHRONOS_PYTHON` | none | Python of the Chronos environment (forecast inference) |
| `GROQ_API_KEY` | none | Required only while `LLM_PROVIDER=groq` |
| `LLM_LOG_PATH` | `./llm_calls.jsonl` | Structured per-LLM-call log (task, model, tokens, latency, cache reads) |

## Switching LLM providers (Groq and Amazon Bedrock)

The pipeline never talks to a provider SDK directly: every call goes through
`llm_client.py`, which builds a LangChain `BaseChatModel` from config
(`ChatGroq` or `ChatBedrockConverse`). The app runs on **Amazon Nova** (Nova 2
Lite as the main model, Nova Pro for reasoning, corrections and planning, Nova
Lite for routing). Groq remains as an alternative provider for the original
pipeline.

To use Bedrock:

1. Request Bedrock model access for the Nova tiers in the AWS console.
2. Make AWS credentials visible to the process (SSO profile, access keys, or an
   `AWS_BEARER_TOKEN_BEDROCK` API key) with `bedrock:InvokeModel`.
3. Set `LLM_PROVIDER=bedrock` and `AWS_REGION` in `.env`; restart the server.
4. Re-run the baselines (`python evals/run_golden_chat.py --runs 3`,
   `python sql_eval.py`) and compare. Per-call latency and tokens land in
   `llm_calls.jsonl`.
5. Prompt tuning per model family goes in `prompts/` as `<name>.bedrock.txt`
   overrides, with no pipeline code changes.

Nova has no JSON response-format mode, so `llm_client.chat(json_mode=True)` is
a no-op on Bedrock: prompts already demand JSON and all callers parse
defensively. Contract-document retrieval (a Bedrock Knowledge Base) has its own
runway: `infra/terraform/` (unapplied IaC), `doc_ingestion/` (parser
findings), and `retrievers.ContractDocumentRetriever` (the code seam).

## Tech stack

| Layer | Technology |
|---|---|
| Web framework | FastAPI + Uvicorn (single server, port 8001) |
| Map | MapLibre GL, ES modules built by Vite |
| Geospatial | ArcGIS block export → GeoJSON; Sentinel-2 L2A via STAC + COG range requests (numpy only, no GDAL/rasterio) |
| Operational models | Ridge / logistic fits with backtests, trust grades and fallbacks (`gis/models/learn.py`); greedy scheduling with swap improvement and a relaxed bound |
| FFB forecasting | Chronos-2 zero-shot (own Python env, SageMaker for remote runs); SARIMAX + LightGBM + Trailing3 ensemble with conformal prediction (ACI) and TreeSHAP as the fallback and analyst reference |
| LLM inference | Provider-agnostic LangChain layer (`llm_client.py`): Amazon Bedrock (Nova 2 Lite / Pro / Lite), Groq as an alternative |
| Agents | LangGraph (forecast investigator), tool-calling copilot (`gis/copilot.py`, 48 tools) |
| Retrieval | ChromaDB + fastembed (BAAI/bge-small-en-v1.5), BM25 (rank-bm25), sqlglot schema check |
| Database | PostgreSQL via SQLAlchemy (read-only); optional SQL Server staging via pyodbc (read-only login) |
| Chat frontend | Plain HTML/CSS/JS, server-sent events, marked + DOMPurify + Chart.js (vendored, no build) |
| Decision log / assumptions | SQLite, never EPMS |
| Public feeds | Copernicus / Sentinel-2, Open-Meteo (history, forecast, previous runs), NASA FIRMS, NASA POWER |
| Tracking and CI | MLflow; GitHub Actions on synthetic estates |

## License

MIT
