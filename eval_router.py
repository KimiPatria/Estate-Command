"""
Eval dashboard router (Stage-3 R&D) — mounted on dashboard_server.py.

Serves the investigator eval results written by eval_harness.py:
  GET /evals          — dashboard page (eval_static/evals.html)
  GET /evals/data     — {runs: [...summaries newest first], cases: [...rows of
                         the selected run], run_id}
  GET /evals/data?run_id=run_...   — a specific run

Read-only: this router only reads evals/results/*.jsonl + *_summary.json.
"""

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

log = logging.getLogger("epms-evals")

router = APIRouter(prefix="/evals", tags=["evals"])

_DIR = Path(__file__).parent
_RESULTS = _DIR / "evals" / "results"
_PAGE = _DIR / "eval_static" / "evals.html"


def _summaries() -> list:
    out = []
    if _RESULTS.exists():
        for p in sorted(_RESULTS.glob("*_summary.json"), reverse=True):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception as exc:
                log.warning("[evals] bad summary %s: %s", p.name, exc)
    return out


def _rows(run_id: str) -> list:
    p = _RESULTS / f"{run_id}.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


@router.get("/data")
def evals_data(run_id: str | None = None):
    runs = _summaries()
    if not runs:
        return {"runs": [], "cases": [], "run_id": None,
                "note": "No eval runs yet — run `python eval_harness.py`."}
    chosen = run_id if run_id in {r["run_id"] for r in runs} else runs[0]["run_id"]
    cases = _rows(chosen)
    for c in cases:  # keep the payload lean; verdict details stay expandable
        c.pop("judge_full", None)
    return {"runs": runs, "cases": cases, "run_id": chosen}


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
def evals_page():
    if not _PAGE.exists():
        return JSONResponse(status_code=404, content={"detail": "evals.html missing"})
    return HTMLResponse(_PAGE.read_text(encoding="utf-8"))
