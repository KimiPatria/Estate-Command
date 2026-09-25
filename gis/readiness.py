"""UC-15 - the data readiness register, and the interview that updates it.

Two halves.

The register (CAPABILITIES) is the discovery instrument: every status below
was measured against the two EPMS databases and the estate exports, not
assumed, and the measurement is quoted in `evidence` so the panel can show its
working when the client pushes back on a red row. It lived in gis_router.py
until the interview needed to read it too.

The interview is the half that makes it a conversation. A red row states what
is missing; the model turns that into the questions worth asking in the room,
and the client's answer is scored back against the row - "this changes the
status to partial, and here is what it unlocks" - and persisted. That answer
is the most valuable thing produced in a discovery meeting and it is normally
lost in someone's notebook.

Scoring is advisory. The model may propose a revised status and the panel
shows it as *proposed*, beside the measured one, never overwriting it. A
sentence in a meeting is not a measurement, and the register only means
anything if the difference is visible.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from threading import Lock

from gis.decisions import DB_PATH

log = logging.getLogger("estate-command.readiness")

_LOCK = Lock()

STATUSES = ("ready", "partial", "synthetic", "unavailable")

_READINESS = [
    {
        "id": "block_map",
        "capability": "Block-level map (UC-01, 02, 03, 10, 11)",
        "needs": "Block boundary polygons",
        "status": "ready",
        "evidence": "844 real block polygons across all three estates on the "
                    "map - EC 291, EA 196, EB 357 - held in EPMS m_overlay as "
                    "the client's ArcGIS export (Shape_Leng / Shape_Area "
                    "attributes present). Every harvested block joins to one.",
        "degrades_to": "Estate outline only, no block layer.",
        "ask": "Nothing for these three. Any further estate needs its "
               "m_overlay rows, which EPMS already carries company-wide.",
    },
    {
        "id": "yield_choropleth",
        "capability": "Yield and forecast choropleth (UC-08)",
        "needs": "Per-block harvest history",
        "status": "partial",
        "evidence": "Every harvested block joins to recorded harvest (EC 291, "
                    "EA 196, EB 356 of 357), but the databases hold 19 months "
                    "for EC (2025-01-01 to 2026-07-22), eight for EB "
                    "(2025-06-02 to 2026-01-31) and seven weeks for EA "
                    "(2025-06-02 to 2025-07-20) - against the multi-year "
                    "history the age-curve and replanting work assume.",
        "degrades_to": "Trailing-mean per block, not a fitted model.",
        "ask": "Full harvest history for the blocks we have geometry for.",
    },
    {
        "id": "pest_watchlist",
        "capability": "Pest and disease watchlist (UC-04)",
        "needs": "Grading deductions with usable variance, or a palm census",
        "status": "unavailable",
        "evidence": "EC grading is degenerate across all 291 blocks: mean "
                    "deduction rate 0.25%, maximum 2.1%, minimum ripe rate "
                    "98.8%. There is no variance to detect against. There is "
                    "also no table in the 138-table schema that can hold an "
                    "infected palm.",
        "degrades_to": "Nothing. The proxy has no signal to detect.",
        "ask": "Is grading recorded at the mill rather than the block? And is "
               "there any Ganoderma census, in any form, even a spreadsheet?",
    },
    {
        "id": "vegetation_stress",
        "capability": "Vegetation stress (UC-03)",
        "needs": "Block polygons plus Sentinel-2",
        "status": "ready",
        "evidence": "EC polygons are WGS84 and Sentinel-2 L2A is free over "
                    "Merauke. Needs nothing from the client beyond what EC "
                    "already provides.",
        "degrades_to": "Sentinel-1 SAR where cloud blocks optical.",
        "ask": "None. This one runs today.",
    },
    {
        "id": "terrain",
        "capability": "Terrain slope and canopy roughness",
        "needs": "Block polygons plus a public elevation model",
        "status": "ready",
        "evidence": "Copernicus DEM GLO-30 measured over all 291 EC blocks at "
                    "30 m. Needs nothing from the client.",
        "degrades_to": "Harvester targets set without any terrain adjustment.",
        "ask": "None. This one runs today.",
    },
    {
        "id": "rainfall",
        "capability": "Rainfall history for lagged yield modelling",
        "needs": "Multi-year daily rainfall over the estate",
        "status": "ready",
        "evidence": "Open-Meteo historical archive, free and keyless, pulled "
                    "for the estate centroid. One reanalysis grid cell covers "
                    "EC, so this is an estate series rather than a per-block "
                    "surface.",
        "degrades_to": "A yield model with no weather term at all.",
        "ask": "None. This one runs today, and it takes rainfall off the "
               "request list for the lagged forecast.",
    },
    {
        "id": "harvest_history",
        "capability": "Lagged yield forecasting (UC-08)",
        "needs": "Multi-year block-level harvest history",
        "status": "unavailable",
        "evidence": "Oil palm sets its bunch load 20-24 months before the cut, "
                    "so the lag structure needs at least three years of target "
                    "series to identify. EC's database carries 2025-01-01 to "
                    "2026-07-22: 19 months, the longest of the three. The "
                    "synthetic forward months are still built on the first "
                    "five of them.",
        "degrades_to": "A trailing mean, which is what the forward months "
                       "currently show.",
        "ask": "t_oph for the EC blocks back to 2022. The geometry and the "
               "join key are already proven on the 19 months we have.",
    },
    {
        "id": "weighbridge",
        "capability": "Field-to-mill shrinkage and true bunch weight",
        "needs": "TPH bunch counts per trip, and mill weighbridge tickets",
        "status": "unavailable",
        "evidence": "The EC export carries bunch counts with no weights and no "
                    "trips. Summed as recorded the estate runs 2,870 bunches "
                    "per hectare per year, which at a normal 12-16 kg bunch "
                    "implies 34-46 t/ha/yr against a ~28 t/ha/yr ceiling for a "
                    "very good estate. Something already disagrees between "
                    "count and weight, and neither side can be checked without "
                    "the tickets.",
        "degrades_to": "Bunch counts with an assumed weight. Every priced "
                       "layer inherits the assumption.",
        "ask": "Weighbridge tickets with the trip, vehicle and driver, joined "
               "to the TPH count that trip collected.",
    },
    {
        "id": "fleet",
        "capability": "Transport efficiency and fleet reliability",
        "needs": "Vehicle trip logs, fuel issues, breakdown work orders",
        "status": "unavailable",
        "evidence": "SAP Plant Maintenance holds all three. None of them are "
                    "in the EC export, which carries a single synthetic "
                    "transport cost line and a straight-line distance to mill.",
        "degrades_to": "Haulage priced as a flat rate per kilometre.",
        "ask": "Trip logs with depart and arrive timestamps, fuel goods "
               "issues against vehicle, and PM breakdown orders.",
    },
    {
        "id": "mill_lab",
        "capability": "Quality decay and mill reconciliation",
        "needs": "Mill grading dockage and daily laboratory reports",
        "status": "unavailable",
        "evidence": "No OER, FFA or KER anywhere in the export. Free fatty "
                    "acid climbs with the delay between cut and mill, which "
                    "is the one quality loss the estate directly controls.",
        "degrades_to": "No view of quality at all past the collection point.",
        "ask": "Daily lab reports and mill grading dockage, by consignment.",
    },
    {
        "id": "pest_census",
        "capability": "Pest and disease management (UC-04)",
        "needs": "A palm census, scouting rounds and treatment records",
        "status": "unavailable",
        "evidence": "No table in the 138-table schema can hold an infected "
                    "palm. The only proxy, grading deductions, is degenerate: "
                    "mean 0.25% across all 291 blocks. The estate currently "
                    "has no visibility into pest pressure of any kind.",
        "degrades_to": "Nothing. The entire domain is dark.",
        "ask": "Any census in any form, even a spreadsheet. This is the "
               "largest blind spot in the whole operation.",
    },
    {
        "id": "fertiliser",
        "capability": "Nutrition against agronomy programme",
        "needs": "MM goods issues in kilograms, and the dosage programme",
        "status": "unavailable",
        "evidence": "The generated cost ledger carries a fertiliser line in "
                    "rupiah, which cannot answer an agronomic question. "
                    "Nutrition is judged in kilograms of nutrient per palm "
                    "against a programme, not in currency.",
        "degrades_to": "Fertiliser as a cost centre rather than an input.",
        "ask": "Goods issues per block by material, plus the agronomy "
               "recommendation they are meant to satisfy.",
    },
    {
        "id": "herbicide",
        "capability": "Weeding and herbicide adherence",
        "needs": "Herbicide batches issued per block",
        "status": "unavailable",
        "evidence": "SAP MM holds the issues. The EC export carries none, and "
                    "the upkeep feed records only that an activity happened.",
        "degrades_to": "Round adherence with no rate and no chemical.",
        "ask": "Herbicide and pesticide goods issues by block and date.",
    },
    {
        "id": "roads",
        "capability": "Road condition and haulage cost",
        "needs": "Road grading and culvert repair work orders",
        "status": "unavailable",
        "evidence": "Road condition is the largest controllable term in "
                    "haulage cost and in crop damage during evacuation. "
                    "Nothing in the export touches it.",
        "degrades_to": "Distance-only transport cost, blind to road state.",
        "ask": "PM work orders for road grading and culvert repair, with the "
               "segment they were raised against.",
    },
    {
        "id": "worker_output",
        "capability": "Crew productivity and fair targets",
        "needs": "Per-worker daily output with the block worked",
        "status": "unavailable",
        "evidence": "EPMS records harvest per worker in t_oph, but the EC "
                    "export is aggregated to block and month. Terrain, which "
                    "is the other half of a fair target, is already measured.",
        "degrades_to": "Flat quotas, which is what drives turnover on the "
                       "difficult blocks.",
        "ask": "t_oph at employee grain for the EC blocks.",
    },
    {
        "id": "loose_fruit",
        "capability": "Loose fruit recovery",
        "needs": "Loose fruit collected per block",
        "status": "partial",
        "evidence": "t_oph carries a loose_fruits count on every harvest "
                    "record: 38,067,571 fruits against 23,891,875 bunches over "
                    "892,869 records, 2025-01-01 to 2026-07-22, all 291 EC "
                    "blocks. Block ratios run 0.58 to 2.53 per bunch (median "
                    "1.57); Division 3 collects 1.31 against Division 1's "
                    "2.13. The count is non-zero on 57% of records, and the "
                    "low-ratio blocks are also the ones recording it least. "
                    "Counts, not kilograms: nothing in the export weighs "
                    "loose fruit.",
        "degrades_to": "Loose fruit per bunch by block and month, and a "
                       "recovery rate against the estate's own upper quartile. "
                       "Tonnes and rupiah rest on a placeholder fruit weight.",
        "ask": "Loose fruit weight at the collection point - the TPH weighing "
               "slip or the mill's loose-fruit line - joined to the block and "
               "day. And a rule for the zeros: does a harvest record with no "
               "count mean none collected, or none written down?",
    },
    {
        "id": "upkeep_log",
        "capability": "Upkeep rotation adherence",
        "needs": "Activity completion dates per block",
        "status": "synthetic",
        "evidence": "EPMS records that an activity happened; the EC export "
                    "carries none of it. Generated here against standard "
                    "rotations of 240, 75, 110 and 100 days.",
        "degrades_to": "No upkeep view.",
        "ask": "Upkeep completion dates per block and activity.",
    },
    {
        "id": "contract",
        "capability": "Contract position (UC-08)",
        "needs": "FFB delivery commitments",
        "status": "synthetic",
        "evidence": "ZEPMS_SD_SORD_OUT exists in EPMS but the EC export "
                    "carries no orders. Commitments are generated against the "
                    "estate's own production so the gap moves realistically.",
        "degrades_to": "Production with nothing to measure it against.",
        "ask": "Open sales orders by month and customer.",
    },
    {
        "id": "tonnage",
        "capability": "Tonnage, and anything priced (UC-08, 09, 10, 11)",
        "needs": "Average bunch weight",
        "status": "synthetic",
        "evidence": "The EC export carries bunch counts only, no t_abw. Summed "
                    "correctly the estate runs 20.8 bunches per palm per year "
                    "against a normal 10-14, so the counts cannot be converted "
                    "to tonnage by assuming a textbook bunch weight without "
                    "producing a number an agronomist will reject.",
        "degrades_to": "Bunch counts only. Every priced layer switches off.",
        "ask": ("t_abw for the EC blocks, or mill weighbridge tickets. This is "
                "the single highest-value missing feed: it is the keystone under "
                "cost per kg, margin, contract position and vendor sourcing."),
    },
    {
        "id": "replanting",
        "capability": "Replanting and capital allocation (UC-11)",
        "needs": "Planting years",
        "status": "ready",
        "evidence": "Every EC block was planted 2015-2019, so nothing needs "
                    "replanting today. That uniformity is the finding: all 291 "
                    "blocks reach age 25 inside a five-year window, and 52% of "
                    "the estate falls due in 2041 alone. Replanting on schedule "
                    "would idle half the estate at once with three immature "
                    "years behind it.",
        "degrades_to": "Nothing. This card runs entirely on the client's own "
                       "planting years.",
        "ask": "None for the finding. Replant cost and CPO price assumptions "
               "are needed to turn it into an NPV-ranked schedule.",
    },
    {
        "id": "stores",
        "capability": "Stores: safety stock and reorder points",
        "needs": "PO history with goods receipts, goods movements, MRP settings, reservations",
        "status": "synthetic",
        "evidence": "SAP MM holds all four; the EC export carries none. Twenty-four "
                    "months of store records are generated, reconciled to the "
                    "operations ledger from 2025, under SAP's own settings, so the "
                    "learned reorder points are judged against the policy the "
                    "recorded stock came from.",
        "degrades_to": "Days of cover against the quoted lead time: a flag, not a policy.",
        "ask": ("MB51, ME80FN with EKBE, MM60 with the MARC fields, MD04 or RESB, and "
                "MB52 for plant EC over 24 months. Five standard reports, no custom "
                "development."),
    },
    {
        "id": "vendor_sourcing",
        "capability": "Vendor sourcing (UC-09)",
        "needs": "Vendor locations",
        "status": "synthetic",
        "evidence": "ZEPMS_EM_VENDOR_OUT carries three fields - LIFNR, NAME1, "
                    "WERKS. No address, no coordinates.",
        "degrades_to": "Ranking by price and fill rate, with no landed cost "
                       "and no map.",
        "ask": ("Vendor addresses from SAP LFA1, for geocoding. Small, winnable, "
                "and being an SAP consultancy is exactly why it is easy for you."),
    },
    {
        "id": "cost_margin",
        "capability": "Cost and margin by block (UC-10)",
        "needs": "Cost ledger routed to block",
        "status": "synthetic",
        "evidence": "m_cost_control_mapping already encodes the routing rule in "
                    "EPMS, but the EC export carries no cost at all. The layer "
                    "runs on a generated ledger of 30M IDR/ha/yr split across "
                    "harvest, upkeep, fertiliser, transport and overhead.",
        "degrades_to": "Nothing priced. Yield layers still work.",
        "ask": "A cost extract routed through m_cost_control_mapping.",
    },
    {
        "id": "harvest_rotation",
        "capability": "Harvest rotation (UC-01)",
        "needs": "Rotation state and gang assignment",
        "status": "synthetic",
        "evidence": "Ripeness pressure needs last-harvest date per block and "
                    "EPMS has it in t_oph; the EC export does not. Gang "
                    "assignment needs m_gang_employee, also absent. Both are "
                    "generated here on a 7-14 day round.",
        "degrades_to": "No rotation view at all.",
        "ask": "Last-harvest date per block and the gang roster. Both already "
               "exist in EPMS.",
    },
    {
        "id": "labour_deficit",
        "capability": "Labour deficit (UC-12)",
        "needs": "Attendance and gang establishment",
        "status": "synthetic",
        "evidence": "t_attendance and m_gang_employee exist in EPMS; the EC "
                    "export carries neither. Generated at one harvester per "
                    "13 ha with the Ramadan and Lebaran migration modelled "
                    "explicitly on March and April.",
        "degrades_to": "No labour view.",
        "ask": "t_attendance for the EC estate.",
    },
    {
        "id": "operations_ledger",
        "capability": "Work orders, crew assignment and daily attendance",
        "needs": "Planned beside actual, per block per crew per day",
        "status": "synthetic",
        "evidence": "EPMS holds t_harvesting_plan, t_harvester_assignment, "
                    "t_workplan and t_work_assignment; the EC export carries "
                    "none of them, and no table at all exists for a pest work "
                    "order. Generated here as 143 days of orders across six "
                    "operations, simulated crew by crew against real daily "
                    "rainfall, with harvest actuals reconciling to the real "
                    "block-month bunch counts.",
        "degrades_to": "Demand signals with no supply side: which blocks are "
                       "overdue, never who goes where tomorrow.",
        "ask": "The plan and assignment tables at day grain, and t_attendance "
               "per crew per day. Both already exist in EPMS. For pest work "
               "there is nothing to ask for yet, which is itself the finding.",
    },
    {
        "id": "fire",
        "capability": "Fire and force majeure (UC-06)",
        "needs": "NASA FIRMS hotspots plus asset locations",
        "status": "partial",
        "evidence": "Hotspots and weather are live and real: NASA FIRMS VIIRS "
                    "returned 364 non-low-confidence detections within 50 km of "
                    "EC, and Open-Meteo supplies wind, humidity and 14-day "
                    "rainfall. Block geometry and palm counts are the client's. "
                    "Fire posts, water sources and crew rosters are recorded "
                    "nowhere in EPMS and are SYNTHETIC in this build - 17 "
                    "invented assets placed against the real geometry.",
        "degrades_to": "Real detection, real exposure, invented mobilisation. "
                       "The triage is sound; the response plan is a placeholder.",
        "ask": "Fire post, watch tower and reservoir coordinates, plus the shift "
               "roster. Small collection job, and it is the only thing standing "
               "between this card and a real one.",
    },
]

CAPABILITIES = _READINESS          # public name; _READINESS kept for the diff


def _by_id() -> dict:
    return {c["id"]: c for c in CAPABILITIES}


def get(cap_id: str) -> dict | None:
    return _by_id().get(cap_id)


# ── the interview store ────────────────────────────────────────────────────
#
# Shares decisions.db, because both tables answer the same governance
# question - what was proposed, and what a human said about it - and a demo
# with two SQLite files to reset is a demo that gets reset wrong.

def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with _LOCK, _conn() as c:
        c.execute("""
            create table if not exists readiness_answers (
                capability_id  text primary key,
                answered_at    text not null,
                answer         text not null,
                actor          text,
                verdict        text
            )
        """)


def record_answer(cap_id: str, answer: str, verdict: dict | None = None,
                  actor: str = "demo user") -> dict:
    """Store what the client said about one capability, and how it scored."""
    init()
    row = {
        "capability_id": cap_id,
        "answered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "answer": answer.strip(),
        "actor": actor,
        "verdict": json.dumps(verdict, ensure_ascii=False) if verdict else None,
    }
    with _LOCK, _conn() as c:
        c.execute("insert or replace into readiness_answers values "
                  "(:capability_id,:answered_at,:answer,:actor,:verdict)", row)
    log.info("[readiness] answer recorded for %s", cap_id)
    return _hydrate(row)


def answers() -> dict:
    init()
    with _LOCK, _conn() as c:
        rows = [_hydrate(dict(r)) for r in
                c.execute("select * from readiness_answers").fetchall()]
    return {r["capability_id"]: r for r in rows}


def clear() -> int:
    init()
    with _LOCK, _conn() as c:
        n = c.execute("select count(*) from readiness_answers").fetchone()[0]
        c.execute("delete from readiness_answers")
    return n


def _hydrate(row: dict) -> dict:
    out = dict(row)
    if out.get("verdict"):
        try:
            out["verdict"] = json.loads(out["verdict"])
        except (TypeError, ValueError):
            pass
    return out


# ── the register as the UI reads it ────────────────────────────────────────

def catalogue() -> dict:
    """Every capability, with live evidence and any interview answer applied.

    `status` stays the measured one. A recorded answer rides alongside it as
    `proposed_status`, so the panel can show movement without the register
    quietly rewriting its own measurements.
    """
    stored = answers()
    rows = []
    for cap in CAPABILITIES:
        row = dict(cap)
        live = _live_evidence(cap["id"])
        if live:
            row.update(live)
        ans = stored.get(cap["id"])
        if ans:
            verdict = ans.get("verdict") or {}
            row["interview"] = {
                "answer": ans["answer"],
                "answered_at": ans["answered_at"],
                "actor": ans.get("actor"),
                "proposed_status": verdict.get("proposed_status"),
                "rationale": verdict.get("rationale"),
                "unlocks": verdict.get("unlocks") or [],
                "next_ask": verdict.get("next_ask"),
                "model": verdict.get("model"),
            }
        rows.append(row)

    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    moved = sum(1 for r in rows if (r.get("interview") or {}).get("proposed_status")
                and r["interview"]["proposed_status"] != r["status"])
    return {"capabilities": rows, "summary": counts,
            "answered": len(stored), "proposed_moves": moved}


def _live_evidence(cap_id: str) -> dict | None:
    """Rows whose status is no longer a fixed string, because a layer now runs.

    Vegetation stress is the one capability that needed nothing from the
    client, so once a Sentinel-2 scene has actually been pulled the register
    should say which scene, on what date, over how many blocks - not the
    prediction that it would work.
    """
    try:
        if cap_id == "vegetation_stress":
            from gis import vegetation
            return vegetation.readiness_evidence()
        if cap_id in ("terrain", "rainfall"):
            from gis import environment
            return environment.readiness_evidence(cap_id)
    except Exception:      # the register must render with or without a feed
        return None
    return None
