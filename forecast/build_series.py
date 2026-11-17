"""
Extract one source's harvest series into the forecast contract (buildplan_forecast_mlops.md Phase 3).

For every estate forecast/estates.yaml lists under the source, the estate's
adapter (forecast/adapters/) reads the rows; the result is validated against
forecast/contract.py and gated (block file = estate daily total, day for day;
monthly file = daily totals; every block has one division; no negative counts).
A failed gate writes nothing. Files land in <series_dir>/<CODE>/:

  oph.csv          core + native block-day columns
  monthly.csv      monthly totals with partial-month flags
  daily.csv        the join-free daily total the gate compared against
  meta.json        window, exclusions, gate, contract version, `columns` block
  columns.json     the adapter's column registry and fill statistics, so the same
                   files can be delivered again as a file drop
  <extras>         e.g. weight_monthly.csv (staging FDN tonnes)

Writes go to a temporary folder that replaces the estate's folder only when every
file is written, so a failure never leaves half an extract behind.

Usage (dashboard env):
    python forecast/build_series.py --source staging
    python forecast/build_series.py --source live --estates EC EB
    python forecast/build_series.py --source staging --out <dir>      # elsewhere, e.g. a file-drop test
"""

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime

import pandas as pd

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

import adapters          # noqa: E402
import contract          # noqa: E402
import estate_config     # noqa: E402


def _ints(df, cols):
    for c in cols:
        if c in df.columns and pd.api.types.is_numeric_dtype(df[c]):
            df[c] = df[c].round().astype("int64")
    return df


def extract(sid: str, code: str, cfg: dict) -> dict:
    """Run one estate through its adapter, the contract and the gate (nothing written)."""
    t0 = time.perf_counter()
    ad = adapters.get(cfg["adapter"])
    registry = ad.column_registry(cfg)
    blocks = ad.extract_blocks(cfg)
    daily = ad.extract_daily_totals(cfg)
    if daily.empty:
        raise SystemExit(f"[{sid}/{code}] the source returned no harvest rows")
    contract.validate(blocks, registry)
    months = contract.monthly(daily)
    result = contract.gate(blocks, daily, months)
    stats = ad.column_stats(cfg)
    info = ad.describe(cfg)
    first, last = daily["harvest_date"].min(), daily["harvest_date"].max()
    full = months[months["is_partial"] == 0]
    bunches = float(daily["bunches_total"].sum())
    meta = {
        "estate": code, "label": cfg["label"], "source": sid, "adapter": ad.name,
        "contract_version": contract.CONTRACT_VERSION,
        **{k: info[k] for k in ("table", "measure", "werks") if k in info},
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "first_day": str(first.date()), "last_day": str(last.date()),
        "calendar_days": int((last - first).days + 1), "harvest_days": int(len(daily)),
        "tickets": int(daily["tickets"].sum()) if "tickets" in daily else None,
        "bunches": bunches,
        "blocks": int(blocks[["division_code", "block_code"]].drop_duplicates().shape[0]),
        "months": int(len(months)), "full_months": int(len(full)),
        "first_full_month": str(full["month"].iloc[0]) if len(full) else None,
        "last_full_month": str(full["month"].iloc[-1]) if len(full) else None,
        **({"window": info["window"]} if "window" in info else {}),
        "excluded": info.get("excluded", {"rule": "none", "rows": 0}),
        "joined": cfg.get("joined", []),
        "columns": contract.columns_block(registry, stats, bunches),
        "gate": result,
    }
    extras = ad.extras(cfg)
    meta["extract_seconds"] = round(time.perf_counter() - t0, 1)   # the source reads, for the comparison
    return {"meta": meta, "blocks": blocks, "daily": daily, "months": months,
            "registry": registry, "stats": stats, "extras": extras}


def write(out: str, ex: dict) -> None:
    """Write one estate's files into `out`, atomically."""
    tmp = out + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    written = [c["written"] for c in ex["registry"]
                         if c["written"] and c["written"] not in contract.CORE_COLUMNS]
    blocks = ex["blocks"].sort_values(["harvest_date", "division_code", "block_code"]).copy()
    blocks["harvest_date"] = blocks["harvest_date"].dt.strftime("%Y-%m-%d")
    blocks = _ints(blocks, ["bunches_total"] + written)
    blocks[contract.CORE_COLUMNS + written].to_csv(os.path.join(tmp, "oph.csv"), index=False)
    months = _ints(ex["months"].copy(), ["bunches_total"])
    months.to_csv(os.path.join(tmp, "monthly.csv"), index=False)
    daily = ex["daily"].sort_values("harvest_date").copy()
    daily["harvest_date"] = daily["harvest_date"].dt.strftime("%Y-%m-%d")
    _ints(daily, ["bunches_total", "tickets"]).to_csv(os.path.join(tmp, "daily.csv"), index=False)
    with open(os.path.join(tmp, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(ex["meta"], fh, indent=2)
    with open(os.path.join(tmp, "columns.json"), "w", encoding="utf-8") as fh:
        json.dump({"registry": ex["registry"], "stats": ex["stats"]}, fh, indent=1)
    for name, df in ex["extras"].items():
        df.to_csv(os.path.join(tmp, name), index=False)
    if os.path.exists(out):
        shutil.rmtree(out)
    os.replace(tmp, out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", choices=estate_config.sources(), required=True)
    ap.add_argument("--estates", nargs="+", help="estate codes (default: every estate of the source)")
    ap.add_argument("--out", help="write under this folder instead of the source's series_dir")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ests = estate_config.estates(args.source)
    codes = args.estates or list(ests)
    failed = 0
    for code in codes:
        if code not in ests:
            raise SystemExit(f"{code} is not an estate of {args.source} in forecast/estates.yaml")
        cfg = ests[code]
        ex = extract(args.source, code, cfg)
        m = ex["meta"]
        print(f"[{args.source}/{code}] {m['first_day']}..{m['last_day']}  {m['harvest_days']} harvest days  "
              f"{m['bunches']:,.0f} bunches  {m['blocks']} blocks  {m['full_months']} full months")
        for c in m["gate"]["checks"]:
            print(f"    {'ok  ' if c['ok'] else 'FAIL'} {c['name']}: {c['detail']}")
        if not m["gate"]["ok"]:
            print(f"    gate failed: nothing written for {code}")
            failed += 1
            continue
        out = (os.path.join(args.out, code) if args.out else estate_config.series_dir(args.source, code))
        write(out, ex)
        cols = m["columns"]
        print(f"    wrote {out}  ({sum(1 for c in cols.values() if c['status'] == 'populated')} populated, "
              f"{sum(1 for c in cols.values() if c['status'] == 'sparse')} sparse, "
              f"{sum(1 for c in cols.values() if c['status'] == 'empty')} empty columns)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
