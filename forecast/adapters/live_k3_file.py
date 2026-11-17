"""
K3's live export: forecast/epms_production_daily.csv, one row per harvest record,
M/D/YYYY dates, the same grade columns as the live estate databases. K3 has no
database connection in this project; this file is its live source.
"""

import os

import pandas as pd

import contract
import estate_config
from adapters.base import Adapter
from adapters.live_postgres import GRADES

C = contract.column
REGISTRY = (
    [C("bunches_total", "bunches_total", "bunches", written="bunches_total", agg="sum")]
    + [C(g, g, "bunches", written=g, agg="sum") for g in GRADES]
    + [C("loose_fruits", "loose_fruit", "as recorded", written="loose_fruits", agg="sum"),
       C("harvest_date", "tickets", "records", written="tickets", agg="count")]
)
_SUMS = ["bunches_total"] + GRADES + ["loose_fruits"]


class LiveK3File(Adapter):
    name = "live_k3_file"
    source_kind = "file"

    def column_registry(self, cfg):
        return REGISTRY

    def _raw(self, cfg):
        """The export's rows; a grade column the export lacks counts as zero in the
        sums but stays absent from the column statistics ("not available")."""
        rel = cfg["path"]
        path = rel if os.path.isabs(rel) else os.path.join(estate_config.REPO, *rel.split("/"))
        df = pd.read_csv(path)
        df.attrs["present"] = set(df.columns)
        df["harvest_date"] = pd.to_datetime(df["harvest_date"], format="mixed")
        for c in _SUMS:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0) if c in df.columns else 0.0
        return df

    def extract_blocks(self, cfg, since=None):
        df = self._raw(cfg)
        df["division_code"] = df["division_code"].astype("string").str.strip()
        df["block_code"] = df["block_code"].astype("string").str.strip()
        g = df.groupby(["harvest_date", "division_code", "block_code"], as_index=False)
        out = g[_SUMS].sum()
        out["tickets"] = g.size()["size"].astype("int64").to_numpy()
        return out

    def extract_daily_totals(self, cfg, since=None):
        df = self._raw(cfg)
        g = df.groupby("harvest_date")
        return pd.DataFrame({"bunches_total": g["bunches_total"].sum(), "tickets": g.size()}).reset_index()

    def column_stats(self, cfg):
        df = self._raw(cfg)
        present = df.attrs.get("present", set(df.columns))
        return {k: v for k, v in self.frame_stats(df, REGISTRY).items() if k in present}

    def describe(self, cfg):
        df = self._raw(cfg)
        return {"table": os.path.basename(cfg["path"]), "measure": "SUM(bunches_total)",
                "window": {"from": str(df["harvest_date"].min().date()),
                           "to": str(df["harvest_date"].max().date()), "rule": "the whole export"},
                "excluded": {"rule": "none", "rows": 0}}


ADAPTER = LiveK3File()
