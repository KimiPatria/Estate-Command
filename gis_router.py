"""Estate Command - the GIS decision layer, mounted at /command.

A new full-page map on the existing EPMS AI app. Serves GeoJSON from
gis/ontology.py (files on disk, no PostGIS) plus the data readiness panel
that turns the demo itself into the requirements conversation (UC-15).

Nothing here writes to EPMS. The two databases are opened read-only by
config.engine, and the only database-derived artefact is the estate
footprint file, built offline by gis/build_footprints.py.

Two kinds of endpoint live here and the difference matters:

  * deterministic - the map, the metrics, the panels, the readiness register.
    These are the page. They never call a model and never fail because one is
    unavailable.

  * generated - /gis/ask, the briefs, the handover, the interview. These call
    Amazon Nova through llm_client and return {"available": false, "reason"}
    when they cannot. Every figure in their output is audited against the
    server-computed payload that produced it, and the audit rides on the
    response so the UI can mark anything unverified.

No model in this file can write anything. The only side effect any of them can
cause is moving the manager's map.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from config import FIRMS_MAP_KEY
from gis import (assumptions, briefings, copilot, decisions, environment, features, forecasts,
                 fire, layers, ontology, ops, readiness, reasoning, stores, vegetation)

log = logging.getLogger("epms-dashboard")

COMMAND_STATIC_DIR = Path(__file__).parent / "command_static"
COMMAND_DIST_DIR = COMMAND_STATIC_DIR / "dist"

router = APIRouter(tags=["estate-command"])

# The four forecasting models fit and score once per process. Doing it in the
# background at import means the first plan anyone opens is not the one that
# waits for them.
forecasts.warm()
# The stores models (buildplan_stores.md) warm on their own thread, so the rail's
# "to order" hint never waits on the forecasts and neither waits on the replay.
stores.warm()


@router.get("/command", include_in_schema=False)
def command_page():
    """The Estate Command page.

    Prefers the built bundle. The pre-build single file is kept as a fallback
    so a missing or half-written `dist/` serves a working page rather than a
    blank one, which is not a thing to discover in front of a client. Run
    `npm run build` in command_static/ after changing anything under src/.
    """
    built = COMMAND_DIST_DIR / "index.html"
    if built.exists():
        return FileResponse(built)
    log.warning("command_static/dist is missing; serving the pre-build page. "
                "Run `npm run build` in command_static/.")
    return FileResponse(COMMAND_STATIC_DIR / "command.html")


# -- the feature manifest --------------------------------------------------
#
# What this app can do, and what each feature would need to run on the
# client's real numbers. The manifest is the spine of the demo: the rail
# renders from it, every panel draws its requirements strip from it, and the
# data request writes itself out of it.


@router.get("/gis/features")
def gis_features(domain: str | None = Query(None, description="Filter to one domain")):
    """Every feature, grouped by operational domain, with coverage counts."""
    return features.catalogue(domain)


@router.get("/gis/features/panel")
def gis_feature_panel(panel: str = Query(..., description="Panel key, e.g. rotation")):
    """What one panel hosts and the union of data it needs.

    The requirements strip reads this. A panel carrying three features asks
    for the union of their requirements, deduplicated, because three features
    wanting the agronomy programme is still one extract to request.
    """
    view = features.by_panel(panel)
    if view is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"No feature registered for panel '{panel}'.",
                     "available": sorted({f["panel"] for f in features.FEATURES})},
        )
    return view


@router.get("/gis/data-request")
def gis_data_request():
    """The leave-behind: every extract we would need, and what each unlocks.

    Grouped by source system and ranked by how many features the extract
    unblocks, so the largest ask against a single system reads first. This is
    the document that would otherwise be written from memory after the
    meeting.
    """
    return features.data_request()


@router.get("/gis/estates")
def gis_estates():
    """One row per estate, saying exactly what geometry and history it has.

    The left rail reads this to decide which estates can be drilled into and
    which can only be outlined, so the UI never offers a zoom that leads to
    an empty map.
    """
    return {"estates": ontology.estate_index()}


@router.get("/gis/estates.geojson")
def gis_estates_geojson():
    return ontology.estates_geojson()


@router.get("/gis/blocks")
def gis_blocks(estate: str = Query(..., description="Estate code, e.g. EC")):
    """Block polygons for one estate.

    Returns 404, not an empty 200, for an estate with no shapefile - the same
    honest-N/A convention forecast_router.py uses, so the UI can tell "no
    geometry yet" apart from "estate with no blocks".
    """
    geo = ontology.blocks_geojson(estate)
    if geo is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": f"No block geometry for estate '{estate}'.",
                "reason": "no_shapefile",
                "degrades_to": "Estate outline hulled from harvester GPS.",
            },
        )
    log.info("[gis] served %d block(s) for estate=%s", len(geo["features"]), estate)
    return geo


@router.get("/gis/divisions")
def gis_divisions(estate: str = Query(...)):
    geo = ontology.divisions_geojson(estate)
    if geo is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"No division geometry for estate '{estate}'.",
                     "reason": "no_shapefile"},
        )
    return geo


@router.get("/gis/metrics")
def gis_metrics(
    estate: str = Query(...),
    metric: str = Query("bunches_per_ha"),
    month: str | None = Query(None, description="YYYY-MM, or omit for the full window"),
):
    """Per-block values for one metric, optionally for one month.

    Every metric is bunch-count based on purpose. See ontology.METRICS.
    """
    data = layers.metric_values(estate, metric, month)
    if data is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": f"No metrics for estate '{estate}' / metric '{metric}'.",
                "available_metrics": list(layers.METRICS),
            },
        )
    return data


@router.get("/gis/metrics/catalogue")
def gis_metric_catalogue(estate: str = Query("EC")):
    """Every metric the map can colour by, with its provenance.

    The UI badges each metric real / derived / synthetic from this, so a viewer
    always knows whether the number in front of them is the client's. Canopy
    vigour is the one entry whose badge is not fixed at import: it reads
    synthetic until a Sentinel-2 scene has been pulled, and real afterwards.
    """
    return layers.catalogue(estate)


@router.get("/gis/blocks/table")
def gis_block_table(estate: str = Query("EC"), month: str | None = Query(None)):
    """Every merged per-block value: real attributes plus derived and synthetic."""
    rows = layers.block_rows(estate, month)
    if rows is None:
        return JSONResponse(status_code=404,
                            content={"detail": f"No block data for '{estate}'."})
    return {"estate": estate.upper(), "month": month, "rows": rows}


@router.get("/gis/contract")
def gis_contract(estate: str = Query("EC")):
    """UC-08: production and forecast against committed volume."""
    return layers.contract_position(estate)


@router.get("/gis/vendors")
def gis_vendors(shortfall_t: float | None = Query(None)):
    """UC-09: vendors ranked by landed cost, with an allocation if asked."""
    return layers.vendor_ranking(shortfall_t)


@router.get("/gis/labour")
def gis_labour(estate: str = Query("EC")):
    """UC-12: harvester supply against demand."""
    return layers.labour_position(estate)


@router.get("/gis/replant")
def gis_replant(estate: str = Query("EC")):
    """UC-11: when the estate hits its replanting cliff. Real planting years."""
    return layers.replant_schedule(estate)


@router.get("/gis/rotation")
def gis_rotation(estate: str = Query("EC"), top: int = Query(25)):
    """UC-01: which blocks are due for harvest, and which gang covers them."""
    return layers.rotation_plan(estate, top)


@router.get("/gis/fire")
def gis_fire(
    estate: str = Query(...),
    scenario: str = Query("live", description="live | near_miss | severe"),
):
    """UC-06 fire triage: threat, exposure, mobilisation, and renderable layers.

    Hotspots and weather are live and real. Fire posts, water sources and crew
    numbers are synthetic, because EPMS records none of them. The payload
    labels which is which in `provenance_summary`, and the map colours them
    differently. Never collapse the two.
    """
    blocks = ontology.blocks_geojson(estate)
    if blocks is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": f"No block geometry for estate '{estate}'.",
                "reason": "no_shapefile",
                "degrades_to": "Estate-level exposure only, with no block triage.",
            },
        )
    if scenario not in fire.SCENARIOS:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unknown scenario '{scenario}'.",
                     "available": fire.SCENARIOS},
        )
    result = fire.assess(blocks, estate, FIRMS_MAP_KEY, scenario)
    log.info("[gis] fire %s/%s: %d clusters, %d threatening, %d blocks exposed",
             estate, scenario, result["hotspots"].get("clusters_total", 0),
             result["hotspots"]["threatening"], result["exposure"]["blocks"])
    return result


@router.get("/gis/fire/assets")
def gis_fire_assets(estate: str = Query("EC")):
    """Synthetic fire-response assets. Every feature is tagged synthetic."""
    assets = fire.load_assets()
    return {
        "assets": assets,
        "count": len(assets["features"]),
        "provenance": "synthetic",
        "note": ("EPMS has no table that can hold a fire post, water source or "
                 "standby crew. These are invented for the demo, placed against "
                 "the estate's real geometry, and labelled as invented."),
    }


@router.get("/gis/fire/scenarios")
def gis_fire_scenarios():
    return {"scenarios": fire.SCENARIOS}


@router.post("/gis/reload", include_in_schema=False)
def gis_reload():
    from gis.models import scheduler
    ontology.reload_ontology()
    layers.reload_layers()
    vegetation.reload_vegetation()
    environment.reload_environment()
    ops.reload_ops()
    forecasts.reload()
    stores.reload()
    scheduler.clear_cache()
    briefings.clear_caches()
    return {"reloaded": True, "estates": ontology.estate_index()}


# ── the operating rhythm: ledger, capacity, tomorrow's plan ────────────────
#
# Five operations as first-class menus, one row shape underneath. The ledger
# endpoints are deterministic joins over the generated work orders and never
# fail; the plan endpoints run the scheduler, which is arithmetic over the
# ledger and the assumption register, cached, and never fits anything inside
# a request. Every figure any of them returns is computed server-side, so
# the copilot and the artifact can quote it and the figure audit can find it.


@router.get("/gis/ops")
def gis_ops_summary():
    """Every operation's headline, for the rail: orders, adherence, carried."""
    return {"operations": {k: {kk: vv for kk, vv in v.items() if kk != "file"}
                           for k, v in ops.OPERATIONS.items()},
            "summary": ops.summary()}


