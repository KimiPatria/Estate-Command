"""The leak playbook: every driver, what could cause it, and what to do (buildplan_finance.md §5).

Pure data, like gis/features.py. Three things hang off each leak driver:

  * the rule-based Why and Do, which the first screen always shows and which
    stand alone when the model is off or wrong
  * the candidate causes, each with the trace it would leave in the records
    (`looks_like`), which is what the model weighs the server's checks against
  * the actions: vetted, each with an owner, the document it becomes and the
    figure to watch afterwards

The model may choose among these and tailor the wording to one unit. It may
not add a cause (it can say `other`, shown as unvetted) or an action, and it
never sets a saving: the server attaches the driver's monthly leakage.

`why` is a format string over the unit's facts; a missing fact falls back to
the driver label, so a template can never take the page down.
"""

FUNCTIONS = {
    "harvesting": {"label": "Harvesting", "unit": "block", "owner": "Estate Manager"},
    "transport": {"label": "FFB transport", "unit": "vehicle", "owner": "Workshop Head"},
    "fleet": {"label": "Fleet maintenance", "unit": "vehicle", "owner": "Workshop Head"},
    "fertiliser": {"label": "Fertiliser", "unit": "block", "owner": "Assistant Manager"},
    "upkeep": {"label": "Field upkeep", "unit": "block", "owner": "Assistant Manager"},
    "pest": {"label": "Pest & disease", "unit": "block", "owner": "Assistant Manager"},
    "workforce": {"label": "Workforce", "unit": "crew", "owner": "Estate Manager"},
    "stores": {"label": "Stores", "unit": "material", "owner": "Store Keeper"},
    "roads": {"label": "Roads", "unit": "segment", "owner": "Assistant Manager"},
    "overhead": {"label": "Overhead", "unit": "division", "owner": "Estate Manager"},
}
FUNCTION_ORDER = list(FUNCTIONS)
SCORED = [f for f in FUNCTION_ORDER if f != "overhead"]


def _c(cid, label, looks_like):
    return {"id": cid, "label": label, "looks_like": looks_like}


def _x(aid, text, owner, watch, document=None):
    return {"id": aid, "text": text, "owner": owner, "watch": watch, "document": document}


