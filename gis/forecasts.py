"""Tomorrow's outlook: the four forecasts, packaged for people who run the estate.

The models live in gis/models (rain, headcount, slippage, rates). This module
never fits anything. It turns their output into what a manager or mandor can
act on without knowing what a regression is:

    a sentence first     "a 54% chance of rain heavy enough to wash off spraying"
    a range, not a point "expect 19 to 23 of 24 (most likely 21)"
    what it means        "hold spraying: a lost round wastes more than a day's wait"
    how far to trust it  one word from the backtest, and the comparison behind it
    how it works         four plain steps, with the technical note kept separate
    what makes it real   the extract that would replace the generated history

Every figure is computed server-side and returned beside its sentence, so the
screen and the copilot quote the same numbers.
"""

import logging
import threading
from datetime import date, timedelta

from gis import assumptions, ops
from gis.build_operations import TOMORROW, WINDOW_END

log = logging.getLogger("estate-command.forecasts")

WORK_OPS = ("harvest", "prune", "weed", "spray", "pest")
TYPE_WORDS = {"harvest": "harvest gangs", "upkeep": "upkeep crews", "spray": "spray teams",
              "pest": "pest teams"}

EXPLAIN = {
    "rain": {
        "title": "Rain",
        "question": "Will it rain enough to matter tomorrow?",
        "what": ("The chance that tomorrow's rain reaches the amounts that matter for field work: 15 mm, "
                 "which washes herbicide off; 25 mm, heavy rain that slows crews and roads; and the "
                 "cutoff that stops field work."),
        "how": [
            "Every evening a weather service publishes a forecast for the next day.",
            ("On this estate that forecast is often wrong about amounts, so it is not taken at face "
             "value."),
            ("Instead the app finds the past days whose forecast looked most like tomorrow's, and counts "
             "how often it actually rained that much on them."),
            ("That count is the chance. If 24 of 40 similar days had 15 mm or more, the chance is about "
             "60%. A few days of the month's usual weather are mixed in so a small sample cannot say "
             "0% or 100%."),
        ],
        "read": [
            ("A 50% chance means that on days like this it happens about half the time. It does not mean "
             "rain for half the day."),
            "Unlikely is not impossible: a 20% event still comes about 1 day in 5.",
        ],
        "real": ("Already real: real forecasts, scored against real recorded rainfall. The estate's own "
                 "rain gauges would make it sharper, because the recorded figure used now is an estimate "
                 "for the area, not a gauge on the estate."),
        "technical": ("k-nearest neighbours on log(1 + forecast mm) one and two days ahead plus season; "
                      "k chosen on days before the plan's anchor date; blended with monthly climatology "
                      "(8 pseudo-days); scored by Brier score against climatology and the raw forecast."),
    },
    "headcount": {
        "title": "Who turns up",
        "question": "How many people will each crew have tomorrow?",
        "what": "How many people on each crew's roll are likely to turn up: a most likely number and a range.",
        "how": [
            "It starts from how well the crew has turned up on normal working days over the last two weeks.",
            ("It adjusts for the day of the week, Lebaran leave, the days after payday and the chance of "
             "heavy rain, by how much each of those has mattered before."),
            "The range is set so the real number lands inside it about 8 days in 10.",
        ],
        "read": [
            "'Expect 19 to 23 (most likely 21)': plan for 21, and nothing from 19 to 23 should surprise you.",
            "A wider range means the day is harder to call, for example when heavy rain is possible.",
        ],
        "real": ("Learned from generated attendance. Two years of EPMS attendance (t_attendance, "
                 "m_gang_employee) would teach it this estate's real patterns, including more than one "
                 "Lebaran."),
        "technical": ("Binomial logistic regression per crew-day (IRLS, ridge-pooled crew effects, separate "
                      "Sunday and holiday terms for harvest gangs); 80% interval from the overdispersed "
                      "binomial plus rain uncertainty; weekly rolling-origin backtest against the trailing "
                      "mean."),
    },
    "work_done": {
        "title": "How much gets done",
        "question": "How much of tomorrow's plan will actually get done?",
        "what": ("How much of the planned work is likely to get done, and which blocks are likely to need "
                 "another day to finish."),
        "how": [
            "It looks at every past order: what was planned, what got done, and the conditions that day.",
            ("It learns how much rain, poor roads, low turnout and a block's own track record cut the work "
             "done."),
            ("For tomorrow it runs the plan through every rain amount the forecast allows and the turnout "
             "expected, and averages the results."),
        ],
        "read": [
            "'Expect about 81% done (66% to 92%)': 8 days out of 10 like tomorrow land in that range.",
            ("A block flagged as likely to need another day is not a fault in the plan. It is where a "
             "supervisor's attention pays off."),
        ],
        "real": ("Learned from the generated ledger. It needs the plan side of the EPMS work records "
                 "(planned beside actual, per block per day), which the export does not carry."),
        "technical": ("Logistic regression for whether an order is worked, log-linear ridge regression for "
                      "the share done, averaged over the model's own past errors; Monte Carlo over rain "
                      "analogues and headcount draws; weekly rolling-origin backtest."),
    },
    "speeds": {
        "title": "Crew speeds",
        "question": "Which crews work faster or slower than the book?",
        "what": ("Which crews cover more or less ground than the textbook rate, and which harvest blocks "
                 "go faster or slower than their target."),
        "how": [
            "Every crew starts at the textbook rate from the assumption register.",
            "As records build up, its speed moves toward what it actually did on ordinary days.",
            ("The more man-days of records, the further it moves, and it never moves more than a set "
             "amount in one week, so one odd week cannot swing the plan."),
        ],
        "read": [
            ("'About 7% faster than the average crew': it covers about 7% more ground in a day, so the plan "
             "gives it more."),
            ("A speed describes pace, not effort. A slow crew may have harder blocks, older tools or fewer "
             "experienced hands."),
        ],
        "real": ("Learned from the generated ledger, where each crew was given a hidden speed; the check "
                 "shows the learning finds it. Real EPMS work records with man-days would make it real."),
        "technical": ("Empirical Bayes shrinkage of log(observed rate / book rate) toward the operation mean, "
                      "man-day weighted, recomputed weekly with a capped step; completed orders excluded as "
                      "censored; rain divided out where it slows the rate."),
    },
}


