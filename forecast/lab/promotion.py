"""
Champion/challenger rule for the served monthly production forecast.

FIXED 2026-09-23, before the formal test below was first run and before any
shadow forecast was logged. Moving a threshold after seeing a result defeats
the purpose: if the rule has to change, add a new dated rule beside this one
and say why, rather than editing it.

Champion    served_ensemble       what /forecast serves today
Challenger  chronos2_monthly_cov  picked on 2026-09-23 from the k3 per-horizon
                                  backtest over Chronos-2 zero-shot (monthly,
                                  monthly + weather, daily), TimesFM 2.5 and
                                  Direct_pooled: best at every step 1-12
Estate      k3                    the only estate with enough history to
                                  backtest 1-3 months ahead
Steps       1, 2, 3               the served next-3-months product

Gate A -- backtest (k3 walk-forward; both models on the same (origin, step) rows)
  A1  pooled steps 1-3 MAPE: challenger lower
  A2  Diebold-Mariano with the HLN small-sample correction, APE loss, h=3,
      pooled steps 1-3 (the 2026-09 blend analysis's test): p < 0.05 in the
      challenger's favour. Origins overlap, so this p-value is too small;
      that is why Gate A alone never promotes.
  A3  MAE at each of steps 1, 2 and 3: challenger not higher
Gate B -- shadow (shadow.py; forecasts logged before their outcome was known)
  B1  at least 6 target months resolved at step 1
  B2  pooled steps 1-3 MAPE over the resolved rows: challenger lower
  B3  challenger closer than the champion in at least half of the resolved
      step-1 months
Verdict: PROMOTE when every check in A and B passes, otherwise HOLD.

Promotion is a person's decision, not automatic. When it happens, serve the
challenger for steps 1-6 only: on k3 nothing clearly beats "same month last
year" further out (challenger MASE 0.96 at steps 9 and 12), so steps 7-12
should be presented as seasonal outlook. Keep the champion computed as the
fallback for when the challenger's forecast is missing or stale.
"""

import importlib.util
import os

import numpy as np

_LAB = os.path.dirname(os.path.abspath(__file__))

RULE_DATE = "2026-09-23"
CHAMPION = "served_ensemble"
CHALLENGER = "chronos2_monthly_cov"
ESTATE = "k3"
STEPS = (1, 2, 3)
ALPHA = 0.05
DM_H = 3
SHADOW_MIN_RESOLVED = 6
SHADOW_MIN_WIN_SHARE = 0.5
MAX_SERVED_STEP = 6


def _load_by_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


reg = _load_by_path("epms_lab_registry", os.path.join(_LAB, "registry.py"))
ev = _load_by_path("epms_lab_evaluate", os.path.join(_LAB, "evaluate.py"))
shadow = _load_by_path("epms_lab_shadow", os.path.join(_LAB, "shadow.py"))


def _check(cid, label, passed, detail):
    return {"id": cid, "label": label, "passed": bool(passed), "detail": detail}


def backtest_gate():
    ref, why_ref = reg.backtest(CHAMPION, ESTATE, "month")
    alt, why_alt = reg.backtest(CHALLENGER, ESTATE, "month")
    if ref is None or alt is None:
        missing = why_ref if ref is None else why_alt
        return {"checks": [_check("A", "backtests available", False, missing)], "passed": False}
    j = ev.paired(ref, alt, STEPS)
    mape_ref = float(ev.ape(j["actual"], j["pred_ref"]).mean())
    mape_alt = float(ev.ape(j["actual"], j["pred_alt"]).mean())
    dm = ev.dm_hln(ev.ape(j["actual"], j["pred_ref"]), ev.ape(j["actual"], j["pred_alt"]), h=DM_H)
    s_ref, s_alt = ev.by_step(j, "ref"), ev.by_step(j, "alt")
    worse = [int(s) for s in STEPS if s in s_alt.index and s_alt.loc[s, "mae"] > s_ref.loc[s, "mae"]]
    checks = [
        _check("A1", "pooled steps 1-3 MAPE lower", mape_alt < mape_ref,
               f"challenger {mape_alt:.2f}% vs champion {mape_ref:.2f}%"),
        _check("A2", f"Diebold-Mariano (HLN, h={DM_H}) p < {ALPHA}, favouring the challenger",
               np.isfinite(dm["p"]) and dm["DM_hln"] > 0 and dm["p"] < ALPHA,
               f"DM_hln {dm['DM_hln']:+.2f}, p = {dm['p']:.4f}, n = {dm['n']} rows"),
        _check("A3", "MAE not higher at any of steps 1-3", not worse,
               "; ".join(f"h{int(s)} {s_alt.loc[s, 'mae']:,.0f} vs {s_ref.loc[s, 'mae']:,.0f}"
                         for s in s_ref.index)
               + (f" (worse at h{', h'.join(map(str, worse))})" if worse else "")),
    ]
    return {"checks": checks, "passed": all(c["passed"] for c in checks),
            "n_origins": int(j["origin"].nunique()), "n_rows": int(len(j)),
            "by_step": {int(s): {"champion": float(s_ref.loc[s, "mae"]),
                                 "challenger": float(s_alt.loc[s, "mae"]),
                                 "n": int(s_ref.loc[s, "n"])} for s in s_ref.index},
            "dm": {k: (None if isinstance(v, float) and not np.isfinite(v) else v)
                   for k, v in dm.items()}}


