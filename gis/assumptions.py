"""The assumption register: every number a plan is priced with, in one place.

Tomorrow's assignment quotes rupiah and tonnes. Each of those figures rests
on something nobody measured on this estate: the FFB price, the share of a
ripe crop lost per day past the round, how many palms a pruner clears in a
day, how much ground a knapsack tank covers. Buried as module constants those
are "you invented a number". Registered here, with a value, a unit, a source
and the list of figures that depend on them, they become "here is the one
number we need you to agree with us on", which is a far stronger position.

Same shape as gis/readiness.py: a static register, a small table in
decisions.db holding whatever the client changed, and a catalogue the panel
reads. The scheduler and the ops layer read values through get(), never from
the constants, so an edit in the panel reprices the plan on the next call.

Sources, in the vocabulary the panel badges:

    literature      an agronomic or industry figure, cited in `basis`
    calibrated      back-solved from the client's own data by the build
    derived         computed by a model in this app from generated feeds
    assumed         a planning figure with no better source yet
    client          set by the client in the panel; overrides the default

reasoning.audit_figures still works once the app starts quoting money because
every derived figure on screen is computed server-side from these values and
returned alongside them.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from threading import Lock

from gis.decisions import DB_PATH

log = logging.getLogger("estate-command.assumptions")

_LOCK = Lock()
SOURCES = ("literature", "calibrated", "derived", "assumed", "client")


def _a(key, label, value, unit, source, basis, used_by, lo=None, hi=None,
       editable=True, group="pricing"):
    return {"key": key, "label": label, "default": value, "unit": unit,
            "source": source, "basis": basis, "used_by": used_by,
            "min": lo, "max": hi, "editable": editable, "group": group}


ASSUMPTIONS = [
    # ── pricing ─────────────────────────────────────────────────────────
    _a("ffb_price_idr_kg", "FFB farmgate price", 2600, "IDR/kg", "assumed",
       "Indonesian FFB farmgate, 2025-26 band 2,300-3,200 IDR/kg. The same "
       "figure prices margin and the contract position.",
       ["value recovered", "cost of deferral", "margin per hectare"], 1500, 4500),
    _a("abw_kg", "Average bunch weight", 8.0, "kg", "calibrated",
       "Back-solved so the estate lands at 23 t/ha/yr. The export carries "
       "bunch counts and no weights; at a normal 12-16 kg the counts imply an "
       "impossible 34-46 t/ha/yr. Per-block values in ec_abw.csv scale with "
       "this mean.",
       ["tonnes on every harvest and dispatch plan"], 5.0, 18.0),
    _a("man_day_cost_idr", "Cost of one man-day", 150_000, "IDR", "assumed",
       "Papua provincial minimum wage 2025 is about 4.3M IDR/month, roughly "
       "165k a day including the mandor's share; rounded down for a field "
       "crew on daily rates.",
       ["travel penalty", "value per man-day"], 80_000, 400_000),

    # ── harvest ─────────────────────────────────────────────────────────
    _a("harvest_loss_pct_per_day_overdue", "Crop value lost per day past the round",
       1.5, "% per day", "literature",
       "Overripe bunches shed loose fruit and free fatty acid climbs; field "
       "trials put the loss at 1-2% of crop value per day beyond the optimal "
       "cutting interval.",
       ["cost of deferral (harvest)", "deferral decay term"], 0.2, 5.0, group="harvest"),
    _a("harvest_bunches_per_man_day", "Flat harvester quota", 138, "bunches", "derived",
       "The estate-wide mean from the generated worker-day feed. The "
       "productivity model replaces it with a per-block target of 97-187 "
       "wherever it has one; this is the fallback.",
       ["man-days needed per block", "crew capacity in bunches"], 60, 250, group="harvest"),
    _a("harvest_ripening_lookback_days", "Ripening rate lookback", 30, "days", "assumed",
       "The trailing window over which a block's bunches per calendar day is "
       "measured, to estimate what is ripe on it now.",
       ["bunches ready per block"], 14, 90, group="harvest"),

    # ── upkeep ──────────────────────────────────────────────────────────
    _a("prune_palms_per_man_day", "Pruning rate", 55, "palms", "literature",
       "Mature palm, chisel and pole: 50-70 palms a day depending on frond "
       "load. The ledger was generated at this rate.",
       ["man-days per pruning job", "prune plan capacity"], 20, 120, group="upkeep"),
    _a("circle_weed_ha_per_man_day", "Circle weeding rate", 1.1, "ha", "literature",
       "Manual circle weeding at 130-140 palms/ha: about one hectare a day.",
       ["man-days per weeding job", "weed plan capacity"], 0.4, 3.0, group="upkeep"),
    _a("path_upkeep_ha_per_man_day", "Path upkeep rate", 1.5, "ha", "literature",
       "Harvest path slashing, mature stand.",
       ["man-days per path job", "weed plan capacity"], 0.5, 4.0, group="upkeep"),
    _a("spray_ha_per_man_day", "Spraying rate", 2.6, "ha", "literature",
       "Knapsack herbicide round, 15 L tank, mixed spot and blanket.",
       ["man-days per spray job", "spray plan capacity"], 1.0, 6.0, group="upkeep"),
    _a("spray_tank_coverage_ha", "Coverage per knapsack tank", 0.13, "ha", "literature",
       "A 15 L knapsack at 110-120 L/ha covers about an eighth of a hectare. "
       "Tanks per hectare on the spray plan come from this.",
       ["tanks per hectare"], 0.05, 0.4, group="upkeep"),
    _a("upkeep_loss_pct_per_day_overdue", "Yield lost per day an upkeep round is overdue",
       0.03, "% of annual yield per day", "assumed",
       "A weeding or pruning round a month late costs about 1% of the block's "
       "year, from competition and harvesting difficulty. Nobody has measured "
       "it on this estate; this is the number to agree.",
       ["cost of deferral (prune, weed, spray)", "deferral decay term"], 0.0, 0.3, group="upkeep"),

    # ── pest ────────────────────────────────────────────────────────────
    _a("pest_census_palms_per_man_day", "Census rate", 400, "palms", "literature",
       "A walked census with a tally sheet: 350-500 palms per person per day.",
       ["man-days per census walk"], 100, 1000, group="pest"),
    _a("pest_treatment_palms_per_man_day", "Ganoderma treatment rate", 30, "palms", "literature",
       "Soil mounding plus trunk injection, 25-40 palms a day.",
       ["man-days per treatment job"], 10, 100, group="pest"),
    _a("pest_spread_loss_pct_per_day", "Value at risk per day a follow-up is overdue",
       0.05, "% of block yield per day", "assumed",
       "Basal stem rot spreads root to root; an untreated focus adds palms "
       "every month. The rate is not known for this estate.",
       ["cost of deferral (pest)", "deferral decay term"], 0.0, 0.5, group="pest"),

    # ── transport ───────────────────────────────────────────────────────
    _a("dispatch_ffa_loss_pct_per_day", "Value lost per day fruit waits at the platform",
       1.2, "% per day", "literature",
       "Free fatty acid climbs from the moment a bunch is cut; mills dock "
       "fruit over 24 hours old and reject it over 48.",
       ["cost of deferral (dispatch)", "deferral decay term"], 0.2, 5.0, group="transport"),
    _a("truck_hours_per_day", "Vehicle working hours", 9, "hours", "assumed",
       "Depot to depot, including the mill queue.",
       ["loads per vehicle per day"], 6, 14, group="transport"),

    # ── scheduling ──────────────────────────────────────────────────────
    _a("work_day_hours", "Field working day", 7, "hours", "assumed",
       "Muster to knock-off, less breaks.",
       ["man-day capacity"], 5, 10, group="scheduling"),
    _a("rain_cutoff_mm", "Rain that stops field work", 45, "mm on the day", "assumed",
       "Above this the ledger shows crews weathered off; spray rounds stop at "
       "15 mm because the herbicide washes off.",
       ["weather constraint in the plan"], 10, 100, group="scheduling"),
    _a("crew_transport_km_per_hour", "Crew transport speed", 15, "km/h", "assumed",
       "Tractor and trailer on estate roads.",
       ["travel penalty"], 5, 40, group="scheduling"),
    _a("contiguity_bonus_pct", "Contiguity bonus", 15, "% of block value", "assumed",
       "How much of a block's value the scheduler is willing to give up to "
       "keep a crew on adjacent blocks. Zero produces the arithmetically "
       "optimal scattered plan nobody executes.",
       ["contiguity term", "contiguity cost in the Why tab"], 0, 60, group="scheduling"),
    _a("attendance_lookback_days", "Attendance lookback for tomorrow's headcount",
       14, "days", "assumed",
       "Tomorrow's present figure is the crew's mean attendance over this "
       "many trailing days, applied to its roll.",
       ["expected present per crew"], 3, 60, group="scheduling"),

    # ── forecasting ─────────────────────────────────────────────────────
    # The four models behind tomorrow's plan (buildplan_ml.md). Each has a
    # switch: 1 uses the model wherever it beats the method it replaces on
    # past days, 0 goes back to that method.
    _a("use_rain_model", "Use the rain forecast", 1, "1 on, 0 off", "assumed",
       "On: the plan reads the chance of rain from last night's forecast, as "
       "learned against what actually fell. Off: the rain recorded on the day "
       "for a replay, unknown for tomorrow.",
       ["rain chance on the plan", "spray go or hold", "expected work done"], 0, 1,
       group="forecasting"),
    _a("use_headcount_model", "Use the headcount forecast", 1, "1 on, 0 off", "assumed",
       "On: each crew's expected turnout comes from the headcount model, with a "
       "likely range. Off: the mean attendance over the lookback days.",
       ["expected present per crew", "crew capacity"], 0, 1, group="forecasting"),
    _a("use_slippage_model", "Use the work-done forecast", 1, "1 on, 0 off", "assumed",
       "On: expected work done per block and crew comes from the slippage model. "
       "Off: the ledger's adherence in the day's rain band.",
       ["expected quantity on the plan", "blocks likely to be carried"], 0, 1,
       group="forecasting"),
    _a("use_learned_rates", "Use learned crew speeds", 1, "1 on, 0 off", "assumed",
       "On: a crew's capacity and a block's harvest rate are adjusted by what the "
       "ledger shows they actually do. Off: the textbook and productivity rates.",
       ["crew capacity", "man-days per harvest block"], 0, 1, group="forecasting"),
    _a("plan_on_expected_adherence", "Plan on expected work done", 1, "1 on, 0 off", "assumed",
       "On: a block's value on the plan is discounted by the chance its work does "
       "not get done, so on a wet day gangs go to good roads first.",
       ["deferral value in the ranking"], 0, 1, group="forecasting"),
    _a("rate_prior_man_days", "Evidence needed before a crew speed moves", 20, "man-days",
       "assumed",
       "How many man-days of records it takes before a crew's learned speed sits "
       "halfway between the textbook rate and what the crew actually did.",
       ["learned crew speed"], 1, 200, group="forecasting"),
    _a("rate_max_weekly_change_pct", "Largest weekly change in a crew speed", 10, "% per week",
       "assumed",
       "A guardrail: a learned speed moves at most this much in a week, so one "
       "odd week cannot swing the plan.",
       ["learned crew speed"], 1, 50, group="forecasting"),
    _a("holiday_attendance_factor", "Turnout during Lebaran leave", 0.72, "of a normal day",
       "assumed",
       "Used only until the headcount model has seen a Lebaran in its training "
       "days; after that the model's own figure replaces it.",
       ["expected present over Lebaran"], 0.3, 1.0, group="forecasting"),
    _a("payday_day_of_month", "Payday", 25, "day of the month", "assumed",
       "The headcount model checks whether turnout changes in the two days after "
       "payday. To agree with the estate.",
       ["expected present after payday"], 1, 28, group="forecasting"),
    _a("herbicide_idr_per_ha", "Herbicide cost per planted hectare", 100_000, "IDR/ha", "assumed",
       "Chemical for one circle-and-path round, per planted hectare: about 40% of "
       "the ground is treated. A placeholder to agree with the estate; it sets how "
       "much a round lost to rain wastes in the spray-or-hold call.",
       ["spray go or hold"], 0, 2_000_000, group="forecasting"),

    # ── stores ──────────────────────────────────────────────────────────
    _a("use_stock_model", "Use learned reorder points", 1, "1 on, 0 off", "assumed",
       "On: each material's reorder point and safety stock come from how long its "
       "supplier really takes and how much the estate really uses, wherever the "
       "twelve-month replay shows that costs less. Off: SAP's own settings (MINBE, "
       "EISBE, PLIFZ).",
       ["reorder point", "safety stock", "order-by date"], 0, 1, group="stores"),
    _a("service_level_fertiliser", "Service level, fertiliser", 97, "% of cycles without running out",
       "assumed",
       "The share of replenishment cycles that should end without a rush buy. A "
       "missed round costs yield months later, so this is set high. To agree with "
       "the estate.",
       ["fertiliser reorder point"], 50, 99.5, group="stores"),
    _a("service_level_agrochemical", "Service level, agrochemicals", 95, "% of cycles without running out",
       "assumed", "Herbicide and pest chemicals. A spray round can wait a few days.",
       ["agrochemical reorder point"], 50, 99.5, group="stores"),
    _a("service_level_fuel", "Service level, diesel", 98, "% of cycles without running out", "assumed",
       "No diesel, no evacuation: fruit left at the platform loses oil by the hour.",
       ["diesel reorder point"], 50, 99.5, group="stores"),
    _a("service_level_parts", "Service level, spare parts", 90, "% of cycles without running out", "assumed",
       "A breakdown waits for the part; a rush buy from Jayapura usually saves the week.",
       ["spare part reorder point"], 50, 99.5, group="stores"),
    _a("holding_cost_pct_yr", "Cost of holding stock", 12, "% of stock value a year", "assumed",
       "Money tied up in the store, at roughly the estate's cost of capital. To agree "
       "with finance. Storage loss (caked urea) is learned from the counts and added "
       "on top.",
       ["holding cost", "cheapest service level"], 0, 40, group="stores"),
    _a("leadtime_prior_orders", "Orders needed before a supplier's lead time moves", 8, "orders", "assumed",
       "How many purchase orders it takes before a supplier's learned lead time sits "
       "halfway between its route's usual time and its own record.",
       ["supplier lead time"], 1, 100, group="stores"),
    _a("order_cover_weeks", "Cover per order", 6, "weeks of use", "assumed",
       "A reorder brings stock up to the reorder point plus this many weeks of expected "
       "use, rounded to the bag, can or tanker. Fertiliser is ordered against the "
       "programme instead.",
       ["order quantity"], 1, 26, group="stores"),
    _a("glyphosate_l_per_ha", "Glyphosate dose", 1.0, "L per sprayed ha", "literature",
       "Glyphosate 480 SL on a circle-and-path round, per hectare sprayed. To agree with "
       "the estate agronomist; the store's issues are compared with it.",
       ["herbicide per hectare"], 0.2, 5.0, group="stores"),
    _a("metsulfuron_kg_per_ha", "Metsulfuron dose", 0.04, "kg per sprayed ha", "literature",
       "Metsulfuron-methyl 20 WG added for broadleaf weeds on about a third of rounds.",
       ["herbicide per hectare"], 0.005, 0.2, group="stores"),
    _a("hexaconazole_l_per_palm", "Hexaconazole per treated palm", 0.02, "L per palm", "literature",
       "Hexaconazole 5 SC soil drench against Ganoderma, per palm treated or followed up.",
       ["pest chemical per palm"], 0.005, 0.2, group="stores"),
    _a("bait_kg_per_palm", "Rat bait per palm", 0.015, "kg per palm", "literature",
       "One bait block per palm per baiting round.",
       ["pest chemical per palm"], 0.002, 0.1, group="stores"),
]

_BY_KEY = {a["key"]: a for a in ASSUMPTIONS}


# ── the override store ─────────────────────────────────────────────────────
#
# Shares decisions.db with the decision log and the readiness interview,
# because all three answer the same governance question: what did a person
# change, and when.

def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with _LOCK, _conn() as c:
        c.execute("""
            create table if not exists assumptions (
                key      text primary key,
                value    real not null,
                set_at   text not null,
                actor    text,
                note     text
            )
        """)


def overrides() -> dict:
    init()
    with _LOCK, _conn() as c:
        return {r["key"]: dict(r) for r in c.execute("select * from assumptions").fetchall()}


def get(key: str):
    """The value in force: the client's override if there is one, else the default."""
    a = _BY_KEY[key]
    ov = overrides().get(key)
    return ov["value"] if ov else a["default"]


