import os
from dotenv import load_dotenv
from sqlalchemy import create_engine

load_dotenv()

# --- provider selection (Bedrock migration seam) ---
# "groq" today; "bedrock" once AWS access lands. Business logic never reads
# this directly — llm_client.py resolves the provider + model from it.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").lower()
# Inert until AWS credentials exist. Nova Pro is a placeholder tier; swap for
# Micro/Lite/Premier once the target tier is decided.
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "amazon.nova-pro-v1:0")
# Cheap/fast tier used where ROUTING_MODEL is used under Groq.
BEDROCK_ROUTING_MODEL_ID = os.getenv("BEDROCK_ROUTING_MODEL_ID", "amazon.nova-lite-v1:0")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# Structured per-call LLM log (JSONL): provider, model, tokens, latency, task.
# Feeds the eval harness and future Groq-vs-Nova comparisons. Empty string disables.
LLM_LOG_PATH = os.getenv("LLM_LOG_PATH", "./llm_calls.jsonl")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
# Llama 3.3 70B was decommissioned on Groq; GPT OSS 120B is the replacement default.
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_MODEL_120B = os.getenv("GROQ_MODEL_120B", "openai/gpt-oss-120b")
GROQ_MODEL_20B = os.getenv("GROQ_MODEL_20B", "openai/gpt-oss-20b")
# Kept distinct from GROQ_MODEL/GROQ_MODEL_120B so the eval judge is never the
# same model family as the agent it grades (self-enhancement bias mitigation).
GROQ_MODEL_JUDGE = os.getenv("GROQ_MODEL_JUDGE", "qwen/qwen3-32b")
DEFAULT_PROVIDER = os.getenv("DEFAULT_PROVIDER", "groq").lower()
MAX_RESULT_ROWS = int(os.getenv("MAX_RESULT_ROWS", "5000"))
LARGE_RESULT_THRESHOLD = int(os.getenv("LARGE_RESULT_THRESHOLD", "50"))
STATEMENT_TIMEOUT_MS = int(os.getenv("STATEMENT_TIMEOUT_MS", "15000"))
DB_SCHEMA = os.getenv("DB_SCHEMA", "public")

# --- dynamic routing (iteration 2) ---
ROUTING_MODEL = os.getenv("ROUTING_MODEL", "llama-3.1-8b-instant")

# --- forecast investigator agent (Stage-2/3 R&D) ---
# Unset (None) = follow the active provider's main-tier model (GROQ_MODEL or
# BEDROCK_MODEL_ID); set explicitly to move the ReAct agent onto a different
# quota bucket (e.g. openai/gpt-oss-20b) for eval runs.
INVESTIGATOR_MODEL = os.getenv("INVESTIGATOR_MODEL") or None

# --- forecast intelligence layer ---
# Optional free NASA FIRMS map key (https://firms.modaps.eosdis.nasa.gov/api/area/).
# Enables the fire-hotspot signal; the intelligence layer degrades gracefully without it.
FIRMS_MAP_KEY = os.getenv("FIRMS_MAP_KEY")

# --- conversational context (chat history) ---
# Text Q&A turns included in prompts (cheap, ~50-150 tokens each).
CHAT_HISTORY_TEXT_TURNS = int(os.getenv("CHAT_HISTORY_TEXT_TURNS", "4"))
# Data-bearing turns whose rows are replayed when answering from context (expensive).
CHAT_HISTORY_DATA_TURNS = int(os.getenv("CHAT_HISTORY_DATA_TURNS", "2"))
# Row cap per replayed data turn.
CHAT_HISTORY_MAX_ROWS = int(os.getenv("CHAT_HISTORY_MAX_ROWS", "30"))
CATALOG_TTL_SECONDS = int(os.getenv("CATALOG_TTL_SECONDS", "1800"))
MAX_ROUTED_TABLES = int(os.getenv("MAX_ROUTED_TABLES", "12"))
GLOSSARY_PATH = os.getenv("GLOSSARY_PATH", "./glossary.yaml")

# --- manual RAG source toggle (SQL vs. Bedrock Knowledge Base, POC) ---
# "sql" | "knowledge_base" | "both". Overridable per-request via ChatRequest.source.
# No automatic routing/classification yet — see rag_sources.py.
RAG_SOURCE_DEFAULT = os.getenv("RAG_SOURCE_DEFAULT", "sql").lower()
# Provisioned by infra/terraform/bedrock_kb.tf (NOT YET APPLIED). Empty until AWS lands.
BEDROCK_KB_ID = os.getenv("BEDROCK_KB_ID", "")
# Foundation model used for the KB's RetrieveAndGenerate call; defaults to the
# same Nova tier as the main chat model so the two paths are cost-comparable.
BEDROCK_KB_MODEL_ID = os.getenv("BEDROCK_KB_MODEL_ID") or BEDROCK_MODEL_ID
# Number of chunks retrieved per KB query — the main token-cost lever on this path.
KB_TOP_K = int(os.getenv("KB_TOP_K", "5"))
# Structured per-RAG-request log (JSONL: source, route, llm_calls, tokens, latency).
# Separate from LLM_LOG_PATH (per-LLM-call log) — this is one line per /chat/message
# request, tagged by which source(s) served it, for SQL-vs-KB-vs-both comparison.
RAG_LOG_PATH = os.getenv("RAG_LOG_PATH", "./rag_calls.jsonl")

if LLM_PROVIDER == "groq" and not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is not set. Copy .env.example to .env and fill it in.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")

_pg_options = (
    f"-c default_transaction_read_only=on "
    f"-c statement_timeout={STATEMENT_TIMEOUT_MS}"
)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"options": _pg_options},
)

# --- multi-estate registry (Head Office chatbot: BA + K3) -------------------
# Both estate databases share an identical schema (see schema_metadata.json)
# — table retrieval and SQL generation stay single-shot against one schema;
# only chat SQL execution fans out per estate (see estates.py / chat_router.py).
# `engine` above (K3) remains the primary connection used by everything that
# isn't estate-aware (metadata_loader, dashboard, reports, forecast).
DATABASE_URL_BA = os.getenv("DATABASE_URL_BA")
if not DATABASE_URL_BA:
    raise RuntimeError("DATABASE_URL_BA is not set. Copy .env.example to .env and fill it in.")

ESTATES: dict[str, dict] = {
    "ba": {
        "label": "BA",
        "aliases": ["ba", "estate ba", "kebun ba"],
        "database_url": DATABASE_URL_BA,
        "engine": create_engine(
            DATABASE_URL_BA,
            pool_pre_ping=True,
            connect_args={"options": _pg_options},
        ),
    },
    "k3": {
        "label": "K3",
        "aliases": ["k3", "estate k3", "kebun k3", "kebun 3"],
        "database_url": DATABASE_URL,
        "engine": engine,  # reuse the existing pool, don't duplicate it
    },
}
ESTATE_ORDER = ["ba", "k3"]  # deterministic fan-out / display order
