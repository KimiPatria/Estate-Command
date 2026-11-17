"""Phase 7 of buildplan_forecast_mlops.md: the whole pipeline on synthetic estates.

What CI runs (no company data, no Chronos, no network): synthetic estates in both
source shapes (forecast/fixtures/make_synthetic.py) go through the real refresh --
extract through each adapter, contract, gate, Lab build with the stub model run,
Chronos-only selection, health, publish -- in a temporary runs folder. Then the
monitors are shown to fire on injected problems, and on the realised-error rules.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "forecast"))

import monitor                 # noqa: E402
from fixtures import make_synthetic   # noqa: E402


def _env(tmp_path, config):
    e = dict(os.environ)
    e.update({"FORECAST_ESTATES_CONFIG": str(config), "FORECAST_RUNS_DIR": str(tmp_path / "runs"),
              "FORECAST_LEGACY_LAB_DIR": str(tmp_path / "no_lab"), "PYTHONIOENCODING": "utf-8"})
    e.pop("FORECAST_RUN_DIR", None)
    return e


def _refresh(tmp_path, config):
    p = subprocess.run([sys.executable, str(ROOT / "forecast" / "refresh.py"), "--source", "all",
                        "--stub-inference", "--skip-after"], cwd=ROOT, env=_env(tmp_path, config),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    return p.returncode, p.stdout + p.stderr


def _current(tmp_path):
    cur = json.loads((tmp_path / "runs" / "current.json").read_text())
    return tmp_path / "runs" / cur["run_id"]


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ci")
    config = make_synthetic.make(str(tmp / "data"))
    code, out = _refresh(tmp, config)
    assert code == 0, out[-3000:]
    first = _current(tmp)
    make_synthetic.make(str(tmp / "data"), inject={"empty_column": ("SA", "ZBRONDOLAN"),
                                                    "volume_drop": ("SB", 0.5)})
    code2, out2 = _refresh(tmp, config)
    assert code2 == 0, out2[-3000:]
    return {"tmp": tmp, "config": config, "first": first, "second": _current(tmp)}


def test_a_synthetic_refresh_publishes_both_sources(pipeline):
    run = pipeline["first"]
    info = json.loads((run / "run.json").read_text())
    assert info["status"] == "published" and info["inference"] == "stub"
    assert info["health"]["ok"], info["health"]
    for sid in ("live", "staging"):
        for code in ("SA", "SB"):
            meta = json.loads((run / "series" / sid / code / "meta.json").read_text())
            assert meta["gate"]["ok"] and meta["columns"]
        sel = json.loads((run / "lab" / f"{sid}_selection.json").read_text())
        assert sel["served"].startswith("chronos2_")
    served = info["health"]["served"]
    assert all(served[f"{s}/{c}"]["served"] for s in ("live", "staging") for c in ("sa", "sb"))


def test_the_seasonally_adjusted_arm_is_built_and_reported_but_not_served(pipeline):
    run = pipeline["first"]
    lab = run / "lab"
    for sid, prefix in (("live", ""), ("staging", "stg_")):
        for code in ("sa", "sb"):
            est = f"{prefix}{code}"
            fwd = json.loads((lab / "chronos2_weekly_seasadj" / f"{est}_month.json").read_text())
            # 30 seasonal months: the estate has seen every month of the year, so it is on
            assert any(n.startswith("seasonally adjusted") for n in fwd["notes"]), fwd["notes"]
            bt = pd.read_csv(lab / "backtest" / "chronos2_weekly_seasadj" / f"{est}_month.csv")
            assert len(bt) and bt["pred"].notna().all()
        sel = json.loads((lab / f"{sid}_selection.json").read_text())
        assert sel["served"] != "chronos2_weekly_seasadj"
        assert "chronos2_weekly_seasadj" in sel["challengers"]
    health = json.loads((run / "health.json").read_text())
    shape = [m for m in health["monitoring"] if m["check"] == "seasonal_shape"]
    assert shape and all(m["level"] in ("ok", "info") for m in shape)


_SERVE = r"""
import json, os
from fastapi import FastAPI
from fastapi.testclient import TestClient
import forecast_router, forecast_sources as fs
from forecast import overrides
reg = fs._lab()[0]
app = FastAPI(); app.include_router(forecast_router.router)
c = TestClient(app)
sel_path = fs._lab()[2].selection_path(reg, "staging")
sel_before = open(sel_path, encoding="utf-8").read()
out = {}
base = fs.payload("sa", 3, source="staging")
out["rule"] = base["model_id"]
arm = "chronos2_weekly_seasadj"
r = c.post("/forecast/serve", json={"source": "staging", "estate": "sa", "model": arm})
out["set_status"] = r.status_code
d = fs.payload("sa", 3, source="staging")
out["served"] = d["model_id"]; out["mode"] = d["serving"]["mode"]
out["rule_model"] = d["serving"]["estates"][0]["rule_model"]
out["changed"] = [f["ensemble"] for f in d["forecast"]] != [f["ensemble"] for f in base["forecast"]]
out["other_estate"] = fs.payload("sb", 3, source="staging")["model_id"]
out["group"] = fs.payload("all", 3, source="staging")["model_id"]
h = c.get("/forecast/model-health", params={"source": "staging", "estate": "sa"}).json()
out["health_served"] = h["lifecycle"]["served_model_id"]; out["health_version"] = h["lifecycle"]["version"]
out["board_ids"] = [row.get("id") for row in h["scoreboard"]]
out["unknown"] = c.post("/forecast/serve", json={"source": "staging", "estate": "sa", "model": "nope"}).status_code
out["bad_estate"] = c.post("/forecast/serve", json={"source": "staging", "estate": "zz", "model": arm}).status_code
r = c.post("/forecast/serve", json={"source": "staging", "estate": "sa", "model": out["rule"]})
out["after_rule_pick"] = fs.payload("sa", 3, source="staging")["serving"]["mode"]
c.post("/forecast/serve", json={"source": "staging", "estate": "all", "model": arm})
out["group_all"] = fs.payload("all", 3, source="staging")["model_id"]
c.post("/forecast/serve", json={"source": "staging", "estate": "all", "model": None})
out["cleared"] = fs.payload("all", 3, source="staging")["model_id"]
out["left"] = overrides.read()
out["log"] = [json.loads(l)["action"] for l in open(overrides.log_path(), encoding="utf-8")]
out["selection_untouched"] = open(sel_path, encoding="utf-8").read() == sel_before
print(json.dumps(out))
"""


def test_a_candidate_can_be_served_on_demand_and_returned_to_the_rule(pipeline):
    p = subprocess.run([sys.executable, "-c", _SERVE], cwd=ROOT, env=_env(pipeline["tmp"], pipeline["config"]),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    assert p.returncode == 0, p.stderr[-3000:]
    d = json.loads(p.stdout.strip().splitlines()[-1])
    assert d["rule"] != "chronos2_weekly_seasadj" and d["set_status"] == 200
    assert d["served"] == "chronos2_weekly_seasadj" and d["mode"] == "override"
    assert d["rule_model"] == d["rule"] and d["changed"]
    assert d["other_estate"] == d["rule"] and d["group"] == "mixed"      # only SA was picked
    assert d["health_served"] == "chronos2_weekly_seasadj" and d["health_version"] == "manual pick"
    assert "chronos2_weekly_seasadj" in d["board_ids"]
    assert d["unknown"] == 409 and d["bad_estate"] == 409
    assert d["after_rule_pick"] == "rule"                               # picking the rule's model clears it
    assert d["group_all"] == "chronos2_weekly_seasadj" and d["cleared"] == d["rule"]
    assert d["left"] == {} and d["selection_untouched"]
    assert d["log"] == ["set", "clear", "set", "set", "clear", "clear"]


def test_the_comparison_builds_on_synthetic_data(pipeline):
    code = ("import json, forecast_compare as fc; d = fc.payload(); "
            "print(json.dumps({'items': len(d['availability']['items']), "
            "'harvester': next(r for r in d['availability']['items'] if r['item'] == 'harvester'), "
            "'acc': [a['estate'] for a in d['accuracy']], 'agree': [a['shared_months'] for a in d['agreement']]}))")
    p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=_env(pipeline["tmp"], pipeline["config"]),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    assert p.returncode == 0, p.stderr[-2000:]
    d = json.loads(p.stdout.strip().splitlines()[-1])
    assert d["items"] > 10
    assert d["harvester"]["live"]["status"] == "not available"          # the live shape has no harvester
    assert d["harvester"]["staging"]["columns"] == ["EMPNR"]
    assert set(d["acc"]) == {"SA", "SB"} and all(n > 20 for n in d["agree"])


def test_monitors_fire_on_injected_problems(pipeline):
    health = json.loads((pipeline["second"] / "health.json").read_text())
    alerts = {(m["source"], m["estate"], m["check"]) for m in health["monitoring"] if m["level"] == "alert"}
    assert ("staging", "sa", "column_drift") in alerts                 # ZBRONDOLAN populated -> empty
    assert ("staging", "sb", "volume") in alerts                       # newest full month halved
    assert ("live", "sa", "column_drift") not in alerts
    fresh = [m for m in health["monitoring"] if m["check"] == "freshness"]
    assert fresh and all(m["level"] == "ok" for m in fresh)            # synthetic data ends yesterday


def test_no_data_folder_is_tracked_in_git():
    p = subprocess.run(["git", "ls-files", "forecast/staging", "forecast/live", "forecast/runs",
                        "forecast/experiments", "gis/data/estates"], cwd=ROOT, capture_output=True, text=True)
    if p.returncode != 0:
        pytest.skip("not a git checkout")
    assert p.stdout.strip() == "", "company data is tracked:\n" + p.stdout[:500]


# ── the realised-error rules, on a hand-made shadow log ─────────────────────

def _resolved(preds, actual=100.0, model="chronos2_monthly", naive=None):
    rows = [{"model": model, "step": 1, "period": f"2026-{i + 1:02d}", "pred": p, "actual": actual}
            for i, p in enumerate(preds)]
    if naive is not None:
        rows += [{"model": "naive_trailing3", "step": 1, "period": f"2026-{i + 1:02d}", "pred": p,
                  "actual": actual} for i, p in enumerate(naive)]
    return pd.DataFrame(rows)


def test_two_months_running_outside_the_band_is_an_alert():
    r = monitor.realised_error("live", "ec", _resolved([100, 101, 140, 150]), "chronos2_monthly", {1: 10.0}, 9.0)
    assert any(x["level"] == "alert" and "last two running" in x["detail"] for x in r)
    ok = monitor.realised_error("live", "ec", _resolved([100, 140, 101]), "chronos2_monthly", {1: 10.0}, 30.0)
    assert all(x["level"] == "ok" for x in ok)


def test_rolling_error_beyond_one_and_a_half_times_the_backtest_is_an_alert():
    r = monitor.realised_error("live", "ec", _resolved([108, 108, 108]), "chronos2_monthly", {1: 50.0}, 4.0)
    assert any(x["level"] == "alert" and "in the backtest" in x["detail"] for x in r)


def test_chronos_losing_to_naive_is_an_alert_only_with_three_months():
    r = monitor.chronos_vs_naive("live", "eb", _resolved([130, 130, 130], naive=[105, 105, 105]), "chronos2_monthly")
    assert r[0]["level"] == "alert"
    r = monitor.chronos_vs_naive("live", "eb", _resolved([130, 130], naive=[105, 105]), "chronos2_monthly")
    assert r[0]["level"] == "info"


def test_freshness_and_volume_thresholds():
    from datetime import date
    assert monitor.freshness("live", "ec", "2026-07-22", today=date(2026, 11, 12))["level"] == "alert"
    assert monitor.freshness("staging", "ec", "2026-11-05", today=date(2026, 11, 12))["level"] == "ok"
    m = pd.DataFrame({"month": ["2025-10", "2026-09", "2026-10"], "bunches_total": [1000, 900, 500],
                      "is_partial": [0, 0, 0]})
    levels = {x["detail"].split(" against ")[1]: x["level"] for x in monitor.volume("staging", "ec", m)}
    assert levels == {"2026-09": "ok", "2025-10": "alert"}              # -44% vs the month before, -50% YoY
