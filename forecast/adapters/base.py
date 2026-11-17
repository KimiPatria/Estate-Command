"""The adapter interface every source implements."""

import os

import pandas as pd

import contract
import estate_config


class Adapter:
    """How one kind of source is read. Subclasses set `name` and `source_kind` and
    implement the four extract methods; nothing else in the pipeline knows the source.

    extract_blocks(cfg, since)       block-day frame: core + written native columns
    extract_daily_totals(cfg, since) harvest_date, bunches_total, tickets -- read WITHOUT
                                     the block-level joins, so the gate can catch a join
                                     that multiplies a total
    column_stats(cfg)                {source_name: {rows, filled, sum, first_month,
                                     last_month}} at the source's own grain
    describe(cfg)                    what was read, for meta.json (window, exclusions)
    extras(cfg)                      {file name: DataFrame} written beside oph.csv
    """

    name = ""
    source_kind = ""        # "database" or "file"

    def column_registry(self, cfg) -> list:
        raise NotImplementedError

    def extract_blocks(self, cfg, since=None) -> pd.DataFrame:
        raise NotImplementedError

    def extract_daily_totals(self, cfg, since=None) -> pd.DataFrame:
        raise NotImplementedError

    def column_stats(self, cfg) -> dict:
        raise NotImplementedError

    def describe(self, cfg) -> dict:
        return {}

    def extras(self, cfg) -> dict:
        return {}

    # helpers ---------------------------------------------------------------
    @staticmethod
    def database_url(cfg) -> str:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(estate_config.REPO, ".env"))
        env = cfg.get("database_env")
        url = os.getenv(env or "")
        if not url:
            raise SystemExit(f"{cfg['source']}/{cfg['code']}: {env} is not set in .env")
        return url

    @staticmethod
    def written_columns(registry) -> list:
        return [c["written"] for c in registry if c["written"] and c["written"] not in contract.CORE_COLUMNS]

    @staticmethod
    def frame_stats(df, registry, month_col="harvest_date") -> dict:
        """Fill statistics of declared columns, measured on a frame at source grain."""
        months = pd.to_datetime(df[month_col]).dt.to_period("M").astype(str)
        out = {}
        for c in registry:
            col = c["source_name"]
            if col not in df.columns:
                continue
            v = df[col]
            if pd.api.types.is_numeric_dtype(v):
                filled = v.fillna(0) > 0
                s = float(v.fillna(0).sum())
            else:
                t = v.astype("string").str.strip()
                filled = t.notna() & ~t.isin(["", "0"])
                s = None
            m = months[filled]
            out[col] = {"rows": int(len(v)), "filled": int(filled.sum()), "sum": s,
                        "first_month": m.min() if len(m) else None,
                        "last_month": m.max() if len(m) else None}
        return out
