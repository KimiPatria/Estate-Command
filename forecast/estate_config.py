"""
The forecast pipeline's estate list, read from forecast/estates.yaml.

One place says which estates each source covers. Everything else asks here:
  build_series.py      what to extract, with which adapter, into which folder
  lab/registry.py      the Forecast Lab's estates (lab_estates)
  block_weekly.py      where each series file lives (block_sources)
  forecast_sources.py  page estate id -> lab id, per source (page_estates)

Stdlib + PyYAML only, so every env (dashboard, chronos) can import it.
"""

import os
from functools import lru_cache

import yaml

_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(_DIR)
# FORECAST_ESTATES_CONFIG and FORECAST_RUNS_DIR point a process (CI, a test) at
# another estate list and another runs folder; production leaves them unset.
CONFIG_PATH = os.environ.get("FORECAST_ESTATES_CONFIG") or os.path.join(_DIR, "estates.yaml")
LEGACY_LAB_DIR = os.environ.get("FORECAST_LEGACY_LAB_DIR") or os.path.join(_DIR, "experiments", "lab")

# Versioned runs (forecast/refresh.py, buildplan Phase 5). A run is a complete
# snapshot -- series/<source>/<CODE>/ for every source plus the Lab's lab/ tree --
# under RUNS_DIR/<run_id>/. RUNS_DIR/current.json names the run the pages serve.
# A refresh in progress points its own steps at its unfinished run through the
# FORECAST_RUN_DIR environment variable, so nothing else sees it until it is
# published. Before the first run, the paths below are the legacy ones.
RUNS_DIR = os.environ.get("FORECAST_RUNS_DIR") or os.path.join(_DIR, "runs")
CURRENT_PATH = os.path.join(RUNS_DIR, "current.json")
RUN_ENV = "FORECAST_RUN_DIR"
_pointer_cache = {"mtime": None, "value": None}


def current_run():
    """{"run_id", "dir", ...} of the published run, or None before the first refresh."""
    try:
        mtime = os.stat(CURRENT_PATH).st_mtime
    except OSError:
        return None
    if _pointer_cache["mtime"] != mtime:
        import json
        with open(CURRENT_PATH, encoding="utf-8") as fh:
            v = json.load(fh)
        v["dir"] = os.path.join(RUNS_DIR, v["run_id"])
        _pointer_cache.update(mtime=mtime, value=v)
    return _pointer_cache["value"]


def run_root():
    """Folder of the run being read: the refresh's own (env) or the published one."""
    env = os.environ.get(RUN_ENV)
    if env:
        return env
    cur = current_run()
    return cur["dir"] if cur else None


def lab_dir() -> str:
    """The Forecast Lab artifact tree of the run being read."""
    root = run_root()
    return os.path.join(root, "lab") if root else LEGACY_LAB_DIR


@lru_cache(maxsize=None)
def load(path: str = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    for sid, src in cfg["sources"].items():
        src.setdefault("enabled", True)
        for code, est in (src.get("estates") or {}).items():
            est.setdefault("code", code)
            est.setdefault("forecastable", True)
            est.setdefault("joined", [])
            est.setdefault("label", code)
            est["source"] = sid
    return cfg


def contract_version() -> int:
    return int(load()["contract_version"])


def thresholds() -> dict:
    return dict(load()["status_thresholds"])


def sources(enabled_only: bool = True) -> list:
    return [s for s, v in load()["sources"].items() if v["enabled"] or not enabled_only]


def source(sid: str) -> dict:
    return load()["sources"][sid]


def estates(sid: str) -> dict:
    """{code: estate config} for one source, in file order."""
    return dict(source(sid).get("estates") or {})


def legacy_series_dir(sid: str, code: str) -> str:
    return os.path.join(REPO, *source(sid)["series_dir"].split("/"), code)


def series_dir(sid: str, code: str) -> str:
    """Folder holding this estate's extract in the run being read (absolute)."""
    root = run_root()
    return os.path.join(root, "series", sid, code) if root else legacy_series_dir(sid, code)


def block_key(sid: str, code: str) -> str:
    """block_weekly.SOURCES key of an estate's series file."""
    return f"{'stg' if sid == 'staging' else sid}_{code}"


def block_sources() -> dict:
    """{block_weekly key: oph.csv path} for every configured estate."""
    return {block_key(sid, code): os.path.join(series_dir(sid, code), "oph.csv")
            for sid in sources() for code in estates(sid)}


def _abs(rel):
    return os.path.join(REPO, *rel.split("/")) if rel else None


def lab_estates() -> dict:
    """{lab_id: registry spec} -- the shape forecast/lab/registry.py ESTATES has."""
    out = {}
    for sid in sources():
        for code, e in estates(sid).items():
            out[e["lab_id"]] = {
                "label": e["label"], "blocks": block_key(sid, code),
                "served": e.get("served_ensemble"), "source": sid, "region": e.get("region"),
                "features": _abs(e.get("features")), "weather": _abs(e.get("weather")),
                "forecastable": e["forecastable"],
            }
    return out


def page_estates(sid: str) -> dict:
    """{page estate id: lab id} for the forecastable estates of a source."""
    return {code.lower(): e["lab_id"] for code, e in estates(sid).items() if e["forecastable"]}
