"""Phase 4 of buildplan_forecast_mlops.md: the Compare sources payload (forecast_compare.py).

  * every availability cell traces to the extract's own metadata;
  * a column missing from an extract shows as "not available";
  * agreement flags months that differ by more than 0.5%;
  * the same-model rows compare like with like.
"""
import json
from pathlib import Path

import pandas as pd
import pytest

import forecast_compare as fc
from forecast import estate_config


def _ex(columns: dict, monthly=None):
    """A fake extract: columns = {source_name: (item, rows, filled)}."""
    reg = [{"source_name": n, "item": it, "label": it, "unit": "", "written": None, "agg": None, "grain": "x"}
           for n, (it, _r, _f) in columns.items()]
    stats = {n: {"rows": r, "filled": f, "sum": None, "first_month": "2026-01", "last_month": "2026-02"}
             for n, (_it, r, f) in columns.items() if r is not None}
    return {"meta": {}, "stats": stats, "registry": reg, "monthly": monthly, "dir": Path(".")}


def test_a_column_missing_from_an_extract_is_not_available(monkeypatch):
    monkeypatch.setattr(estate_config, "estates", lambda sid: {})
    ex = {"live": {"EC": _ex({"bunches_total": ("bunches_total", 10, 10), "oph_lat": ("gps", None, None)})},
          "staging": {"EC": _ex({"ZMASAK": ("bunches_total", 10, 9)})}}
    items = {r["item"]: r for r in fc._availability(ex)["items"]}
    assert items["gps"]["live"]["per_estate"]["EC"]["status"] == "not available"
    assert items["gps"]["staging"]["status"] == "not available"          # staging never declared it
    assert items["bunches_total"]["staging"]["columns"] == ["ZMASAK"]
    assert items["bunches_total"]["staging"]["status"] == "populated"


def test_pooled_status_is_the_share_of_rows_across_estates(monkeypatch):
    monkeypatch.setattr(estate_config, "estates", lambda sid: {})
    ex = {"live": {"EC": _ex({"oph_lat": ("gps", 100, 50)}), "EB": _ex({"oph_lat": ("gps", 900, 0)})},
          "staging": {}}
    gps = next(r for r in fc._availability(ex)["items"] if r["item"] == "gps")["live"]
    assert gps["fill_rate"] == pytest.approx(0.05) and gps["status"] == "sparse"
    assert gps["per_estate"]["EC"]["status"] == "populated" and gps["per_estate"]["EB"]["status"] == "empty"


def test_agreement_flags_months_beyond_half_a_percent():
    m = lambda vals: pd.DataFrame({"month": ["2026-01", "2026-02", "2026-03"], "bunches_total": vals,
                                   "is_partial": [0, 0, 0]})
    ex = {"live": {"EC": {"monthly": m([1000, 1000, 1000])}},
          "staging": {"EC": {"monthly": m([1000, 1004, 1010])}}}
    a = fc._agreement(ex)[0]
    assert a["shared_months"] == 3 and a["matching_months"] == 2
    assert [x["month"] for x in a["mismatches"]] == ["2026-03"]


built = pytest.mark.skipif(not (Path(estate_config.series_dir("live", "EC")) / "columns.json").exists()
                           or not (Path(estate_config.series_dir("staging", "EC")) / "columns.json").exists(),
                           reason="extracts not built")


@built
def test_every_availability_cell_traces_to_the_extract_metadata():
    d = fc.payload(refresh=True)
    for row in d["availability"]["items"]:
        for sid in ("live", "staging"):
            for code, cell in row[sid]["per_estate"].items():
                if cell.get("joined") or cell["status"] == "not available":
                    continue
                meta = json.loads((Path(estate_config.series_dir(sid, code)) / "meta.json").read_text())
                col = meta["columns"][cell["column"]]
                assert col["status"] == cell["status"], (sid, code, cell["column"])
                assert col["fill_rate"] == pytest.approx(cell["fill_rate"], abs=1e-4)


@built
def test_same_model_rows_use_one_model_on_both_sources():
    d = fc.payload()
    ec = next(a for a in d["accuracy"] if a["estate"] == "EC")
    assert ec["same_model"] and all(m["origins"] > 0 for m in ec["same_model"])
    assert {m["model"] for m in ec["same_model"]} <= set(fc.SAME_MODELS)