@router.get("/gis/ops/capacity")
def gis_ops_capacity(
    date: str | None = Query(None, description="YYYY-MM-DD; default is tomorrow, 2025-05-24"),
    crew_type: str | None = Query(None, description="harvest | upkeep | spray | pest | transport"),
):
    """Who is on the roll, who is expected, and what they can do that day."""
    return ops.capacity(date, crew_type)


@router.get("/gis/ops/outcomes")
def gis_ops_outcomes(estate: str = Query("EC")):
    """Did it work: accepted plans read back against the ledger."""
    return ops.outcomes(estate)


@router.get("/gis/ops/{operation}/ledger")
def gis_ops_ledger(
    operation: str,
    crew: str | None = Query(None), block: str | None = Query(None),
    division: str | None = Query(None), date_from: str | None = Query(None),
    date_to: str | None = Query(None), status: str | None = Query(None),
    activity: str | None = Query(None),
    limit: int = Query(150, le=500), offset: int = Query(0, ge=0),
):
    """The ledger: what was worked, when, by whom, planned against actual."""
    out = ops.ledger(operation, crew, block, division, date_from, date_to, status,
                     activity, limit, offset)
    if not out.get("available") and "available_operations" in out:
        return JSONResponse(status_code=404, content=out)
    return out


@router.get("/gis/ops/{operation}/adherence")
def gis_ops_adherence(operation: str, top: int = Query(12, le=50)):
    """Planned against actual, by crew, block, division, month and driver."""
    out = ops.adherence(operation, top)
    if not out.get("available") and "reason" in out and operation not in ops.OPERATIONS:
        return JSONResponse(status_code=404, content=out)
    return out


