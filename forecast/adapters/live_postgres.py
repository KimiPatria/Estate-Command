"""
Live estate databases (PostgreSQL): one EPMS database per estate, table t_oph.

The same rows gis/build_estate_data.py snapshots for the map: t_oph minus
oph_is_deleted, the harvest date is oph_created_date, restricted to the
estate's recording window (the longest run of months each holding >= 25% of
the busiest month's records -- the map's own recording_window(), imported so
both read the same months). Read with a read-only transaction.
"""

import importlib.util
import os

import pandas as pd

import contract
import estate_config
from adapters.base import Adapter

C = contract.column
GRADES = ["bunches_ripe", "bunches_overripe", "bunches_underripe", "bunches_unripe",
          "bunches_wet", "bunches_rotten", "bunches_long_stalk", "bunches_empty",
          "bunches_dirty", "bunches_unfresh", "bunches_old", "bunches_pest_damaged_old",
          "bunches_pest_damaged_new", "bunches_diseased"]
REGISTRY = (
    [C("bunches_total", "bunches_total", "bunches", written="bunches_total", agg="sum")]
    + [C(g, g, "bunches", written=g, agg="sum") for g in GRADES]
    + [C("loose_fruits", "loose_fruit", "as recorded", written="loose_fruits", agg="sum"),
       C("oph_id", "tickets", "records", written="tickets", agg="count"),
       C("oph_tph_code", "harvest_point", "code", written="harvest_points", agg="count_distinct"),
       C("oph_card_id", "harvest_card", "card id", written="harvest_cards", agg="count_distinct"),
       C("mandor_employee_code", "foreman", "employee id"),
       C("kerani_panen_employee_code", "checker", "employee id"),
       C("oph_harvesting_method", "method", "code"),
       C("bunches_not_sent", "bunches_not_sent", "bunches"),
       C("oph_lat", "gps", "latitude"),
       C("oph_is_raining", "rain_flag", "flag"),
       C("oph_raining_duration", "rain_duration", "as recorded")]
)
_NUMERIC = set(["bunches_total", "loose_fruits", "bunches_not_sent"] + GRADES)


def _gis_window():
    path = os.path.join(estate_config.REPO, "gis", "build_estate_data.py")
    spec = importlib.util.spec_from_file_location("epms_gis_estate_data", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_WHERE = "oph_estate_code = :estate and coalesce(oph_is_deleted, 0) = 0"


def _filled(col):
    if col in _NUMERIC:
        return f"coalesce({col}, 0) > 0"
    return f"{col} is not null and trim({col}::text) not in ('', '0', 'false')"


def _month(d):
    return str(pd.Timestamp(d).to_period("M")) if d is not None and not pd.isna(d) else None


class LivePostgres(Adapter):
    name = "live_postgres"
    source_kind = "database"

    def __init__(self):
        self._window = {}

    def column_registry(self, cfg):
        return REGISTRY

    def _engine(self, cfg):
        from sqlalchemy import create_engine
        return create_engine(self.database_url(cfg), pool_pre_ping=True,
                             connect_args={"options": "-c default_transaction_read_only=on"})

    def _read(self, cfg, sql, **params):
        from sqlalchemy import text
        with self._engine(cfg).connect() as conn:
            return pd.read_sql(text(sql), conn, params={"estate": cfg["code"], **params})

    def window(self, cfg):
        """(first day, last day, months dropped, deleted rows) of the recording window."""
        if cfg["code"] not in self._window:
            gis = _gis_window()
            from sqlalchemy import text
            with self._engine(cfg).connect() as conn:
                months = [(r.month, r.n, int(r.bunches))
                          for r in conn.execute(gis.MONTHS_SQL, {"estate": cfg["code"]})]
                deleted = conn.execute(gis.DELETED_SQL, {"estate": cfg["code"]}).scalar()
                m0, m1 = gis.recording_window(months)
                lo, hi = conn.execute(gis.LAST_DAY_SQL, {"estate": cfg["code"], "m0": m0, "m1": m1}).one()
            dropped = [{"month": m, "records": n, "bunches": b} for m, n, b in months if not (m0 <= m <= m1)]
            self._window[cfg["code"]] = (lo, hi, dropped, int(deleted or 0))
        return self._window[cfg["code"]]

    def _bounds(self, cfg):
        lo, hi, _d, _x = self.window(cfg)
        return {"lo": lo, "hi": hi}

    def extract_blocks(self, cfg, since=None):
        sums = ", ".join(f"sum(coalesce({g}, 0)) as {g}" for g in ["bunches_total"] + GRADES + ["loose_fruits"])
        sql = f"""
            select oph_created_date as harvest_date, oph_division_code as division_code,
                   oph_block_code as block_code, {sums},
                   count(*) as tickets, count(distinct oph_tph_code) as harvest_points,
                   count(distinct oph_card_id) as harvest_cards
            from t_oph
            where {_WHERE} and oph_created_date between :lo and :hi
            group by 1, 2, 3
        """
        df = self._read(cfg, sql, **self._bounds(cfg))
        df["harvest_date"] = pd.to_datetime(df["harvest_date"])
        for c in ["bunches_total"] + GRADES + ["loose_fruits"]:
            df[c] = df[c].astype(float)
        for c in ("tickets", "harvest_points", "harvest_cards"):
            df[c] = df[c].astype("int64")
        df["division_code"] = df["division_code"].astype("string").str.strip()
        df["block_code"] = df["block_code"].astype("string").str.strip()
        return df

    def extract_daily_totals(self, cfg, since=None):
        sql = f"""
            select oph_created_date as harvest_date, sum(coalesce(bunches_total, 0)) as bunches_total,
                   count(*) as tickets
            from t_oph where {_WHERE} and oph_created_date between :lo and :hi
            group by 1
        """
        df = self._read(cfg, sql, **self._bounds(cfg))
        df["harvest_date"] = pd.to_datetime(df["harvest_date"])
        df["bunches_total"] = df["bunches_total"].astype(float)
        return df

    def column_stats(self, cfg):
        parts = ["count(*) as n"]
        cols = [c["source_name"] for c in REGISTRY]
        for col in cols:
            f = _filled(col)
            parts += [f"sum(case when {f} then 1 else 0 end) as f_{col}",
                      f"min(case when {f} then oph_created_date end) as lo_{col}",
                      f"max(case when {f} then oph_created_date end) as hi_{col}"]
            if col in _NUMERIC:
                parts.append(f"sum(coalesce({col}, 0))::float as s_{col}")
        sql = ("select " + ", ".join(parts)
               + f" from t_oph where {_WHERE} and oph_created_date between :lo and :hi")
        r = self._read(cfg, sql, **self._bounds(cfg)).iloc[0]
        n = int(r["n"])
        return {col: {"rows": n, "filled": int(r[f"f_{col}"] or 0),
                      "sum": float(r[f"s_{col}"]) if col in _NUMERIC else None,
                      "first_month": _month(r[f"lo_{col}"]), "last_month": _month(r[f"hi_{col}"])}
                for col in cols}

    def describe(self, cfg):
        lo, hi, dropped, deleted = self.window(cfg)
        return {"table": "t_oph", "measure": "SUM(bunches_total)",
                "window": {"from": str(lo), "to": str(hi),
                           "rule": "contiguous months each holding >= 25% of the busiest month's records"},
                "excluded": {"rule": "oph_is_deleted = 1, and months outside the recording window",
                             "rows": deleted + sum(d["records"] for d in dropped),
                             "deleted_rows": deleted, "outside_window": dropped}}


ADAPTER = LivePostgres()
