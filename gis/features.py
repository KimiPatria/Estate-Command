"""The feature manifest - what this app can do, and what each feature needs.

This module is the spine of the demo. Estate Command is not an operations
tool; it is a feature catalogue that runs. Every feature below is built and
working, most of them on synthetic feeds, and each one carries the precise
list of data entities it would need to run on the client's real numbers.

That list is the product. The sentence the whole build exists to support is:

    "Here is field-to-mill shrinkage detection running on your estate. To run
     it on your real numbers we need four things: TPH bunch counts per trip,
     mill weighbridge tickets, the vehicle and driver on each trip, and mill
     grading dockage. You have all four in EPMS and SAP today."

Three things generate from this manifest rather than being written by hand:

  * the requirements strip on every panel, so the ask sits beside the feature
  * GET /gis/data-request, the leave-behind: every unsatisfied requirement,
    deduplicated, grouped by source system, each listing what it unblocks
  * the coverage headline - "N features, M on your data, K waiting on J feeds"

Pure data and pure functions. No imports from the rest of the package, so
anything can read it without cost.

Requirement status vocabulary, which must match the rest of the build:

    real       the client already supplies this and it is in the app today
    synthetic  stubbed in gis/data/synthetic/, swap is a file replacement
    derived    computed from a real quantity through a synthetic factor
    absent     not stubbed either; the feature degrades without it

`readiness` points at a row id in gis/readiness.py so the fine-grained
per-feature list and the coarse capability register stay consistent.
"""

from collections import OrderedDict

# ── domains ────────────────────────────────────────────────────────────────
#
# The four operational domains the estate actually divides work into, plus the
# two that carry everything belonging to none of them. Ordered as the rail
# should render them.

DOMAINS = OrderedDict([
    ("harvesting", {
        "label": "Harvesting",
        "blurb": "Getting ripe fruit off the palm and to the collection point.",
    }),
    ("upkeep", {
        "label": "Upkeep & Maintenance",
        "blurb": "Keeping the stand and the estate fabric in condition.",
    }),
    ("pest", {
        "label": "Pest & Disease",
        "blurb": "Finding infection and damage before it spreads.",
    }),
    ("transport", {
        "label": "Transport",
        "blurb": "Moving fruit from the collection point to the mill.",
    }),
    ("commercial", {
        "label": "Commercial",
        "blurb": "Selling the crop and buying what the estate cannot grow.",
    }),
    ("governance", {
        "label": "Governance",
        "blurb": "What was proposed, what was decided, and what is missing.",
    }),
])


# ── the source systems we would be asking ──────────────────────────────────

SYSTEMS = {
    "epms_field": "EPMS field mobile",
    "epms_core": "EPMS core",
    "sap_pm": "SAP Plant Maintenance",
    "sap_mm": "SAP Materials Management",
    "sap_sd": "SAP Sales & Distribution",
    "mill": "Mill weighbridge & laboratory",
    "arcgis": "Client ArcGIS",
    "public": "Public data, no client action needed",
}


# ── requirement shorthand ──────────────────────────────────────────────────
#
# Written once here and referenced by id from each feature, because the same
# entity backs many features and the data request must deduplicate them. A
# requirement that appears against nine features is a stronger ask than one
# that appears against one, and /gis/data-request ranks on exactly that.

def _r(entity, system, table, status, readiness=None, note=None):
    return {"entity": entity, "system": system, "table": table,
            "status": status, "readiness": readiness, "note": note}