@router.get("/gis/ops/{operation}/demand")
def gis_ops_demand(operation: str, date: str | None = Query(None)):
    """What is due on a date, how urgent, and what deferring it costs."""
    out = ops.demand(operation, date)
    if not out.get("available") and operation not in ops.OPERATIONS:
        return JSONResponse(status_code=404, content=out)
    return out


@router.get("/gis/ops/{operation}/plan")
def gis_ops_plan(operation: str, date: str | None = Query(None)):
    """Tomorrow's assignment. Every figure pre-computed; nothing fitted."""
    from gis.models import scheduler
    out = scheduler.plan(operation, date)
    if not out.get("available") and "available_operations" in out:
        return JSONResponse(status_code=404, content=out)
    return out


class PlanIn(BaseModel):
    date: str | None = None
    # {"G1-03": {"present": 17}, "G1-04": {"exclude": true}}
    crews: dict | None = None
    exclude_blocks: list[str] | None = None
    road_closed: list[str] | None = None
    rain_mm: float | None = None
    contiguity_bonus_pct: float | None = None
    division: str | None = None


@router.post("/gis/ops/{operation}/plan")
def gis_ops_replan(operation: str, body: PlanIn):
    """Re-run the plan with edited constraints: a gang short, a road closed,
    rain forecast, a block held back, or contiguity switched off to see what
    it costs."""
    from gis.models import scheduler
    overrides = {k: v for k, v in body.model_dump().items() if v not in (None, {}, [])}
    on = overrides.pop("date", None)
    out = scheduler.plan(operation, on, overrides)
    if not out.get("available") and "available_operations" in out:
        return JSONResponse(status_code=404, content=out)
    return out


