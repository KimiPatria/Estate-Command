"""
EPMS AI Dashboard Server  —  port 8001
Run with:   python dashboard_server.py
Or:         uvicorn dashboard_server:app --port 8001 --reload

The original text-to-SQL server (main.py) stays on port 8000.
This server shares the same database, retrieval indexes, and LLM modules
but runs as a completely separate process.
"""

import decimal
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from config import engine, ESTATES, ESTATE_ORDER
from estates import detect_estate_mentions, strip_estate_mentions
import metadata_loader
from metadata_loader import load_all_cards, compact_ddl_for
from retrieval import initialize as init_retrieval, retrieve_tables
from sql_validator import validate, ensure_limit
from layout_schema import DashboardLayout, WidgetConfig
from layout_prompt import build_layout_messages
from widget_resolver import resolve_widget
from llm import call_llm, get_provider, list_providers, PROVIDER_LABELS
from prompt_builder import strip_fences
from report_router import router as report_router
from chat_router import router as chat_router
from forecast_router import router as forecast_router, get_forecast
from eval_router import router as eval_router
from gis_router import router as gis_router
from experiment_router import router as experiment_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("epms-dashboard")

DASHBOARD_PORT    = 8001
STATIC_DIR          = Path(__file__).parent / "dashboard_static"
REPORT_STATIC_DIR   = Path(__file__).parent / "report_static"
CHAT_STATIC_DIR     = Path(__file__).parent / "chat_static"
FORECAST_STATIC_DIR = Path(__file__).parent / "forecast_static"
COMMAND_STATIC_DIR  = Path(__file__).parent / "command_static"
EXPERIMENT_STATIC_DIR = Path(__file__).parent / "experiment_static"

app = FastAPI(
    title="EPMS AI Dashboard",
    description="Natural-language dashboard generator connected to the EPMS database.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/report-static", StaticFiles(directory=str(REPORT_STATIC_DIR)), name="report-static")
app.mount("/chat-static", StaticFiles(directory=str(CHAT_STATIC_DIR)), name="chat-static")
app.mount("/forecast-static", StaticFiles(directory=str(FORECAST_STATIC_DIR)), name="forecast-static")
# The built bundle when it exists, the source tree otherwise, matching the
# same fallback the /command route takes.
COMMAND_ASSET_DIR = (COMMAND_STATIC_DIR / "dist"
                     if (COMMAND_STATIC_DIR / "dist").is_dir() else COMMAND_STATIC_DIR)
app.mount("/command-static", StaticFiles(directory=str(COMMAND_ASSET_DIR)), name="command-static")

app.include_router(report_router)
app.include_router(chat_router)
app.include_router(forecast_router)
app.include_router(eval_router)
app.include_router(gis_router)
app.include_router(experiment_router)


# ── startup ────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup() -> None:
    cards = load_all_cards()
    init_retrieval(cards)
    log.info(
        "EPMS AI Dashboard ready — http://127.0.0.1:%d  (%d tables indexed)",
        DASHBOARD_PORT, len(cards),
    )


# ── request / response models ──────────────────────────────────────────────

class GenerateRequest(BaseModel):
    goal: str
    max_widgets: int = 5


class WidgetData(BaseModel):
    columns: list[str] = []
    rows: list[dict] = []
    error: Optional[str] = None
    estates_used: list[str] = []
    estates_failed: list[str] = []


class DashboardResponse(BaseModel):
    title: str
    widgets: list[WidgetConfig]
    tables_used: list[str]
    widget_data: dict[str, WidgetData]
    provider: str
    provider_label: str
    tables_considered: list[str]
    used_llm_fallback: bool = False


# ── helpers ────────────────────────────────────────────────────────────────

def _coerce(v):
    """Convert DB-native types that are not JSON-serialisable."""
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return str(v)
    if isinstance(v, (memoryview, bytes, bytearray)):
        return None
    return v