REQUIREMENTS = {
    # -- already satisfied, from the client's own export ---------------------
    "block_polygons": _r(
        "Block boundary polygons", "arcgis", "EC_overlay.csv", "real",
        "block_map", "291 blocks, WGS84, with planted hectares and palm counts."),
    "planting_year": _r(
        "Planting year per block", "arcgis", "EC_overlay.csv / TT", "real",
        "replanting", "2015-2019 across the whole estate."),
    "harvest_short": _r(
        "Block harvest records", "epms_core", "t_oph", "real",
        "yield_choropleth", "2025-01-01 to 2025-05-23 only, under five months."),
    "grading_log": _r(
        "Bunch grading at the collection point", "epms_field", "t_oph grading columns",
        "real", "pest_watchlist", "Present but degenerate: 0.25% mean deduction."),

    # -- satisfied by us, from public sources --------------------------------
    "satellite": _r(
        "Sentinel-2 multispectral imagery", "public", "Copernicus L2A", "real",
        "vegetation_stress", "Free over Merauke. We pull it; the client supplies nothing."),
    "rainfall": _r(
        "Daily rainfall history", "public", "Open-Meteo archive", "real",
        "rainfall", "Free, no key, back to 1940. We pull it."),
    "terrain": _r(
        "Terrain elevation and slope", "public", "Copernicus DEM 30 m", "real",
        "terrain", "Free. Same STAC catalogue as the Sentinel-2 pull."),
    "hotspots": _r(
        "Active fire detections", "public", "NASA FIRMS VIIRS", "real", "fire"),

    # -- the asks: EPMS holds it, the export did not carry it ----------------
    "harvest_long": _r(
        "Block harvest history, 36 months", "epms_core", "t_oph", "synthetic",
        "harvest_history", "EPMS holds it. The export was cut to five months."),
    "last_harvest": _r(
        "Last-harvest date per block", "epms_core", "t_oph", "synthetic",
        "harvest_rotation"),
    "gang_roster": _r(
        "Gang roster and block assignment", "epms_core", "m_gang_employee", "synthetic",
        "harvest_rotation"),
    "attendance": _r(
        "Worker attendance and activity codes", "epms_core", "t_attendance", "synthetic",
        "labour_deficit"),
    "worker_output": _r(
        "Per-worker daily output", "epms_field", "t_oph by employee", "synthetic",
        "worker_output", "Bunches cut per man-day, with the block worked."),
    "loose_fruit": _r(
        "Loose fruit collected per block", "epms_field", "t_oph loose_fruits", "real",
        "loose_fruit", "11.6M fruits counted on 183,850 records, all 291 blocks. "
        "Counts, not kilograms."),
    "tph_counts": _r(
        "TPH bunch counts per trip", "epms_field", "t_tph", "synthetic",
        "weighbridge", "The field side of the shrinkage ledger."),
    "upkeep_log": _r(
        "Upkeep activity completion dates", "epms_core", "t_upkeep", "synthetic",
        "upkeep_log"),
    "cost_ledger": _r(
        "Cost routed to block", "epms_core", "m_cost_control_mapping", "synthetic",
        "cost_margin", "The routing rule exists in EPMS. The export carried no cost."),

    # -- the asks: SAP ------------------------------------------------------
    "weighbridge": _r(
        "Weighbridge tickets, gross/tare/net", "mill", "ZEPMS weighbridge", "synthetic",
        "weighbridge", "The mill side of the shrinkage ledger. Highest-value single ask."),
    "mill_grading": _r(
        "Mill grading dockage", "mill", "mill receipt", "synthetic", "mill_lab"),
    "lab_quality": _r(
        "Laboratory OER, FFA, KER", "mill", "daily lab report", "synthetic", "mill_lab"),
    "trip_log": _r(
        "Vehicle trip logs with timestamps", "sap_pm", "vehicle trip log", "synthetic",
        "fleet"),
    "fuel_issues": _r(
        "Fuel issues and odometer readings", "sap_pm", "goods issue, fuel", "synthetic",
        "fleet"),
    "breakdowns": _r(
        "Breakdown work orders and downtime", "sap_pm", "PM work order", "synthetic",
        "fleet"),
    "road_orders": _r(
        "Road grading and culvert repair orders", "sap_pm", "PM work order", "synthetic",
        "roads"),
    "fertiliser_issues": _r(
        "Goods issues for NPK, urea, borate, kieserite", "sap_mm", "MM goods issue",
        "synthetic", "fertiliser"),
    "agronomy_programme": _r(
        "Agronomy dosage programme per block", "sap_mm", "agronomy recommendation",
        "synthetic", "fertiliser", "The target the actuals are judged against."),
    "herbicide_issues": _r(
        "Herbicide and pesticide batches issued", "sap_mm", "MM goods issue", "synthetic",
        "herbicide"),
    "stock_levels": _r(
        "Warehouse stock and lead times", "sap_mm", "MB52 / MARD", "synthetic", "stores"),
    "mm_movements": _r(
        "Material documents: receipts, issues, scrap and count differences", "sap_mm", "MB51 / MATDOC",
        "synthetic", "stores", "24 months for plant EC, movement types 101, 201, 261, 551, 701/702."),
    "mm_purchase_history": _r(
        "Purchase orders with every goods receipt", "sap_mm", "ME80FN / EKKO, EKPO, EKET, EKBE",
        "synthetic", "stores", "What each supplier actually took, against what it quoted."),
    "mm_material_master": _r(
        "Reorder point, safety stock, planned delivery time, rounding", "sap_mm", "MM60 / MARC",
        "synthetic", "stores", "SAP's own settings: the baseline the learned reorder points must beat."),
    "mm_reservations": _r(
        "Reservations, open and closed", "sap_mm", "MD04 / RESB", "synthetic", "stores",
        "Demand a stockout would otherwise hide, and the fertiliser programme on the books."),
    "sales_orders": _r(
        "FFB delivery commitments", "sap_sd", "ZEPMS_SD_SORD_OUT", "synthetic",
        "contract"),
    "vendor_master": _r(
        "Vendor addresses for geocoding", "sap_sd", "LFA1", "synthetic",
        "vendor_sourcing", "ZEPMS_EM_VENDOR_OUT carries no address and no coordinates."),

    # -- the asks: collected nowhere today ----------------------------------
    "palm_census": _r(
        "Palm census with per-palm health status", "epms_field", "no table exists",
        "synthetic", "pest_census",
        "No table in the 138-table schema can hold an infected palm."),
    "scouting": _r(
        "Pest scouting rounds with damage counts", "epms_field", "no table exists",
        "synthetic", "pest_census"),
    "treatment": _r(
        "Treatment records with dates and coverage", "epms_field", "no table exists",
        "synthetic", "pest_census"),
    "fire_assets": _r(
        "Fire posts, water sources, standby crews", "epms_core", "no table exists",
        "synthetic", "fire"),
    "abw": _r(
        "Average bunch weight per block", "mill", "t_abw", "synthetic", "tonnage",
        "The keystone under every priced layer. Back-solved here, not measured."),

    # -- the supply side: who was sent where, and what they did ------------
    "harvest_plan": _r(
        "Harvest plan and gang assignment, per block per day", "epms_core",
        "t_harvesting_plan / t_harvester_assignment", "synthetic", "operations_ledger",
        "Planned beside actual. Adherence, productivity and slippage are ratios of the two."),
    "work_assignment": _r(
        "Upkeep work plan and crew assignment, per block per day", "epms_core",
        "t_workplan / t_work_assignment", "synthetic", "operations_ledger",
        "Prune, weed, path and spray orders with planned and actual quantities."),
    "pest_orders": _r(
        "Pest census, treatment and follow-up work orders", "epms_field",
        "no table exists", "synthetic", "operations_ledger",
        "The client cannot schedule pest work today because there is nowhere to "
        "record that they did."),
    "dispatch_plan": _r(
        "Vehicle dispatch plan, per block per day", "sap_pm", "vehicle trip plan",
        "synthetic", "operations_ledger",
        "The plan side of the trip log: loads sent against tonnes at the platform."),
    "rain_forecast": _r(
        "Weather forecast as issued the evening before", "public",
        "Open-Meteo Previous Runs API", "real", "rainfall",
        "Free, no key. What the forecast said the night before each day, from February 2024."),
    "rain_gauge": _r(
        "Daily rain-gauge readings per division", "epms_field", "estate rain-gauge book", "absent",
        "rainfall", "The ground truth the rain forecast is scored against; today it is scored against "
        "an area estimate instead."),
    "crew_attendance": _r(
        "Daily attendance per crew", "epms_core", "t_attendance", "synthetic",
        "labour_deficit", "Tomorrow's plan is capped by who turns up; a monthly rate "
        "cannot schedule a Tuesday."),
}