def _d(on) -> date:
    if isinstance(on, date):
        return on
    return date.fromisoformat(on) if on else TOMORROW


def _label(d: date) -> str:
    return d.strftime("%A %d %B %Y").replace(" 0", " ")


def switches() -> dict:
    av = assumptions.values()
    return {"rain": int(av["use_rain_model"]) == 1, "headcount": int(av["use_headcount_model"]) == 1,
            "work_done": int(av["use_slippage_model"]) == 1, "speeds": int(av["use_learned_rates"]) == 1,
            "plan_on_expected": int(av["plan_on_expected_adherence"]) == 1}


def _tone(grade: dict | None) -> str:
    return (grade or {}).get("tone") or "low"


# ── trust ──────────────────────────────────────────────────────────────────

def trust() -> list[dict]:
    from gis.models import headcount, rain, rates, slippage
    sw = switches()
    rb, hb, sb, qb = rain.backtest(), headcount.backtest(), slippage.backtest(), rates.backtest()
    out = []
    if rb.get("available"):
        out.append({"model": "rain", "title": EXPLAIN["rain"]["title"], "grade": rb["grade"],
                    "in_use": sw["rain"], "trained_on": "real", "headline": rb["plain"]["headline"],
                    "detail": rb["plain"]["raw"]})
    if hb.get("available"):
        out.append({"model": "headcount", "title": EXPLAIN["headcount"]["title"], "grade": hb["grade"],
                    "in_use": sw["headcount"] and hb["grade"]["passes"], "trained_on": "synthetic",
                    "headline": hb["plain"]["headline"], "detail": hb["plain"]["estate"]})
    if sb.get("available"):
        out.append({"model": "work_done", "title": EXPLAIN["work_done"]["title"], "grade": sb["grade"],
                    "in_use": sw["work_done"] and sb["grade"]["passes"], "trained_on": "synthetic",
                    "headline": sb["plain"]["headline"], "detail": sb["plain"]["knowing"]})
    if qb.get("available"):
        out.append({"model": "speeds", "title": EXPLAIN["speeds"]["title"], "grade": qb["grade"],
                    "in_use": sw["speeds"], "trained_on": "synthetic",
                    "headline": qb["plain"]["headline"],
                    "detail": qb["plain"].get("by_operation") or qb["plain"]["how"]})
    return out


# ── the outlook ────────────────────────────────────────────────────────────

