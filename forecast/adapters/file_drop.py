"""
A delivered file drop: a folder (or, later, an S3 prefix) holding one estate's
files already in the contract's shape -- the likely production path for either
database when the client's IT pushes extracts instead of opening a connection.

Expected in the drop folder (estates.yaml `path`, may contain {code}):
  oph.csv        core + native block-day columns (forecast/contract.py)
  daily.csv      optional: harvest_date, bunches_total, tickets, computed at the
                 source WITHOUT the block joins; without it the gate can only
                 check the files against each other
  columns.json   optional: {"registry": [...], "stats": {...}} as the source's
                 adapter produced them, so the comparison keeps the source's own
                 column names and fill rates; without it they are measured on oph.csv
  weight_monthly.csv and any other extras are copied through.
"""

import json
import os

import pandas as pd

import contract
import estate_config
from adapters.base import Adapter


class FileDrop(Adapter):
    name = "file_drop"
    source_kind = "file"

    @staticmethod
    def folder(cfg):
        rel = cfg["path"].format(code=cfg["code"])
        return rel if os.path.isabs(rel) else os.path.join(estate_config.REPO, *rel.split("/"))

    def _columns(self, cfg):
        p = os.path.join(self.folder(cfg), "columns.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
        return None

    def _oph(self, cfg):
        df = pd.read_csv(os.path.join(self.folder(cfg), "oph.csv"),
                         dtype={"division_code": "string", "block_code": "string"})
        df["harvest_date"] = pd.to_datetime(df["harvest_date"])
        return df

    def column_registry(self, cfg):
        cols = self._columns(cfg)
        if cols:
            return cols["registry"]
        # Without the source's columns.json a drop declares its core column only: a
        # native column's meaning cannot be guessed from its header.
        return [contract.column("bunches_total", "bunches_total", "bunches", written="bunches_total",
                                agg="sum", grain="block day")]

    def extract_blocks(self, cfg, since=None):
        df = self._oph(cfg)
        df["bunches_total"] = df["bunches_total"].astype(float)
        return df

    def extract_daily_totals(self, cfg, since=None):
        p = os.path.join(self.folder(cfg), "daily.csv")
        if os.path.exists(p):
            df = pd.read_csv(p)
            df["harvest_date"] = pd.to_datetime(df["harvest_date"])
        else:
            o = self._oph(cfg)
            df = o.groupby("harvest_date").agg(bunches_total=("bunches_total", "sum")).reset_index()
            df["tickets"] = o.groupby("harvest_date")["tickets"].sum().to_numpy() if "tickets" in o else 0
        df["bunches_total"] = df["bunches_total"].astype(float)
        return df

    def column_stats(self, cfg):
        cols = self._columns(cfg)
        if cols:
            return cols["stats"]
        return self.frame_stats(self._oph(cfg), self.column_registry(cfg))

    def describe(self, cfg):
        p = os.path.join(self.folder(cfg), "meta.json")
        meta = {}
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                meta = json.load(fh)
        keep = {k: meta[k] for k in ("table", "measure", "window", "excluded", "werks") if k in meta}
        return {**keep, "delivered_from": self.folder(cfg)}

    def extras(self, cfg):
        out = {}
        for name in os.listdir(self.folder(cfg)):
            if name.endswith(".csv") and name not in ("oph.csv", "daily.csv", "monthly.csv"):
                out[name] = pd.read_csv(os.path.join(self.folder(cfg), name))
        return out


ADAPTER = FileDrop()
