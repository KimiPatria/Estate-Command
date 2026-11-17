"""Phase 3 of buildplan_forecast_mlops.md: config-driven estates, the data contract, adapters.

  * the contract rejects broken extracts and refuses another contract version;
  * a native column can never be written under a core name;
  * column status follows the fill-rate thresholds;
  * the same rows give the same core frame through any adapter (file adapters
    here; the database adapters were checked against the existing files when
    they were built, and the last tests re-check that on the built artifacts);
  * a new estate is a config entry.
"""
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "forecast"))

import adapters          # noqa: E402
import build_series      # noqa: E402
import contract          # noqa: E402
import estate_config     # noqa: E402


def _blocks(**over):
    df = pd.DataFrame({"harvest_date": pd.to_datetime(["2026-01-01", "2026-01-02"]),
                       "division_code": ["1", "1"], "block_code": ["007", "008"],
                       "bunches_total": [10.0, 20.0]})
    for k, v in over.items():
        df[k] = v
    return df


REG = [contract.column("bunches_total", "bunches_total", "bunches", written="bunches_total", agg="sum")]


# ── contract rules ───────────────────────────────────────────────────────

def test_contract_accepts_a_good_extract():
    contract.validate(_blocks(), REG)


@pytest.mark.parametrize("broken, why", [
    (_blocks().drop(columns=["block_code"]), "core columns missing"),
    (_blocks(bunches_total=[10.0, -1.0]), "negative"),
    (_blocks(harvest_date=[pd.Timestamp("2026-01-01"), pd.NaT]), "empty values"),
    (_blocks(bunches_total=["10", "x"]), "not numeric"),
])
def test_contract_rejects_broken_extracts(broken, why):
    with pytest.raises(contract.ContractError, match=why):
        contract.validate(broken, REG)


def test_contract_rejects_a_declared_native_column_the_extract_lacks():
    reg = REG + [contract.column("ZBRONDOLAN", "loose_fruit", written="loose_fruits", agg="sum")]
    with pytest.raises(contract.ContractError, match="declared native columns missing"):
        contract.validate(_blocks(), reg)


def test_a_native_column_can_never_be_written_under_a_core_name():
    with pytest.raises(contract.ContractError, match="may not be written as core column"):
        contract.column("ZBRONDOLAN", "loose_fruit", written="bunches_total")


def test_unknown_data_items_are_refused():
    with pytest.raises(contract.ContractError, match="unknown data item"):
        contract.column("X", "not_an_item")


@pytest.mark.parametrize("fill, expected", [
    (None, "not available"), (0.0, "empty"), (0.0009, "empty"), (0.001, "sparse"),
    (0.1999, "sparse"), (0.20, "populated"), (1.0, "populated")])
def test_status_follows_the_fill_rate_thresholds(fill, expected):
    assert contract.status(fill, {"populated": 0.20, "sparse": 0.001}) == expected


def test_loaders_refuse_another_contract_version():
    contract.check_version({"contract_version": contract.CONTRACT_VERSION})
    with pytest.raises(contract.ContractError, match="contract version"):
        contract.check_version({"contract_version": contract.CONTRACT_VERSION + 1}, "EC")


def test_page_ignores_meta_written_under_another_version(tmp_path, monkeypatch):
    import forecast_sources as fs
    d = tmp_path / "EC"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"contract_version": 999, "last_day": "2026-01-01"}))
    monkeypatch.setattr(fs, "_series_dir", lambda page_id, source: d)
    assert fs._meta("ec", fs.STAGING) == {}


def test_columns_block_reports_status_share_and_months():
    reg = REG + [contract.column("oph_lat", "gps", "latitude")]
    stats = {"bunches_total": {"rows": 10, "filled": 10, "sum": 100.0, "first_month": "2026-01", "last_month": "2026-02"},
             "oph_lat": {"rows": 10, "filled": 1, "sum": None, "first_month": "2026-02", "last_month": "2026-02"}}
    cols = contract.columns_block(reg, stats, 100.0, {"populated": 0.20, "sparse": 0.001})
    assert cols["bunches_total"]["status"] == "populated" and cols["bunches_total"]["share_of_bunches"] == 1.0
    assert cols["oph_lat"]["status"] == "sparse" and cols["oph_lat"]["fill_rate"] == 0.1


# ── adapters: same rows, same core frame ─────────────────────────────────

def _k3_like_export(path):
    rows = []
    for day in pd.date_range("2025-01-01", "2025-03-31"):
        for div, blk in [(1, "007"), (1, "008"), (2, "011")]:
            for _ in range(2):                        # two harvest records per block-day
                rows.append({"harvest_date": f"{day.month}/{day.day}/{day.year}", "division_code": div,
                             "block_code": blk, "bunches_total": 5, "bunches_ripe": 5,
                             "loose_fruits": 2})
    pd.DataFrame(rows).to_csv(path, index=False)


