"""
EPMS staging database (SQL Server): every estate in one database, split by WERKS.

The OPH queries are forecast/build_staging_series.py's (2026-11-10), moved
here unchanged except for the added native columns:
  * rows with STATE = 'D' are left out (read as deleted);
  * the division comes from the current BLK row (KDATE = '9999-12-31');
  * the daily total is read a second time with no join, for the gate.
Tonnage is FDN.WB_QTY_TOTAL, the weight of a whole delivery trip repeated on
every row of that trip, so it is taken once per SPB (business_rules_staging.txt
rule 5); a trip weighing 0 has not been weighed and is left out.
"""

import pandas as pd

import contract
from adapters.base import Adapter

C = contract.column
REGISTRY = [
    C("ZMASAK", "bunches_total", "bunches", written="bunches_total", agg="sum", grain="harvest ticket"),
    C("ZBRONDOLAN", "loose_fruit", "as recorded", written="loose_fruits", agg="sum", grain="harvest ticket"),
    C("OPH", "tickets", "tickets", written="tickets", agg="count", grain="harvest ticket"),
    C("EMPNR", "harvester", "employee id", written="harvesters", agg="count_distinct", grain="harvest ticket"),
    C("TPH", "harvest_point", "code", written="harvest_points", agg="count_distinct", grain="harvest ticket"),
    C("EMPNR_M", "foreman", "employee id", grain="harvest ticket"),
    C("EMPNR_K", "checker", "employee id", grain="harvest ticket"),
    C("METHD", "method", "code", grain="harvest ticket"),
    C("CARR_1", "transport", "carrier id", grain="harvest ticket"),
    C("IS_RAIN", "rain_flag", "flag", grain="harvest ticket"),
    C("RAIN_DURT", "rain_duration", "as recorded", grain="harvest ticket"),
    C("WB_QTY_TOTAL", "weighbridge", "kg per trip", grain="delivery trip (FDN)"),
]

_WHERE = "o.WERKS = :werks AND (o.STATE IS NULL OR o.STATE <> 'D')"

_BLOCK_SQL = f"""
SELECT o.BUDAT AS harvest_date, b.DIVNR AS division_code, o.BLOCK AS block_code,
       SUM(CAST(o.ZMASAK AS decimal(18, 2))) AS bunches_total,
       -- ZBRONDOLAN is a varchar column: P1 holds '1000.00'. Every value in the database
       -- parses as a number (checked 2026-11-12), so TRY_CAST loses nothing.
       SUM(COALESCE(TRY_CAST(o.ZBRONDOLAN AS decimal(18, 2)), 0)) AS loose_fruits,
       COUNT(*) AS tickets,
       COUNT(DISTINCT o.EMPNR) AS harvesters,
       COUNT(DISTINCT o.TPH) AS harvest_points
FROM OPH o
LEFT JOIN BLK b ON b.ESTNR = RIGHT(o.WERKS, 2) AND b.BLOCK = o.BLOCK AND b.KDATE = '9999-12-31'
WHERE {_WHERE}
GROUP BY o.BUDAT, b.DIVNR, o.BLOCK
"""

_DAILY_SQL = """
SELECT BUDAT AS harvest_date, SUM(CAST(ZMASAK AS decimal(18, 2))) AS bunches_total, COUNT(*) AS tickets
FROM OPH
WHERE WERKS = :werks AND (STATE IS NULL OR STATE <> 'D')
GROUP BY BUDAT
"""

_EXCLUDED_SQL = """
SELECT COUNT(*) AS n, COALESCE(SUM(CAST(ZMASAK AS decimal(18, 2))), 0) AS bunches
FROM OPH WHERE WERKS = :werks AND STATE = 'D'
"""


def _filled(col, numeric):
    if numeric:
        return f"COALESCE(TRY_CAST({col} AS float), 0) > 0"
    return f"{col} IS NOT NULL AND LTRIM(RTRIM(CAST({col} AS nvarchar(100)))) NOT IN ('', '0')"


_NUMERIC = {"ZMASAK", "ZBRONDOLAN", "RAIN_DURT"}
_OPH_COLS = [c["source_name"] for c in REGISTRY if c["source_name"] not in ("WB_QTY_TOTAL",)]


def _stats_sql():
    parts = ["COUNT(*) AS n"]
    for col in _OPH_COLS:
        f = _filled(f"o.{col}", col in _NUMERIC)
        parts.append(f"SUM(CASE WHEN {f} THEN 1 ELSE 0 END) AS f_{col}")
        parts.append(f"MIN(CASE WHEN {f} THEN o.BUDAT END) AS lo_{col}")
        parts.append(f"MAX(CASE WHEN {f} THEN o.BUDAT END) AS hi_{col}")
        if col in _NUMERIC:
            parts.append(f"SUM(COALESCE(TRY_CAST(o.{col} AS float), 0)) AS s_{col}")
    return "SELECT " + ",\n       ".join(parts) + f"\nFROM OPH o WHERE {_WHERE}"