def _req(*ids):
    """Expand requirement ids, failing loudly on a typo rather than silently."""
    out = []
    for i in ids:
        if i not in REQUIREMENTS:
            raise KeyError(f"Unknown requirement id {i!r} in the feature manifest.")
        out.append(i)
    return out


# ── the features ───────────────────────────────────────────────────────────
#
# `status` is about the feature, not its data:
#
#     live     built and working in this app right now
#     planned  specified here, not yet built
#
# `runs_on` is about the data underneath it:
#
#     real       every requirement satisfied by the client or by public data
#     mixed      some real, some stubbed
#     synthetic  entirely stubbed

def _f(fid, domain, label, question, panel, requires, status="live", note=None):
    return {"id": fid, "domain": domain, "label": label, "question": question,
            "panel": panel, "requires": _req(*requires), "status": status,
            "note": note}


FEATURES = [
    # ── harvesting ─────────────────────────────────────────────────────────
    _f("harvest_rotation", "harvesting",
       "Harvest rotation and ripeness pressure",
       "Which blocks are past their cutting round?",
       "rotation",
       ["block_polygons", "last_harvest", "gang_roster"]),
    _f("peer_yield", "harvesting",
       "Yield against age-matched peers",
       "Which blocks underperform others planted the same year?",
       "peer_yield",
       ["block_polygons", "planting_year", "harvest_short"]),
    _f("cutting_interval", "harvesting",
       "Realised cutting interval against target",
       "Is the round actually being kept, block by block?",
       "cutting_interval",
       ["block_polygons", "harvest_short", "last_harvest"], "live",
       "The intervals are the client's own harvest dates; only the target "
       "round is stubbed."),
    _f("crew_productivity", "harvesting",
       "Crew productivity and adjusted targets",
       "What is a fair day's target on this block's terrain?",
       "productivity",
       ["block_polygons", "worker_output", "terrain", "planting_year",
        "harvest_short"],),
    _f("grading_by_harvester", "harvesting",
       "Grading quality by harvester",
       "Whose fruit gets docked, and for what?",
       "grading",
       ["grading_log", "worker_output"], "planned",
       "Blocked on data quality, not on code: the export's grading is "
       "99.82% ripe across 183,850 rows; wet and pest-damaged-old are all "
       "zero and dirty has two non-zero rows. There is nothing to attribute "
       "to a harvester until grading carries variance."),
    _f("loose_fruit_recovery", "harvesting",
       "Loose fruit recovery rate",
       "How much detached fruit is being left on the ground?",
       "loose_fruit",
       ["block_polygons", "loose_fruit", "harvest_short"], "live",
       "Runs on the export's own loose_fruits counts: 11.6 million over "
       "five months."),
    _f("abw_trend", "harvesting",
       "Average bunch weight trend per block",
       "Is bunch weight moving, and where?",
       "abw_trend",
       ["block_polygons", "weighbridge", "tph_counts"], "live"),
    _f("labour_deficit", "harvesting",
       "Harvester supply against demand",
       "Where will there not be enough cutters?",
       "labour",
       ["block_polygons", "attendance", "gang_roster"]),
    _f("harvest_schedule", "harvesting",
       "Harvest ledger and tomorrow's assignment",
       "Which gang cuts which blocks tomorrow, and did yesterday's plan work?",
       "ops_harvest",
       ["block_polygons", "harvest_short", "harvest_plan", "gang_roster",
        "crew_attendance", "rainfall", "terrain"], "live",
       "The ledger reconciles to the real bunch counts; the plan begins the day "
       "the export ends."),
    _f("lagged_forecast", "harvesting",
       "Lagged yield forecast, one to six months",
       "What will each block yield, given rain and inputs two years ago?",
       "forecast",
       ["block_polygons", "harvest_long", "rainfall", "fertiliser_issues",
        "planting_year"], "live",
       "Oil palm sets its bunch load 20-24 months ahead. The lag is the model."),

    # ── upkeep & maintenance ───────────────────────────────────────────────
    _f("upkeep_adherence", "upkeep",
       "Upkeep rotation adherence",
       "Which blocks have slipped their pruning, weeding or spraying round?",
       "upkeep",
       ["block_polygons", "upkeep_log"]),
    _f("nutrient_gap", "upkeep",
       "Nutrient applied against agronomy target",
       "Which blocks are underfed, in kilograms per palm?",
       "nutrient",
       ["block_polygons", "fertiliser_issues", "agronomy_programme"],),
    _f("application_lag", "upkeep",
       "Application lag against the target window",
       "Is fertiliser going out in the window it was meant to?",
       "nutrient",
       ["fertiliser_issues", "agronomy_programme", "rainfall"],),
    _f("herbicide_adherence", "upkeep",
       "Weeding and herbicide programme adherence",
       "Is the spraying round being kept, and at what rate?",
       "herbicide",
       ["block_polygons", "herbicide_issues", "upkeep_log"], "live"),
    _f("underperformance_clusters", "upkeep",
       "Agronomic underperformance clustering",
       "Why do two identical neighbouring blocks yield differently?",
       "clusters",
       ["block_polygons", "satellite", "harvest_short", "planting_year",
        "fertiliser_issues", "upkeep_log"],),
    _f("road_backlog", "upkeep",
       "Road condition and grading backlog",
       "Which roads are overdue, and what do they cost in haulage?",
       "roads",
       ["block_polygons", "road_orders"],),
    _f("stock_cover", "upkeep",
       "Fertiliser stock cover and lead time",
       "Will the store run out before the next application window?",
       "nutrient",
       ["stock_levels", "agronomy_programme"],),
    _f("replant_horizon", "upkeep",
       "Replant horizon",
       "When does this estate hit its replanting cliff?",
       "replant",
       ["block_polygons", "planting_year"]),
    _f("prune_schedule", "upkeep",
       "Pruning ledger and tomorrow's assignment",
       "Which crew prunes which blocks tomorrow, and how far has the round slipped?",
       "ops_prune",
       ["block_polygons", "work_assignment", "crew_attendance", "rainfall"]),
    _f("weed_schedule", "upkeep",
       "Weeding and spraying ledger and tomorrow's assignment",
       "Which crews weed and spray which blocks tomorrow, and what did the rain cost?",
       "ops_weed",
       ["block_polygons", "work_assignment", "crew_attendance", "rainfall",
        "road_orders"]),

    # ── pest & disease ─────────────────────────────────────────────────────
    _f("ganoderma_census", "pest",
       "Ganoderma census and incidence map",
       "Where is basal stem rot, and how fast is it moving?",
       "pest_census",
       ["block_polygons", "palm_census"], "live",
       "The client has no visibility into this at all today."),
    _f("spread_risk", "pest",
       "Spread risk from infected neighbours",
       "Which healthy blocks sit next to infected ones?",
       "pest_spread",
       ["block_polygons", "palm_census"],),
    _f("damage_watchlist", "pest",
       "Rat and rhinoceros beetle damage watchlist",
       "Where is vertebrate and beetle damage concentrating?",
       "pest_damage",
       ["block_polygons", "scouting"],),
    _f("treatment_coverage", "pest",
       "Treatment coverage and follow-up due",
       "What was treated, and what is due a second round?",
       "pest_treatment",
       ["block_polygons", "treatment", "palm_census"],),
    _f("pest_schedule", "pest",
       "Pest control ledger and tomorrow's assignment",
       "Which follow-ups are overdue, and which team goes where tomorrow?",
       "ops_pest",
       ["block_polygons", "pest_orders", "treatment", "palm_census",
        "crew_attendance"], "live",
       "No EPMS table exists for any of this. The ledger is the ask."),
    _f("vigour_early_warning", "pest",
       "Early warning: canopy anomaly against census",
       "Does the satellite see stress the census has not reached yet?",
       "pest_warning",
       ["block_polygons", "satellite", "palm_census"], "live",
       "Half of this is real today. The satellite reads every block; the "
       "census it is checked against is invented, and the two do not agree, "
       "which is what a real census would be tested on."),

    # ── transport ──────────────────────────────────────────────────────────
    _f("shrinkage_detection", "transport",
       "Field-to-mill shrinkage",
       "Which trips deliver less than the bunch count says they should?",
       "shrinkage",
       ["block_polygons", "tph_counts", "weighbridge", "trip_log",
        "mill_grading", "abw"],
       ),
    _f("turnaround", "transport",
       "Turnaround time, block to mill",
       "Where is the haulage cycle losing hours?",
       "turnaround",
       ["block_polygons", "trip_log", "weighbridge"],
       ),
    _f("fuel_efficiency", "transport",
       "Diesel litres per tonne evacuated",
       "What does it cost in fuel to move a tonne off each block?",
       "fuel",
       ["block_polygons", "fuel_issues", "trip_log", "weighbridge"],
       ),
    _f("fleet_reliability", "transport",
       "Fleet reliability and downtime",
       "Which vehicle class fails, how often, and at what cost?",
       "fleet",
       ["breakdowns", "trip_log", "fuel_issues"],
       ),
    _f("dispatch_schedule", "transport",
       "Dispatch ledger and tomorrow's vehicle plan",
       "Which vehicle collects which block tomorrow, and how much waits at the platform?",
       "ops_dispatch",
       ["block_polygons", "dispatch_plan", "weighbridge", "trip_log", "abw"]),
    _f("ffa_decay", "transport",
       "Free fatty acid against dispatch delay",
       "How much quality is lost between cut and mill?",
       "ffa",
       ["trip_log", "lab_quality", "weighbridge"], "live"),
    _f("collection_coverage", "transport",
       "Collection point coverage and scheduling",
       "Which collection points are being missed, and for how long?",
       "collection",
       ["block_polygons", "tph_counts", "trip_log"], "live"),

    # ── commercial ─────────────────────────────────────────────────────────
    _f("contract_position", "commercial",
       "Contract position",
       "Can the estate cover what it has committed to sell?",
       "contract",
       ["block_polygons", "harvest_short", "abw", "sales_orders"]),
    _f("vendor_sourcing", "commercial",
       "Vendor sourcing by landed cost",
       "Who should cover a shortfall, once haulage and quality are priced in?",
       "vendors",
       ["vendor_master", "sales_orders", "lab_quality"]),
    _f("stock_outlook", "commercial",
       "Stores: what to order and when",
       "Which materials will run out before a new delivery can arrive?",
       "stores",
       ["stock_levels", "mm_movements", "mm_purchase_history", "mm_reservations"]),
    _f("safety_stock", "commercial",
       "Safety stock and reorder points",
       "How much buffer does each material need, and what does holding it cost?",
       "stores",
       ["mm_movements", "mm_material_master", "mm_purchase_history"]),
    _f("supplier_lead_time", "commercial",
       "Supplier lead times against the quote",
       "How long do suppliers really take, against what SAP plans on?",
       "stores",
       ["mm_purchase_history", "mm_material_master"]),
    _f("cost_margin", "commercial",
       "Cost and margin by block",
       "Which blocks earn their keep?",
       "margin",
       ["block_polygons", "cost_ledger", "abw", "harvest_short"]),

    # ── governance and force majeure ───────────────────────────────────────
    _f("fire_triage", "governance",
       "Fire and force majeure",
       "What is burning, what is in its path, and who moves?",
       "fire",
       ["block_polygons", "hotspots", "fire_assets"]),
    _f("canopy_vigour", "governance",
       "Canopy vigour from satellite",
       "Which blocks look stressed from orbit?",
       "canopy",
       ["block_polygons", "satellite"]),
    _f("decision_log", "governance",
       "Decision log and audit trail",
       "What was proposed, and what did a human do about it?",
       "audit",
       []),
    _f("data_readiness", "governance",
       "Data readiness register",
       "What can this estate support today, and what is missing?",
       "readiness",
       []),
    _f("ask_the_map", "governance",
       "Ask the map",
       "Answer a question in the estate's own figures, and move the map to it.",
       "ask",
       []),
    _f("assumption_register", "governance",
       "Assumption register",
       "Which numbers is every rupiah and tonne on a plan resting on?",
       "assumptions",
       [], "live",
       "Editable. A changed value reprices the next plan."),
    _f("tomorrow_outlook", "governance",
       "Tomorrow's outlook",
       "Will it rain, who turns up, how much gets done, and how far can the forecasts be trusted?",
       "forecasts",
       ["rainfall", "rain_forecast", "rain_gauge", "crew_attendance", "harvest_plan",
        "work_assignment"], "live",
       "Four forecasts behind every plan, in plain words, each checked against the method it replaces."),
    _f("did_it_work", "governance",
       "Did it work",
       "Of the plans accepted, what was delivered against what was expected?",
       "outcomes",
       ["harvest_plan", "work_assignment", "pest_orders", "dispatch_plan"], "live",
       "Reads accepted plans back against the ledger once their date has passed."),
]


