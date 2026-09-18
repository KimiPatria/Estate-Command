"""Phase 4.2 and 4.3 - action artifacts and the decision log.

Two things the demo needs and nothing else in the app provides:

  * an *artifact*. Accepting a recommendation has to produce a document a
    person would recognise - a harvesting plan, a work order, a purchase
    requisition - not a toast that says "done". The artifact is what makes the
    approval conversation concrete.

  * an *audit trail*. Every proposal, and what a human did with it, is written
    down. "Every decision the system proposes, and what your people did with
    it, is recorded" is the sentence that answers the governance question, and
    it is only true if something actually persists.

Nothing here writes to EPMS. Artifacts name the EPMS approval table they would
land in (log_harvesting_plan_approval, log_workplan_approval,
log_giplan_approval) and stop there. The log is a local SQLite file, separate
from every EPMS connection, and the EPMS engines are read-only anyway.
"""

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

log = logging.getLogger("estate-command.decisions")

DB_PATH = Path(__file__).parent / "data" / "decisions.db"
_LOCK = Lock()

# Which EPMS approval queue each artifact kind would land in. These tables are
# real and already carry the client's approval workflow; the demo stops at
# naming them.
ARTIFACT_KINDS = {
    "harvesting_plan": {
        "label": "Harvesting plan",
        "epms_table": "log_harvesting_plan_approval",
        "epms_entity": "t_harvesting_plan / t_harvester_assignment",
        "approver": "Assistant Manager",
        "provenance": ("Block identity, division and planted area are REAL, from "
                       "the client's ArcGIS export. Days since harvest, ripeness "
                       "pressure and the gang assignment are SYNTHETIC: the EC "
                       "export carries no last-harvest date and no gang roster."),
    },
    "work_order": {
        "label": "Work order",
        "epms_table": "log_workplan_approval",
        "epms_entity": "t_workplan / t_work_assignment",
        "approver": "Assistant Manager",
        "provenance": ("Block identity and planted area are REAL. Upkeep "
                       "intervals and days overdue are SYNTHETIC; EPMS records "
                       "no upkeep state in this export."),
    },
    "goods_issue": {
        "label": "Goods issue request",
        "epms_table": "log_giplan_approval",
        "epms_entity": "tr_gi_plan / tr_gi_plan_detail",
        "approver": "Estate Manager",
        "provenance": ("Block identity is REAL. Quantities and materials are "
                       "SYNTHETIC."),
    },
    "purchase_requisition": {
        "label": "Purchase requisition",
        "epms_table": "(SAP MM, via ZEPMS_EM_PURCHASE_ORDER_OUT)",
        "epms_entity": "purchase requisition",
        "approver": "Head Office Commercial",
        "provenance": ("EVERY figure on this document is SYNTHETIC. "
                       "ZEPMS_EM_VENDOR_OUT carries no address and no "
                       "coordinates, so landed cost is invented, and the "
                       "tonnage rests on an invented average bunch weight."),
    },
    "fire_mobilisation": {
        "label": "Fire mobilisation order",
        "epms_table": "(none - EPMS has no fire module)",
        "epms_entity": "not recorded in EPMS",
        "approver": "Estate Manager",
        "provenance": ("The detection and the exposure are REAL: NASA FIRMS "
                       "hotspots, live weather, and the client's own block "
                       "geometry and palm counts. Every post, crew number, "
                       "water source and travel time is SYNTHETIC."),
    },
    # The five operations' daily assignments. Same document shape, drafted
    # from the scheduler's plan; the EPMS queue differs by operation and pest
    # has none at all, which is the strongest line in the data request.
    "harvest_assignment": {
        "label": "Harvest assignment",
        "epms_table": "log_harvesting_plan_approval",
        "epms_entity": "t_harvesting_plan / t_harvester_assignment",
        "approver": "Assistant Manager",
        "provenance": ("Block identity and planted area are REAL. Every other "
                       "figure is SCHEDULED: ripe bunches, tonnes and rupiah are "
                       "computed from the generated ledger and the assumption "
                       "register, and the gang's present figure is a forecast "
                       "from generated attendance."),
    },
    "upkeep_assignment": {
        "label": "Upkeep assignment",
        "epms_table": "log_workplan_approval",
        "epms_entity": "t_workplan / t_work_assignment",
        "approver": "Assistant Manager",
        "provenance": ("Block identity, planted area and palm counts are REAL. "
                       "Days overdue, crews, rates and the rupiah at risk are "
                       "SCHEDULED from the generated ledger and the assumption "
                       "register."),
    },
    "pest_assignment": {
        "label": "Pest & disease assignment",
        "epms_table": "(none - no EPMS table can hold a pest work order)",
        "epms_entity": "not recorded in EPMS",
        "approver": "Estate Manager",
        "provenance": ("Block identity and palm counts are REAL. Everything "
                       "else is SYNTHETIC: the census, the treatments, the "
                       "follow-ups and the team. There is no EPMS queue for "
                       "this document to enter, which is the finding."),
    },
    "dispatch_assignment": {
        "label": "Dispatch assignment",
        "epms_table": "(SAP PM, vehicle trip plan)",
        "epms_entity": "trip plan",
        "approver": "Transport Supervisor",
        "provenance": ("Block identity is REAL. Tonnes rest on a calibrated "
                       "bunch weight and the scheduled harvest plan; vehicles, "
                       "loads and turnaround are SYNTHETIC."),
    },
    # The store's two documents (buildplan_stores.md): buy now, or fix the
    # setting that keeps making the store buy late.
    "material_requisition": {
        "label": "Purchase requisition (stores)",
        "epms_table": "(SAP MM, ME51N purchase requisition)",
        "epms_entity": "purchase requisition",
        "approver": "Estate Manager",
        "provenance": ("EVERY figure on this document is SYNTHETIC: the stock, the "
                       "use and the purchase-order history come from generated MM "
                       "records shaped as MB51, ME80FN and MB52. The reorder point "
                       "is computed from them."),
    },
    "mrp_settings_change": {
        "label": "Material master change",
        "epms_table": "(SAP MM, MM02 on MARC)",
        "epms_entity": "MRP settings: reorder point, safety stock, planned delivery time",
        "approver": "Head Office Procurement",
        "provenance": ("The current values are SAP's settings as generated; the "
                       "proposed values are learned from generated purchase-order "
                       "and goods-issue history."),
    },
    "inspection_order": {
        "label": "Field inspection order",
        "epms_table": "log_workplan_approval",
        "epms_entity": "t_workplan",
        "approver": "Assistant Manager",
        "provenance": ("Everything on this document is REAL: block identity and "
                       "planted area from the client's ArcGIS export, and the "
                       "peer index derived from their own EPMS harvest records."),
    },
}


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with _LOCK, _conn() as c:
        c.execute("""
            create table if not exists decisions (
                id            text primary key,
                created_at    text not null,
                estate        text,
                use_case      text,
                title         text,
                subject       text,
                action        text,
                actor         text,
                evidence      text,
                artifact_kind text,
                artifact      text,
                epms_table    text
            )
        """)
        c.execute("create index if not exists ix_decisions_created on decisions(created_at desc)")
        # The loop-closing columns, added to a log that predates them. The
        # system proposes, a human accepts, and these four are what lets
        # something later check whether it worked.
        have = {r[1] for r in c.execute("pragma table_info(decisions)").fetchall()}
        for col in ("due_date", "expected_effect", "order_ref", "observed_effect"):
            if col not in have:
                c.execute(f"alter table decisions add column {col} text")
    log.info("[decisions] log ready at %s", DB_PATH)