@router.get("/gis/ops/{operation}/deferral")
def gis_ops_deferral(
    operation: str, block: str = Query(..., description="Block label, e.g. 32-51"),
    days: int = Query(1, ge=1, le=60), date: str | None = Query(None),
):
    """What waiting costs on one block, from the same arithmetic as the plan."""
    from gis.models import scheduler
    return scheduler.cost_of_deferral(operation, block, days, date)


# ── the assumption register ────────────────────────────────────────────────

@router.get("/gis/assumptions")
def gis_assumptions():
    """Every number a plan is priced with: value, unit, source, what uses it."""
    return assumptions.catalogue()


class AssumptionIn(BaseModel):
    key: str
    value: float
    actor: str = "demo user"
    note: str | None = None


@router.post("/gis/assumptions")
def gis_set_assumption(body: AssumptionIn):
    """Set one value. The next plan is priced at it; nothing is recomputed in
    the browser and nothing is written to EPMS."""
    from gis.models import scheduler
    try:
        row = assumptions.set_value(body.key, body.value, body.actor, body.note)
    except KeyError as exc:
        return JSONResponse(status_code=404, content={"detail": str(exc)})
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    forecasts.assumption_changed(body.key)
    stores.assumption_changed(body.key)
    scheduler.clear_cache()
    return {"set": True, "assumption": row}


@router.delete("/gis/assumptions")
def gis_reset_assumptions(key: str | None = Query(None)):
    """Back to the defaults, for one key or all of them."""
    from gis.models import scheduler
    n = assumptions.reset(key)
    forecasts.assumption_changed(key)
    stores.assumption_changed(key)
    scheduler.clear_cache()
    return {"reset": n, "key": key}


# ── tomorrow's outlook: the four forecasts behind the plan ─────────────────
#
# Rain, who turns up, how much gets done, and crew speeds (buildplan_ml.md).
# Every payload carries its sentence beside its figure, a trust grade from a
# backtest on days the model had not seen, and what would make it real.

@router.get("/gis/forecast/outlook")
def gis_forecast_outlook(date: str | None = Query(None, description="YYYY-MM-DD; default tomorrow, 2025-05-24")):
    """All four forecasts for one day, in plain words, with how far to trust each."""
    return forecasts.outlook(date)


@router.get("/gis/forecast/rain")
def gis_forecast_rain(date: str | None = Query(None)):
    """The chance of wash-off, heavy rain and a stop, learned from real forecasts."""
    return forecasts.rain_view(date)


@router.get("/gis/forecast/headcount")
def gis_forecast_headcount(date: str | None = Query(None),
                           crew_type: str | None = Query(None, description="harvest | upkeep | spray | pest")):
    """Who is likely to turn up, per crew, as a most likely figure and a range."""
    return forecasts.headcount_view(date, crew_type)


@router.get("/gis/forecast/work-done")
def gis_forecast_work_done(operation: str = Query("harvest"), date: str | None = Query(None)):
    """How much of the plan is likely to get done, and which blocks may need another day."""
    out = forecasts.work_done_view(operation, date)
    if not out.get("available") and operation not in forecasts.WORK_OPS:
        return JSONResponse(status_code=404, content=out)
    return out


@router.get("/gis/forecast/speeds")
def gis_forecast_speeds(operation: str = Query("weed"), date: str | None = Query(None)):
    """Learned crew speeds (or harvest block pace) against the book."""
    return forecasts.speeds_view(operation, date)