# ── derived views ──────────────────────────────────────────────────────────

_SATISFIED = ("real", "derived")


def _runs_on(feature) -> str:
    """What the feature is standing on: real, mixed, or synthetic."""
    reqs = [REQUIREMENTS[i] for i in feature["requires"]]
    if not reqs:
        return "real"
    real = sum(1 for r in reqs if r["status"] in _SATISFIED)
    if real == len(reqs):
        return "real"
    return "mixed" if real else "synthetic"


def hydrate(feature) -> dict:
    """One feature with its requirements expanded and its footing computed."""
    reqs = [dict(REQUIREMENTS[i], id=i) for i in feature["requires"]]
    for r in reqs:
        r["system_label"] = SYSTEMS.get(r["system"], r["system"])
        r["satisfied"] = r["status"] in _SATISFIED
    return {
        **feature,
        "domain_label": DOMAINS[feature["domain"]]["label"],
        "requires": reqs,
        "runs_on": _runs_on(feature),
        "requirements_total": len(reqs),
        "requirements_satisfied": sum(1 for r in reqs if r["satisfied"]),
    }


def catalogue(domain: str | None = None) -> dict:
    """Every feature, grouped by domain, with coverage counts.

    This is what the rail renders and what the copilot reads when asked what
    the app can do.
    """
    feats = [hydrate(f) for f in FEATURES
             if domain is None or f["domain"] == domain]

    by_domain = []
    for key, meta in DOMAINS.items():
        rows = [f for f in feats if f["domain"] == key]
        if not rows:
            continue
        by_domain.append({
            "domain": key,
            "label": meta["label"],
            "blurb": meta["blurb"],
            "features": rows,
            "count": len(rows),
            "live": sum(1 for f in rows if f["status"] == "live"),
            "on_real_data": sum(1 for f in rows if f["runs_on"] == "real"),
        })

    return {
        "domains": by_domain,
        "summary": coverage(),
    }


