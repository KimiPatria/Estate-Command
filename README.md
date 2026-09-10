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

### 5. Run

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