def outlook(on=None) -> dict:
    from gis.models import headcount, rain, rates, scheduler
    d = _d(on)
    av = assumptions.values()
    sw = switches()
    cards = {}

    fc = rain.forecast(d, float(av["rain_cutoff_mm"])) if sw["rain"] else {"available": False}
    cards["rain"] = ({"available": True, **{k: fc[k] for k in (
        "headline", "chances", "amount", "forecast_plain", "recorded_plain", "how", "grade",
        "source", "source_label", "month_average_pct")}} if fc.get("available") else
        {"available": False, "reason": "The rain forecast is switched off in the assumption register."})

    if sw["headcount"]:
        est = headcount.estate(d)
        if est.get("available"):
            by_type = []
            for t, words in TYPE_WORDS.items():
                e = headcount.estate(d, t)
                if e.get("available") and e["crews"]:
                    by_type.append({"crew_type": t, "label": words, "low": e["low"], "high": e["high"],
                                    "most_likely": e["most_likely"], "on_roll": e["on_roll"],
                                    "plain": f"{words.capitalize()}: expect {e['low']:,} to {e['high']:,} "
                                             f"of {e['on_roll']:,} (most likely {e['most_likely']:,})."})
            dips = sorted((r for r in est["rows"] if r["recent_pct"] is not None),
                          key=lambda r: r["expected"] - r["on_roll"] * r["recent_pct"] / 100)[:4]
            cards["headcount"] = {
                "available": True, "low": est["low"], "high": est["high"],
                "most_likely": est["most_likely"], "on_roll": est["on_roll"], "crews": est["crews"],
                "turnout_pct": est["turnout_pct"], "plain": est["plain"], "by_type": by_type,
                "recorded": est["recorded"] if d <= WINDOW_END else None,
                "watch": [{"crew_code": r["crew_code"], "plain": r["plain"], "drivers": r["drivers"][1:]}
                          for r in dips if r["on_roll"] * r["recent_pct"] / 100 - r["expected"] >= 1.5],
                "grade": headcount.backtest().get("grade"),
            }
        else:
            cards["headcount"] = {"available": False, "reason": est.get("reason")}
    else:
        cards["headcount"] = {"available": False,
                              "reason": "The headcount forecast is switched off in the assumption register."}

    work = []
    for op in WORK_OPS:
        p = scheduler.plan(op, d.isoformat())
        if not p.get("available"):
            continue
        f = p.get("forecast") or {}
        wd = f.get("work_done") or {}
        row = {"operation": op, "label": p["label"], "unit": p["unit"],
               "window_key": {"harvest": "ops_harvest", "prune": "ops_prune", "weed": "ops_weed",
                              "spray": "ops_weed", "pest": "ops_pest"}[op],
               "stops": p["weather"]["stops_work"], "reason": p["weather"].get("reason"),
               "in_use": bool(wd.get("in_use"))}
        if wd.get("in_use"):
            row.update({"expected_pct": wd["expected_pct"], "low_pct": wd["low_pct"], "high_pct": wd["high_pct"],
                        "plain": wd["plain"], "risk_plain": wd["risk_plain"],
                        "at_risk_count": wd["at_risk_count"]})
        elif row["stops"]:
            row["plain"] = p["headline"]
        else:
            row["plain"] = f"Work done is not forecast for this plan: {wd.get('reason', 'switched off')}."
        if f.get("spray_call"):
            row["spray_call"] = f["spray_call"]
        work.append(row)
    cards["work_done"] = {"available": bool(work), "operations": work,
                          "grade": __import__("gis.models.slippage", fromlist=["x"]).backtest().get("grade")}

    speeds = []
    for op in rates.RATE_OPS:
        t = rates.table(op, d, limit=3)
        if not t.get("available"):
            continue
        speeds.append({"operation": op, "label": ops.OPERATIONS[op]["label"], "unit": t["unit"],
                       "faster": t["faster"], "slower": t["slower"], "total": t["total"],
                       "in_use": t["in_use"], "examples": [r["plain"] for r in t["rows"][:2]],
                       "plain": (f"{ops.OPERATIONS[op]['label']}: {t['faster']} {t['unit']}s faster than "
                                 f"average and {t['slower']} slower, out of {t['total']}.")})
    cards["speeds"] = {"available": bool(speeds) and sw["speeds"], "operations": speeds,
                       "reason": None if sw["speeds"] else "Learned speeds are switched off in the assumption register.",
                       "grade": rates.backtest().get("grade")}

    # What tomorrow looks like, in a few lines.
    lines = []
    if cards["rain"].get("available"):
        c = cards["rain"]["chances"]
        lines.append(f"Rain: {c['washoff']['pct']}% chance of enough to wash off spraying; heavy rain is "
                     f"{c['heavy']['words']} ({c['heavy']['pct']}%).")
    if cards["headcount"].get("available"):
        h = cards["headcount"]
        lines.append(f"People: expect {h['low']:,} to {h['high']:,} of {h['on_roll']:,} to turn up.")
    hv = next((w for w in work if w["operation"] == "harvest" and w.get("in_use")), None)
    if hv:
        lines.append(f"Harvest: expect about {hv['expected_pct']}% of the plan done "
                     f"({hv['low_pct']}% to {hv['high_pct']}%).")
    sp = next((w for w in work if w["operation"] == "spray"), None)
    if sp and sp.get("spray_call"):
        go = sp["spray_call"]["go"]
        lines.append(f"Spraying: {'go ahead' if go else 'hold'} "
                     f"({sp['spray_call']['p_washoff_pct']}% chance of wash-off, break-even "
                     f"{sp['spray_call']['breakeven_pct']}%).")
    elif sp and sp["stops"]:
        lines.append("Spraying: held for the weather.")

    return {
        "available": True, "date": d.isoformat(), "date_label": _label(d),
        "is_tomorrow": d == TOMORROW, "inside_ledger": d <= WINDOW_END,
        "window": ops.window(), "switches": sw,
        "summary": lines, "cards": cards, "trust": trust(),
        "note": ("Rain is learned from real forecasts and real rainfall. Headcount, work done and crew "
                 "speeds are learned from the generated ledger: they show how the method works on data "
                 "shaped like this estate's, not yet what its crews really do."),
        "glossary": [
            {"term": "Chance", "plain": "How often this happened on past days like this one."},
            {"term": "Likely range", "plain": "Where the real figure lands about 8 days in 10."},
            {"term": "Most likely", "plain": "The single best guess, in the middle of the range."},
            {"term": "Trust grade", "plain": ("How much better the forecast did than the simple method it "
                                              "replaces, on past days it had not seen.")},
        ],
    }