def coverage() -> dict:
    """The headline: how much of the catalogue stands on the client's data.

    Every number the UI or a generated brief will quote is computed here, so
    nothing downstream has to derive one. The figure audit in gis/reasoning.py
    rejects arithmetic performed by a model on the way to a sentence.
    """
    feats = [hydrate(f) for f in FEATURES]
    unmet = unmet_requirements()
    return {
        "features_total": len(feats),
        "features_live": sum(1 for f in feats if f["status"] == "live"),
        "features_planned": sum(1 for f in feats if f["status"] == "planned"),
        "on_real_data": sum(1 for f in feats if f["runs_on"] == "real"),
        "on_mixed_data": sum(1 for f in feats if f["runs_on"] == "mixed"),
        "on_synthetic_data": sum(1 for f in feats if f["runs_on"] == "synthetic"),
        "requirements_total": len(REQUIREMENTS),
        "requirements_satisfied": sum(1 for r in REQUIREMENTS.values()
                                      if r["status"] in _SATISFIED),
        "feeds_requested": len(unmet),
        "domains": len(DOMAINS),
        "headline": (
            f"{len(feats)} features. "
            f"{sum(1 for f in feats if f['runs_on'] == 'real')} run on data you "
            f"already have. The rest wait on {len(unmet)} extracts."
        ),
    }