# share of the estate's tickets that sit on a weighed delivery note
_FDN_COVER_SQL = f"""
SELECT COUNT(*) AS n, SUM(x.on_fdn) AS on_fdn,
       MIN(CASE WHEN x.on_fdn = 1 THEN x.BUDAT END) AS lo,
       MAX(CASE WHEN x.on_fdn = 1 THEN x.BUDAT END) AS hi
FROM (SELECT o.BUDAT,
             CASE WHEN EXISTS (SELECT 1 FROM FDN f WHERE f.OPH = o.OPH AND f.WB_QTY_TOTAL > 0)
                  THEN 1 ELSE 0 END AS on_fdn
      FROM OPH o WHERE {_WHERE}) x
"""

_WEIGHT_SQL = """
SELECT FORMAT(t.d, 'yyyy-MM') AS month, COUNT(*) AS trips, SUM(t.w) / 1000.0 AS tonnes
FROM (SELECT SPB, MIN(BUDAT) AS d, MAX(WB_QTY_TOTAL) AS w FROM FDN WHERE ESTNR = :code GROUP BY SPB) t
WHERE t.w > 0
GROUP BY FORMAT(t.d, 'yyyy-MM')
ORDER BY 1
"""


def _month(d):
    return str(pd.Timestamp(d).to_period("M")) if d is not None and not pd.isna(d) else None


class StagingSqlServer(Adapter):
    name = "staging_sqlserver"
    source_kind = "database"

    def __init__(self):
        self._engines = {}

    def _engine(self, cfg):
        url = self.database_url(cfg)
        if url not in self._engines:
            from sqlalchemy import create_engine
            self._engines[url] = create_engine(url, pool_pre_ping=True)
        return self._engines[url]

    @staticmethod
    def _werks(cfg):
        return "00" + cfg["code"]

    def column_registry(self, cfg):
        return REGISTRY

    def _read(self, cfg, sql, **params):
        from sqlalchemy import text
        with self._engine(cfg).connect() as conn:
            return pd.read_sql(text(sql), conn, params={"werks": self._werks(cfg), **params})

    def extract_blocks(self, cfg, since=None):
        df = self._read(cfg, _BLOCK_SQL)
        df["harvest_date"] = pd.to_datetime(df["harvest_date"])
        for c in ("bunches_total", "loose_fruits"):
            df[c] = df[c].astype(float)
        for c in ("tickets", "harvesters", "harvest_points"):
            df[c] = df[c].astype("int64")
        df["division_code"] = df["division_code"].astype("string").str.strip()
        df["block_code"] = df["block_code"].astype("string").str.strip()
        return df

    def extract_daily_totals(self, cfg, since=None):
        df = self._read(cfg, _DAILY_SQL)
        df["harvest_date"] = pd.to_datetime(df["harvest_date"])
        df["bunches_total"] = df["bunches_total"].astype(float)
        return df

    def column_stats(self, cfg):
        r = self._read(cfg, _stats_sql()).iloc[0]
        n = int(r["n"])
        out = {}
        for col in _OPH_COLS:
            out[col] = {"rows": n, "filled": int(r[f"f_{col}"] or 0),
                        "sum": float(r[f"s_{col}"]) if col in _NUMERIC else None,
                        "first_month": _month(r[f"lo_{col}"]), "last_month": _month(r[f"hi_{col}"])}
        c = self._read(cfg, _FDN_COVER_SQL).iloc[0]
        out["WB_QTY_TOTAL"] = {"rows": int(c["n"]), "filled": int(c["on_fdn"] or 0), "sum": None,
                               "first_month": _month(c["lo"]), "last_month": _month(c["hi"]),
                               "note": "share of harvest tickets on a weighed delivery note"}
        return out

    def describe(self, cfg):
        e = self._read(cfg, _EXCLUDED_SQL).iloc[0]
        return {"table": "OPH", "measure": "SUM(ZMASAK), ripe bunches", "werks": self._werks(cfg),
                "excluded": {"rule": "STATE = 'D'", "rows": int(e["n"]), "bunches": float(e["bunches"])}}

    def extras(self, cfg):
        w = self._read(cfg, _WEIGHT_SQL, code=cfg["code"])
        w["tonnes"] = w["tonnes"].astype(float).round(3)
        return {"weight_monthly.csv": w}


ADAPTER = StagingSqlServer()