@router.get("/gis/forecast/accuracy")
def gis_forecast_accuracy():
    """Every model's backtest and recovery checks, with the rules they are held to."""
    return forecasts.accuracy()


# ── stores: reorder points, lead times and the MM record ───────────────────
#
# Safety stock on SAP MM records (buildplan_stores.md). Every payload carries
# its sentence beside its figure; the reorder points are used only where a
# twelve-month replay of recorded use shows they cost less than SAP's settings.

@router.get("/gis/stores")
def gis_stores(group: str | None = Query(None, description="FERT | AGCH | FUEL | SPARE, or fertiliser | agrochemical | fuel | parts"),
               date: str | None = Query(None, description="YYYY-MM-DD; default tomorrow, 2025-05-24")):
    """Every material: stock, reorder point, order-by date and what to do, in plain words."""
    return stores.overview(group, date)


@router.get("/gis/stores/material")
def gis_stores_material(matnr: str = Query(..., description="Material number, e.g. FE-001"),
                        date: str | None = Query(None)):
    """One material: the stock outlook, why the buffer is that size, the cost of the service level."""
    out = stores.material_view(matnr, date)
    if not out.get("available") and "materials" in out:
        return JSONResponse(status_code=404, content=out)
    return out


@router.get("/gis/stores/lead-times")
def gis_stores_lead_times(date: str | None = Query(None)):
    """Per supplier: quoted, usual and one-in-ten lead times, very-late chance, slow months."""
    return stores.lead_times(date)


@router.get("/gis/stores/ledger")
def gis_stores_ledger(matnr: str | None = Query(None), date_from: str | None = Query(None, alias="from"),
                      date_to: str | None = Query(None, alias="to"),
                      bwart: str | None = Query(None, description="Movement types, comma separated"),
                      limit: int = Query(100, le=1000), offset: int = Query(0, ge=0)):
    """The material documents, newest first, MB51-style."""
    return stores.ledger(matnr, date_from, date_to, bwart, limit, offset)


@router.get("/gis/stores/purchase-orders")
def gis_stores_purchase_orders(matnr: str | None = Query(None),
                               kind: str | None = Query(None, description="normal | rush | open"),
                               limit: int = Query(100, le=1000)):
    """Purchase order lines with what each actually took, newest first."""
    return stores.purchase_orders(matnr, kind, limit)


@router.get("/gis/stores/accuracy")
def gis_stores_accuracy():
    """The lead-time and consumption backtests, the policy replay, recovery checks and the rules."""
    return stores.accuracy()


@router.get("/gis/stores/document")
def gis_stores_document(matnr: str = Query(...), kind: str = Query("material_requisition"),
                        date: str | None = Query(None)):
    """The payload a requisition or a settings change is drafted from, so the window and the log agree."""
    if kind == "mrp_settings_change":
        return stores.settings_change_payload(matnr, date)
    return stores.requisition_payload(matnr, date)


# -- Phase 4.2 / 4.3: artifacts and the decision log -----------------------

class DecisionIn(BaseModel):
    estate: str = "EC"
    use_case: str
    title: str
    subject: str
    action: str                       # accepted | rejected | deferred
    evidence: list[str] = []
    artifact_kind: str | None = None
    artifact_payload: dict | None = None
    actor: str = "demo user"
    # The loop-closing fields a scheduled plan carries; see gis/decisions.py.
    due_date: str | None = None
    expected_effect: dict | None = None
    order_ref: list[str] | None = None


@router.post("/gis/decisions")
def gis_record_decision(body: DecisionIn):
    """Log an accept / reject / defer, drafting the artifact when accepted.

    The artifact is drafted, never sent. It names the EPMS approval table it
    would enter and stops. Both EPMS engines are read-only regardless.
    """
    artifact = None
    if body.action == "accepted" and body.artifact_kind:
        artifact = decisions.draft(body.artifact_kind, body.estate,
                                   body.artifact_payload or {})
        # The document is deterministic; the instruction text on it is written
        # by the model from that document. A failure here leaves the artifact
        # intact and unnarrated - never blocks the decision from being logged.
        if artifact:
            prose = briefings.artifact_prose(artifact, body.evidence)
            if prose:
                artifact["prose"] = prose
    order_ref = body.order_ref
    if artifact and not order_ref:
        order_ref = [l["order_ref"] for l in artifact.get("lines") or [] if l.get("order_ref")] or None
    row = decisions.record(
        estate=body.estate.upper(), use_case=body.use_case, title=body.title,
        subject=body.subject, action=body.action, evidence=body.evidence,
        artifact_kind=body.artifact_kind, artifact=artifact, actor=body.actor,
        due_date=body.due_date, expected_effect=body.expected_effect,
        order_ref=order_ref,
    )
    return {"recorded": True, "decision": row, "artifact": artifact}