def unmet_requirements() -> list[dict]:
    """Every requirement the client has not supplied, ranked by what it unblocks.

    This is the leave-behind. A requirement backing nine features is a stronger
    ask than one backing a single panel, and the order here says so.
    """
    blocked: dict[str, list] = {}
    for f in FEATURES:
        for rid in f["requires"]:
            if REQUIREMENTS[rid]["status"] in _SATISFIED:
                continue
            blocked.setdefault(rid, []).append(f)

    rows = []
    for rid, feats in blocked.items():
        r = REQUIREMENTS[rid]
        rows.append({
            "id": rid,
            **r,
            "system_label": SYSTEMS.get(r["system"], r["system"]),
            "unblocks": [{"id": f["id"], "label": f["label"],
                          "domain": f["domain"]} for f in feats],
            "unblocks_count": len(feats),
            # A feature is fully unblocked by this ask only when nothing else
            # it needs is also missing. That distinction is what stops the
            # data request from promising nine panels for one CSV.
            "fully_unblocks": [
                f["id"] for f in feats
                if all(REQUIREMENTS[o]["status"] in _SATISFIED
                       for o in f["requires"] if o != rid)
            ],
        })
    rows.sort(key=lambda r: (-r["unblocks_count"], r["entity"]))
    return rows


def data_request() -> dict:
    """The document that would otherwise be written by hand after the meeting.

    Every unsatisfied requirement, grouped by the system it would come out of,
    each saying what it unblocks. Ordered so the largest ask against a single
    system reads first.
    """
    unmet = unmet_requirements()
    by_system: dict[str, list] = {}
    for r in unmet:
        by_system.setdefault(r["system"], []).append(r)

    groups = []
    for sys_key, rows in by_system.items():
        groups.append({
            "system": sys_key,
            "label": SYSTEMS.get(sys_key, sys_key),
            "requirements": rows,
            "count": len(rows),
            "unblocks_count": len({f["id"] for r in rows for f in r["unblocks"]}),
        })
    groups.sort(key=lambda g: (-g["unblocks_count"], g["label"]))

    top = unmet[0] if unmet else None
    return {
        "groups": groups,
        "total_requirements": len(unmet),
        "systems": len(groups),
        "highest_value": ({"entity": top["entity"], "system_label": top["system_label"],
                           "unblocks_count": top["unblocks_count"]} if top else None),
        "summary": coverage(),
        "note": ("Every row is a feature in this app that already runs on "
                 "generated data. Supplying the extract swaps the file and the "
                 "panel keeps working on your own numbers."),
    }