DRIVERS = {
    # ── harvesting ─────────────────────────────────────────────────────────
    "rate_gap": {
        "function": "harvesting", "label": "Man-days per tonne",
        "why": "Rp {rate_t:,.0f} a tonne in cutters' wages, against Rp {bench_t:,.0f} on its best peers",
        "causes": [
            _c("thin_crop", "Thin crop on the round",
               "few bunches per pass, so cutters walk further per bunch; bunches per harvest day well below peers"),
            _c("long_walk", "Far from the gang's home block",
               "the block sits well beyond the gang's other blocks; the gap is worst on days the gang also cut far blocks"),
            _c("paths_overdue", "Harvest paths overgrown",
               "path upkeep overdue on the block; cutters slow on every pass, not only some"),
            _c("rain", "Rain on harvest days",
               "the block's harvest days were wetter than its peers'"),
            _c("slow_crew", "Gang cutting below its own speed",
               "the gang's output per man-day is low on all its blocks, not only this one"),
        ],
        "checks": ["bunches_per_day", "home_distance", "path_overdue", "rain_days", "crew_speed"],
        "actions": [
            _x("rate_walk", "Walk the block with the mandor: check paths, crop and where the gang loses time",
               "Assistant Manager", "basic wages per tonne"),
            _x("rate_paths", "Bring the block's path upkeep forward", "Assistant Manager",
               "basic wages per tonne", "work order"),
            _x("rate_reassign", "Give the block to the nearer gang", "Estate Manager", "basic wages per tonne",
               "harvesting plan"),
            _x("rate_round", "Lengthen the round on a thin block so each pass carries more crop",
               "Assistant Manager", "bunches per harvest day"),
        ],
    },
    "loose_fruit": {
        "function": "harvesting", "label": "Loose fruit left",
        "why": "Loose fruit picked up at {lf_ratio:.2f} a bunch, against {lf_bench:.2f} on the best quarter of blocks",
        "causes": [
            _c("not_collected", "Fruit left on the ground",
               "few collectors behind the cutters; low ratio on every harvest day"),
            _c("not_recorded", "Collected but not written down",
               "few harvest records carry a loose fruit count at all"),
            _c("tight_round", "A tight round sheds less",
               "the block is cut well inside its target round, so less fruit has dropped"),
        ],
        "checks": ["lf_recording", "lf_collectors", "lf_interval"],
        "actions": [
            _x("lf_collect", "Put collectors behind the cutters on this block", "Assistant Manager",
               "loose fruit per bunch"),
            _x("lf_record", "Have the kerani record loose fruit on every harvest record", "Assistant Manager",
               "records with a loose fruit count"),
            _x("lf_inspect", "Spot-check the block's circles the day after cutting", "Assistant Manager",
               "loose fruit per bunch"),
        ],
    },
    "late_rounds": {
        "function": "harvesting", "label": "Late harvest rounds",
        "why": "Cut {days_over:.1f} days past its round on average, losing ripe crop",
        "causes": [
            _c("crew_capacity", "Gang short of cutter-days",
               "the gang had fewer cutter-days than its blocks needed; late across all its blocks"),
            _c("rain_outs", "Rain stopped cutting",
               "the late rounds follow heavy-rain days"),
            _c("transport", "Fruit could not be moved",
               "dispatch on the block fell short of plan in the same weeks"),
        ],
        "checks": ["crew_days", "rain_outs", "dispatch_adherence"],
        "actions": [
            _x("lr_crew", "Lend cutters from a gang ahead of its round", "Estate Manager",
               "days past round", "harvesting plan"),
            _x("lr_saturday", "Plan a catch-up Saturday on the late blocks", "Assistant Manager",
               "days past round", "harvesting plan"),
            _x("lr_dispatch", "Reserve a truck for the late blocks' platforms", "Transport Supervisor",
               "days past round", "dispatch assignment"),
        ],
    },

    # ── transport ──────────────────────────────────────────────────────────
    "fuel_gap": {
        "function": "transport", "label": "Fuel gap",
        "why": "{gap_l:,.0f} L more diesel issued than its trips burned ({gap_pct:.0f}%)",
        "causes": [
            _c("tank_leak", "Tank or fuel line leaking",
               "fuel lost every day, including days without trips; spread across every driver; worse after a repair"),
            _c("siphoning", "Fuel siphoned off",
               "extra fills concentrated on one driver's shifts; fills near the tank's size on days the truck burned little"),
            _c("unrecorded_work", "Non-FFB runs not on the trip ledger",
               "extra fills spread across drivers, steady month to month, small fills; other vehicles show the same"),
            _c("worn_engine", "Engine burning above spec",
               "the trips themselves burn more per km than spec; issued and burned litres agree"),
        ],
        "checks": ["fuel_by_driver", "fuel_trip_days", "fuel_fill_size", "fuel_after_repair",
                   "fuel_burn_vs_spec", "fuel_class_peers"],
        "actions": [
            _x("fuel_reconcile", "Reconcile the vehicle's fuel log against its trips for the last 30 days",
               "Workshop Head", "fuel gap litres"),
            _x("fuel_driver_rotate", "Rotate the driver off the vehicle for two weeks and compare",
               "Transport Supervisor", "fuel gap by driver"),
            _x("fuel_card_cap", "Cap the driver's fuel card at the litres the day's planned trips need",
               "Store Keeper", "card litres per trip-day"),
            _x("fuel_tank_inspect", "Pressure-test the tank and lines", "Workshop Head", "fuel gap litres",
               "PM order"),
            _x("fuel_log_runs", "Log every non-FFB run on the trip sheet", "Transport Supervisor",
               "fuel gap litres"),
        ],
    },
    "over_spec": {
        "function": "transport", "label": "Burn above spec",
        "why": "Trips burned {over_pct:.0f}% more diesel per km than the vehicle's spec",
        "causes": [
            _c("worn_engine", "Engine due a service", "high burn on every route and every driver"),
            _c("rough_roads", "Rough roads on its routes", "high burn only on the poor-road routes"),
            _c("driving", "Driving style", "high burn concentrated on some drivers"),
        ],
        "checks": ["burn_by_route", "burn_by_driver"],
        "actions": [
            _x("spec_service", "Bring the engine service forward", "Workshop Head", "litres per km",
               "PM order"),
            _x("spec_driver", "Brief the drivers with the highest burn", "Transport Supervisor",
               "litres per km by driver"),
        ],
    },
    "part_loads": {
        "function": "transport", "label": "Part loads",
        "why": "{part_share:.0f}% of trips left under {threshold:.0f}% of payload",
        "causes": [
            _c("small_lots", "Dispatch sends small lots", "part loads on every route, often early in the day"),
            _c("vehicle_too_big", "Vehicle too big for its blocks",
               "the blocks it serves rarely produce a full load in a day"),
            _c("waiting", "Leaves before the platform fills", "part loads concentrated in the afternoon run"),
        ],
        "checks": ["load_by_hour", "load_by_route"],
        "actions": [
            _x("pl_combine", "Combine platforms on the same route into one trip", "Transport Supervisor",
               "load factor", "dispatch assignment"),
            _x("pl_smaller", "Swap to a smaller vehicle on these routes", "Transport Supervisor",
               "load factor"),
        ],
    },
    "queue_ffa": {
        "function": "transport", "label": "Mill queue FFA",
        "why": "Rp {queue_idr:,.0f} of fruit quality lost waiting {queue_h:.1f} h on average at the mill",
        "causes": [
            _c("afternoon", "Afternoon dispatch", "long queues on trips leaving after noon"),
            _c("mill_capacity", "Mill intake slow for everyone", "every vehicle queues about as long"),
        ],
        "checks": ["queue_by_hour", "queue_vs_class"],
        "actions": [
            _x("q_morning", "Dispatch before noon", "Transport Supervisor", "queue hours"),
            _x("q_slot", "Agree a delivery slot with the mill", "Estate Manager", "queue hours"),
        ],
    },
    "shrinkage": {
        "function": "transport", "label": "Shrinkage",
        "why": "{shrink_pct:.1f}% of the expected fruit lost between platform and mill, against {fleet_pct:.1f}% fleet-wide",
        "causes": [
            _c("theft_in_transit", "Fruit taken in transit",
               "light loads concentrated on one driver across several vehicles and routes, over a few weeks"),
            _c("left_on_platform", "Fruit left at the platform", "light loads concentrated on certain blocks or routes"),
            _c("weighing_error", "Weighbridge error", "light loads on every driver on the same days"),
            _c("abw_wrong", "Bunch weight wrong for the blocks",
               "the same blocks weigh light whoever drives"),
        ],
        "checks": ["shrink_concentration", "shrink_when", "shrink_same_vehicle", "shrink_blocks"],
        "actions": [
            _x("sh_audit", "Audit the driver's trips and platform counts for the period", "Transport Supervisor",
               "shrinkage on the driver's trips"),
            _x("sh_seal", "Seal loads at the platform and check the seal at the mill", "Transport Supervisor",
               "shrinkage"),
            _x("sh_rotate", "Pair the driver with a second crew member for a month", "Transport Supervisor",
               "shrinkage on the driver's trips"),
        ],
    },

    # ── fleet ──────────────────────────────────────────────────────────────
    "breakdowns": {
        "function": "fleet", "label": "Breakdowns",
        "why": "Rp {repair_idr:,.0f} in repairs and {downtime_h:.0f} h off the road, against Rp {bench_trip:,.0f} a trip for its class",
        "causes": [
            _c("end_of_life", "Vehicle at the end of its life", "many different faults; oldest in its class"),
            _c("repeat_fault", "The same fault, badly repaired",
               "one fault repaired again and again within weeks, by the same workshop"),
            _c("overloading", "Overloading", "loads routinely above payload; suspension and tyre faults"),
            _c("rough_roads", "Rough roads on its routes", "routes over poor roads; faults spread"),
        ],
        "checks": ["repeat_faults", "fleet_age", "fleet_load", "fleet_roads", "fleet_vendor"],
        "actions": [
            _x("fl_root_cause", "Send the failing part to the dealer workshop and stop local patching",
               "Workshop Head", "repair cost per trip", "PM order"),
            _x("fl_replace", "Price a replacement against another year of repairs", "Estate Manager",
               "repair cost per trip"),
            _x("fl_load", "Enforce the payload limit at the platform", "Transport Supervisor", "load factor"),
        ],
    },
    "services": {
        "function": "fleet", "label": "Servicing",
        "why": "Routine servicing above its class",
        "causes": [_c("over_servicing", "Serviced too often", "short intervals between services"),
                   _c("vendor_rate", "Workshop rate high", "same interval, dearer jobs")],
        "checks": ["service_interval"],
        "actions": [_x("sv_interval", "Service on the hour-meter, not the calendar", "Workshop Head",
                       "service cost per trip")],
    },

    # ── fertiliser ─────────────────────────────────────────────────────────
    "application_labour": {
        "function": "fertiliser", "label": "Application labour",
        "why": "{md_per_t:.1f} contract man-days a tonne applied, against {std_md_per_t:.1f} standard",
        "causes": [
            _c("padded_timesheets", "Contract sheets padded",
               "extra sheets dated after the material was already all issued; uniform across blocks whatever the terrain"),
            _c("difficult_terrain", "Hard ground to carry over",
               "man-days per tonne rise with slope and distance from the road"),
            _c("split_dosing", "Applied in more passes",
               "more sheets per block, each with its own issue of material"),
        ],
        "checks": ["fa_by_division", "fa_terrain", "fa_sheet_timing", "fa_contractor_elsewhere"],
        "actions": [
            _x("fa_verify", "Check the contractor's sheets against the mandor's muster, block by block",
               "Assistant Manager", "man-days per tonne"),
            _x("fa_per_kg", "Pay the contract per tonne applied, not per man-day", "Estate Manager",
               "application cost per tonne", "contract change"),
            _x("fa_recover", "Recover the overpaid sheets from the next payment", "Estate Manager",
               "application cost per tonne"),
        ],
    },
    "over_issue": {
        "function": "fertiliser", "label": "Over-issue",
        "why": "Issued {over_pct:.0f}% above prescription",
        "causes": [
            _c("bag_rounding", "Rounded up to whole bags", "small excess on every block, about half a bag"),
            _c("programme_outdated", "Programme not updated", "the division's blocks all over by a similar share"),
            _c("diversion", "Material diverted", "large excess on a few blocks"),
        ],
        "checks": ["oi_size", "oi_spread"],
        "actions": [
            _x("oi_prescription", "Issue against the block prescription only", "Store Keeper",
               "issued vs prescribed", "goods issue request"),
            _x("oi_count", "Count the division's fertiliser at the platform", "Assistant Manager",
               "issued vs prescribed"),
        ],
    },

    # ── upkeep ─────────────────────────────────────────────────────────────
    "labour_over_standard": {
        "function": "upkeep", "label": "Labour over standard",
        "why": "{md_over:,.0f} man-days more than the best quarter of blocks needed for the same work",
        "causes": [
            _c("overgrown", "Round overdue, so the work is heavier", "the job came well past its round"),
            _c("slow_crew", "Crew below its usual pace", "the crew is slow on every block"),
            _c("rain_interrupted", "Rain broke the day", "the job's days were wet"),
        ],
        "checks": ["upkeep_overdue_at_job", "upkeep_crew_pace", "rain_days"],
        "actions": [
            _x("up_rate", "Check the crew's daily output against the rate card", "Assistant Manager",
               "man-days per ha"),
            _x("up_schedule", "Put the block back on its round so the work stays light", "Assistant Manager",
               "days overdue", "work order"),
        ],
    },
    "chemical_over_dose": {
        "function": "upkeep", "label": "Chemical over dose",
        "why": "Herbicide issued {over_pct:.0f}% above dose for the hectares sprayed",
        "causes": [
            _c("handling_loss", "Spills and rinsing", "a small excess on every job and every team"),
            _c("wrong_mix", "Mixed too strong", "a team's excess is large and steady"),
            _c("diversion", "Chemical taken", "large excess on some jobs only"),
        ],
        "checks": ["dose_by_team", "dose_by_job"],
        "actions": [
            _x("ch_calibrate", "Calibrate the knapsacks and issue per tank, not per day", "Assistant Manager",
               "litres per ha"),
            _x("ch_return", "Return unused chemical to the store at day's end", "Store Keeper", "litres per ha"),
        ],
    },

    # ── pest ───────────────────────────────────────────────────────────────
    "untriggered_work": {
        "function": "pest", "label": "Untriggered treatment",
        "why": "Treated without a census finding on the block",
        "causes": [
            _c("census_stale", "Census out of date", "the last census was long before the treatment"),
            _c("preventive", "Preventive policy", "treatments on clean blocks next to infected ones"),
            _c("wrong_block", "Booked to the wrong block", "a neighbouring block has the finding"),
        ],
        "checks": ["pest_census_age", "pest_neighbours"],
        "actions": [
            _x("pw_census", "Census before treating; treat only where the census says", "Assistant Manager",
               "treatments without a finding"),
        ],
    },
    "pest_labour": {
        "function": "pest", "label": "Pest labour over standard",
        "why": "{md_over:,.0f} man-days above the rate card for the palms treated",
        "causes": [_c("slow_team", "Team below standard pace", "slow on every job")],
        "checks": ["pest_pace"],
        "actions": [_x("pl_rate", "Check the team's palms per day against the rate card",
                       "Assistant Manager", "palms per man-day")],
    },

    # ── workforce ──────────────────────────────────────────────────────────
    "overtime": {
        "function": "workforce", "label": "Overtime",
        "why": "Overtime on {ot_days} of {work_days} days, Rp {ot_idr:,.0f}",
        "causes": [
            _c("standing_overtime", "Overtime as habit",
               "overtime on far more of its days than other gangs, and a clearly smaller share of its overtime "
               "days are heavy-crop days than for other gangs (whose overtime falls on heavy days); headcount and "
               "start times the same on overtime days as on others"),
            _c("rounds_too_big", "Rounds too big for the gang",
               "like other gangs, nearly all its overtime days are heavy-crop days, and it is short-handed on "
               "them: fewer present on overtime days than on others"),
            _c("late_start", "Late starts", "overtime on days the gang started late"),
            _c("rain_mornings", "Rain in the morning", "overtime on days with morning rain"),
        ],
        "checks": ["ot_vs_output", "ot_dry_wet", "ot_headcount", "ot_start", "ot_peers"],
        "actions": [
            _x("ot_approve", "Pay overtime only on the assistant's written approval, day by day",
               "Estate Manager", "overtime hours"),
            _x("ot_split", "Split the gang's heaviest blocks with a neighbouring gang", "Assistant Manager",
               "overtime hours", "harvesting plan"),
            _x("ot_start", "Start the gang on time: transport at muster", "Assistant Manager", "start times"),
        ],
    },
    "pay_over_attendance": {
        "function": "workforce", "label": "Pay above attendance",
        "why": "{pad_md:,.0f} man-days paid above the muster",
        "causes": [
            _c("ghost_names", "Names on the payroll who do not work",
               "the same number paid above the muster every day, including days the crew mustered short"),
            _c("muster_not_recorded", "Muster written up late or partly",
               "the gap moves with absence: large on high-absence days, none on full days"),
            _c("contract_labour", "Casuals paid through the crew",
               "the gap appears only on heavy work days"),
        ],
        "checks": ["pad_constant", "pad_absence", "pad_workload"],
        "actions": [
            _x("pa_reconcile", "Reconcile payroll with the muster roll, name by name", "Estate Manager",
               "paid vs present"),
            _x("pa_headcount", "Count the crew at muster, unannounced, for two weeks", "Assistant Manager",
               "paid vs present"),
            _x("pa_recover", "Stop the unmatched names and recover the pay", "Estate Manager", "paid vs present"),
        ],
    },
    "paid_idle": {
        "function": "workforce", "label": "Paid idle",
        "why": "{idle_md:,.0f} man-days present with no work order, beyond the best crews of its type",
        "causes": [
            _c("no_work_planned", "No work planned for part of the crew", "idle on dry, ordinary days"),
            _c("crew_oversized", "Crew larger than its work", "idle every day, a steady share"),
            _c("support_roles", "Support roles not on orders", "idle share matches the crew's mandor, kerani and collectors"),
        ],
        "checks": ["idle_dry_days", "idle_vs_roles"],
        "actions": [
            _x("pi_standing_job", "Give idle hands a standing job (collection, circle weeding)",
               "Assistant Manager", "idle man-days"),
            _x("pi_resize", "Resize the crew at the next roster change", "Estate Manager", "idle man-days"),
        ],
    },

    # ── stores ─────────────────────────────────────────────────────────────
    "price_variance": {
        "function": "stores", "label": "Price above standard",
        "why": "Paid Rp {ppv:,.0f} above standard on {receipts} receipts",
        "causes": [
            _c("rush_buy", "Rush buys at local prices", "the excess sits on RUSH orders"),
            _c("vendor_over_contract", "Supplier invoicing above the PO",
               "invoice price above the supplier's own PO price, on normal orders"),
            _c("standard_outdated", "Standard price out of date",
               "PO prices themselves above standard, for every supplier"),
        ],
        "checks": ["pv_order_type", "pv_invoice_vs_po", "pv_market"],
        "actions": [
            _x("pv_hold", "Hold the supplier's open invoices and recover the overcharge",
               "Head Office Procurement", "invoice vs PO price"),
            _x("pv_match", "Block invoices above PO price at three-way match", "Head Office Procurement",
               "invoice vs PO price"),
            _x("pv_order_early", "Order earlier so rush buys stop", "Store Keeper", "rush buys",
               "purchase requisition"),
            _x("pv_standard", "Update the standard price", "Head Office Procurement", "price variance"),
        ],
    },
    "write_offs": {
        "function": "stores", "label": "Write-offs",
        "why": "Rp {writeoff:,.0f} written off at counts",
        "causes": [_c("storage_loss", "Lost in storage", "steady losses at every count"),
                   _c("count_error", "Count errors", "gains and losses alternate")],
        "checks": ["wo_counts"],
        "actions": [_x("wo_store", "Fix storage: pallets, cover, first-in first-out", "Store Keeper",
                       "count differences")],
    },

    # ── roads ──────────────────────────────────────────────────────────────
    "repeat_repairs": {
        "function": "roads", "label": "Repeat repairs",
        "why": "{repairs} culvert repairs in 12 months, Rp {repeat_idr:,.0f} beyond the first",
        "causes": [
            _c("poor_repair", "Patched, not fixed", "repairs weeks apart on the same culvert, each one dearer"),
            _c("drainage", "Water not draining", "repairs follow heavy rain"),
            _c("traffic", "Heavy traffic", "many loaded trips over the segment"),
        ],
        "checks": ["rr_interval", "rr_rain", "rr_traffic"],
        "actions": [
            _x("rr_rebuild", "Rebuild the culvert properly instead of patching", "Assistant Manager",
               "repairs per year", "PM order"),
            _x("rr_drain", "Clear the drain upstream", "Assistant Manager", "repairs per year"),
        ],
    },
    "billing_over_length": {
        "function": "roads", "label": "Billed over length",
        "why": "{km_over:.1f} km of grading billed beyond the segments' length",
        "causes": [
            _c("contractor_overbilling", "Contractor billing extra km",
               "billed km over length on every segment of one division since a date; same contractor elsewhere normal"),
            _c("length_wrong", "Segment lengths wrong in the register", "the same segments over-billed by every contractor, always"),
        ],
        "checks": ["bl_by_division", "bl_since"],
        "actions": [
            _x("bl_measure", "Measure graded km with the contractor before signing the sheet",
               "Assistant Manager", "km billed vs length"),
            _x("bl_recover", "Recover the over-billed km", "Estate Manager", "km billed vs length"),
        ],
    },
    "grading_cost": {
        "function": "roads", "label": "Grading cost",
        "why": "Grading cost above segments in the same condition",
        "causes": [_c("graded_often", "Graded too often", "short intervals between gradings")],
        "checks": ["grading_interval"],
        "actions": [_x("gr_interval", "Grade on condition, not the calendar", "Assistant Manager",
                       "grading per km")],
    },

    # ── residual ───────────────────────────────────────────────────────────
    "other": {
        "function": "", "label": "Other",
        "why": "Spend above its peers with no single driver",
        "causes": [], "checks": [],
        "actions": [_x("other_review", "Review the unit's postings with its owner", "Estate Manager",
                       "leakage")],
    },
}


def driver(did: str) -> dict:
    return DRIVERS.get(did) or DRIVERS["other"]


def why(did: str, facts: dict) -> str:
    d = driver(did)
    try:
        return d["why"].format(**facts)
    except (KeyError, ValueError, TypeError, IndexError):
        return d["label"]


def do(did: str, unit_label: str) -> dict:
    a = driver(did)["actions"][0]
    return {"id": a["id"], "text": a["text"], "owner": a["owner"], "unit": unit_label}