@router.get("/gis/decisions")
def gis_decision_log(limit: int = Query(100), estate: str | None = Query(None)):
    """The audit view: every proposal and what a human did with it."""
    return decisions.history(limit, estate)


@router.delete("/gis/decisions", include_in_schema=False)
def gis_clear_decisions():
    """Reset the log between demo runs."""
    return {"cleared": decisions.clear()}


@router.get("/gis/artifact-kinds")
def gis_artifact_kinds():
    return {"kinds": decisions.ARTIFACT_KINDS}


# ── UC-15, the data readiness panel ---------------------------------------
#
# The register itself moved to gis/readiness.py when the interview needed to
# read it too. Everything below is HTTP.


@router.get("/gis/readiness")
def gis_readiness():
    """Per-capability data readiness - the discovery instrument (UC-15).

    Deliberately not hidden behind a toggle. When the client sees a red row
    they tell you what they actually have, in the room, unprompted - and the
    interview endpoints below turn that sentence into a recorded answer.
    """
    return readiness.catalogue()


@router.get("/gis/readiness/interview")
def gis_readiness_interview(
    capability: str = Query(..., description="Capability id, e.g. pest_watchlist"),
    refresh: bool = Query(False),
):
    """The questions worth asking the client about one capability."""
    return briefings.interview_questions(capability, refresh)


class AnswerIn(BaseModel):
    capability: str
    answer: str
    actor: str = "demo user"


@router.post("/gis/readiness/interview")
def gis_readiness_answer(body: AnswerIn):
    """Record what the client said, scored against the measurement.

    The proposal never overwrites the measured status. It rides beside it
    until someone does the verification the verdict names.
    """
    return briefings.score_answer(body.capability, body.answer, body.actor)


@router.delete("/gis/readiness/interview", include_in_schema=False)
def gis_clear_interview():
    """Reset the interview between demo runs. The register itself is static."""
    return {"cleared": readiness.clear()}


# -- The AI layer ----------------------------------------------------------
#
# Everything below reasons in language over the layers above. All of it
# degrades to {"available": false, "reason": ...}; none of it is load-bearing
# for the map, and the UI renders the deterministic panels either way.


class AskIn(BaseModel):
    question: str
    estate: str = "EC"
    # What the manager is looking at, so "this block" and "this month" resolve.
    view: dict | None = None
    # Prior Q&A pairs from the same chat, oldest first, so a follow-up
    # question resolves against what was already asked and answered.
    history: list[dict] | None = None


@router.post("/gis/ask")
def gis_ask(body: AskIn):
    """Ask the map. A tool-calling agent over every layer on this page.

    Returns the answer, the full tool trace behind it, a focus instruction for
    the map, and an audit of every figure in the answer against the tool
    results that produced it.
    """
    result = copilot.ask(body.question, body.estate, body.view, body.history)
    audit = result.get("figure_audit") or {}
    log.info("[gis] ask: %d tool call(s), %s, audit %s",
             len(result.get("trace") or []), result.get("model"),
             "clean" if audit.get("clean", True) else audit.get("unverified"))
    return result


@router.get("/gis/ask/examples")
def gis_ask_examples():
    return {"examples": copilot.EXAMPLES}


@router.get("/gis/blocks/brief")
def gis_block_brief(
    estate: str = Query("EC"),
    block_id: str = Query(..., description="Feature id, e.g. block:EC:5|373"),
    month: str | None = Query(None),
    refresh: bool = Query(False),
):
    """UC-03: the agronomist's read on one block, grounded in its own figures."""
    return briefings.block_brief(estate, block_id, month, refresh)