def by_panel(panel: str) -> dict | None:
    """Everything one panel hosts, for the requirements strip it renders.

    A panel can carry more than one feature - the nutrition panel answers the
    nutrient gap, the application lag and the stock cover in one place - so the
    strip shows the union of what they need, deduplicated. Three features
    asking for the agronomy programme is still one extract to request.
    """
    feats = [hydrate(f) for f in FEATURES if f["panel"] == panel]
    if not feats:
        return None

    merged: dict[str, dict] = {}
    for f in feats:
        for r in f["requires"]:
            merged.setdefault(r["id"], r)
    reqs = list(merged.values())

    return {
        "panel": panel,
        "domain": feats[0]["domain"],
        "domain_label": feats[0]["domain_label"],
        "label": feats[0]["label"] if len(feats) == 1 else DOMAINS[feats[0]["domain"]]["label"],
        "features": feats,
        "requires": reqs,
        "requirements_total": len(reqs),
        "requirements_satisfied": sum(1 for r in reqs if r["satisfied"]),
        "runs_on": ("real" if all(r["satisfied"] for r in reqs) else
                    "synthetic" if not any(r["satisfied"] for r in reqs) else "mixed"),
    }


def get(feature_id: str) -> dict | None:
    f = next((x for x in FEATURES if x["id"] == feature_id), None)
    return hydrate(f) if f else None