def record(estate, use_case, title, subject, action, evidence,
           artifact_kind=None, artifact=None, actor="demo user",
           due_date=None, expected_effect=None, order_ref=None) -> dict:
    """Write one accept / reject / defer to the log and return the row.

    due_date, expected_effect and order_ref are what a scheduled plan carries:
    when it should have happened, what the scheduler expected, and the work
    orders it drafted. observed_effect is never written here; gis/ops.py reads
    it back from the ledger once the date has passed, so the log records what
    was promised and the ledger says what was delivered.
    """
    init()
    row = {
        "id": uuid.uuid4().hex[:12],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "estate": estate,
        "use_case": use_case,
        "title": title,
        "subject": subject,
        "action": action,
        "actor": actor,
        "evidence": json.dumps(evidence or [], ensure_ascii=False),
        "artifact_kind": artifact_kind,
        "artifact": json.dumps(artifact, ensure_ascii=False) if artifact else None,
        "epms_table": (ARTIFACT_KINDS.get(artifact_kind) or {}).get("epms_table"),
        "due_date": due_date,
        "expected_effect": (json.dumps(expected_effect, ensure_ascii=False)
                            if expected_effect else None),
        "order_ref": json.dumps(order_ref, ensure_ascii=False) if order_ref else None,
        "observed_effect": None,
    }
    with _LOCK, _conn() as c:
        c.execute(
            "insert into decisions (id, created_at, estate, use_case, title, subject, "
            "action, actor, evidence, artifact_kind, artifact, epms_table, due_date, "
            "expected_effect, order_ref, observed_effect) values (:id,:created_at,"
            ":estate,:use_case,:title,:subject,:action,:actor,:evidence,:artifact_kind,"
            ":artifact,:epms_table,:due_date,:expected_effect,:order_ref,:observed_effect)",
            row)
    log.info("[decisions] %s: %s on %s", action, use_case, subject)
    return _hydrate(row)