def shadow_gate():
    r = shadow.resolved(ESTATE)
    r = r[r["step"].isin(STEPS)]
    a = r[r["model"] == CHAMPION][["anchor", "step", "period", "actual", "pred"]]
    b = r[r["model"] == CHALLENGER][["anchor", "step", "pred"]]
    j = a.merge(b, on=["anchor", "step"], suffixes=("_ref", "_alt"))
    one = j[j["step"] == 1]
    n1 = int(one["period"].nunique())
    checks = [_check("B1", f"at least {SHADOW_MIN_RESOLVED} months resolved at step 1",
                     n1 >= SHADOW_MIN_RESOLVED, f"{n1} resolved so far")]
    if len(j):
        m_ref = float(ev.ape(j["actual"], j["pred_ref"]).mean())
        m_alt = float(ev.ape(j["actual"], j["pred_alt"]).mean())
        wins = int(((one["pred_alt"] - one["actual"]).abs()
                    < (one["pred_ref"] - one["actual"]).abs()).sum())
        checks += [
            _check("B2", "pooled steps 1-3 MAPE lower", m_alt < m_ref,
                   f"challenger {m_alt:.2f}% vs champion {m_ref:.2f}% over {len(j)} rows"),
            _check("B3", f"closer in at least {SHADOW_MIN_WIN_SHARE:.0%} of step-1 months",
                   n1 > 0 and wins / n1 >= SHADOW_MIN_WIN_SHARE, f"{wins} of {n1}"),
        ]
    else:
        checks += [_check("B2", "pooled steps 1-3 MAPE lower", False, "no resolved rows yet"),
                   _check("B3", f"closer in at least {SHADOW_MIN_WIN_SHARE:.0%} of step-1 months",
                          False, "no resolved rows yet")]
    return {"checks": checks, "passed": all(c["passed"] for c in checks), "n_resolved_h1": n1}


def status():
    a, b = backtest_gate(), shadow_gate()
    failing = [c["id"] for c in a["checks"] + b["checks"] if not c["passed"]]
    return {
        "rule_date": RULE_DATE, "champion": CHAMPION, "challenger": CHALLENGER,
        "estate": ESTATE, "steps": list(STEPS), "max_served_step": MAX_SERVED_STEP,
        "backtest": a, "shadow": b,
        "verdict": "PROMOTE" if not failing else "HOLD",
        "failing": failing,
        "shadow_log": shadow.summary(ESTATE),
    }


if __name__ == "__main__":
    s = status()
    print(f"Rule of {s['rule_date']}: {s['challenger']} vs {s['champion']} on {s['estate']}, "
          f"steps {s['steps']}")
    for gate in ("backtest", "shadow"):
        print(f"\n[{gate}]")
        for c in s[gate]["checks"]:
            print(f"  {'PASS' if c['passed'] else 'fail'}  {c['id']}  {c['label']}: {c['detail']}")
    print(f"\nVERDICT: {s['verdict']}" + (f" (failing: {', '.join(s['failing'])})" if s["failing"] else ""))
