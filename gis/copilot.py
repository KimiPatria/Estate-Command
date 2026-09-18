"""Ask the map - a tool-calling agent over the Estate Command layers.

The manager types a question at the map and gets an answer computed from the
estate's own data, with the map moved to show it. Same ReAct shape as
forecast_investigator.py, deliberately smaller: a plain loop rather than a
LangGraph state machine, because there is no reflexion, no judge and no
conformal band to defend - just call the layer, quote the layer.

Three rules hold this together:

  * The model never computes. Every tool returns figures already computed by
    layers.py / fire.py / vegetation.py, and the prompt forbids deriving new
    ones. The trace is returned to the UI so a viewer can check each claim
    against the call that produced it.

  * Every tool result carries its own provenance string. "synthetic" is not a
    footnote in the system prompt, it rides on the data, in every result, so
    the model cannot lose track of which half of the estate is invented.

  * The agent has no write tools and the databases are read-only. The only
    side effect it can cause is moving the map, via focus_map.

Tool results are clipped hard. A question about 291 blocks must not put 291
blocks in the context window; query_blocks ranks and returns the top slice.
"""

import json
import logging
import re
import time

from gis import (decisions, fire, layers, ontology, ops, readiness, reasoning,
                 vegetation)
from prompts import load_prompt
from tool_schemas import make_tool, to_openai_tools

log = logging.getLogger("estate-command.copilot")

MAX_TURNS = 6          # model turns, not tool calls
MAX_ROWS = 15          # rows any single tool may return
DEFAULT_ESTATE = "EC"
MAX_HISTORY_TURNS = 6  # prior Q&A pairs from the same chat kept as context


# ── tool implementations ───────────────────────────────────────────────────
#
# Each returns a plain dict. The `provenance` key is mandatory: it is what
# stops the model presenting a generated cost as the client's own.

def _t_estate_overview(args, ctx) -> dict:
    estate = ctx["estate"]
    row = next((e for e in ontology.estate_index()
                if e["estate_code"] == estate), None)
    mo = layers.months(estate)
    cat = layers.catalogue(estate)
    return {
        "estate": estate,
        "blocks_with_geometry": (row or {}).get("blocks"),
        "planted_ha": (row or {}).get("planted_ha"),
        "blocks_with_harvest": (row or {}).get("blocks_with_harvest"),
        "harvest_window": (row or {}).get("harvest_window"),
        "caveat": (row or {}).get("caveat"),
        "recorded_months": mo["observed"],
        "forecast_months": mo["forward"],
        "metrics_available": [{"key": m["key"], "label": m["label"],
                               "provenance": m["provenance"]}
                              for m in cat["metrics"]],
        "satellite_scene": cat.get("satellite"),
        "provenance": "real: the client's ArcGIS export and EPMS harvest records",
    }