# ── detail views ───────────────────────────────────────────────────────────

def rain_view(on=None) -> dict:
    from gis.models import rain
    d = _d(on)
    av = assumptions.values()
    fc = rain.forecast(d, float(av["rain_cutoff_mm"]))
    bt = rain.backtest()
    recent = []
    for back in range(7, 0, -1):
        x = d - timedelta(days=back)
        if x >= TOMORROW:
            continue
        f = rain.forecast(x, float(av["rain_cutoff_mm"]))
        if f.get("available") and f.get("recorded_mm") is not None:
            said = f["chances"]["washoff"]["pct"]
            fell = f["recorded_mm"]
            recent.append({"date": x.isoformat(), "label": x.strftime("%a %d %b"), "said_pct": said,
                           "fell_mm": fell, "happened": fell >= 15.0,
                           "plain": f"{x.strftime('%a %d %b')}: said {said}%, {fell:.0f} mm fell."})
    return {"available": fc.get("available", False), "date": d.isoformat(), "date_label": _label(d),
            "in_use": switches()["rain"], "forecast": fc, "backtest": bt, "recent": recent,
            "explain": EXPLAIN["rain"]}


def headcount_view(on=None, crew_type: str | None = None) -> dict:
    from gis.models import headcount
    d = _d(on)
    est = headcount.estate(d, crew_type)
    recent = []
    for back in range(7, 0, -1):
        x = d - timedelta(days=back)
        if x > WINDOW_END:
            continue
        e = headcount.estate(x, crew_type)
        if e.get("available") and e.get("recorded") is not None:
            ok = e["low"] <= e["recorded"] <= e["high"]
            recent.append({"date": x.isoformat(), "label": x.strftime("%a %d %b"), "low": e["low"],
                           "high": e["high"], "most_likely": e["most_likely"], "recorded": e["recorded"],
                           "inside": ok,
                           "plain": (f"{x.strftime('%a %d %b')}: expected {e['low']:,} to {e['high']:,}, "
                                     f"{e['recorded']:,} came.")})
    rows = sorted(est.get("rows") or [], key=lambda r: (str(r["division_code"]), r["crew_code"]))
    return {"available": est.get("available", False), "date": d.isoformat(), "date_label": _label(d),
            "in_use": switches()["headcount"], "estate": {k: v for k, v in est.items() if k != "rows"},
            "crews": rows, "crew_type": crew_type, "backtest": headcount.backtest(),
            "recovery": headcount.recovery(), "recent": recent, "explain": EXPLAIN["headcount"]}