def test_a_file_export_and_its_file_drop_give_the_same_files(tmp_path):
    src = tmp_path / "export.csv"
    _k3_like_export(src)
    cfg = {"code": "XX", "label": "XX", "source": "live", "adapter": "live_k3_file", "joined": [],
           "path": str(src)}
    ex = build_series.extract("live", "XX", cfg)
    assert ex["meta"]["gate"]["ok"]
    first = tmp_path / "built" / "XX"
    build_series.write(str(first), ex)
    drop = {"code": "XX", "label": "XX", "source": "live", "adapter": "file_drop", "joined": [],
            "path": str(first)}
    ex2 = build_series.extract("live", "XX", drop)
    second = tmp_path / "again" / "XX"
    build_series.write(str(second), ex2)
    for f in ("oph.csv", "monthly.csv", "daily.csv"):
        assert (first / f).read_bytes() == (second / f).read_bytes(), f
    assert json.loads((first / "meta.json").read_text())["columns"] == \
        json.loads((second / "meta.json").read_text())["columns"]
    oph = pd.read_csv(first / "oph.csv")
    assert list(oph.columns[:4]) == contract.CORE_COLUMNS
    assert oph["bunches_total"].sum() == 90 * 3 * 2 * 5            # every record counted once
    assert oph["tickets"].sum() == 90 * 3 * 2


def test_a_failed_gate_writes_nothing(tmp_path, monkeypatch):
    src = tmp_path / "export.csv"
    _k3_like_export(src)
    cfg = {"code": "XX", "label": "XX", "source": "live", "adapter": "live_k3_file", "joined": [], "path": str(src)}
    ad = adapters.get("live_k3_file")
    real = ad.extract_daily_totals
    monkeypatch.setattr(ad, "extract_daily_totals",
                        lambda c, since=None: real(c).assign(bunches_total=lambda d: d["bunches_total"] * 2))
    ex = build_series.extract("live", "XX", cfg)
    assert not ex["meta"]["gate"]["ok"]                              # the join-free total disagrees
    monkeypatch.setattr(estate_config, "estates", lambda sid: {"XX": cfg})
    monkeypatch.setattr(estate_config, "sources", lambda enabled_only=True: ["live"])
    assert build_series.main(["--source", "live", "--out", str(tmp_path / "out")]) == 1
    assert not (tmp_path / "out" / "XX").exists()


# ── config ───────────────────────────────────────────────────────────────

def test_page_order_and_lab_ids_come_from_the_config():
    assert list(estate_config.page_estates("staging")) == ["ea", "eb", "ec"]
    assert estate_config.page_estates("live") == {"k3": "k3", "ec": "ec", "eb": "eb", "ea": "ea"}
    lab = estate_config.lab_estates()
    assert lab["stg_ec"]["blocks"] == "stg_EC" and lab["k3"]["served"] == "k3"


def test_a_new_estate_is_a_config_entry(tmp_path):
    cfg = (ROOT / "forecast" / "estates.yaml").read_text(encoding="utf-8")
    cfg = cfg.replace("      # P1:\n      #   lab_id: stg_p1\n      #   label: P1\n      #   adapter: staging_sqlserver\n"
                      "      #   code: P1\n      #   database_env: DATABASE_URL_STAGING\n      #   region: plasma\n"
                      "      #   forecastable: false\n",
                      "      P1:\n        lab_id: stg_p1\n        label: P1\n        adapter: staging_sqlserver\n"
                      "        code: P1\n        database_env: DATABASE_URL_STAGING\n        region: plasma\n"
                      "        forecastable: false\n")
    path = tmp_path / "estates.yaml"
    path.write_text(cfg, encoding="utf-8")
    estate_config.load.cache_clear()
    try:
        loaded = estate_config.load(str(path))
        assert "P1" in loaded["sources"]["staging"]["estates"]
        assert loaded["sources"]["staging"]["estates"]["P1"]["forecastable"] is False
    finally:
        estate_config.load.cache_clear()


# ── the built extracts (skipped until built) ─────────────────────────────

LIVE_EC = ROOT / "forecast" / "live" / "EC"
GIS_EC = ROOT / "gis" / "data" / "estates" / "EC" / "oph.csv"


@pytest.mark.skipif(not (LIVE_EC / "oph.csv").exists() or not GIS_EC.exists(), reason="live extract not built")
def test_live_extract_matches_the_map_snapshot_block_day_for_block_day():
    import importlib.util
    spec = importlib.util.spec_from_file_location("bw_t", ROOT / "forecast" / "block_weekly.py")
    bw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bw)
    a, b = bw.load_daily_blocks("EC"), bw.load_daily_blocks("live_EC")
    assert a.shape == b.shape and a.columns.equals(b.columns) and np.allclose(a.values, b.values)


@pytest.mark.skipif(not (LIVE_EC / "meta.json").exists(), reason="live extract not built")
def test_every_built_extract_lists_its_columns_with_a_status():
    for sid in estate_config.sources():
        for code in estate_config.estates(sid):
            meta_path = Path(estate_config.series_dir(sid, code)) / "meta.json"
            if not meta_path.exists():
                continue
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            assert meta["contract_version"] == contract.CONTRACT_VERSION
            assert meta["gate"]["ok"]
            assert meta["columns"] and all(c["status"] in ("populated", "sparse", "empty", "not available")
                                           for c in meta["columns"].values())