def history(limit: int = 100, estate: str | None = None) -> dict:
    init()
    q = "select * from decisions"
    args: list = []
    if estate:
        q += " where estate = ?"
        args.append(estate.upper())
    q += " order by created_at desc limit ?"
    args.append(limit)
    with _LOCK, _conn() as c:
        rows = [_hydrate(dict(r)) for r in c.execute(q, args).fetchall()]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    return {"decisions": rows, "total": len(rows), "by_action": counts,
            "db": str(DB_PATH)}


def clear() -> int:
    init()
    with _LOCK, _conn() as c:
        n = c.execute("select count(*) from decisions").fetchone()[0]
        c.execute("delete from decisions")
    return n


def _hydrate(row: dict) -> dict:
    out = dict(row)
    for f in ("evidence", "artifact", "expected_effect", "order_ref", "observed_effect"):
        if out.get(f):
            try:
                out[f] = json.loads(out[f])
            except (TypeError, ValueError):
                pass
    return out


# ── artifact drafting ──────────────────────────────────────────────────────

def draft(kind: str, estate: str, payload: dict) -> dict:
    """Build the document a person would recognise, without sending it anywhere.

    The `status` is always "drafted". Nothing is transmitted, nothing is
    written to EPMS, and the artifact says which queue it would enter.
    """
    meta = ARTIFACT_KINDS.get(kind)
    if not meta:
        return {}
    now = datetime.now(timezone.utc)
    ref = f"{kind[:2].upper()}-{estate.upper()}-{now:%Y%m%d}-{uuid.uuid4().hex[:4].upper()}"

    doc = {
        "reference": ref,
        "kind": kind,
        "label": meta["label"],
        "estate": estate.upper(),
        "drafted_at": now.isoformat(timespec="seconds"),
        "status": "drafted",
        "would_route_to": meta["epms_table"],
        "would_create": meta["epms_entity"],
        "approver_role": meta["approver"],
        "lines": [],
        "disclaimer": ("Draft only. Nothing was written to EPMS and nothing was "
                       "sent. In production this would enter the approval queue "
                       "the client already uses."),
    }

    if kind == "harvesting_plan":
        doc["title"] = f"Harvesting plan, {payload.get('date', 'next round')}"
        doc["lines"] = [{
            "block": b.get("block_label"),
            "division": b.get("division_code"),
            "gang": b.get("gang_code"),
            "days_since_harvest": b.get("days_since_harvest"),
            "ripeness_pressure": b.get("ripeness_pressure"),
            "planted_ha": b.get("planted_ha"),
        } for b in payload.get("blocks", [])]
        doc["summary"] = (f"{len(doc['lines'])} blocks, "
                          f"{sum(l['planted_ha'] or 0 for l in doc['lines']):.1f} ha")

    elif kind == "purchase_requisition":
        doc["title"] = f"FFB purchase requisition, {payload.get('month', '')}"
        doc["lines"] = [{
            "vendor_code": a.get("vendor_code"),
            "vendor": a.get("name"),
            "tonnes": a.get("tonnes"),
            "landed_idr_per_kg": a.get("landed_idr_per_kg"),
            "value_idr": a.get("cost_idr"),
        } for a in payload.get("allocation", [])]
        doc["summary"] = (f"{sum(l['tonnes'] or 0 for l in doc['lines']):,.0f} t "
                          f"across {len(doc['lines'])} vendors")

    elif kind == "material_requisition":
        doc["title"] = f"Purchase requisition, {payload.get('maktx', '')}"
        doc["lines"] = [{
            "material": payload.get("matnr"),
            "description": payload.get("maktx"),
            "quantity": payload.get("qty"),
            "unit": payload.get("unit"),
            "supplier": payload.get("supplier"),
            "order_by": payload.get("order_by"),
            "reorder_point": payload.get("reorder_point"),
            "safety_stock": payload.get("safety_stock"),
        }]
        doc["summary"] = f"{payload.get('qty', 0):,.0f} {str(payload.get('unit', '')).lower()} from {payload.get('supplier', '')}"

    elif kind == "mrp_settings_change":
        doc["title"] = f"MRP settings change, {payload.get('maktx', '')}"
        doc["lines"] = [{"material": payload.get("matnr"), "field": l.get("field"),
                         "current": l.get("current"), "proposed": l.get("proposed")}
                        for l in payload.get("lines", [])]
        doc["summary"] = f"{len(doc['lines'])} fields on {payload.get('matnr', '')}"

    elif kind == "fire_mobilisation":
        m = payload.get("mobilisation", {})
        doc["title"] = f"Fire mobilisation, block {m.get('target_block')}"
        doc["lines"] = [{
            "post": (m.get("post") or {}).get("name"),
            "crew": (m.get("post") or {}).get("crew_on_shift"),
            "target_block": m.get("target_block"),
            "road_km": (m.get("post") or {}).get("road_km"),
            "travel_minutes": (m.get("post") or {}).get("travel_minutes"),
            "water_source": (m.get("water") or {}).get("name"),
        }]
        doc["summary"] = (f"{payload.get('blocks', 0)} blocks exposed, "
                          f"{payload.get('ha', 0)} ha")

    elif kind == "inspection_order":
        doc["title"] = f"Field inspection, block {payload.get('block_label')}"
        doc["lines"] = [{
            "block": payload.get("block_label"),
            "division": payload.get("division_code"),
            "reason": payload.get("reason"),
            "peer_index": payload.get("peer_index"),
            "planted_ha": payload.get("planted_ha"),
        }]
        doc["summary"] = payload.get("reason", "")

    elif kind.endswith("_assignment"):
        # Drafted from the scheduler's plan: one line per crew and block, in
        # the order the crew is meant to work them. The order reference on
        # each line is the work order this decision generates.
        plan = payload.get("plan") or {}
        doc["title"] = (f"{meta['label']}, {plan.get('date_label') or plan.get('date') or 'tomorrow'}")
        doc["plan_date"] = plan.get("date")
        for c in plan.get("crews") or []:
            for b in c.get("blocks") or []:
                doc["lines"].append({
                    "order_ref": b.get("order_ref"),
                    "crew": c.get("crew_code"),
                    "present": c.get("present"),
                    "block": b.get("block_label"),
                    "division": b.get("division_code"),
                    "activity": b.get("activity"),
                    "qty": b.get("qty"), "unit": b.get("unit"),
                    "man_days": b.get("man_days"),
                    "tonnes": b.get("tonnes"),
                    "days_over_round": b.get("days_over_round"),
                })
        t = plan.get("totals") or {}
        doc["summary"] = (f"{t.get('crews_with_work', 0)} crews, {t.get('blocks', 0)} blocks, "
                          f"{t.get('ha', 0)} ha, {t.get('tonnes', 0)} t, "
                          f"{t.get('used_md', 0)} man-days")
        nr = plan.get("not_reached") or {}
        if nr.get("blocks"):
            doc["not_reached"] = (f"{nr['blocks']} blocks not reached, deferring costs "
                                  f"{(nr.get('deferral_cost_idr_per_week') or 0) / 1e6:,.1f}M IDR this week")

    elif kind == "work_order":
        doc["title"] = f"Upkeep work order, {payload.get('activity', '')}"
        doc["lines"] = [{
            "block": b.get("block_label"),
            "division": b.get("division_code"),
            "activity": b.get("activity"),
            "days_overdue": b.get("days_overdue"),
            "planted_ha": b.get("planted_ha"),
        } for b in payload.get("blocks", [])]
        doc["summary"] = (f"{len(doc['lines'])} blocks overdue for "
                          f"{payload.get('activity', 'upkeep')}")

    return doc