def values(keys=None) -> dict:
    """Every value in force at once, for a plan that reads a dozen of them."""
    ov = overrides()
    out = {}
    for a in ASSUMPTIONS:
        if keys and a["key"] not in keys:
            continue
        out[a["key"]] = ov[a["key"]]["value"] if a["key"] in ov else a["default"]
    return out


def set_value(key: str, value, actor: str = "demo user", note: str | None = None) -> dict:
    a = _BY_KEY.get(key)
    if a is None:
        raise KeyError(f"No assumption {key!r}.")
    if not a["editable"]:
        raise ValueError(f"{key} is calibrated from the client's own data and is not editable here.")
    v = float(value)
    if a["min"] is not None and v < a["min"]:
        raise ValueError(f"{key} below its floor of {a['min']} {a['unit']}.")
    if a["max"] is not None and v > a["max"]:
        raise ValueError(f"{key} above its ceiling of {a['max']} {a['unit']}.")
    init()
    row = {"key": key, "value": v,
           "set_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "actor": actor, "note": note}
    with _LOCK, _conn() as c:
        c.execute("insert or replace into assumptions values (:key,:value,:set_at,:actor,:note)", row)
    log.info("[assumptions] %s set to %s by %s", key, v, actor)
    return describe(key)


def reset(key: str | None = None) -> int:
    init()
    with _LOCK, _conn() as c:
        if key:
            n = c.execute("delete from assumptions where key = ?", (key,)).rowcount
        else:
            n = c.execute("select count(*) from assumptions").fetchone()[0]
            c.execute("delete from assumptions")
    return n


def describe(key: str) -> dict:
    """One assumption as the panel shows it: default, value in force, source."""
    a = _BY_KEY[key]
    ov = overrides().get(key)
    return {
        **a,
        "value": ov["value"] if ov else a["default"],
        "overridden": bool(ov),
        "source_in_force": "client" if ov else a["source"],
        "set_at": ov["set_at"] if ov else None,
        "set_by": ov["actor"] if ov else None,
    }


def catalogue() -> dict:
    rows = [describe(a["key"]) for a in ASSUMPTIONS]
    by_source: dict[str, int] = {}
    for r in rows:
        by_source[r["source_in_force"]] = by_source.get(r["source_in_force"], 0) + 1
    groups = []
    for g in ("pricing", "harvest", "upkeep", "pest", "transport", "scheduling", "forecasting", "stores"):
        rs = [r for r in rows if r["group"] == g]
        if rs:
            groups.append({"group": g, "assumptions": rs})
    return {
        "assumptions": rows,
        "groups": groups,
        "total": len(rows),
        "overridden": sum(1 for r in rows if r["overridden"]),
        "by_source": by_source,
        "note": ("Every rupiah and tonne on a plan is computed from these values "
                 "and returned beside them. Change one here and the next plan "
                 "is priced at the new figure; nothing is recomputed in the "
                 "browser and nothing is written to EPMS."),
    }


def used(keys) -> list[dict]:
    """The subset a payload actually read, so the panel can show its basis."""
    out = []
    for k in keys:
        d = describe(k)
        out.append({"key": k, "label": d["label"], "value": d["value"],
                    "unit": d["unit"], "source": d["source_in_force"]})
    return out


def fingerprint() -> str:
    """A cache key for anything priced off the register."""
    return json.dumps(values(), sort_keys=True)
