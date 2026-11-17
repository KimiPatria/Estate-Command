"""Source adapters: one per way of reading a source (buildplan_forecast_mlops.md Phase 3).

estates.yaml names the adapter per estate; get(name) returns it. Every adapter
returns frames in the shape forecast/contract.py defines, so build_series.py
treats them alike.
"""

import importlib

NAMES = ("staging_sqlserver", "live_postgres", "live_k3_file", "file_drop")


def get(name: str):
    if name not in NAMES:
        raise ValueError(f"unknown adapter {name!r}; known: {NAMES}")
    return importlib.import_module(f"adapters.{name}").ADAPTER