@router.get("/gis/fire/brief")
def gis_fire_brief(
    estate: str = Query("EC"),
    scenario: str = Query("live"),
    refresh: bool = Query(False),
):
    """UC-06: the duty officer's brief over a live fire assessment.

    The assessment is recomputed here rather than accepted from the client, so
    the brief can never be written over figures the browser edited.
    """
    blocks = ontology.blocks_geojson(estate)
    if blocks is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"No block geometry for estate '{estate}'.",
                     "reason": "no_shapefile"})
    if scenario not in fire.SCENARIOS:
        return JSONResponse(status_code=400,
                            content={"detail": f"Unknown scenario '{scenario}'.",
                                     "available": fire.SCENARIOS})
    assessment = fire.assess(blocks, estate, FIRMS_MAP_KEY, scenario)
    return briefings.fire_brief(assessment, estate, refresh)


@router.get("/gis/handover")
def gis_handover(estate: str = Query("EC"), refresh: bool = Query(False)):
    """UC-14: the shift handover note over the decision log and open positions."""
    return briefings.handover(estate, refresh)


# ── the seven features the manifest listed as planned ─────────────────────
#
# Each lives in its own module under gis/ with one entry point, position(),
# returning the same shape every other layer does: available, a sentence, the
# figures, the blocks that matter, provenance and caveat. Imported lazily so a
# module still being written cannot take the page down with it.


@router.get("/gis/loose-fruit")
def gis_loose_fruit(estate: str = Query("EC"), top: int = Query(12, le=50)):
    """Loose fruit recovery rate per block, from the export's own counts."""
    from gis import loose_fruit
    return loose_fruit.position(estate, top)


@router.get("/gis/herbicide")
def gis_herbicide(estate: str = Query("EC"), top: int = Query(12, le=50)):
    """Spraying round adherence and the herbicide issued against it."""
    from gis import herbicide
    return herbicide.position(estate, top)


@router.get("/gis/ffa")
def gis_ffa(estate: str = Query("EC"), top: int = Query(12, le=50)):
    """Free fatty acid against the delay between cutting and the mill."""
    from gis import ffa
    return ffa.position(estate, top)


@router.get("/gis/cutting-interval")
def gis_cutting_interval(estate: str = Query("EC"), top: int = Query(12, le=50)):
    """Realised cutting interval per block against its target round."""
    from gis import cutting_interval
    return cutting_interval.position(estate, top)


@router.get("/gis/abw-trend")
def gis_abw_trend(estate: str = Query("EC"), top: int = Query(12, le=50)):
    """Average bunch weight by block and month, from the weighbridge."""
    from gis import abw_trend
    return abw_trend.position(estate, top)


@router.get("/gis/pest-warning")
def gis_pest_warning(estate: str = Query("EC"), top: int = Query(12, le=50)):
    """Canopy anomaly against the census: where vigour fell before a count did."""
    from gis import pest_warning
    return pest_warning.position(estate, top)


@router.get("/gis/collection")
def gis_collection(estate: str = Query("EC"), top: int = Query(12, le=50)):
    """Collection point coverage and the wait at the platform."""
    from gis import collection
    return collection.position(estate, top)


@router.get("/gis/vegetation")
def gis_vegetation(estate: str = Query("EC")):
    """UC-03: the Sentinel-2 scene behind the canopy layer, and its coverage.

    Real measurement over the client's own polygons, taken from the free
    Copernicus archive. Returns available:false with the command to run when
    no scene has been pulled for the estate.
    """
    return vegetation.summary(estate)


@router.get("/gis/transport")
def gis_transport(estate: str = Query("EC")):
    """The Transport domain: haulage, fleet reliability and quality decay.

    Built on the trip ledger, which joins the EPMS field count to the mill
    weighbridge ticket. No single client system joins those today, which is
    why none of this is currently visible to them.
    """
    return layers.transport_position(estate)


@router.get("/gis/shrinkage")
def gis_shrinkage(top: int = Query(15, le=50)):
    """Field-to-mill shrinkage detection, with its own validation.

    Returns the flagged trips, where the losses concentrate, and the
    injected-versus-recovered score against the anomaly cohort planted in the
    generated ledger. The score is part of the answer, not a footnote: a
    detector run over generated data is only worth anything if it can be
    measured against a known truth.
    """
    from gis.models import shrinkage
    return shrinkage.assess(top)