def _dedupe_columns(keys: list[str]) -> list[str]:
    """Make result column names unique.

    Postgres names every unaliased aggregate after its function, so
    `SELECT a, SUM(x), SUM(y)` comes back as ["a", "sum", "sum"] and building a
    row dict from it silently drops a measure. The layout prompt now demands
    explicit aliases, but a duplicate must never cost us a column."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for k in keys:
        if k in seen:
            seen[k] += 1
            out.append(f"{k}_{seen[k]}")
        else:
            seen[k] = 0
            out.append(k)
    return out


def _execute_safe(
    sql: str, max_rows: int = 500, engine_obj=None
) -> tuple[list[str], list[dict], Optional[str]]:
    """Run a SELECT against the EPMS database. Returns (columns, rows, error_or_None)."""
    engine_obj = engine if engine_obj is None else engine_obj
    try:
        limited = ensure_limit(sql, max_rows)
        with engine_obj.connect() as conn:
            result  = conn.execute(text(limited))
            columns = _dedupe_columns([str(k) for k in result.keys()])
            # Zip positionally rather than using r._mapping: the mapping is
            # keyed by the original (possibly duplicated) names.
            rows    = [
                {c: _coerce(v) for c, v in zip(columns, tuple(r))}
                for r in result.fetchall()
            ]
        return columns, rows, None
    except SQLAlchemyError as exc:
        err = str(exc.orig) if hasattr(exc, "orig") and exc.orig else str(exc)
        log.warning("[execute_safe] %s", err[:200])
        return [], [], err
    except Exception as exc:
        log.warning("[execute_safe] unexpected: %s", exc)
        return [], [], str(exc)


# Postgres rejects TRIM/btrim on numeric types. The layout prompt tells the
# model to reserve that wrapper for varchar columns, but when it over-applies
# it the query fails everywhere and the widget renders empty — so unwrap it
# mechanically and retry once instead of losing the widget.
_BTRIM_TYPE_ERR = re.compile(r"function pg_catalog\.btrim\((?!unknown|character)", re.I)
_NULLIF_TRIM = re.compile(r"NULLIF\s*\(\s*TRIM\s*\(([^()]*)\)\s*,\s*''\s*\)", re.I)
_BARE_TRIM   = re.compile(r"\bTRIM\s*\(([^()]*)\)", re.I)


def _unwrap_trim(sql: str) -> Optional[str]:
    """Strip TRIM()/NULLIF(TRIM(),'') wrappers. Returns None if none present."""
    out = _NULLIF_TRIM.sub(r"\1", sql)
    out = _BARE_TRIM.sub(r"\1", out)
    return out if out != sql else None


def _execute_multi(
    sql: str, estate_ids: list[str], max_rows: int = 500
) -> dict[str, tuple[list[str], list[dict], Optional[str]]]:
    """Run the same SQL against each estate's engine concurrently. Mirrors
    chat_router._execute_multi — safe because each estate has its own engine/pool."""
    results: dict[str, tuple[list[str], list[dict], Optional[str]]] = {}
    with ThreadPoolExecutor(max_workers=len(estate_ids)) as pool:
        futures = {
            pool.submit(_execute_safe, sql, max_rows, ESTATES[eid]["engine"]): eid
            for eid in estate_ids
        }
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()
    return results


def _parse_layout(raw_text: str) -> tuple[Optional[DashboardLayout], Optional[str]]:
    """Parse an LLM layout response. Returns (layout, None) or (None, reason).

    Tolerates the two things models put around JSON: code fences (handled by
    strip_fences) and prose before/after the object (handled by slicing to the
    outermost braces)."""
    clean = strip_fences(raw_text or "").strip()
    if not clean:
        return None, "Model returned an empty response."
    start, end = clean.find("{"), clean.rfind("}")
    if start > 0 or (end != -1 and end < len(clean) - 1):
        clean = clean[start:end + 1] if start != -1 and end > start else clean
    try:
        return DashboardLayout.model_validate_json(clean), None
    except ValidationError as exc:
        return None, "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
        ) or str(exc)
    except Exception as exc:
        return None, str(exc)


def _run_widget(sql: str, estate_ids: list[str], max_rows: int = 500) -> WidgetData:
    """Execute one widget's sql_hint across estate_ids and merge into a single
    WidgetData, tagging rows with _source_estate when more than one estate is
    in scope. Estates that error are dropped, not fatal — same
    graceful-degradation policy as chat_router/report_router."""
    exec_results = _execute_multi(sql, estate_ids, max_rows)
    ok_estates = [e for e in estate_ids if not exec_results[e][2]]

    if not ok_estates and any(_BTRIM_TYPE_ERR.search(exec_results[e][2] or "") for e in estate_ids):
        retry_sql = _unwrap_trim(sql)
        if retry_sql:
            log.info("[widget] btrim type error — retrying without TRIM wrappers")
            exec_results = _execute_multi(retry_sql, estate_ids, max_rows)
            ok_estates = [e for e in estate_ids if not exec_results[e][2]]

    failed_estates = [e for e in estate_ids if exec_results[e][2]]

    if not ok_estates:
        # Estates fail for different reasons — t_harvest_completion exists in
        # K3 but not BA, so reporting only estate_ids[0]'s error showed
        # "relation does not exist" for a table that is simply missing from one
        # database, hiding the real (different) failure on the other.
        by_error: dict[str, list[str]] = {}
        for e in estate_ids:
            first_line = (exec_results[e][2] or "unknown error").splitlines()[0][:160]
            by_error.setdefault(first_line, []).append(ESTATES[e]["label"])
        return WidgetData(
            error=" | ".join(f"{', '.join(labels)}: {err}" for err, labels in by_error.items()),
            estates_failed=[ESTATES[e]["label"] for e in failed_estates],
        )

    multi_estate = len(estate_ids) > 1
    columns = exec_results[ok_estates[0]][0]
    combined_rows: list[dict] = []
    for eid in ok_estates:
        est_columns, est_rows, _ = exec_results[eid]
        label = ESTATES[eid]["label"]
        tagged_rows = (
            [{**r, "_source_estate": label} for r in est_rows] if multi_estate else est_rows
        )
        combined_rows.extend(tagged_rows)
    if multi_estate and "_source_estate" not in columns:
        columns = columns + ["_source_estate"]

    return WidgetData(
        columns=columns,
        rows=combined_rows,
        estates_used=[ESTATES[e]["label"] for e in ok_estates],
        estates_failed=[ESTATES[e]["label"] for e in failed_estates],
    )


# ── routes ─────────────────────────────────────────────────────────────────

# Browsers heuristically cache FileResponse pages; force revalidation so UI
# changes show up without a hard refresh.
_NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", headers=_NO_CACHE)


@app.get("/report")
def report_page():
    return FileResponse(REPORT_STATIC_DIR / "report.html", headers=_NO_CACHE)


@app.get("/chat")
def chat_page():
    return FileResponse(CHAT_STATIC_DIR / "chat.html", headers=_NO_CACHE)


@app.get("/forecast")
def forecast_page():
    import json
    try:
        data_json = json.dumps(get_forecast()).replace("</script>", "<\\/script>")
    except Exception:
        data_json = "null"
    html = (FORECAST_STATIC_DIR / "forecast.html").read_text(encoding="utf-8")
    html = html.replace("</head>", f"<script>window.__FORECAST_DATA__={data_json};</script>\n</head>", 1)
    return HTMLResponse(content=html, headers=dict(_NO_CACHE))


@app.get("/model-health")
def model_health_page():
    """MLOps view for the served forecast model — deliberately a separate page
    so the operator-facing /forecast stays about production, not diagnostics."""
    return FileResponse(FORECAST_STATIC_DIR / "model_health.html", headers=_NO_CACHE)


@app.get("/experiment")
def experiment_index():
    """Experiment pages live under /experiment; the Forecast Lab is the only one so far."""
    return RedirectResponse("/experiment/forecast")


@app.get("/experiment/forecast")
def forecast_lab_page():
    """Side-by-side forecast models for the forecasting team. Deliberately not
    linked from the client-facing /forecast page (it is reached from Model
    health) and never alters what that page serves."""
    return FileResponse(EXPERIMENT_STATIC_DIR / "forecast_lab.html", headers=_NO_CACHE)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "provider": get_provider(),
        "providers": list_providers(),
    }


@app.post("/api/generate", response_model=DashboardResponse)
def generate_dashboard(req: GenerateRequest) -> DashboardResponse:
    goal        = (req.goal or "").strip()
    max_widgets = max(3, min(req.max_widgets, 12))

    if not goal:
        return JSONResponse(status_code=400, content={"detail": "goal must not be empty"})

    from guardrails import apply_input_guardrails
    goal = apply_input_guardrails(goal, context="dashboard")

    t0 = time.monotonic()
    log.info('[dashboard] goal="%s"', goal)

    mentioned_estates = detect_estate_mentions(goal)
    estate_ids = mentioned_estates or list(ESTATE_ORDER)
    # Layout/SQL generation only — table retrieval still sees the original
    # goal. See estates.strip_estate_mentions for why this is needed.
    sql_goal = strip_estate_mentions(goal, mentioned_estates) if mentioned_estates else goal

    # ── 1. Retrieve tables ────────────────────────────────────────────────
    table_cards, used_llm_fallback = retrieve_tables(goal, k=5)
    table_names = [c.name for c in table_cards]
    log.info("[dashboard] retrieved tables: %s (llm_fallback=%s)", table_names, used_llm_fallback)

    if not table_cards:
        from error_handler import error_response, is_off_domain
        kind = "off_domain" if is_off_domain(goal) else "no_data"
        log.info("[dashboard] no tables retrieved — returning %s", kind)
        return JSONResponse(content=error_response(kind, used_llm_fallback))

    # ── 2. Build compact DDL ──────────────────────────────────────────────
    ddl = compact_ddl_for(table_names)

    # ── 3. Build prompt → call LLM ───────────────────────────────────────
    messages = build_layout_messages(ddl, sql_goal, max_widgets)
    raw_text, provider_used, usage = call_llm(
        messages, max_tokens=1000, temperature=0, task="dashboard_layout"
    )
    log.info(
        "[dashboard] provider=%s input=%d output=%d",
        provider_used, usage["input_tokens"], usage["output_tokens"],
    )

    # ── 4. Parse layout JSON ──────────────────────────────────────────────
    layout, parse_err = _parse_layout(raw_text)
    if layout is None:
        # One repair round-trip before giving up. A malformed layout used to
        # 422 the whole request, which is what a user sees as "I asked for a
        # dashboard and nothing came back".
        log.warning("[dashboard] layout parse failed, retrying: %s", parse_err)
        repair = messages + [
            {"role": "assistant", "content": raw_text},
            {"role": "user", "content":
                f"That response could not be parsed: {parse_err}\n"
                "Return ONLY the corrected JSON object — no prose, no code fences."},
        ]
        raw_text, provider_used, usage = call_llm(
            repair, max_tokens=1200, temperature=0, task="dashboard_layout_repair"
        )
        layout, parse_err = _parse_layout(raw_text)

    if layout is None:
        log.warning("[dashboard] layout unrecoverable: %s | raw=%r", parse_err, raw_text[:300])
        return JSONResponse(
            status_code=422,
            content={"detail": parse_err, "raw_llm_output": raw_text},
        )

    # ── 5. Validate and execute each sql_hint ─────────────────────────────
    widget_data: dict[str, WidgetData] = {}
    for widget in layout.widgets:
        if not widget.sql_hint:
            widget_data[widget.id] = WidgetData(error="No SQL hint provided")
            continue

        ok, reason = validate(widget.sql_hint)
        if not ok:
            widget.sql_hint = None
            widget_data[widget.id] = WidgetData(error=f"SQL validation: {reason}")
            continue

        data = _run_widget(widget.sql_hint, estate_ids)
        widget_data[widget.id] = data
        # Reconcile the LLM's pre-execution chart config with the columns the
        # query actually returned. See widget_resolver for why this is needed.
        resolve_widget(widget, data.columns, data.rows)

    elapsed = time.monotonic() - t0
    log.info("[dashboard] done %.2fs, widgets=%d", elapsed, len(layout.widgets))

    return DashboardResponse(
        title=layout.title,
        widgets=layout.widgets,
        tables_used=layout.tables_used,
        widget_data=widget_data,
        provider=provider_used,
        provider_label=PROVIDER_LABELS.get(provider_used, provider_used),
        tables_considered=table_names,
        used_llm_fallback=used_llm_fallback,
    )


# ── entrypoint ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "dashboard_server:app",
        host="0.0.0.0",
        port=DASHBOARD_PORT,
        reload=True,
    )
