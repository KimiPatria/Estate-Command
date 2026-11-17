"""
Manual serving overrides: serve a candidate model on demand, per source and estate.

The source's dated rule (forecast/lab/select_source.py) or a manual promotion
(forecast/lab/promotion.py) decides what the forecast page serves. An override,
set from the Model health page's candidate scoreboard, takes precedence over
both for one (source, estate) until it is cleared:

  * it lives beside current.json (RUNS_DIR/serving_overrides.json), outside any
    run folder, so a refresh does not silently drop it; refresh.py records the
    overrides in force in run.json and MLflow;
  * every set and clear is appended to RUNS_DIR/serving_overrides_log.jsonl,
    which is never rewritten: who picked what, when, and what the rule served;
  * forecast_sources.py falls back to the rule's pick, and says so, when the
    overridden model can no longer build a forecast for that estate.

Nothing here touches the selection files, so the rule's own decision and its
challengers stay exactly as the refresh wrote them.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from threading import Lock

from forecast import estate_config as ec

_LOCK = Lock()


def path() -> str:
    return os.path.join(ec.RUNS_DIR, "serving_overrides.json")


def log_path() -> str:
    return os.path.join(ec.RUNS_DIR, "serving_overrides_log.jsonl")


def _key(source: str, page_id: str) -> str:
    return f"{source}/{page_id}"


def read() -> dict:
    """{"<source>/<page id>": {"model", "set_at", "rule_model", "set_by"}}; {} when none."""
    try:
        with open(path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get(source: str, page_id: str):
    return read().get(_key(source, page_id))


def _write(data: dict) -> None:
    os.makedirs(ec.RUNS_DIR, exist_ok=True)
    tmp = path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, path())


def _log(event: dict) -> None:
    with open(log_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, sort_keys=True) + "\n")


def set_model(source: str, page_ids: list, model: str, rule_models: dict,
              set_by: str = "model health page") -> dict:
    """Serve `model` on these estates of a source. An estate whose rule already picks
    `model` has its override cleared instead: serving the rule's pick needs none.
    Returns the overrides now in force for the source."""
    now = datetime.now().isoformat(timespec="seconds")
    with _LOCK:
        data = read()
        for pid in page_ids:
            key, rule = _key(source, pid), rule_models.get(pid)
            before = data.get(key)
            if model == rule:
                if before:
                    data.pop(key)
                    _log({"at": now, "action": "clear", "source": source, "estate": pid,
                          "model": before["model"], "rule_model": rule, "set_by": set_by,
                          "why": "the rule's own pick was chosen"})
                continue
            data[key] = {"model": model, "set_at": now, "rule_model": rule, "set_by": set_by}
            _log({"at": now, "action": "set", "source": source, "estate": pid, "model": model,
                  "previous": before["model"] if before else None, "rule_model": rule,
                  "set_by": set_by})
        _write(data)
    return {k: v for k, v in data.items() if k.startswith(source + "/")}


def clear(source: str, page_ids: list, set_by: str = "model health page") -> dict:
    """Return these estates of a source to the rule's pick."""
    now = datetime.now().isoformat(timespec="seconds")
    with _LOCK:
        data = read()
        for pid in page_ids:
            before = data.pop(_key(source, pid), None)
            if before:
                _log({"at": now, "action": "clear", "source": source, "estate": pid,
                      "model": before["model"], "rule_model": before.get("rule_model"),
                      "set_by": set_by})
        _write(data)
    return {k: v for k, v in data.items() if k.startswith(source + "/")}