@router.get("/gis/pest")
def gis_pest(estate: str = Query("EC"), top: int = Query(12, le=50)):
    """UC-04: the Pest & Disease domain. Entirely invented, and it says so.

    No table in the 138-table EPMS schema can hold an infected palm, and the
    only proxy in the export is degenerate at 0.25% mean deduction. This is
    the largest blind spot in the client's operation.
    """
    return layers.pest_position(estate, top)


@router.get("/gis/nutrition")
def gis_nutrition(estate: str = Query("EC"), top: int = Query(12, le=50)):
    """Fertiliser applied against the agronomy programme, in kg per palm."""
    return layers.nutrition_position(estate, top)


@router.get("/gis/roads")
def gis_roads(estate: str = Query("EC"), top: int = Query(12, le=50)):
    """Road condition, the grading backlog, and what it costs in haulage."""
    return layers.roads_position(estate, top)


@router.get("/gis/clusters")
def gis_clusters(estate: str = Query("EC")):
    """Agronomic underperformance clustering.

    Four of the seven input dimensions are real measurements. Clusters are
    named by which axes are extreme, never by root cause: the nutrition feed
    is deliberately confounded, because weak blocks are prescribed more
    fertiliser, so a causal reading of this layer is backwards.
    """
    from gis.models import clusters
    return clusters.assess(estate)


@router.get("/gis/productivity")
def gis_productivity(estate: str = Query("EC"), top: int = Query(12, le=50)):
    """Crew productivity and the adjusted daily target per block.

    Block conditions are real: terrain from the Copernicus DEM, palm age from
    the client's planting years, bunch density from recorded harvest. The
    per-worker rows are generated down from real block totals.
    """
    from gis.models import productivity
    return productivity.assess(estate, top)


@router.get("/gis/forecast/lagged")
def gis_lagged_forecast(estate: str = Query("EC")):
    """Lagged yield model, and the history it would need to be real.

    Fitted on 36 months of generated history because the export carries under
    five. The rainfall driving it is real. The payload states plainly what is
    demonstrated and what is merely rediscovered.
    """
    from gis.models import lagged_forecast
    return lagged_forecast.assess(estate)


@router.get("/gis/terrain")
def gis_terrain(estate: str = Query("EC")):
    """Real terrain over the client's own polygons, from the Copernicus DEM.

    Free, public, and needs nothing from the client, exactly like the canopy
    layer. Returns available:false with the command to run when no DEM has
    been pulled for the estate.
    """
    return environment.terrain_summary(estate)


@router.get("/gis/rainfall")
def gis_rainfall(estate: str = Query("EC")):
    """Multi-year rainfall over the estate, from the Open-Meteo archive.

    The feed the lagged yield model reads at 20-24 months. Free and keyless,
    so rainfall is struck off the data request rather than asked for.
    """
    return environment.rainfall_summary(estate)


@router.get("/gis/metrics/compare")
def gis_metrics_compare(
    estate: str = Query("EC"),
    metric_a: str = Query(...),
    metric_b: str = Query(...),
    month: str | None = Query(None),
):
    """Do two metrics agree about which blocks are the problem?

    Rank correlation plus the blocks in the bottom fifth of both. Deterministic;
    no model is involved. It exists because "the satellite and the harvest
    records disagree" is a finding, and eyeballing two lists is not a method.
    """
    return layers.compare_metrics(estate, metric_a, metric_b, month)


@router.get("/gis/ai/status")
def gis_ai_status():
    """What is wired, so the page can say which model is answering."""
    info = reasoning.model_info()
    return {
        "provider": info.get("provider"),
        "models": {"reasoning": info.get("main"), "fast": info.get("fast")},
        "features": {
            "ask_the_map": {"model": info.get("main"),
                            "tools": len(copilot._TOOLS)},
            "block_brief": {"model": info.get("fast")},
            "fire_brief": {"model": info.get("main")},
            "handover": {"model": info.get("main")},
            "artifact_prose": {"model": info.get("fast")},
            "readiness_interview": {"model": info.get("fast")},
        },
        "guardrail": ("Every figure in generated text is checked against the "
                      "tool results behind it. Unverified figures are reported, "
                      "not hidden."),
        "note": "No model can write to EPMS. Both engines are opened read-only.",
    }


@router.post("/gis/ai/reset", include_in_schema=False)
def gis_ai_reset():
    """Drop the briefing caches, for a demo run that wants fresh text."""
    return {"cleared": briefings.clear_caches()}