def work_done_view(operation: str = "harvest", on=None) -> dict:
    from gis.models import scheduler, slippage
    d = _d(on)
    if operation not in WORK_OPS:
        return {"available": False, "reason": f"Work done is forecast for {', '.join(WORK_OPS)}."}
    p = scheduler.plan(operation, d.isoformat())
    f = (p.get("forecast") or {}) if p.get("available") else {}
    wd = f.get("work_done") or {}
    blocks = {b["block_label"]: b for c in (p.get("crews") or []) for b in c["blocks"]}
    risk = []
    for r in wd.get("at_risk") or []:
        b = blocks.get(r["block_label"]) or {}
        risk.append({**r, "drivers": b.get("risk_drivers") or [],
                     "plain": (f"Block {r['block_label']} ({r['crew_code']}): a {round(100 * r['p_carried'])}% "
                               f"chance it needs another day; expect about {round(100 * r['expected_share'])}% "
                               "of it done.")})
    crews = []
    for c in p.get("crews") or []:
        t = c["totals"]
        if not c["blocks"] or t.get("expected_qty_low") is None:
            continue
        crews.append({"crew_code": c["crew_code"], "planned_qty": t["qty"], "expected_qty": t["expected_qty"],
                      "low_qty": t["expected_qty_low"], "high_qty": t["expected_qty_high"],
                      "unit": t["unit"], "block_labels": c["block_labels"],
                      "plain": (f"{c['crew_code']}: expect {t['expected_qty']:,.0f} of {t['qty']:,.0f} {t['unit']} "
                                f"({t['expected_qty_low']:,.0f} to {t['expected_qty_high']:,.0f}).")})
    bt = slippage.backtest()
    return {"available": bool(p.get("available")), "operation": operation,
            "label": p.get("label"), "unit": p.get("unit"),
            "date": d.isoformat(), "date_label": _label(d), "in_use": bool(wd.get("in_use")),
            "work_done": wd, "at_risk": risk, "crews": crews,
            "stops": (p.get("weather") or {}).get("stops_work"), "headline": p.get("headline"),
            "spray_call": f.get("spray_call"),
            "backtest": {**bt, "this_operation": ((bt.get("modes") or {}).get("six_in_the_morning") or {})
                         .get("by_operation", {}).get(operation),
                         "this_operation_knowing": ((bt.get("modes") or {}).get("knowing_the_weather") or {})
                         .get("by_operation", {}).get(operation)},
            "recovery": slippage.recovery(), "effects": slippage.effects_plain(),
            "explain": EXPLAIN["work_done"]}


def speeds_view(operation: str = "weed", on=None) -> dict:
    from gis.models import rates
    d = _d(on)
    t = rates.table(operation, d, limit=200)
    rec = rates.recovery()
    rows = [r for r in rec.get("rows") or []
            if r["effect"].startswith(operation) or (operation == "harvest" and r["effect"].startswith("harvest"))]
    k, cap = rates._settings()
    return {**t, "date_label": _label(d), "recovery": rows, "all_recovery": rec,
            "settings": {"prior_man_days": k, "max_weekly_change_pct": cap},
            "explain": EXPLAIN["speeds"]}


def accuracy() -> dict:
    from gis.models import headcount, rain, rates, slippage
    return {
        "trust": trust(),
        "rain": rain.backtest(),
        "headcount": {"backtest": headcount.backtest(), "recovery": headcount.recovery()},
        "work_done": {"backtest": slippage.backtest(), "recovery": slippage.recovery()},
        "speeds": {"backtest": rates.backtest(), "recovery": rates.recovery()},
        "explain": EXPLAIN,
        "rules": [
            "Every forecast is checked on past days it had not seen, using only what was known the evening before.",
            "A forecast that does not beat the simple method it replaces is not used; the plan keeps the simple method.",
            ("Three of the four learn from generated data. The checks show they find what the generator put there "
             "and invent nothing it did not."),
        ],
    }


# ── lifecycle ──────────────────────────────────────────────────────────────

def warm() -> None:
    """Fit and score everything once, in the background, so the first plan is quick."""
    def run():
        try:
            from gis.models import headcount, rain, rates, slippage
            rain.backtest()
            headcount.backtest()
            headcount.recovery()
            slippage.backtest()
            slippage.recovery()
            rates.backtest()
            rates.recovery()
            log.info("[forecasts] models warm")
        except Exception as exc:
            log.warning("[forecasts] warm-up failed: %s", exc)
    threading.Thread(target=run, name="forecasts-warm", daemon=True).start()


def reload() -> None:
    from gis.models import headcount, rain, rates, slippage
    for m in (rain, headcount, slippage, rates):
        m.reload()


def assumption_changed(key: str | None) -> None:
    """Drop what a register edit invalidates. Switches are read live."""
    from gis.models import headcount, rates
    if key is None or key in ("payday_day_of_month", "attendance_lookback_days", "holiday_attendance_factor"):
        headcount.reload()
    if key is None or key in ("rate_prior_man_days", "rate_max_weekly_change_pct", "harvest_bunches_per_man_day",
                              "prune_palms_per_man_day", "circle_weed_ha_per_man_day",
                              "path_upkeep_ha_per_man_day", "spray_ha_per_man_day"):
        rates.reload()