def _t_query_blocks(args, ctx) -> dict:
    estate = ctx["estate"]
    metric = args.get("metric") or "bunches_per_ha"
    if metric not in layers.METRICS:
        return layers.unknown_metric(metric)
    month = args.get("month") or None
    rows = layers.block_rows(estate, month)
    if rows is None:
        return {"error": f"No block data for estate {estate!r}."}

    lo, hi = args.get("min_value"), args.get("max_value")
    picked = [r for r in rows if isinstance(r.get(metric), (int, float))]
    if isinstance(lo, (int, float)):
        picked = [r for r in picked if r[metric] >= lo]
    if isinstance(hi, (int, float)):
        picked = [r for r in picked if r[metric] <= hi]

    ascending = (args.get("order") or "lowest") == "lowest"
    picked.sort(key=lambda r: r[metric], reverse=not ascending)
    limit = min(int(args.get("limit") or 10), MAX_ROWS)

    fields = ("block_label", "division_code", "planted_year", "palm_age_years",
              "planted_ha", "bunches", "bunches_per_ha", "peer_index",
              "ndre", "ndre_anomaly", "ripeness_pressure", "deduction_rate")
    out = []
    for r in picked[:limit]:
        row = {f: r.get(f) for f in fields}
        row[metric] = r.get(metric)
        out.append(row)

    values = [r[metric] for r in picked]
    return {
        "metric": metric,
        "label": layers.metric_label(metric, estate),
        "provenance": layers.metric_provenance(metric, estate),
        "month": month or "full recorded window",
        "order": "lowest first" if ascending else "highest first",
        "matched_blocks": len(picked),
        "of_total": len(rows),
        "distribution": ({"min": round(min(values), 4),
                          "median": round(sorted(values)[len(values) // 2], 4),
                          "max": round(max(values), 4)} if values else None),
        "rows": out,
        "note": "Blocks with no value for this metric are excluded, not zeroed.",
    }


def _t_compare_metrics(args, ctx) -> dict:
    return layers.compare_metrics(
        ctx["estate"], args.get("metric_a"), args.get("metric_b"),
        args.get("month") or None, 8)


def _t_block_detail(args, ctx) -> dict:
    from gis import briefings
    label = str(args.get("block_label") or "").strip()
    rows = layers.block_rows(ctx["estate"], args.get("month") or None)
    row = next((r for r in (rows or []) if str(r["block_label"]) == label), None)
    if row is None:
        return {"error": f"No block labelled {label!r} on estate {ctx['estate']}.",
                "hint": "Block labels look like '32-51'. Use query_blocks to list them."}
    return briefings.block_grounding(ctx["estate"], row["block_id"],
                                     args.get("month") or None)


def _t_contract(args, ctx) -> dict:
    d = layers.contract_position(ctx["estate"])
    return {
        "months": d["months"], "worst_forward_month": d["worst_forward_month"],
        "first_deficit_month": d["first_deficit_month"],
        "months_short_at_p50": d["months_in_p50_deficit"],
        "months_short_at_p10": d["months_in_p10_deficit"],
        "provenance": d["provenance"], "note": d["note"],
    }


def _t_vendors(args, ctx) -> dict:
    """Vendor ranking, always costed.

    An uncosted ranking is a trap: asked what covering a shortfall would cost,
    a model that has no total in front of it invents one by multiplying tonnes
    by a per-kilo price. Rather than rely on it passing the right argument on a
    second call - which Nova reliably does not do - the tool falls back to the
    estate's own worst forward gap and says so. There is then always a real
    total to quote, and nothing to derive.
    """
    shortfall = args.get("shortfall_t")
    source = "given in the question"
    if not shortfall:
        worst = (layers.contract_position(ctx["estate"])
                 .get("worst_forward_month") or {})
        gap = worst.get("gap_t")
        if isinstance(gap, (int, float)) and gap < 0:
            shortfall = abs(round(gap, 1))
            source = (f"not given, so the worst forward month was used: "
                      f"{worst.get('month')}, short {shortfall} t at P50")
        else:
            source = "no shortfall given and no forward month is in deficit"
    d = layers.vendor_ranking(shortfall)
    d["vendors"] = (d.get("vendors") or [])[:MAX_ROWS]
    d["shortfall_source"] = source
    return d


def _t_rotation(args, ctx) -> dict:
    d = layers.rotation_plan(ctx["estate"], min(int(args.get("top") or 10), MAX_ROWS))
    # by_gang is a mapping keyed by gang code; take the busiest few rather than
    # every gang on the estate.
    gangs = sorted((d.get("by_gang") or {}).items(),
                   key=lambda kv: -(kv[1].get("blocks") or 0)
                   if isinstance(kv[1], dict) else 0)[:8]
    out = {k: v for k, v in d.items() if k != "by_gang"}
    out["busiest_gangs"] = dict(gangs)
    return out


def _t_labour(args, ctx) -> dict:
    """Labour position, aggregated by month.

    The underlying rows are per division per month - 77 of them for EC, which
    is a wall of numbers no answer needs and every one of which costs context.
    The monthly deficit series is what the question is actually about.
    """
    d = layers.labour_position(ctx["estate"])
    rows = d.get("rows") or []
    return {
        "estate": d.get("estate"),
        "deficit_by_month": d.get("deficit_by_month"),
        "worst_month": d.get("worst_month"),
        "worst_deficit": d.get("worst_deficit"),
        "harvesters_on_roll": d.get("gangs"),
        "divisions": len({r.get("division_code") for r in rows}),
        "months": sorted({r.get("month") for r in rows}),
        "provenance": d.get("provenance"),
        "note": d.get("note"),
    }


def _t_replant(args, ctx) -> dict:
    return layers.replant_schedule(ctx["estate"])


def _t_canopy(args, ctx) -> dict:
    return vegetation.summary(ctx["estate"])


def _t_fire(args, ctx) -> dict:
    from config import FIRMS_MAP_KEY
    from gis import briefings
    scenario = args.get("scenario") or "live"
    if scenario not in fire.SCENARIOS:
        return {"error": f"Unknown scenario {scenario!r}.", "available": fire.SCENARIOS}
    blocks = ontology.blocks_geojson(ctx["estate"])
    if blocks is None:
        return {"error": f"No block geometry for {ctx['estate']}, so no fire triage."}
    assessment = fire.assess(blocks, ctx["estate"], FIRMS_MAP_KEY, scenario)
    ctx["fire"] = assessment
    return briefings.fire_grounding(assessment, ctx["estate"])


def _t_readiness(args, ctx) -> dict:
    reg = readiness.catalogue()
    status = args.get("status")
    caps = reg["capabilities"]
    if status:
        caps = [c for c in caps if c["status"] == status]
    return {
        "summary": reg["summary"],
        "capabilities": [{"id": c["id"], "capability": c["capability"],
                          "status": c["status"], "needs": c["needs"],
                          "evidence": c["evidence"], "degrades_to": c["degrades_to"],
                          "ask": c["ask"]} for c in caps[:MAX_ROWS]],
        "provenance": "measured against the two EPMS databases and the estate exports",
    }


def _t_decisions(args, ctx) -> dict:
    d = decisions.history(min(int(args.get("limit") or 10), MAX_ROWS), ctx["estate"])
    return {
        "total": d["total"], "by_action": d["by_action"],
        "decisions": [{"at": r["created_at"], "action": r["action"],
                       "title": r["title"], "subject": r["subject"],
                       "use_case": r["use_case"],
                       "artifact": (r.get("artifact") or {}).get("reference")}
                      for r in d["decisions"]],
        "note": "Artifacts were drafted, never sent. Nothing was written to EPMS.",
    }


def _t_focus_map(args, ctx) -> dict:
    """The only tool with a side effect: it tells the UI what to show.

    Block labels are resolved to feature ids here rather than in the browser,
    so a label the model invented fails loudly on the server instead of
    silently highlighting nothing.
    """
    estate = ctx["estate"]
    metric = args.get("metric")
    if metric and metric not in layers.METRICS:
        return layers.unknown_metric(metric)

    labels = args.get("block_labels") or []
    if isinstance(labels, str):
        labels = [labels]
    geo = ontology.blocks_geojson(estate) or {"features": []}
    by_label = {str(f["properties"].get("block_label")): f["id"]
                for f in geo["features"]}
    resolved, unknown = [], []
    for lb in [str(x).strip() for x in labels][:25]:
        (resolved if lb in by_label else unknown).append(lb)

    focus = {
        "estate": estate,
        "metric": metric,
        "month": args.get("month"),
        "panel": args.get("panel"),
        "block_labels": resolved,
        "block_ids": [by_label[lb] for lb in resolved],
        "note": str(args.get("note") or "")[:200],
    }
    ctx["focus"] = focus
    # Deliberately terse. An earlier version returned a friendly sentence here
    # and Nova simply echoed it as the whole answer - the model will happily
    # treat a quotable string in a tool result as the reply it owes you.
    out = {"focused": True, "highlighted_blocks": len(resolved),
           "metric_provenance": (layers.metric_provenance(metric, estate)
                                 if metric else None),
           "next": "Write the finding and its figures now."}
    if unknown:
        out["unknown_labels"] = unknown
        out["warning"] = ("These labels are not on this estate. Do not state "
                          "figures for them.")
    return out


# ── the layers built on 2026-09-11, which the tool set had not caught up with ──
#
# Every one of these returns its figures pre-computed. The figure audit in
# gis/reasoning.py re-reads each number in the answer and looks for it in the
# payload behind it, so a tool that returns cluster assignments but not cluster
# sizes hands the model a narrative that will be flagged unverified. Summarise
# here, never in the prompt.


def _t_transport(args, ctx) -> dict:
    """Haulage: shrinkage, turnaround, queueing and diesel, estate-wide.

    fleet is per vehicle and 17 rows long for EC; the totals and the two worst
    performers are what a question about haulage is actually about.
    """
    d = layers.transport_position(ctx["estate"])
    fleet = d.get("fleet") or []
    return {
        "totals": d.get("totals"),
        "vehicles": len(fleet),
        "worst_shrinkage": d.get("worst_shrinkage"),
        "slowest_turnaround": d.get("slowest_turnaround"),
        "note": d.get("note"),
    }


def _t_shrinkage(args, ctx) -> dict:
    """Field-to-mill shrinkage, with the detector's own scorecard.

    `validation` is the part that matters in the room: anomalies were planted
    in the feed and this reports how many the detector recovered. Quote it.
    """
    from gis.models import shrinkage
    d = shrinkage.assess(min(int(args.get("top") or 10), MAX_ROWS))
    if not d.get("available"):
        return d
    return {k: v for k, v in d.items() if k not in ("by_driver", "by_vehicle")}


def _t_pest(args, ctx) -> dict:
    """Ganoderma census, spread risk and treatment backlog."""
    return layers.pest_position(ctx["estate"], min(int(args.get("top") or 10), MAX_ROWS))


def _t_nutrition(args, ctx) -> dict:
    """Fertiliser applied against agronomy target, application lag, stock cover."""
    return layers.nutrition_position(ctx["estate"], min(int(args.get("top") or 10), MAX_ROWS))


def _t_roads(args, ctx) -> dict:
    """Road condition, grading backlog, and what it costs haulage."""
    return layers.roads_position(ctx["estate"], min(int(args.get("top") or 10), MAX_ROWS))


def _t_clusters(args, ctx) -> dict:
    """Agronomic underperformance groups over vigour, yield, age and inputs.

    Groups are labelled by which axes are extreme, never by root cause: "low
    vigour, on-programme upkeep, average age" survives the figure audit and
    "fertiliser deficit" does not, because the latter is a claim the payload
    cannot support.
    """
    from gis.models import clusters
    d = clusters.assess(ctx["estate"])
    if not d.get("available"):
        return d
    # Group membership is up to 89 labels per group; the caller wants the
    # shape of the groups, not the roll.
    groups = [{k: v for k, v in g.items() if k != "block_labels"}
              for g in (d.get("groups") or [])]
    return {**{k: v for k, v in d.items() if k != "groups"}, "groups": groups}


def _t_productivity(args, ctx) -> dict:
    """Expected bunches per man-day per block, and the fair target it implies."""
    from gis.models import productivity
    d = productivity.assess(ctx["estate"], min(int(args.get("top") or 10), MAX_ROWS))
    if not d.get("available"):
        return d
    return {k: v for k, v in d.items() if k not in ("top_workers", "bottom_workers")}


def _t_lagged_forecast(args, ctx) -> dict:
    """Lagged yield forecast, with the honest reading of what it found.

    `honesty` records that rainfall importance landed on lags 1 to 3 rather
    than the 20-24 month sex-determination window, which means the model found
    season rather than biology. Say so; it is the argument for the extract.
    """
    from gis.models import lagged_forecast
    d = lagged_forecast.assess(ctx["estate"])
    if not d.get("available"):
        return d
    return {k: v for k, v in d.items() if k != "rows"}


def _t_environment(args, ctx) -> dict:
    """Terrain and rainfall: both real, both free, neither from the client.

    Elevation and canopy vigour correlate at rho 0.415 on this estate, which
    on a coastal plain points at drainage. Slope explains nothing here (rho
    0.05 against yield), which is worth saying: it is flat.
    """
    from gis import environment
    which = (args.get("which") or "both").lower()
    out = {}
    if which in ("terrain", "both"):
        out["terrain"] = environment.terrain_summary(ctx["estate"])
    if which in ("rainfall", "both"):
        r = environment.rainfall_summary(ctx["estate"])
        # `months` is 36 rows of daily aggregates and 13 KB of context. The
        # seasonal shape and the window are what an answer ever quotes; a
        # question about one month should ask for that month.
        months = r.get("months") or []
        out["rainfall"] = {k: v for k, v in r.items() if k != "months"}
        out["rainfall"]["months_available"] = len(months)
        out["rainfall"]["recent_months"] = months[-6:]
    return out


def _t_data_requirements(args, ctx) -> dict:
    """What it would take to run a feature, or the whole catalogue, for real.

    This is the question the app exists to answer. Without a feature id it
    returns the grouped data request: every extract still wanted, which system
    it lives in, and how many features each one unblocks.
    """
    from gis import features
    fid = args.get("feature")
    if fid:
        # A caller may name either a feature or the panel it lives on, and the
        # two share names often enough that guessing wrong is not worth a
        # round trip. Try both before giving up.
        return (features.get(fid)
                or features.by_panel(fid)
                or {"error": f"No feature or panel called {fid!r}.",
                    "recovery": "Call with no argument for the whole data request."})
    d = features.data_request()
    # Each requirement carries the full hydrated list of features it unblocks,
    # which triples the payload and repeats what the counts already say. The
    # names and the counts are what an answer quotes.
    groups = [{
        **{k: v for k, v in g.items() if k != "requirements"},
        "requirements": [{
            "entity": r.get("entity"), "table": r.get("table"),
            "status": r.get("status"),
            "unblocks_count": r.get("unblocks_count"),
            "fully_unblocks": r.get("fully_unblocks"),
        } for r in (g.get("requirements") or [])],
    } for g in (d.get("groups") or [])]
    return {**d, "groups": groups}


# ── the operating rhythm, added 2026-09-14 ────────────────────────────────
#
# The model never computes. Every plan below is produced by the scheduler and
# returned with its figures already summed; the tool clips the crew list and
# the block lists so a 28-gang plan does not put 28 x 6 rows in the window,
# but every number the narrative can quote is in the result.

_OPS_HELP = "One of: " + ", ".join(ops.OPERATIONS) + "."


def _clip_plan(p: dict, crews_max: int = 10, blocks_max: int = 8) -> dict:
    if not p.get("available"):
        return p
    crews = []
    for c in (p.get("crews") or [])[:crews_max]:
        crews.append({
            "crew_code": c["crew_code"], "division_code": c.get("division_code"),
            "present": c["present"], "on_roll": c["on_roll"], "cutters": c.get("cutters"),
            "blocks": c["range_label"], "block_labels": c["block_labels"][:blocks_max],
            "totals": c["totals"], "utilisation_pct": c["utilisation_pct"],
            "edited": c.get("edited"),
        })
    why = p.get("why") or {}
    return {
        "operation": p["operation"], "date": p["date"], "date_label": p["date_label"],
        "is_tomorrow": p["is_tomorrow"], "headline": p["headline"],
        "totals": p["totals"], "crews": crews,
        "crews_not_shown": max(0, len(p.get("crews") or []) - crews_max),
        "not_reached": {k: v for k, v in (p.get("not_reached") or {}).items() if k != "top"},
        "not_reached_top": (p.get("not_reached") or {}).get("top", [])[:6],
        "weather": p.get("weather"),
        "binding_constraint": why.get("binding_constraint"),
        "objective": {k: v for k, v in (why.get("objective") or {}).items()
                      if k in ("method", "gap_pct", "gap_reading", "upper_bound_idr",
                               "achieved_idr", "contiguity")},
        "assumptions_used": p.get("assumptions_used"),
        "overrides": p.get("overrides"),
        "provenance": p.get("provenance"), "note": p.get("note"),
    }


def _t_ops_plan(args, ctx) -> dict:
    """Tomorrow's assignment for one operation, every figure pre-computed."""
    from gis.models import scheduler
    op = str(args.get("operation") or "harvest").lower()
    if op not in ops.OPERATIONS:
        return {"error": f"Unknown operation {op!r}.", "recovery": _OPS_HELP}
    p = scheduler.plan(op, args.get("date") or None)
    if not p.get("available"):
        return p
    out = _clip_plan(p)
    if args.get("crew"):
        crew = next((c for c in p["crews"] if c["crew_code"] == args["crew"]), None)
        out["crew_detail"] = crew if crew else {"error": f"No crew {args['crew']!r} on this plan."}
    ctx["ops_plan"] = {"operation": op, "date": p["date"],
                       "block_labels": [b for c in p["crews"] for b in c["block_labels"]]}
    return out


def _t_ops_ledger(args, ctx) -> dict:
    """A slice of the history, aggregates included and rows clipped."""
    op = str(args.get("operation") or "harvest").lower()
    if op not in ops.OPERATIONS:
        return {"error": f"Unknown operation {op!r}.", "recovery": _OPS_HELP}
    d = ops.ledger(op, crew=args.get("crew") or None, block=args.get("block") or None,
                   division=args.get("division") or None,
                   date_from=args.get("from") or None, date_to=args.get("to") or None,
                   status=args.get("status") or None, limit=min(int(args.get("limit") or 8), MAX_ROWS))
    if not d.get("available"):
        return d
    return {
        "operation": op, "label": d["label"], "unit": d["unit"], "window": d["window"],
        "filters": d["filters"], "matched_orders": d["matched"],
        "totals": d["totals"], "footer": d["footer"],
        "drivers": d["drivers"],
        "slippage": {k: v for k, v in d["slippage"].items() if k != "longest"},
        "longest_chains": d["slippage"].get("longest", [])[:3],
        "worst_crews": d["by_crew"][:5], "best_crews": d["by_crew"][-3:][::-1],
        "by_week": d["by_week"][-8:],
        "recent_rows": d["rows"],
        "provenance": d["provenance"], "note": d["note"],
    }


def _t_crew_capacity(args, ctx) -> dict:
    """Who is available on a date and what they can do."""
    d = ops.capacity(args.get("date") or None, args.get("crew_type") or None)
    if not d.get("available"):
        return d
    crews = d["crews"]
    if args.get("division"):
        crews = [c for c in crews if str(c["division_code"]) == str(args["division"])]
    fields = ("crew_code", "crew_type", "division_code", "on_roll", "present", "cutters",
              "attendance_pct", "basis", "rate_per_man_day", "rate_unit", "capacity_units",
              "open_work")
    return {
        "date": d["date"], "is_tomorrow": d["is_tomorrow"], "lookback_days": d["lookback_days"],
        "totals": d["totals"], "by_type": d["by_type"],
        "crews": [{f: c.get(f) for f in fields} for c in crews[:MAX_ROWS]],
        "crews_not_shown": max(0, len(crews) - MAX_ROWS),
        "vehicles": [{k: v[k] for k in ("crew_code", "vehicle_class", "capacity_t",
                                          "loads_per_day", "capacity_units", "basis")}
                     for v in d["vehicles"][:8]],
        "assumptions_used": d["assumptions_used"],
        "provenance": d["provenance"], "note": d["note"],
    }


def _t_cost_of_deferral(args, ctx) -> dict:
    """What waiting costs on one block, from the same arithmetic as the plan."""
    from gis.models import scheduler
    op = str(args.get("operation") or "harvest").lower()
    if op not in ops.OPERATIONS:
        return {"error": f"Unknown operation {op!r}.", "recovery": _OPS_HELP}
    return scheduler.cost_of_deferral(op, str(args.get("block") or "").strip(),
                                      int(args.get("days") or 1), args.get("date") or None)


def _forecast_meta(payload: dict) -> dict:
    return {"provenance": ("predicted: rain is trained on real forecasts and real rainfall; headcount, "
                           "work done and crew speeds are trained on the generated ledger"),
            "how_to_quote": ("Quote the plain sentences and their figures as given. Give a range as a range "
                             "and a chance as a chance; never turn either into a single certain number.")}


def _stores_meta() -> dict:
    return {"provenance": ("predicted: lead times, use and reorder points are learned from generated SAP MM "
                           "records (goods movements, purchase orders, reservations), shaped as the extract "
                           "that would replace them"),
            "how_to_quote": ("Quote the plain sentences and their figures as given. A lead time is a usual time "
                             "and a one-in-ten figure; a reorder point comes with its safety stock and why.")}


def _find_material(name: str) -> str | None:
    from gis.models import mm
    st = mm.state()
    if not st.get("available") or not name:
        return None
    q = name.strip().lower()
    for k, m in st["materials"].items():
        if q == k.lower() or q == m["maktx"].lower():
            return k
    hits = [k for k, m in st["materials"].items() if q in m["maktx"].lower() or m["maktx"].lower().split()[0] in q]
    return hits[0] if hits else None


def _t_stock_position(args, ctx) -> dict:
    """Stock, cover, reorder point and order-by, for one material or a group."""
    from gis import stores
    from gis.models import mm
    if not mm.available():
        return {"error": "No store records.", **_stores_meta()}
    if args.get("material"):
        matnr = _find_material(args["material"])
        if not matnr:
            return {"error": f"No material like {args['material']!r}.",
                    "recovery": "Call stock_position with a group, or with no arguments, to list materials.",
                    **_stores_meta()}
        v = stores.material_view(matnr, args.get("date") or None)
        return {"material": v["maktx"], "status": v["status_label"], "headline": v["plain"]["headline"],
                "on_hand": v["on_hand"], "on_order": v["on_order"], "unit": v["unit"],
                "days_of_cover": v["days_of_cover"], "reorder_point": v["reorder_point"],
                "safety_stock": v["safety_stock"], "order_by": v["order_by"], "order_qty": v["order_qty"],
                "chance_out_before_delivery_pct": round(100 * v["chance_out_before_delivery"]),
                "why": [v["plain"]["supplier"], v["plain"]["buffer"], v["plain"]["risk"]],
                "sap_settings": v["plain"]["sap"],
                "policy_in_force": ("learned reorder point" if v["learned_in_use"] else "SAP's own settings"),
                "replay": (v.get("replay") or {}).get("plain"), **_stores_meta()}
    o = stores.overview(args.get("group") or None, args.get("date") or None)
    return {"date": o["date"], "summary": o["summary"], "counts": o["counts"],
            "materials": [{"material": r["maktx"], "status": r["status_label"], "headline": r["plain"]["headline"],
                           "on_hand": r["on_hand"], "unit": r["unit"], "order_by": r["order_by"]}
                          for r in o["materials"][:MAX_ROWS]],
            "materials_not_shown": max(0, len(o["materials"]) - MAX_ROWS),
            "trust": [{"model": t["title"], "grade": t["grade"]["label"], "why": t["headline"]} for t in o["trust"]],
            **_stores_meta()}


def _t_supplier_lead_time(args, ctx) -> dict:
    """Quoted against real lead times, per supplier."""
    from gis import stores
    from gis.models import mm
    if not mm.available():
        return {"error": "No store records.", **_stores_meta()}
    t = stores.lead_times(args.get("date") or None)
    rows = t["suppliers"]
    if args.get("supplier"):
        q = args["supplier"].lower()
        rows = [r for r in rows if q in r["name"].lower() or q == r["lifnr"].lower()]
    if args.get("material"):
        matnr = _find_material(args["material"])
        name = mm.state()["materials"][matnr]["maktx"] if matnr else args["material"]
        rows = [r for r in rows if name in r["materials"]]
    if not rows:
        return {"error": "No supplier matched.", "recovery": "Call supplier_lead_time with no arguments.",
                **_stores_meta()}
    bt = t["backtest"]
    return {"suppliers": [{"supplier": r["name"], "plain": r["plain"], "quoted_days": r["quoted_days"],
                           "usual_days": r["usual_days"], "one_in_ten_days": r["p90_now"],
                           "very_late_chance_pct": round(100 * r["very_late_chance"]),
                           "slow_months": r["slow_months"], "orders": r["orders"], "materials": r["materials"]}
                          for r in rows],
            "trust": {"grade": bt["grade"]["label"], "why": bt["plain"]["headline"]}, **_stores_meta()}


def _t_reorder_advice(args, ctx) -> dict:
    """Quantity, date, and what a different service level would cost."""
    from gis import stores
    from gis.models import mm
    if not mm.available():
        return {"error": "No store records.", **_stores_meta()}
    matnr = _find_material(args.get("material") or "")
    if not matnr:
        return {"error": f"No material like {args.get('material')!r}.",
                "recovery": "Call stock_position with no arguments to list materials.", **_stores_meta()}
    v = stores.material_view(matnr, args.get("date") or None)
    cc = v["cost_curve"]
    return {"material": v["maktx"], "headline": v["plain"]["headline"], "order_by": v["order_by"],
            "order_qty": v["order_qty"], "unit": v["unit"], "supplier": v["supplier"]["name"],
            "service_level_pct": v["service_level_pct"], "cost_of_service_level": cc["plain"],
            "service_levels": [{"service_pct": r["service_pct"], "safety_stock": r["safety_stock"],
                                "yearly_cost_idr": r["total_idr"]} for r in cc["rows"]],
            "sap_settings": v["plain"]["sap"], "replay": (v.get("replay") or {}).get("plain"),
            "policy_in_force": ("learned reorder point" if v["learned_in_use"] else "SAP's own settings"),
            **_stores_meta()}


def _t_rain_outlook(args, ctx) -> dict:
    """The chance of wash-off, heavy rain and a stop, and the spray call."""
    from gis import forecasts
    v = forecasts.rain_view(args.get("date") or None)
    fc = v.get("forecast") or {}
    if not fc.get("available"):
        return {"error": fc.get("reason") or "No rain forecast.", **_forecast_meta(v)}
    bt = v["backtest"]
    out = {"date": v["date"], "headline": fc["headline"],
           "chances": {k: {kk: c[kk] for kk in ("label", "pct", "words", "meaning")}
                       for k, c in fc["chances"].items()},
           "amount": fc["amount"]["plain"], "forecast_said": fc["forecast_plain"],
           "recorded": fc.get("recorded_plain"), "how": fc["how"],
           "trust": {"grade": bt["grade"]["label"], "why": bt["plain"]["headline"],
                     "raw_forecast": bt["plain"]["raw"]},
           "recent_days": [r["plain"] for r in v["recent"]], **_forecast_meta(v)}
    from gis.models import scheduler
    sp = scheduler.plan("spray", v["date"])
    call = ((sp.get("forecast") or {}).get("spray_call") or (sp.get("weather") or {}).get("spray_call"))
    if call:
        out["spray_call"] = call["plain"]
    return out


def _t_headcount_forecast(args, ctx) -> dict:
    """Who is likely to turn up: estate, by crew type, or one crew."""
    from gis import forecasts
    v = forecasts.headcount_view(args.get("date") or None, args.get("crew_type") or None)
    if not v.get("available"):
        return {"error": "No headcount forecast.", **_forecast_meta(v)}
    crews = v["crews"]
    if args.get("crew"):
        crews = [c for c in crews if c["crew_code"] == args["crew"]]
        if not crews:
            return {"error": f"No crew {args['crew']!r}.", "recovery": "Call crew_capacity to list crews."}
    if args.get("division"):
        crews = [c for c in crews if str(c["division_code"]) == str(args["division"])]
    bt = v["backtest"]
    return {"date": v["date"], "estate": v["estate"]["plain"],
            "crews": [{"crew_code": c["crew_code"], "plain": c["plain"], "low": c["low"], "high": c["high"],
                       "most_likely": c["most_likely"], "on_roll": c["on_roll"], "why": c["drivers"],
                       "recorded": c["recorded"]} for c in crews[:MAX_ROWS]],
            "crews_not_shown": max(0, len(crews) - MAX_ROWS),
            "trust": {"grade": bt["grade"]["label"], "why": bt["plain"]["headline"],
                      "sundays": bt["plain"]["sundays"], "range": bt["plain"]["range"]},
            "recent_days": [r["plain"] for r in v["recent"]], **_forecast_meta(v)}


def _t_work_done_forecast(args, ctx) -> dict:
    """How much of the plan is likely to get done, and which blocks are at risk."""
    from gis import forecasts
    op = str(args.get("operation") or "harvest").lower()
    v = forecasts.work_done_view(op, args.get("date") or None)
    if not v.get("available"):
        return {"error": v.get("reason") or "No plan.", "recovery": _OPS_HELP}
    wd = v["work_done"] or {}
    bt = v["backtest"]
    ctx["ops_plan"] = {"operation": op, "date": v["date"],
                       "block_labels": [r["block_label"] for r in v["at_risk"]]}
    return {"operation": op, "date": v["date"], "headline": v["headline"],
            "work_done": wd.get("plain") or wd.get("reason"), "risk": wd.get("risk_plain"),
            "blocks_at_risk": [{"plain": r["plain"], "why": r["drivers"]} for r in v["at_risk"][:8]],
            "by_crew": [c["plain"] for c in v["crews"][:MAX_ROWS]],
            "spray_call": (v.get("spray_call") or {}).get("plain"),
            "what_drives_a_miss": v["effects"],
            "trust": {"grade": bt["grade"]["label"], "why": bt["plain"]["headline"],
                      "knowing_the_weather": bt["plain"]["knowing"]},
            **_forecast_meta(v)}


def _t_crew_speeds(args, ctx) -> dict:
    """Which crews (or harvest blocks) are faster or slower than the book."""
    from gis import forecasts
    op = str(args.get("operation") or "weed").lower()
    v = forecasts.speeds_view(op, args.get("date") or None)
    if not v.get("available"):
        return {"error": v.get("reason"), **_forecast_meta(v)}
    rows = v["rows"]
    if args.get("crew"):
        rows = [r for r in rows if r["label"] == args["crew"] or r["unit"] == args["crew"]]
    bt = v.get("backtest") or {}
    return {"operation": op, "in_use": v["in_use"], "faster": v["faster"], "slower": v["slower"],
            "total": v["total"],
            "rows": [{"label": r["label"], "plain": r["plain"], "factor": r["factor"],
                      "book_rate": r["book_rate"], "learned_rate": r["learned_rate"],
                      "man_days": r["man_days"]} for r in rows[:MAX_ROWS]],
            "trust": {"grade": (bt.get("grade") or {}).get("label"), "why": bt.get("plain")},
            "checks": [r["plain"] for r in v["recovery"]], **_forecast_meta(v)}


def _t_forecast_accuracy(args, ctx) -> dict:
    """How far to trust each forecast, and the checks behind it."""
    from gis import forecasts
    a = forecasts.accuracy()
    return {"trust": [{"forecast": t["title"], "grade": t["grade"]["label"], "meaning": t["grade"]["meaning"],
                       "headline": t["headline"], "detail": t["detail"], "trained_on": t["trained_on"],
                       "in_use": t["in_use"]} for t in a["trust"]],
            "checks": {
                "headcount": [r["plain"] for r in a["headcount"]["recovery"].get("rows", [])],
                "work_done": [r["plain"] for r in a["work_done"]["recovery"].get("rows", [])],
                "speeds": [r["plain"] for r in a["speeds"]["recovery"].get("rows", [])],
            },
            "rules": a["rules"], **_forecast_meta(a)}


def _t_replan(args, ctx) -> dict:
    """Re-run tomorrow's plan with a constraint changed.

    The constraint vocabulary is deliberately small and named the way a
    manager would say it: a gang short, a gang out, a block held, a road
    closed, rain expected, contiguity off.
    """
    from gis.models import scheduler
    op = str(args.get("operation") or "harvest").lower()
    if op not in ops.OPERATIONS:
        return {"error": f"Unknown operation {op!r}.", "recovery": _OPS_HELP}
    kind = str(args.get("constraint") or "").lower().strip()
    value = args.get("value")
    crew = args.get("crew")
    ov: dict = {}
    if kind in ("crew_present", "men_present", "present"):
        if not crew:
            return {"error": "crew_present needs the crew code.", "recovery": "Pass crew, e.g. G1-03."}
        ov["crews"] = {crew: {"present": int(float(value))}}
    elif kind in ("crew_short", "men_short", "short"):
        if not crew:
            return {"error": "crew_short needs the crew code.", "recovery": "Pass crew, e.g. G1-03."}
        cap = ops.capacity(args.get("date") or None)
        c = next((x for x in cap.get("crews") or [] if x["crew_code"] == crew), None)
        if not c:
            return {"error": f"No crew {crew!r}.", "recovery": "Call crew_capacity to list crews."}
        ov["crews"] = {crew: {"present": max(0, c["present"] - int(float(value or 0)))}}
    elif kind in ("exclude_crew", "crew_out", "crew_absent"):
        if not crew:
            return {"error": "exclude_crew needs the crew code."}
        ov["crews"] = {crew: {"exclude": True}}
    elif kind in ("exclude_block", "hold_block", "block_out"):
        ov["exclude_blocks"] = [str(value)]
    elif kind in ("road_closed", "road_out"):
        ov["road_closed"] = [str(value)]
    elif kind in ("rain_mm", "rain"):
        ov["rain_mm"] = float(value)
    elif kind in ("contiguity_off", "no_contiguity"):
        ov["contiguity_bonus_pct"] = 0.0
    elif kind in ("division",):
        ov["division"] = str(value)
    else:
        return {"error": f"Unknown constraint {kind!r}.",
                "recovery": ("Use one of: crew_present (crew, value=men), crew_short (crew, "
                             "value=men short), exclude_crew (crew), exclude_block (value=label), "
                             "road_closed (value=label), rain_mm (value), contiguity_off, "
                             "division (value).")}
    base = scheduler.plan(op, args.get("date") or None)
    new = scheduler.plan(op, args.get("date") or None, ov)
    if not new.get("available"):
        return new
    out = _clip_plan(new)
    if base.get("available"):
        bt, nt = base["totals"], new["totals"]
        out["change"] = {
            "blocks": nt["blocks"] - bt["blocks"], "ha": round(nt["ha"] - bt["ha"], 1),
            "tonnes": round(nt["tonnes"] - bt["tonnes"], 2),
            "qty": round(nt["qty"] - bt["qty"], 1), "unit": nt["unit"],
            "not_reached_blocks": new["not_reached"]["blocks"] - base["not_reached"]["blocks"],
            "deferral_cost_idr_per_week": (new["not_reached"]["deferral_cost_idr_per_week"]
                                           - base["not_reached"]["deferral_cost_idr_per_week"]),
            "before_headline": base["headline"], "after_headline": new["headline"],
        }
        if crew:
            b0 = next((c for c in base["crews"] if c["crew_code"] == crew), None)
            b1 = next((c for c in new["crews"] if c["crew_code"] == crew), None)
            out["crew_before"] = ({"present": b0["present"], "blocks": b0["range_label"],
                                   "totals": b0["totals"]} if b0 else None)
            out["crew_after"] = ({"present": b1["present"], "blocks": b1["range_label"],
                                  "totals": b1["totals"]} if b1 else "excluded")
    ctx["ops_plan"] = {"operation": op, "date": new["date"],
                       "block_labels": [b for c in new["crews"] for b in c["block_labels"]]}
    return out


# ── the seven features built on 2026-09-18 ────────────────────────────────
#
# Each is one module under gis/ with a position() the endpoint already serves.
# The payloads carry per-block series for the panel's charts, which the model
# has no use for, so every list is cut to MAX_ROWS and any per-block map is
# reduced to its size. The figures, sentence, provenance and caveat pass
# through untouched.

def _clip_payload(d, rows: int = MAX_ROWS, depth: int = 0):
    if isinstance(d, list):
        return [_clip_payload(x, rows, depth + 1) for x in d[:rows]]
    if isinstance(d, dict):
        if depth and len(d) > 60:
            return {"_omitted": f"{len(d)} entries, per block; ask for one block instead"}
        return {k: _clip_payload(v, rows, depth + 1) for k, v in d.items()}
    return d


def _feature_tool(module: str):
    def fn(args, ctx) -> dict:
        import importlib
        top = min(int(args.get("top") or 10), MAX_ROWS)
        mod = importlib.import_module(f"gis.{module}")
        return _clip_payload(mod.position(ctx["estate"], top), top)
    fn.__name__ = f"_t_{module}"
    return fn


_t_loose_fruit = _feature_tool("loose_fruit")
_t_herbicide = _feature_tool("herbicide")
_t_ffa = _feature_tool("ffa")
_t_cutting_interval = _feature_tool("cutting_interval")
_t_abw_trend = _feature_tool("abw_trend")
_t_pest_warning = _feature_tool("pest_warning")
_t_collection = _feature_tool("collection")

_TOP = {"top": {"type": "integer", "description": "How many rows to return, default 10."}}


_TOOLS = {
    "estate_overview": {
        "fn": _t_estate_overview,
        "desc": ("What this estate is: block count, planted hectares, palms, which "
                 "months have recorded harvest, which are forecast, and every metric "
                 "the map can colour by with its provenance. Call this first when you "
                 "do not know what is available."),
        "params": {},
    },
    "query_blocks": {
        "fn": _t_query_blocks,
        "desc": ("Rank blocks by any metric and return the top or bottom slice with "
                 "their key attributes. This answers most 'which blocks' questions: "
                 "worst yield, lowest canopy vigour, most overdue for harvest, "
                 "highest cost. Returns the distribution too, so you can say how "
                 "unusual the extremes are."),
        "params": {
            "metric": {"type": "string",
                       "description": "Metric key, e.g. bunches_per_ha, peer_index, "
                                      "ndre, ndre_anomaly, ripeness_pressure, "
                                      "cost_per_kg, margin_per_ha, palm_age_years, "
                                      "deduction_rate, forecast_p50."},
            "order": {"type": "string", "enum": ["lowest", "highest"],
                      "description": "Which end of the ranking to return."},
            "limit": {"type": "integer", "description": "Rows to return, max 15."},
            "month": {"type": "string",
                      "description": "YYYY-MM. Omit for the full recorded window."},
            "min_value": {"type": "number", "description": "Optional lower bound."},
            "max_value": {"type": "number", "description": "Optional upper bound."},
        },
        "required": ["metric"],
    },
    "compare_metrics": {
        "fn": _t_compare_metrics,
        "desc": ("Do two metrics agree about which blocks are the problem? Returns "
                 "the rank correlation across every block that carries both, plus "
                 "the blocks in the bottom fifth of both, of one, and of the other. "
                 "This is how you answer whether satellite vigour corroborates "
                 "recorded yield, or whether cost tracks age. A block weak on two "
                 "independent measurements is worth a visit; weak on one is a lead."),
        "params": {
            "metric_a": {"type": "string", "description": "First metric key."},
            "metric_b": {"type": "string", "description": "Second metric key."},
            "month": {"type": "string", "description": "YYYY-MM, optional."},
        },
        "required": ["metric_a", "metric_b"],
    },
    "block_detail": {
        "fn": _t_block_detail,
        "desc": ("Everything known about one block by its label, grouped by "
                 "provenance: the client's attributes, its recorded harvest, how it "
                 "compares to its planting cohort, its satellite canopy reading, and "
                 "the synthetic operational figures."),
        "params": {
            "block_label": {"type": "string", "description": "Block label, e.g. '32-51'."},
            "month": {"type": "string", "description": "YYYY-MM, optional."},
        },
        "required": ["block_label"],
    },
    "contract_position": {
        "fn": _t_contract,
        "desc": ("UC-08. Production and forecast against committed sales volume, per "
                 "month, with the gap at P50 and at P10. Use for questions about "
                 "shortfalls, commitments, or whether the estate can cover its orders."),
        "params": {},
    },
    "vendor_ranking": {
        "fn": _t_vendors,
        "desc": ("UC-09. Third-party FFB vendors ranked by landed cost. PASS "
                 "shortfall_t whenever the question is what covering a gap "
                 "would cost: with it, the result carries a costed allocation "
                 "and allocation_total_idr, the total in rupiah. Without it "
                 "there is no total and you must not work one out. Take the "
                 "tonnes from contract_position. Every vendor figure is synthetic."),
        "params": {"shortfall_t": {"type": "number",
                                   "description": "Tonnes to cover, optional."}},
    },
    "rotation_plan": {
        "fn": _t_rotation,
        "desc": ("UC-01. Which blocks are past their harvest round, ranked by ripeness "
                 "pressure, with the gang nominally assigned. Rotation state and gangs "
                 "are synthetic."),
        "params": {"top": {"type": "integer", "description": "Blocks to return, max 15."}},
    },
    "labour_position": {
        "fn": _t_labour,
        "desc": ("UC-12. Harvester supply against demand by month, including the "
                 "Ramadan and Lebaran dip. Entirely synthetic."),
        "params": {},
    },
    "replant_schedule": {
        "fn": _t_replant,
        "desc": ("UC-11. When the estate reaches its replanting cliff, from the "
                 "client's real planting years. The uniformity of those years is the "
                 "finding."),
        "params": {},
    },
    "canopy_vigour": {
        "fn": _t_canopy,
        "desc": ("UC-03. The Sentinel-2 canopy reading: which scene, how much of the "
                 "estate was clear of cloud, the NDRE spread, and the weakest blocks. "
                 "Real measurement over the client's polygons, taken by us, not them."),
        "params": {},
    },
    "fire_assessment": {
        "fn": _t_fire,
        "desc": ("UC-06. Live NASA FIRMS hotspots, weather, and which blocks are in "
                 "the path, with estimated arrival times. Detection and exposure are "
                 "real; every response asset is synthetic."),
        "params": {"scenario": {"type": "string",
                                "enum": ["live", "near_miss", "severe"],
                                "description": "Live feed, or a rehearsal scenario."}},
    },
    "data_readiness": {
        "fn": _t_readiness,
        "desc": ("UC-15. What each capability needs, whether the data exists, what it "
                 "degrades to, and what to ask the client for. Use this whenever the "
                 "honest answer is that something cannot be computed."),
        "params": {"status": {"type": "string",
                              "enum": ["ready", "partial", "synthetic", "unavailable"],
                              "description": "Filter to one status, optional."}},
    },
    "decision_log": {
        "fn": _t_decisions,
        "desc": ("UC-14. Every recommendation this system put up and what a human did "
                 "with it: accepted, rejected or deferred, with any drafted artifact."),
        "params": {"limit": {"type": "integer", "description": "Rows, max 15."}},
    },
    "transport_position": {
        "fn": _t_transport,
        "desc": ("Haulage on this estate: field-to-mill shrinkage, turnaround, how "
                 "much of it is queueing at the mill gate, and diesel per tonne. "
                 "Use for any question about trucks, evacuation or the mill run."),
        "params": {},
    },
    "shrinkage_detection": {
        "fn": _t_shrinkage,
        "desc": ("Trips that delivered less than their bunch count implies, with the "
                 "detector scored against anomalies deliberately planted in the feed. "
                 "Quote the precision and recall: they are the evidence the method "
                 "works, which is what earns the real weighbridge extract."),
        "params": {"top": {"type": "integer", "description": "How many rows to return, default 10."}},
    },
    "pest_position": {
        "fn": _t_pest,
        "desc": ("Ganoderma census, incidence by block, spread risk from infected "
                 "neighbours, and treatment follow-up overdue. Use for any question "
                 "about disease, pests, rats or beetle damage."),
        "params": {"top": {"type": "integer", "description": "How many rows to return, default 10."}},
    },
    "nutrition_position": {
        "fn": _t_nutrition,
        "desc": ("Fertiliser actually applied against the agronomy target in kg per "
                 "palm, how late each application was, and stock cover. Use for "
                 "questions about fertiliser, nutrients, NPK or goods issues."),
        "params": {"top": {"type": "integer", "description": "How many rows to return, default 10."}},
    },
    "roads_position": {
        "fn": _t_roads,
        "desc": ("Road condition across the estate, grading backlog, and the measured "
                 "effect of poor roads on turnaround and diesel."),
        "params": {"top": {"type": "integer", "description": "How many rows to return, default 10."}},
    },
    "underperformance_clusters": {
        "fn": _t_clusters,
        "desc": ("Groups of blocks that underperform in the same way, over canopy "
                 "vigour, yield, palm age, terrain and inputs. Answers 'why do two "
                 "identical neighbouring blocks yield differently'. Groups are named "
                 "by which axes are extreme, not by cause: do not upgrade a group "
                 "label into a diagnosis."),
        "params": {},
    },
    "crew_productivity": {
        "fn": _t_productivity,
        "desc": ("Expected bunches per man-day for each block given terrain, palm "
                 "height and bunch density, and the fair daily target that implies. "
                 "A flat estate-wide quota is wrong at both ends; this says by how "
                 "much. Do not call unexplained variance 'phantom labour'."),
        "params": {"top": {"type": "integer", "description": "How many rows to return, default 10."}},
    },
    "lagged_forecast": {
        "fn": _t_lagged_forecast,
        "desc": ("Yield forecast one to six months out from lagged harvest, rainfall "
                 "and fertiliser. Read the honesty field before answering: the fitted "
                 "lags landed on season rather than on the 20-24 month biological "
                 "window, and saying so is the point of the feature."),
        "params": {},
    },
    "environment": {
        "fn": _t_environment,
        "desc": ("Terrain and rainfall over this estate. Both are real and free, "
                 "pulled from public archives, and neither needs anything from the "
                 "client. Useful whenever an answer would otherwise blame something "
                 "unmeasured."),
        "params": {"which": {"type": "string", "enum": ["terrain", "rainfall", "both"],
                             "description": "Which layer, default both."}},
    },
    "data_requirements": {
        "fn": _t_data_requirements,
        "desc": ("What running a feature on the client's real numbers would take: the "
                 "extracts still wanted, the system each lives in, and how many "
                 "features each one unblocks. Call this whenever asked what is "
                 "missing, what is synthetic, or what we would need."),
        "params": {"feature": {"type": "string",
                               "description": "A feature or panel id, optional."}},
    },
    "ops_plan": {
        "fn": _t_ops_plan,
        "desc": ("Tomorrow's assignment for one operation: which crew takes which "
                 "blocks, present headcount, hectares, tonnes and man-days per crew, "
                 "what is not reached and what deferring it costs, the binding "
                 "constraint, and the contiguity cost. Every figure is computed by "
                 "the scheduler; quote it, never derive. Tomorrow is 2025-05-24, the "
                 "first day beyond the client's data; a date inside the ledger "
                 "replays the plan against what actually happened."),
        "params": {
            "operation": {"type": "string", "enum": list(ops.OPERATIONS),
                          "description": "harvest, prune, weed, spray, pest or dispatch."},
            "date": {"type": "string", "description": "YYYY-MM-DD, optional; default tomorrow."},
            "crew": {"type": "string", "description": "A crew code to return in full, e.g. G1-03."},
        },
        "required": ["operation"],
    },
    "ops_ledger": {
        "fn": _t_ops_ledger,
        "desc": ("The work-order ledger for one operation: planned against actual by "
                 "crew, week and driver (rain, road, attendance), the carry-forward "
                 "chains, how the round stretched, and recent rows. Filter by crew, "
                 "block, division, dates or status. Use for any question about what "
                 "was done, adherence, slippage or productivity."),
        "params": {
            "operation": {"type": "string", "enum": list(ops.OPERATIONS)},
            "crew": {"type": "string", "description": "Crew code, optional."},
            "block": {"type": "string", "description": "Block label, optional."},
            "division": {"type": "string", "description": "Division code, optional."},
            "from": {"type": "string", "description": "YYYY-MM-DD, optional."},
            "to": {"type": "string", "description": "YYYY-MM-DD, optional."},
            "status": {"type": "string",
                       "enum": ["completed", "partial", "not_started", "weathered_off"]},
            "limit": {"type": "integer", "description": "Recent rows to return, max 15."},
        },
        "required": ["operation"],
    },
    "crew_capacity": {
        "fn": _t_crew_capacity,
        "desc": ("Who is available on a date and what they can do: every crew's roll, "
                 "expected present from trailing attendance, rate per man-day, "
                 "capacity in units, and unfinished work carried over; plus the fleet's "
                 "loads per day. Use for questions about men, gangs, attendance or "
                 "vehicles tomorrow."),
        "params": {
            "date": {"type": "string", "description": "YYYY-MM-DD, optional; default tomorrow."},
            "crew_type": {"type": "string",
                          "enum": ["harvest", "upkeep", "spray", "pest", "transport"]},
            "division": {"type": "string", "description": "Division code, optional."},
        },
    },
    "cost_of_deferral": {
        "fn": _t_cost_of_deferral,
        "desc": ("What waiting costs on one block for one operation: value at risk, "
                 "rupiah per day, and the total over N days, with the assumptions it "
                 "rests on. Use when asked what leaving a block another day or week "
                 "would cost."),
        "params": {
            "operation": {"type": "string", "enum": list(ops.OPERATIONS)},
            "block": {"type": "string", "description": "Block label, e.g. 32-51."},
            "days": {"type": "integer", "description": "Days of deferral, default 1."},
            "date": {"type": "string", "description": "YYYY-MM-DD, optional."},
        },
        "required": ["operation", "block"],
    },
    "replan": {
        "fn": _t_replan,
        "desc": ("Re-run tomorrow's plan with one constraint changed and return the "
                 "new plan beside the difference: a gang short of men (crew_short with "
                 "crew and value), a gang out (exclude_crew), a block held back "
                 "(exclude_block with value=label), a road closed (road_closed), rain "
                 "expected (rain_mm), or contiguity switched off to see what it costs "
                 "(contiguity_off). 'G1-03 is four men short tomorrow' is crew_short, "
                 "crew G1-03, value 4."),
        "params": {
            "operation": {"type": "string", "enum": list(ops.OPERATIONS)},
            "constraint": {"type": "string",
                           "enum": ["crew_short", "crew_present", "exclude_crew",
                                    "exclude_block", "road_closed", "rain_mm",
                                    "contiguity_off", "division"]},
            "crew": {"type": "string", "description": "Crew code, for the crew constraints."},
            "value": {"type": "string", "description": "Men, millimetres, a block label or a division."},
            "date": {"type": "string", "description": "YYYY-MM-DD, optional."},
        },
        "required": ["operation", "constraint"],
    },
    "stock_position": {
        "fn": _t_stock_position,
        "desc": ("The estate store: for every material (fertiliser, herbicide, pest chemicals, diesel, spare "
                 "parts) or one group or one material, stock on hand and on order, days of cover, the reorder "
                 "point and its safety stock with why, the date an order is due, the chance of running out "
                 "before a delivery could arrive, and whether the learned reorder point or SAP's settings are in "
                 "force. Use for 'what do we need to order', 'will we run out of urea', 'how much diesel is left'."),
        "params": {
            "material": {"type": "string", "description": "Material name or number, optional, e.g. Urea or FE-002."},
            "group": {"type": "string", "enum": ["fertiliser", "agrochemical", "fuel", "parts"]},
            "date": {"type": "string", "description": "YYYY-MM-DD, optional; default tomorrow."},
        },
    },
    "supplier_lead_time": {
        "fn": _t_supplier_lead_time,
        "desc": ("How long each input supplier really takes against what it quotes (what SAP plans on): the "
                 "usual time, the time 1 order in 10 exceeds, the chance of a very late order and the slow "
                 "months, learned from purchase-order history. Use for 'is Pupuk Kaltim reliable', 'how long "
                 "does fertiliser take to arrive', 'why do we keep running out'."),
        "params": {
            "supplier": {"type": "string", "description": "Supplier name, optional."},
            "material": {"type": "string", "description": "Material name or number, optional."},
            "date": {"type": "string", "description": "YYYY-MM-DD, optional; default tomorrow."},
        },
    },
    "reorder_advice": {
        "fn": _t_reorder_advice,
        "desc": ("For one material: how much to order and by when, from which supplier, and what a higher or "
                 "lower service level would cost a year in stock held against rush buys; with the replay that "
                 "decides whether the learned reorder point or SAP's settings are used. Use for 'how much NPK "
                 "should we order', 'are we holding too much diesel', 'what if we accepted more stockouts'."),
        "params": {
            "material": {"type": "string", "description": "Material name or number, e.g. NPK or FE-001."},
            "date": {"type": "string", "description": "YYYY-MM-DD, optional; default tomorrow."},
        },
        "required": ["material"],
    },
    "rain_outlook": {
        "fn": _t_rain_outlook,
        "desc": ("Tomorrow's rain as chances a manager can act on: rain that washes off spraying "
                 "(15 mm), heavy rain (25 mm) and rain that stops field work, learned from real "
                 "forecasts against real rainfall; the spray-or-hold call with its costs; what the "
                 "forecast said and, on a past date, what fell; how far to trust it. Use for any "
                 "question about rain, weather, or whether to spray."),
        "params": {"date": {"type": "string", "description": "YYYY-MM-DD, optional; default tomorrow."}},
    },
    "headcount_forecast": {
        "fn": _t_headcount_forecast,
        "desc": ("Who is likely to turn up: for the estate, a crew type, a division or one crew, as a "
                 "most likely figure and a likely range, with the reasons the day differs from normal "
                 "and how accurate the forecast has been. Use for 'how many will turn up', 'will G1-03 "
                 "be short', Sunday or Lebaran questions."),
        "params": {
            "date": {"type": "string", "description": "YYYY-MM-DD, optional; default tomorrow."},
            "crew_type": {"type": "string", "enum": ["harvest", "upkeep", "spray", "pest"]},
            "crew": {"type": "string", "description": "Crew code, optional, e.g. G1-03."},
            "division": {"type": "string", "description": "Division code, optional."},
        },
    },
    "work_done_forecast": {
        "fn": _t_work_done_forecast,
        "desc": ("How much of tomorrow's plan for one operation is likely to get done, as a percentage "
                 "and a range, which blocks are likely to need another day and why (rain, road, the "
                 "block's record), expected output per crew, and what drives a miss. Use for 'how much "
                 "will we actually harvest', 'which blocks will slip'."),
        "params": {
            "operation": {"type": "string", "enum": ["harvest", "prune", "weed", "spray", "pest"]},
            "date": {"type": "string", "description": "YYYY-MM-DD, optional; default tomorrow."},
        },
        "required": ["operation"],
    },
    "crew_speeds": {
        "fn": _t_crew_speeds,
        "desc": ("Which pruning, weeding or spraying crews work faster or slower than the textbook "
                 "rate, or which harvest blocks go faster or slower than their target, learned from "
                 "the ledger and used to size each crew's day. Use for 'which crews are slow', "
                 "'is U2-04 fast'."),
        "params": {
            "operation": {"type": "string", "enum": ["harvest", "prune", "weed", "spray"]},
            "crew": {"type": "string", "description": "Crew code or block label, optional."},
            "date": {"type": "string", "description": "YYYY-MM-DD, optional."},
        },
        "required": ["operation"],
    },
    "forecast_accuracy": {
        "fn": _t_forecast_accuracy,
        "desc": ("How far to trust each of the four forecasts (rain, headcount, work done, crew "
                 "speeds): a trust grade from checks on past days each had not seen, compared with "
                 "the simple method it replaces, and the checks that it finds what is in the data "
                 "and invents nothing. Use when asked whether the forecasts are reliable or how "
                 "they were tested."),
        "params": {},
    },
    "loose_fruit_recovery": {
        "fn": _t_loose_fruit,
        "desc": ("Loose fruit per bunch by block and month, from the client's own "
                 "harvest records: which blocks collect least of the detached fruit, "
                 "the estate's achievable rate, and what the gap is worth. Real data. "
                 "Use for questions about loose fruit, brondolan, collection quality "
                 "or fruit left on the ground."),
        "params": _TOP,
    },
    "herbicide_adherence": {
        "fn": _t_herbicide,
        "desc": ("Whether the spraying round is being kept: realised spray interval "
                 "against the target round, hectares sprayed against due, misses "
                 "split into rained-off and other, and herbicide issued per hectare "
                 "against the register dose. Use for spraying, weeding, glyphosate "
                 "or herbicide questions."),
        "params": _TOP,
    },
    "ffa_against_delay": {
        "fn": _t_ffa,
        "desc": ("Free fatty acid against the hours between cutting and the mill "
                 "gate, by delay bucket, block and route; the share over the penalty "
                 "threshold and where the delay comes from (queue against road). Use "
                 "for FFA, fruit quality, oil quality or dispatch delay questions."),
        "params": _TOP,
    },
    "cutting_interval": {
        "fn": _t_cutting_interval,
        "desc": ("Realised days between cuts on each block, from the real harvest "
                 "dates, against the block's target round: median, P90, share of "
                 "rounds missed, the stretch by month, and the blocks whose round has "
                 "slipped most. Use for rotation, round length or cutting interval "
                 "questions."),
        "params": _TOP,
    },
    "bunch_weight_trend": {
        "fn": _t_abw_trend,
        "desc": ("Average bunch weight by block and month from the weighbridge, the "
                 "blocks whose bunch weight is falling fastest, and the weight-against-"
                 "age curve. Carries the calibration caveat on the level. Use for ABW, "
                 "bunch weight or tonnes-per-bunch questions."),
        "params": _TOP,
    },
    "canopy_early_warning": {
        "fn": _t_pest_warning,
        "desc": ("Blocks the satellite flags as weak or patchy that the pest census "
                 "has not: the targeted-census list, the blocks weak on both, and the "
                 "rank agreement between real canopy vigour and the census. Use for "
                 "early warning, where to send the census team, or whether the census "
                 "agrees with the satellite."),
        "params": _TOP,
    },
    "collection_coverage": {
        "fn": _t_collection,
        "desc": ("Whether fruit is collected the day it is cut: same-day evacuation "
                 "share by block, division and month, fruit left overnight, trips per "
                 "cutting day, load factor by route and the wait at the platform. Use "
                 "for collection, evacuation, platform, or fruit-left-overnight "
                 "questions."),
        "params": _TOP,
    },
    "focus_map": {
        "fn": _t_focus_map,
        "desc": ("Move the manager's map. Colour it by a metric, jump to a month, "
                 "highlight the blocks you are talking about, or open a decision "
                 "panel. Call this once you know the answer and before you write it, "
                 "whenever the answer is about particular blocks or a particular "
                 "metric. This is most of the value you add over a text reply."),
        "params": {
            "metric": {"type": "string",
                       "description": "Metric key to colour the map by."},
            "month": {"type": "string", "description": "YYYY-MM to scrub to."},
            "block_labels": {"type": "array", "items": {"type": "string"},
                             "description": "Blocks to highlight, by label."},
            "panel": {"type": "string",
                      "enum": ["contract", "vendors", "rotation", "labour",
                               "replant", "audit", "shrinkage", "turnaround",
                               "fuel", "fleet", "pest_census", "pest_spread",
                               "pest_damage", "pest_treatment", "nutrient",
                               "roads", "clusters", "productivity", "forecast",
                               "canopy", "fire", "ops_harvest", "ops_prune",
                               "ops_weed", "ops_pest", "ops_dispatch",
                               "assumptions", "outcomes", "forecasts", "stores",
                               "loose_fruit", "herbicide", "ffa", "cutting_interval",
                               "abw_trend", "pest_warning", "collection",
                               "peer_yield", "upkeep", "margin"],
                      "description": "A decision panel to open, optional."},
            "note": {"type": "string",
                     "description": "One short line shown on the map explaining why."},
        },
    },
}


def tool_schemas() -> list[dict]:
    return to_openai_tools([
        make_tool(name, t["desc"], t["params"], t.get("required"))
        for name, t in _TOOLS.items()
    ])


# ── keeping the answer and the map honest about each other ────────────────
#
# Nova narrates the map. Told four ways not to, it still closes with "the map
# has been focused on these blocks" - sometimes without having called
# focus_map at all, which is a claim about the world that is simply false.
#
# Rather than fight the model, make the claim true and then delete it: infer
# the focus from the blocks the answer actually names, and strip the sentence.
# Both halves are deterministic and neither can invent a block.

_MAP_NARRATION = re.compile(
    r"(?:^|(?<=[.!?]))\s*[^.!?]*\bmap\b[^.!?]*"
    r"\b(?:focus(?:ed|ing|es)?|highlight(?:ed|ing|s)?|centred|centered|"
    r"zoom(?:ed|ing)?|shown?|display(?:ed|ing|s)?|updated?)\b[^.!?]*[.!?]",
    re.IGNORECASE)


def strip_map_narration(answer: str) -> str:
    """Drop whole sentences whose only content is what the map is now showing."""
    cleaned = _MAP_NARRATION.sub(" ", answer or "")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    # Never hand back an empty answer because the model wrote nothing else.
    return cleaned or (answer or "").strip()


def infer_focus(answer: str, estate: str) -> dict | None:
    """Focus the map on the blocks the answer names, when the model did not.

    Only labels that exist on this estate are matched, so a hallucinated block
    highlights nothing rather than something arbitrary.
    """
    geo = ontology.blocks_geojson(estate)
    if not geo or not answer:
        return None
    by_label = {str(f["properties"].get("block_label")): f["id"]
                for f in geo["features"]}
    seen = []
    for token in re.findall(r"\b\d{2}-\d{2}\b", answer):
        if token in by_label and token not in seen:
            seen.append(token)
    if not seen:
        return None
    return {
        "estate": estate, "metric": None, "month": None, "panel": None,
        "block_labels": seen[:25],
        "block_ids": [by_label[lb] for lb in seen[:25]],
        "note": "Blocks named in the answer.",
        "inferred": True,
    }


# ── the loop ───────────────────────────────────────────────────────────────

def _serialise(result) -> dict:
    """LLMResult -> OpenAI-style assistant dict, the format the loop stores."""
    out = {"role": "assistant", "content": result.text or ""}
    calls = getattr(result.message, "tool_calls", None) or []
    if calls:
        out["tool_calls"] = [{
            "id": tc.get("id") or f"call_{i}",
            "type": "function",
            "function": {"name": tc["name"],
                         "arguments": json.dumps(tc.get("args") or {})},
        } for i, tc in enumerate(calls)]
    return out


def ask(question: str, estate: str = DEFAULT_ESTATE,
        view: dict | None = None, history: list[dict] | None = None) -> dict:
    """Answer one question about the estate. Returns answer, trace and focus.

    `history` is the prior Q&A pairs from the same chat, oldest first, e.g.
    [{"question": "...", "answer": "..."}, ...]. It seeds the conversation so
    a follow-up like "what about last month" resolves against what was
    already asked and answered, rather than starting cold each time.
    """
    import llm_client

    question = (question or "").strip()
    if not question:
        return reasoning.unavailable("Ask a question first.")
    if len(question) > 600:
        question = question[:600]

    ctx = {"estate": estate.upper(), "focus": None, "fire": None}
    # What the manager is currently looking at. Without it, "this block" and
    # "this month" have no referent and the model has to ask.
    view_note = ""
    if view:
        bits = [f"{k}={v}" for k, v in view.items() if v]
        if bits:
            view_note = ("\n\nThe manager is currently looking at: "
                         + ", ".join(bits) + ".")

    messages = [{"role": "system", "content": load_prompt("estate_copilot_system")}]
    for turn in (history or [])[-MAX_HISTORY_TURNS:]:
        prior_q = str(turn.get("question") or "").strip()[:600]
        prior_a = str(turn.get("answer") or "").strip()[:2000]
        if prior_q and prior_a:
            messages.append({"role": "user", "content": prior_q})
            messages.append({"role": "assistant", "content": prior_a})
    messages.append({"role": "user", "content": question + view_note})

    trace, usage_total = [], {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    model_id = None
    t0 = time.monotonic()
    answer = ""

    for turn in range(MAX_TURNS):
        last_turn = turn == MAX_TURNS - 1
        msgs = list(messages)
        if last_turn:
            msgs.append({"role": "user",
                         "content": "Tool budget spent. Answer now from what you "
                                    "have, and say plainly what you could not check."})
        try:
            result = llm_client.chat(
                msgs, tier="main", temperature=0.15, max_tokens=1400,
                tools=None if last_turn else tool_schemas(),
                tool_choice=None if last_turn else "auto",
                task="estate_copilot",
            )
        except Exception as exc:
            log.warning("[copilot] call failed on turn %d: %s", turn + 1, exc)
            return reasoning.unavailable(str(exc)[:300], trace=trace,
                                         question=question)

        model_id = result.model_id
        for k in usage_total:
            usage_total[k] += (result.usage or {}).get(k, 0) or 0
        msg = _serialise(result)
        messages.append(msg)

        calls = msg.get("tool_calls") or []
        if not calls:
            answer = (msg.get("content") or "").strip()
            break

        for tc in calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            t1 = time.monotonic()
            if name not in _TOOLS:
                observation = {"error": f"No tool named {name!r}.",
                               "available": list(_TOOLS)}
                ok = False
            else:
                try:
                    observation = _TOOLS[name]["fn"](args, ctx)
                    ok = "error" not in observation
                except Exception as exc:
                    log.warning("[copilot] tool %s failed: %s", name, exc)
                    observation = {"error": str(exc)[:300],
                                   "hint": "Say what you could not retrieve; do not "
                                           "substitute a guess."}
                    ok = False
            trace.append({
                "tool": name, "args": args, "ok": ok,
                "elapsed_ms": int((time.monotonic() - t1) * 1000),
                "observation": observation,
            })
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": reasoning.clip(observation, 6000)})

    answer = strip_map_narration(answer)
    if trace and not _is_substantive(answer):
        # The model reached for the tools, got the data, and then wrote nothing
        # worth reading - usually because it treated moving the map as the
        # deliverable. Ask once more, with the tools closed, for the finding.
        log.info("[copilot] non-answer %r, asking again for the finding", answer[:60])
        answer = strip_map_narration(_force_finding(messages, usage_total) or answer)
    if not answer:
        answer = ("I could not finish that within the tool budget. "
                  "Try a narrower question.")
    audit = reasoning.audit_figures(answer, [t["observation"] for t in trace])

    # One self-correction pass. The audit is deterministic, so this fires only
    # on a real violation: a figure in the answer that appears in no tool
    # result. Nova reliably reaches for arithmetic when asked what something
    # would cost, and handing it back its own unverified figures - with the
    # tools still available to go and fetch the real one - fixes most of them.
    # One pass only. A model that cannot correct itself twice is telling you
    # something, and the audit still rides on the response either way.
    if not audit["clean"]:
        log.warning("[copilot] unverified figures: %s", audit["unverified"])
        corrected = _self_correct(messages, audit, ctx, trace, usage_total)
        if corrected:
            answer = strip_map_narration(corrected)
            audit = reasoning.audit_figures(
                answer, [t["observation"] for t in trace])
            audit["self_corrected"] = True
            if audit["clean"]:
                log.info("[copilot] self-correction cleared the audit")

    focus = ctx["focus"] or infer_focus(answer, ctx["estate"])

    return {
        "available": True,
        "question": question,
        "estate": ctx["estate"],
        "answer": answer,
        "focus": focus,
        "trace": trace,
        "tools_used": [t["tool"] for t in trace],
        "figure_audit": audit,
        "model": model_id,
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "usage": usage_total,
    }


def _is_substantive(answer: str) -> bool:
    """An answer has to say something. Length alone does not qualify."""
    text = (answer or "").strip()
    if len(text) < 45:
        return False
    # Every question these tools can answer resolves to a figure, a block, or
    # a statement about what is missing. None of those can be said without
    # either a number or the words for their absence.
    if re.search(r"\d", text):
        return True
    return bool(re.search(r"\b(no|not|none|nothing|never|cannot|missing|"
                          r"unavailable|unrecorded)\b", text, re.IGNORECASE))


def _force_finding(messages, usage_total) -> str | None:
    """One tool-less turn that asks for the answer and nothing else."""
    import llm_client

    msgs = list(messages) + [{
        "role": "user",
        "content": ("That is not an answer. Using only the tool results above, "
                    "state the finding: the blocks, their figures, and what the "
                    "reading does not settle. Do not refer to the map."),
    }]
    try:
        result = llm_client.chat(msgs, tier="main", temperature=0.15,
                                 max_tokens=1200, task="estate_copilot_finding")
    except Exception as exc:
        log.warning("[copilot] forced finding failed: %s", exc)
        return None
    for k in usage_total:
        usage_total[k] += (result.usage or {}).get(k, 0) or 0
    return (result.text or "").strip() or None


def _self_correct(messages, audit, ctx, trace, usage_total) -> str | None:
    """Hand the model its own unverified figures and let it fix the answer."""
    import llm_client

    msgs = list(messages) + [{
        "role": "user",
        "content": (
            "These figures in your answer appear in no tool result you called: "
            + ", ".join(audit["unverified"]) + ". "
            "You are not permitted to compute a figure - not a total, not a "
            "product, not a percentage. Either call a tool that returns the "
            "figure you want, or rewrite the answer without it and say plainly "
            "that it was not computed. Reply with the corrected answer only."),
    }]
    try:
        result = llm_client.chat(
            msgs, tier="main", temperature=0.1, max_tokens=1200,
            tools=tool_schemas(), tool_choice="auto", task="estate_copilot_fix")
    except Exception as exc:
        log.warning("[copilot] self-correction failed: %s", exc)
        return None

    for k in usage_total:
        usage_total[k] += (result.usage or {}).get(k, 0) or 0
    msg = _serialise(result)

    # The correction may itself want a tool - most usefully vendor_ranking
    # with the shortfall it should have passed the first time. Run one round
    # of calls, then take the follow-up text.
    calls = msg.get("tool_calls") or []
    if not calls:
        return (msg.get("content") or "").strip() or None

    msgs.append(msg)
    for tc in calls:
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"] or "{}")
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        try:
            observation = (_TOOLS[name]["fn"](args, ctx) if name in _TOOLS
                           else {"error": f"No tool named {name!r}."})
            ok = "error" not in observation
        except Exception as exc:
            observation, ok = {"error": str(exc)[:300]}, False
        trace.append({"tool": name, "args": args, "ok": ok,
                      "elapsed_ms": None, "observation": observation,
                      "phase": "self-correction"})
        msgs.append({"role": "tool", "tool_call_id": tc["id"],
                     "content": reasoning.clip(observation, 6000)})
    try:
        final = llm_client.chat(msgs, tier="main", temperature=0.1,
                                max_tokens=1200, task="estate_copilot_fix")
    except Exception as exc:
        log.warning("[copilot] self-correction follow-up failed: %s", exc)
        return None
    for k in usage_total:
        usage_total[k] += (final.usage or {}).get(k, 0) or 0
    return (final.text or "").strip() or None


# Seeded questions for the empty state. Each one is answerable from the tools
# above, and each lands on a different half of the honest/invented split.
EXAMPLES = [
    "What does Division 1 harvest tomorrow, and what is not reached?",
    "G1-03 is four men short tomorrow. Replan.",
    "How much did rain cost the harvest plan in April?",
    "Which blocks are furthest behind their planting cohort?",
    "Show me the weakest canopy vigour and tell me if it agrees with yield.",
    "Can we cover the July contract, and what would it cost to buy the gap?",
    "Which blocks are overdue for harvest right now?",
    "What is the replanting cliff and why does it matter?",
    "What can this system not tell me about pests, and what would you need?",
]
