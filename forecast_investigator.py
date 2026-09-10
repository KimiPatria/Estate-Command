"""
Forecast Investigator — Stage-2 R&D agent (LangGraph ReAct loop + Reflexion-lite).

Investigates WHY production and the forecast diverge, producing an
evidence-grounded root-cause narrative with a confidence score and a full
tool trace (the trace is the input for the Stage-3 eval harness).

Triggers (evaluate_triggers):
  * band_breach     — the most recent scoreable actual fell outside the
                      1-step conformal band the pipeline served for it
                      (from predictions_multih.csv + the calibrated width).
  * risk_divergence — the LLM risk layer and the numeric forecast disagree:
                      "high" risk while the forecast holds at/above the last
                      actual, or "low" risk while the forecast collapses.
GET /forecast/investigate runs only when triggered (or with ?force=1).

Agent design:
  * LangGraph StateGraph, ReAct via Groq native tool-calling (GROQ_MODEL).
  * Tools: forecast context (with conformal band + coverage), monthly actuals,
    walk-forward backtest errors, TreeSHAP driver lookup, Open-Meteo outlook,
    monthly weather history, NOAA ENSO status, NASA FIRMS hotspots, and a
    validated read-only PostgreSQL query pair (schema + SELECT).
  * Reflexion-lite: a failed tool call returns a structured error plus an
    instruction to state what went wrong before retrying; after two failures
    the tool is demoted ("do not call again this run").
  * Numeric guardrail (same contract as forecast_intelligence): production
    figures may only be quoted verbatim from tool outputs.

Result is TTL-cached 6h (same policy as the intelligence layer).
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import TypedDict

import httpx
import pandas as pd

log = logging.getLogger("epms-forecast-investigator")

_FORECAST_DIR = Path(__file__).parent / "forecast"

# Per-estate artifact directories (mirrors forecast_router.ESTATE_DIRS). Every
# tool resolves its files through _paths() so an investigation launched for EC
# reads EC's history, EC's backtest and EC's weather — previously the forecast
# was estate-bound but every piece of evidence came from k3.
DEFAULT_ESTATE = "k3"
_ESTATE_DIRS = {"k3": _FORECAST_DIR, "ec": _FORECAST_DIR / "EC"}


def _paths(estate: str | None = None) -> dict:
    est = (estate or DEFAULT_ESTATE).lower()
    d = _ESTATE_DIRS.get(est, _FORECAST_DIR)
    return {
        "estate": est,
        "dir": d,
        "multih": d / "predictions_multih.csv",
        "estate_csv": d / "features_estate_monthly.csv",
        "weather": d / "weather_nasa_power_history.csv",
        "joblib": d / "monthly_model.joblib",
    }

_HTTP_TIMEOUT = 12.0
_MAX_ITERATIONS = 8          # ReAct loop budget
_MAX_TOOL_CHARS = 3500       # cap any single tool observation
_TTL_SECONDS = 6 * 3600

_CACHE: dict = {"data": None, "ts": None, "key": None}


# ── small helpers ─────────────────────────────────────────────────────────────

def _clip(obj) -> str:
    s = json.dumps(obj, default=str)
    return s if len(s) <= _MAX_TOOL_CHARS else s[: _MAX_TOOL_CHARS] + '… (truncated)"}'


def _conformal_meta(estate: str | None = None) -> dict:
    import joblib
    try:
        return joblib.load(_paths(estate)["joblib"]).get("conformal") or {}
    except Exception:
        return {}


def _step1_band_check(estate: str | None = None) -> dict:
    """Was the latest scoreable actual inside the 1-step conformal band?"""
    p = _paths(estate)
    if not p["multih"].exists():
        return {"available": False, "reason": "no multi-horizon backtest file"}
    s1 = pd.read_csv(p["multih"]).query("step == 1").sort_values("month")
    if s1.empty:
        return {"available": False, "reason": "no step-1 rows"}
    conf = _conformal_meta(estate)
    w1 = float((conf.get("served_widths") or {}).get(1, 0)) or None
    last = s1.iloc[-1]
    out = {
        "available": True,
        "month": str(last["month"]),
        "actual": round(float(last["actual"])),
        "predicted": round(float(last["pred"])),
        "error": round(float(last["pred"]) - float(last["actual"])),
    }
    if w1:
        out["lower"] = round(float(last["pred"]) - w1)
        out["upper"] = round(float(last["pred"]) + w1)
        out["breach"] = bool(abs(out["error"]) > w1)
        out["band"] = "90% conformal"
    return out


# ── tool implementations (each returns a JSON-serialisable dict) ─────────────

async def _t_forecast_context(args, ctx):
    fc = await asyncio.to_thread(ctx["forecast_loader"])
    return {
        "model": fc.get("model_name"),
        "trained_through": fc.get("trained_through"),
        "interval": fc.get("interval"),
        "anchor_last_actual": fc.get("anchor"),
        "forecast": [{k: f[k] for k in ("month", "ensemble", "lower", "upper")}
                     for f in fc.get("forecast", [])],
        # smape_1step_pct is 1-step-ahead only; smape_h1_3_pct is the pooled
        # multi-horizon figure that describes the 3-month product actually
        # served. Both are named explicitly so the agent cannot mistake the
        # smaller one for the overall accuracy.
        "backtest": {"smape_h1_3_pct": fc.get("headline_smape"),
                     "smape_per_horizon": (fc.get("accuracy_multih") or {}).get("per_horizon"),
                     "smape_1step_pct": fc.get("smape_1step", fc.get("smape")),
                     "mase": fc.get("mase"),
                     "n_folds": fc.get("n_folds")},
    }


async def _t_monthly_history(args, ctx):
    n = int(args.get("months", 18))
    df = pd.read_csv(_paths(ctx.get("estate"))["estate_csv"])
    cols = [c for c in ("month", "bunches_total", "n_harvest_days",
                        "is_underrecorded", "exclude_from_model") if c in df.columns]
    return {"months": df[cols].tail(n).to_dict("records"),
            "note": "bunches_total is the cleaned target; is_underrecorded=1 means "
                    "seasonally imputed; exclude_from_model=1 months are not scored."}


async def _t_backtest_errors(args, ctx):
    n = int(args.get("months", 12))
    est = ctx.get("estate")
    s1 = (pd.read_csv(_paths(est)["multih"])
          .query("step == 1").sort_values("month").tail(n))
    conf = _conformal_meta(est)
    w1 = float((conf.get("served_widths") or {}).get(1, 0)) or None
    rows = []
    for _, r in s1.iterrows():
        err = float(r["pred"]) - float(r["actual"])
        rows.append({"month": str(r["month"]), "actual": round(float(r["actual"])),
                     "predicted": round(float(r["pred"])), "error": round(err),
                     "outside_90pct_band": bool(w1 and abs(err) > w1)})
    return {"one_step_ahead": rows, "band_half_width": w1}


async def _t_shap_drivers(args, ctx):
    fc = await asyncio.to_thread(ctx["forecast_loader"])
    months = (fc.get("drivers") or {}).get("months") or []
    want = args.get("month")
    if want:
        hit = [m for m in months if m["month"] == want]
        if not hit:
            return {"error": f"month {want} not in forecast window",
                    "available_months": [m["month"] for m in months]}
        return hit[0]
    return {"months": months}


async def _t_weather_outlook(args, ctx):
    from forecast_intelligence import estate_geo, fetch_weather_signal
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as c:
        return await fetch_weather_signal(c, estate_geo(ctx.get("estate")))


async def _t_weather_history(args, ctx):
    n = int(args.get("months", 8))
    w = pd.read_csv(_paths(ctx.get("estate"))["weather"], parse_dates=["date"])
    w["month"] = w["date"].dt.to_period("M").astype(str)
    m = (w.groupby("month")
          .agg(rain_mm=("rainfall_mm", "sum"), temp_c=("temp_mean_c", "mean"),
               humidity_pct=("humidity_pct", "mean"),
               water_balance_mm=("water_balance_mm", "sum"))
          .round(1).tail(n))
    return {"monthly": m.reset_index().to_dict("records"),
            "note": "NASA POWER history at the estate point. FFB responds to "
                    "moisture stress with ~5-7 month lag (flowering) and ~1 month "
                    "lag (harvest access)."}


async def _t_enso(args, ctx):
    from forecast_intelligence import fetch_enso_signal
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as c:
        return await fetch_enso_signal(c)


async def _t_firms(args, ctx):
    from forecast_intelligence import estate_geo, fetch_firms_signal
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as c:
        return await fetch_firms_signal(c, estate_geo(ctx.get("estate")))


async def _t_db_schema(args, ctx):
    def run():
        from sqlalchemy import text
        from config import engine, DB_SCHEMA
        q = text(
            "SELECT table_name, string_agg(column_name || ' ' || data_type, ', ' "
            "ORDER BY ordinal_position) AS columns "
            "FROM information_schema.columns WHERE table_schema = :s "
            "GROUP BY table_name ORDER BY table_name LIMIT 40")
        with engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(q, {"s": DB_SCHEMA})]
    return {"tables": await asyncio.to_thread(run)}


async def _t_db_query(args, ctx):
    sql = str(args.get("sql", ""))
    def run():
        from sqlalchemy import text
        import sql_validator
        from config import engine
        ok, err = sql_validator.validate(sql)
        if not ok:
            raise ValueError(f"SQL rejected: {err}")
        limited = sql_validator.ensure_limit(sql, 30)
        with engine.connect() as conn:
            res = conn.execute(text(limited))
            cols = list(res.keys())
            return {"columns": cols,
                    "rows": [list(map(str, r)) for r in res.fetchall()]}
    return await asyncio.to_thread(run)


_TOOLS = {
    "get_forecast_context": dict(
        fn=_t_forecast_context,
        desc="The served ML forecast: per-month ensemble + 90% conformal bounds, "
             "interval calibration/coverage metadata, last actual (anchor), and "
             "backtest accuracy. Start here.",
        params={}),
    "get_monthly_history": dict(
        fn=_t_monthly_history,
        desc="Recent monthly production actuals (cleaned bunches_total) with "
             "data-quality flags (under-recorded / excluded months).",
        params={"months": {"type": "integer", "description": "how many recent months (default 18)"}}),
    "get_backtest_errors": dict(
        fn=_t_backtest_errors,
        desc="Recent 1-step-ahead walk-forward forecast errors of this exact "
             "pipeline, flagged when the miss fell outside the 90% conformal band. "
             "Use to see whether an anomaly is new or chronic.",
        params={"months": {"type": "integer", "description": "how many recent months (default 12)"}}),
    "get_shap_drivers": dict(
        fn=_t_shap_drivers,
        desc="TreeSHAP attribution for a forecast month: which inputs push the "
             "LightGBM member up or down vs its baseline.",
        params={"month": {"type": "string", "description": "forecast month YYYY-MM (omit for all)"}}),
    "get_weather_outlook": dict(
        fn=_t_weather_outlook,
        desc="Live 14-day rainfall/temperature outlook at the estate point (Open-Meteo).",
        params={}),
    "get_weather_history": dict(
        fn=_t_weather_history,
        desc="Recent monthly rainfall, temperature, humidity, water balance at the "
             "estate (NASA POWER). Key lags: flowering 5-7 months, harvest access 1 month.",
        params={"months": {"type": "integer", "description": "how many recent months (default 8)"}}),
    "get_enso_status": dict(
        fn=_t_enso,
        desc="Current ENSO alert status from NOAA CPC (El Nino/La Nina/neutral).",
        params={}),
    "get_fire_hotspots": dict(
        fn=_t_firms,
        desc="VIIRS fire activity around this estate over the last 5 days (NASA FIRMS): "
             "detection count, total and peak Fire Radiative Power in MW, and distance "
             "to the nearest fire. FRP measures burn intensity, so a high count of small "
             "detections and one large fire front are distinguishable. Proxy for "
             "haze/labour disruption.",
        params={}),
    "get_db_schema": dict(
        fn=_t_db_schema,
        desc="List tables and columns of the live EPMS PostgreSQL database "
             "(read-only). Call before query_production_db.",
        params={}),
    "query_production_db": dict(
        fn=_t_db_query,
        desc="Run one read-only SELECT against the EPMS PostgreSQL database "
             "(validated; auto-LIMIT 30). Use for operational detail the CSVs "
             "lack (daily records, blocks, work orders).",
        params={"sql": {"type": "string", "description": "a single SELECT statement"}}),
}


def _neutral_tools() -> list[dict]:
    """Provider-neutral (plain JSON Schema) definitions — see tool_schemas.py."""
    from tool_schemas import make_tool
    return [make_tool(name, t["desc"], t["params"]) for name, t in _TOOLS.items()]


def _tool_schemas():
    from tool_schemas import to_openai_tools
    return to_openai_tools(_neutral_tools())


# ── trigger evaluation ───────────────────────────────────────────────────────

async def evaluate_triggers(forecast_loader, intelligence: dict | None,
                            estate: str | None = None) -> dict:
    """Decide whether an investigation is warranted. Cheap: CSV + cached data
    only — no LLM, no network beyond what is already cached."""
    band = _step1_band_check(estate)
    triggers = []
    if band.get("available") and band.get("breach"):
        triggers.append({
            "type": "band_breach",
            "detail": f"Actual {band['actual']:,} for {band['month']} fell outside "
                      f"the 90% conformal band {band['lower']:,}–{band['upper']:,}.",
        })

    divergence = None
    if intelligence:
        try:
            fc = await asyncio.to_thread(forecast_loader)
            nxt = (fc.get("forecast") or [{}])[0]
            anchor = (fc.get("anchor") or {}).get("value")
            risk = intelligence.get("risk_level")
            if risk == "high" and nxt.get("ensemble") and anchor \
                    and nxt["ensemble"] >= anchor:
                divergence = (f"Risk layer says HIGH risk but the numeric forecast "
                              f"({nxt['ensemble']:,} for {nxt.get('month')}) holds "
                              f"at/above the last actual ({anchor:,}).")
            elif risk == "low" and nxt.get("ensemble") and anchor \
                    and nxt["ensemble"] < 0.85 * anchor:
                divergence = (f"Risk layer says LOW risk but the numeric forecast "
                              f"({nxt['ensemble']:,} for {nxt.get('month')}) is "
                              f">15% below the last actual ({anchor:,}).")
        except Exception as exc:  # forecast unavailable → no divergence check
            log.warning("[investigator] divergence check skipped: %s", exc)
    if divergence:
        triggers.append({"type": "risk_divergence", "detail": divergence})

    return {"triggered": bool(triggers), "triggers": triggers, "band_check": band}


# ── LangGraph agent ──────────────────────────────────────────────────────────

class InvestigatorState(TypedDict):
    messages: list
    iterations: int
    failures: dict
    evidence: list


from prompts import load_prompt

_SYSTEM_PROMPT = load_prompt("investigator_system")


def _serialise_assistant(result) -> dict:
    """llm_client.LLMResult -> OpenAI-style assistant dict (the state format)."""
    out = {"role": "assistant", "content": result.text}
    tool_calls = getattr(result.message, "tool_calls", None) or []
    if tool_calls:
        out["tool_calls"] = [{
            "id": tc.get("id") or f"call_{i}", "type": "function",
            "function": {"name": tc["name"],
                         "arguments": json.dumps(tc.get("args") or {})},
        } for i, tc in enumerate(tool_calls)]
    return out


def _build_graph(ctx):
    from langgraph.graph import StateGraph, END
    import llm_client
    from config import INVESTIGATOR_MODEL

    model_id = llm_client.resolve_model(INVESTIGATOR_MODEL, tier="main")

    def _call_model(messages, with_tools=True):

        def _once(tools_on):
            extra = {"reasoning_effort": "low"} if "gpt-oss" in model_id else None
            result = llm_client.chat(
                messages, model=model_id,
                temperature=0.2, max_tokens=1600,
                tools=_tool_schemas() if tools_on else None,
                tool_choice="auto" if tools_on else None,
                task="investigator", extra_kwargs=extra,
            )
            return _serialise_assistant(result)

        # llama/gpt-oss occasionally emit malformed tool-call JSON, or (once
        # tools are disabled for the forced-final turn) still hallucinate a
        # tool_calls-shaped generation referencing a tool that isn't in the
        # request -> Groq 400 json_validate_failed / tool_use_failed. Retry
        # twice, then degrade to a tool-less call so the run always yields a
        # text verdict instead of dying.
        _RETRYABLE = ("json_validate_failed", "tool_use_failed",
                      "was not in request.tools")
        last_exc = None
        for attempt in range(3):
            try:
                return _once(with_tools)
            except Exception as exc:
                if not any(marker in str(exc) for marker in _RETRYABLE):
                    raise
                last_exc = exc
                log.warning("[investigator] malformed/hallucinated tool call "
                            "(attempt %d/3), retrying", attempt + 1)
        if with_tools:
            log.warning("[investigator] falling back to tool-less final answer")
            try:
                return _once(False)
            except Exception as exc:
                if not any(marker in str(exc) for marker in _RETRYABLE):
                    raise
                last_exc = exc
        log.error("[investigator] model kept hallucinating tool calls with "
                  "tools disabled; synthesising fallback verdict")
        fallback = {
            "cause_category": "unknown_external",
            "root_cause": "Investigator could not produce a verdict: the model "
                          "repeatedly hallucinated tool calls after the tool "
                          "budget was exhausted.",
            "narrative": "The investigation ran out of usable evidence because "
                         "the model kept attempting tool calls even after tools "
                         "were disabled for the final turn. No root cause could "
                         "be determined from the available evidence.",
            "confidence": 0.0,
            "actions": ["Re-run the investigation; if this recurs, check the "
                        "model's tool-calling stability."],
            "key_evidence": [],
        }
        return {"role": "assistant", "content": json.dumps(fallback)}

    async def agent(state: InvestigatorState):
        state["iterations"] += 1
        # Past the budget: force a final answer with tools disabled.
        force_final = state["iterations"] > _MAX_ITERATIONS
        msgs = list(state["messages"])
        if force_final:
            msgs.append({"role": "user",
                         "content": "Tool budget exhausted. Produce your final "
                                    "JSON verdict now from the evidence you have."})
        msg_dict = await asyncio.to_thread(_call_model, msgs, not force_final)
        state["messages"].append(msg_dict)
        return state

    async def tools(state: InvestigatorState):
        calls = state["messages"][-1].get("tool_calls") or []
        for tc in calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):  # model may send "null" or a scalar
                args = {}
            t0 = time.time()
            from tracing import span as _span, set_attrs as _set
            with _span(f"tool:{name}", kind="TOOL",
                       attributes={"tool.name": name, "tool.args": args}) as sp:
                if name not in _TOOLS:
                    content = {"error": f"unknown tool {name}",
                               "reflexion": "Use only the provided tools."}
                    ok = False
                elif state["failures"].get(name, 0) >= 2:
                    content = {"error": f"{name} disabled after repeated failures",
                               "reflexion": "This tool failed twice; use another "
                                            "source of evidence."}
                    ok = False
                else:
                    try:
                        content = await _TOOLS[name]["fn"](args, ctx)
                        ok = True
                    except Exception as exc:
                        state["failures"][name] = state["failures"].get(name, 0) + 1
                        content = {"error": str(exc)[:400],
                                   "reflexion": "State briefly why this failed and "
                                                "correct your next step."}
                        ok = False
                _set(sp, {"tool.ok": ok, "output.value": content})
            state["evidence"].append({
                "tool": name, "args": args, "ok": ok,
                "elapsed_ms": int((time.time() - t0) * 1000),
                "observation": (content if ok else content.get("error")),
            })
            state["messages"].append({"role": "tool", "tool_call_id": tc["id"],
                                      "content": _clip(content)})
        return state

    def route(state: InvestigatorState):
        return "tools" if state["messages"][-1].get("tool_calls") else END

    g = StateGraph(InvestigatorState)
    g.add_node("agent", agent)
    g.add_node("tools", tools)
    g.set_entry_point("agent")
    g.add_conditional_edges("agent", route, {"tools": "tools", END: END})
    g.add_edge("tools", "agent")
    return g.compile()


def _repair_truncated_json(s: str):
    """Best-effort recovery of a max_tokens-truncated JSON object: close any
    open string, drop a dangling key/comma, close open brackets in order."""
    start = s.find("{")
    if start < 0:
        return None
    s = s[start:]
    stack, in_str, esc = [], False, False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    fixed = s + ('"' if in_str else "")
    fixed = re.sub(r",\s*$", "", fixed)
    fixed = re.sub(r":\s*$", ": null", fixed)
    for b in reversed(stack):
        fixed += "}" if b == "{" else "]"
    try:
        return json.loads(fixed)
    except Exception:
        return None


def _parse_verdict(text_out: str) -> dict:
    candidates = [text_out]
    m = re.search(r"\{.*\}", text_out, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    for cand in candidates:
        try:
            return json.loads(cand)
        except Exception:
            pass
    repaired = _repair_truncated_json(text_out)
    if isinstance(repaired, dict) and repaired.get("root_cause"):
        repaired.setdefault("cause_category", "unknown_external")
        repaired.setdefault("confidence", 0.0)
        repaired.setdefault("actions", [])
        repaired.setdefault("key_evidence", [])
        repaired["parse_note"] = "verdict repaired from truncated JSON"
        return repaired
    return {"cause_category": "unknown_external",
            "root_cause": "unparseable agent output", "narrative": text_out[:1200],
            "confidence": 0.0, "actions": [], "key_evidence": []}


def _case_task(case: dict) -> str:
    """Task message for a HISTORICAL labeled-eval case. The ground-truth label
    is deliberately withheld — the agent must find the cause itself."""
    facts = {k: case.get(k) for k in
             ("month", "actual", "predicted", "error", "error_pct",
              "band_lower", "band_upper", "band_breach", "n_harvest_days",
              "is_underrecorded")}
    return (
        "HISTORICAL CASE REVIEW (not a live trigger).\n"
        f"Anomaly month under investigation: {case['month']}.\n"
        f"Facts: {json.dumps(facts)}\n"
        "(actual = cleaned monthly production; predicted = the 1-step-ahead "
        "walk-forward ensemble forecast made before that month; band = 90% "
        "conformal interval. Nulls mean the month predates the backtest.)\n\n"
        "Investigate the root cause of the discrepancy (or state that there is "
        "none). Use the historical tools with windows covering this month; "
        "live outlook tools describe today and are irrelevant here."
    )


async def investigate(forecast_loader, intelligence: dict | None = None,
                      force: bool = False, case: dict | None = None,
                      estate: str | None = None) -> dict:
    """Run trigger evaluation and, if warranted (or force=True), the agent.

    case: a labeled historical anomaly (eval harness) — skips live triggers
    and investigates that month instead; the label is never shown to the agent.
    """
    from tracing import span as _span, set_attrs as _set

    if case is not None:
        trig = {"triggered": True,
                "triggers": [{"type": "historical_case", "detail": case["month"]}],
                "band_check": {"available": False, "reason": "historical case"}}
        task = _case_task(case)
    else:
        trig = await evaluate_triggers(forecast_loader, intelligence, estate)
        task = ("TRIGGERS:\n" + "\n".join(
            f"- [{t['type']}] {t['detail']}" for t in trig["triggers"])
            if trig["triggers"] else
            "No automatic trigger; a manual investigation was requested.")
        if intelligence:
            task += (f"\n\nRisk layer (for context): {intelligence.get('risk_level')} — "
                     f"{intelligence.get('risk_summary', '')}")
        band = trig.get("band_check") or {}
        if band.get("available"):
            task += f"\n\nLatest 1-step band check: {json.dumps(band)}"
        task += "\n\nInvestigate the root cause."

    result = {**trig, "generated_at": datetime.now().isoformat(timespec="seconds")}
    if not trig["triggered"] and not force:
        result["note"] = ("No trigger fired: last actual inside the conformal "
                          "band and no numeric/risk divergence. Pass ?force=1 "
                          "to investigate anyway.")
        return result

    graph = _build_graph({"forecast_loader": forecast_loader,
                          "estate": (estate or DEFAULT_ESTATE).lower()})
    state: InvestigatorState = {
        "messages": [{"role": "system", "content": _SYSTEM_PROMPT},
                     {"role": "user", "content": task}],
        "iterations": 0, "failures": {}, "evidence": [],
    }
    t0 = time.time()
    with _span("investigation", kind="AGENT", attributes={
            "input.value": task,
            "investigation.mode": "case" if case else "live",
            "investigation.case_id": (case or {}).get("id"),
            "investigation.triggers": [t["type"] for t in trig["triggers"]]}) as sp:
        final = await asyncio.wait_for(
            graph.ainvoke(state, {"recursion_limit": 2 * _MAX_ITERATIONS + 4}),
            timeout=300)
        verdict = _parse_verdict(final["messages"][-1].get("content") or "")
        _set(sp, {"output.value": verdict,
                  "investigation.confidence": verdict.get("confidence"),
                  "investigation.cause_category": verdict.get("cause_category"),
                  "investigation.iterations": final["iterations"]})

    result.update(
        report=verdict,
        evidence_log=[{k: e[k] for k in ("tool", "args", "ok", "elapsed_ms")}
                      for e in final["evidence"]],
        # full observations kept separately: the eval judge grades groundedness
        # against these, and Stage-3 traces already carry them per-span.
        evidence_observations=[
            {"tool": e["tool"], "ok": e["ok"],
             "observation": json.dumps(e["observation"], default=str)[:1200]}
            for e in final["evidence"]],
        iterations=final["iterations"],
        elapsed_s=round(time.time() - t0, 1),
    )
    return result


async def get_investigation(forecast_loader, intelligence_loader=None,
                            refresh: bool = False, force: bool = False,
                            estate: str | None = None) -> dict:
    """TTL-cached wrapper (6h), keyed on (estate, force) so a forced deep-dive does
    not mask the cheap no-trigger response, and one estate's investigation is never
    served under another's."""
    now = time.time()
    key = ((estate or DEFAULT_ESTATE).lower(), force)
    if (not refresh and _CACHE["data"] is not None and _CACHE["key"] == key
            and (now - (_CACHE["ts"] or 0)) < _TTL_SECONDS):
        return _CACHE["data"]
    intelligence = None
    if intelligence_loader is not None:
        try:
            intelligence = await intelligence_loader()
        except Exception as exc:
            log.warning("[investigator] intelligence unavailable: %s", exc)
    data = await investigate(forecast_loader, intelligence, force=force, estate=estate)
    _CACHE.update(data=data, ts=now, key=key)
    return data