def validate() -> list[str]:
    """Self-check. Run by the module's own smoke test, not at import.

    A manifest that points at a readiness row which does not exist would print
    a broken requirements strip in front of a client, so it is worth failing
    loudly in development rather than quietly in the room.
    """
    problems = []
    seen_ids = set()
    for f in FEATURES:
        if f["id"] in seen_ids:
            problems.append(f"duplicate feature id {f['id']!r}")
        seen_ids.add(f["id"])
        if f["domain"] not in DOMAINS:
            problems.append(f"{f['id']}: unknown domain {f['domain']!r}")
        # A panel shared by features from two different domains would render
        # one strip under the wrong heading.
        others = [o for o in FEATURES
                  if o["panel"] == f["panel"] and o["domain"] != f["domain"]]
        if others:
            problems.append(f"panel {f['panel']!r} spans domains "
                            f"{f['domain']!r} and {others[0]['domain']!r}")
    for rid, r in REQUIREMENTS.items():
        if r["system"] not in SYSTEMS:
            problems.append(f"{rid}: unknown system {r['system']!r}")
        if r["status"] not in ("real", "derived", "synthetic", "absent"):
            problems.append(f"{rid}: unknown status {r['status']!r}")
    used = {i for f in FEATURES for i in f["requires"]}
    for rid in REQUIREMENTS:
        if rid not in used:
            problems.append(f"requirement {rid!r} is defined but unused")
    return problems


if __name__ == "__main__":
    import json
    issues = validate()
    print(json.dumps(coverage(), indent=2))
    print(f"\ndomains {len(DOMAINS)}, features {len(FEATURES)}, "
          f"requirements {len(REQUIREMENTS)}")
    for g in data_request()["groups"]:
        print(f"  {g['label']:32s} {g['count']:2d} asks -> {g['unblocks_count']} features")
    print("\nvalidate:", "clean" if not issues else issues)
