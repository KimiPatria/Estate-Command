"""
One refresh command for the forecast pages (buildplan_forecast_mlops.md Phase 5).

    python forecast/refresh.py --source staging          # or live, or all
    python forecast/refresh.py --source all --remote     # Chronos on SageMaker
    python forecast/refresh.py --list                    # runs, newest first
    python forecast/refresh.py --rollback <run_id>       # serve an earlier run again

A run is a complete snapshot under forecast/runs/<run_id>/:
  series/<source>/<CODE>/   every source's extract (refreshed sources re-extracted,
                            the others carried over from the run being served)
  lab/                      the Forecast Lab's artifacts, backtests and selections
  run.json                  what happened: steps, timings, watermarks, gate results,
                            checkpoint revision, rule, served models, status
Steps, each a subprocess pointed at the unfinished run by FORECAST_RUN_DIR (so the
server and the pages never see it):
  1 extract    forecast/build_series.py per source (contract + gate; a failed gate stops)
  2 inference  forecast/lab/build_artifacts.py --source <s> in the Chronos env
  3 select     forecast/lab/select_source.py --source <s>
  4 health     every forecastable estate is served, fresh, on a selected model
  5 publish    the run folder is renamed into place and forecast/runs/current.json
               is replaced in one step; the pages switch on their next request
  6 after      shadow log record + backup, MLflow run + model version (best effort:
               a failure is reported in run.json, the run stays published)
A failure in steps 1-4 leaves the published run untouched; the unfinished folder is
kept as <run_id>.failed for inspection. The exit code is non-zero and names the step.

Weather is NOT re-fetched here. K3's weather file is a legacy file that does not
describe K3 (forecast/fetch_nasa_weather.py), and K3 is served on Chronos-2 with
weather covariates whose backtest used that file: replacing it is a modelling
change to decide on, not a refresh step.

Zero-shot Chronos has no training step: inference runs locally in minutes on CPU;
--remote sends it to SageMaker instead (needs a live `aws login`).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_DIR)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

import estate_config as ec      # noqa: E402

KEEP_RUNS = 6
LOCK_NAME = ".refresh.lock"


def _pid_alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259                                       # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock():
    """One refresh at a time. A lock left by a killed refresh is taken over, and the
    unfinished folder it left is renamed <run_id>.killed (kept for inspection)."""
    path = os.path.join(ec.RUNS_DIR, LOCK_NAME)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            held = json.load(fh)
        if _pid_alive(int(held.get("pid", 0))):
            raise SystemExit(f"another refresh is running (pid {held['pid']}, run {held.get('run_id')})")
    killed = []
    for name in os.listdir(ec.RUNS_DIR):
        if name.endswith(".partial"):
            os.replace(os.path.join(ec.RUNS_DIR, name),
                       os.path.join(ec.RUNS_DIR, name[:-len(".partial")] + ".killed"))
            killed.append(name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "since": datetime.now().isoformat(timespec="seconds")}, fh)
    return killed


def release_lock():
    try:
        os.remove(os.path.join(ec.RUNS_DIR, LOCK_NAME))
    except OSError:
        pass
PY = sys.executable


def _chronos_python():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_REPO, ".env"))
    p = os.environ.get("CHRONOS_PYTHON")
    if not p:
        raise SystemExit("CHRONOS_PYTHON is not set: add it to .env (the chronos-rd-py313 python.exe)")
    return p


class StepFailed(Exception):
    def __init__(self, step, detail):
        super().__init__(f"{step}: {detail}")
        self.step, self.detail = step, detail


class Run:
    def __init__(self, sources, remote=False, stub=False, skip_after=False):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.id = f"{stamp}-{'-'.join(sources)}"
        self.sources = sources
        self.remote = remote
        self.stub = stub              # CI: stand-in model run, no Chronos env
        self.skip_after = skip_after  # CI: no shadow log, S3 or MLflow
        self.dir = os.path.join(ec.RUNS_DIR, self.id + ".partial")
        self.info = {"run_id": self.id, "sources": sources, "started_at": datetime.now().isoformat(timespec="seconds"),
                     "previous_run": (ec.current_run() or {}).get("run_id"), "steps": [], "status": "running",
                     "inference": "stub" if stub else "sagemaker" if remote else "local"}

    # -- plumbing --------------------------------------------------------------
    def env(self):
        e = dict(os.environ)
        e[ec.RUN_ENV] = self.dir
        e["PYTHONIOENCODING"] = "utf-8"
        if self.stub:
            e["FORECAST_STUB_INFERENCE"] = "1"
        return e

    def sh(self, step, cmd, python=PY):
        t0 = time.perf_counter()
        print(f"\n[{step}] {' '.join(os.path.basename(c) if i == 0 else c for i, c in enumerate([python] + cmd))}",
              flush=True)
        p = subprocess.run([python] + cmd, cwd=_REPO, env=self.env(), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        out = (p.stdout or "") + (p.stderr or "")
        tail = [l for l in out.splitlines() if l.strip() and "Warning" not in l][-12:]
        for l in tail:
            print("    " + l)
        rec = {"step": step, "seconds": round(time.perf_counter() - t0, 1), "exit": p.returncode}
        self.info["steps"].append(rec)
        if p.returncode != 0:
            raise StepFailed(step, "\n".join(tail[-6:]) or f"exit {p.returncode}")
        return out

    # -- steps -----------------------------------------------------------------
    def snapshot(self):
        """Start from the run being served: every source's series and the Lab tree."""
        os.makedirs(self.dir)
        for sid in ec.sources():
            for code in ec.estates(sid):
                src = ec.series_dir(sid, code)            # resolved against the served run
                if os.path.isdir(src):
                    shutil.copytree(src, os.path.join(self.dir, "series", sid, code))
        if os.path.isdir(ec.lab_dir()):
            shutil.copytree(ec.lab_dir(), os.path.join(self.dir, "lab"),
                            ignore=shutil.ignore_patterns("*.tmp"))
        else:                                             # a fresh install (or CI): nothing built yet
            os.makedirs(os.path.join(self.dir, "lab"))
        self.info["steps"].append({"step": "snapshot", "from": self.info["previous_run"] or "legacy paths",
                                   "seconds": 0.0, "exit": 0})

    def extract(self, sid):
        self.sh(f"extract {sid}", [os.path.join(_DIR, "build_series.py"), "--source", sid,
                                   "--out", os.path.join(self.dir, "series", sid)])

    def ensemble(self):
        """Live only: the legacy ensemble's payloads (K3's fallback, the Lab's incumbent,
        the Investigator), fitted here so no request ever fits it."""
        self.sh("ensemble live", [os.path.abspath(__file__), "--precompute-ensemble"])

    def inference(self, sid):
        cmd = [os.path.join(_DIR, "lab", "build_artifacts.py"), "--source", sid]
        if self.remote:
            cmd.append("--remote")
        self.sh(f"inference {sid}", cmd, python=PY if self.stub else _chronos_python())

    def select(self, sid):
        self.sh(f"select {sid}", [os.path.join(_DIR, "lab", "select_source.py"), "--source", sid])

    def health(self):
        out = self.sh("health", [os.path.abspath(__file__), "--check"])
        report = json.loads(out[out.index("@@HEALTH@@") + 10:].strip().splitlines()[0])
        with open(os.path.join(self.dir, "health.json"), "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1)
        alerts = [m for m in report.get("monitoring", []) if m["level"] == "alert"]
        self.info["health"] = {k: v for k, v in report.items() if k != "monitoring"}
        self.info["health"]["alerts"] = len(alerts)
        for m in alerts:
            print(f"    ALERT {m['source']}/{m['estate']} {m['check']}: {m['detail']}")
        if not report["ok"]:
            raise StepFailed("health", "; ".join(report["problems"]))
        self.watchdog()

    def watchdog(self):
        """The data-quality watchdog's report on this run's series (forecast/watchdog.py),
        kept as data_quality.json beside health.json. Advisory: a failure is recorded in
        run.json and does not stop the run."""
        out = os.path.join(self.dir, "data_quality.json")
        try:
            self.sh("watchdog", [os.path.join(_DIR, "watchdog.py"), "--out", out])
            with open(out, encoding="utf-8") as fh:
                self.info["data_quality"] = json.load(fh)["summary"]["totals"]
        except (StepFailed, OSError, ValueError, KeyError) as exc:
            self.info["data_quality"] = {"error": str(exc)}
            print(f"    watchdog failed (run continues): {exc}")

    def summarize(self):
        """Watermarks, row counts, gates, rule, served models and checkpoint into run.json."""
        est = {}
        for sid in ec.sources():
            for code in ec.estates(sid):
                p = os.path.join(self.dir, "series", sid, code, "meta.json")
                if os.path.exists(p):
                    with open(p, encoding="utf-8") as fh:
                        m = json.load(fh)
                    est[f"{sid}/{code}"] = {"refreshed": sid in self.sources, "last_day": m.get("last_day"),
                                           "full_months": m.get("full_months"), "bunches": m.get("bunches"),
                                           "harvest_days": m.get("harvest_days"), "gate_ok": m["gate"]["ok"],
                                           "extract_seconds": m.get("extract_seconds")}
        self.info["estates"] = est
        man_p = os.path.join(self.dir, "lab", "manifest.json")
        if os.path.exists(man_p):
            with open(man_p, encoding="utf-8") as fh:
                man = json.load(fh)
            self.info["checkpoint"] = man.get("checkpoint")
            self.info["checkpoint_revision"] = man.get("checkpoint_revision")
            self.info["model_run"] = man.get("last_run")
        sel = {}
        for sid in ec.sources():
            p = os.path.join(self.dir, "lab", f"{sid}_selection.json")
            if os.path.exists(p):
                with open(p, encoding="utf-8") as fh:
                    s = json.load(fh)
                sel[sid] = {"rule": s.get("rule"), "served": s.get("served") or s.get("selected"),
                            "flags": s.get("flags", []), "challengers": s.get("challengers", {})}
        self.info["selection"] = sel
        # manual picks from Model health outlive runs; record the ones this run serves under
        sys.path.insert(0, _REPO)
        from forecast import overrides
        self.info["overrides"] = overrides.read()

    def publish(self):
        final = os.path.join(ec.RUNS_DIR, self.id)
        self.info["finished_at"] = datetime.now().isoformat(timespec="seconds")
        self.info["status"] = "published"
        self._write_info(self.dir)
        os.replace(self.dir, final)
        self.dir = final
        tmp = ec.CURRENT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"run_id": self.id, "published_at": self.info["finished_at"],
                       "sources": self.sources}, fh, indent=1)
        os.replace(tmp, ec.CURRENT_PATH)                     # the one atomic switch
        print(f"\n[publish] serving run {self.id}")

    def after(self):
        """Best-effort steps on the published run; failures are recorded, not raised."""
        env = dict(os.environ)
        env.pop(ec.RUN_ENV, None)
        notes = {}
        for name, cmd in [("shadow record", [os.path.join(_DIR, "lab", "shadow.py"), "record"]),
                          ("shadow backup", [os.path.join(_DIR, "lab", "shadow.py"), "backup"])]:
            p = subprocess.run([PY] + cmd, cwd=_REPO, env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            lines = [l for l in ((p.stdout or "") + (p.stderr or "")).splitlines() if l.strip()]
            key = [l for l in lines if any(k in l for k in ("rows appended", "nothing new", "copied", "failed"))]
            last = (key or lines)[-1:] or [""]
            notes[name] = {"ok": p.returncode == 0, "detail": last[0][:300]}
            print(f"[{name}] {'ok' if p.returncode == 0 else 'FAILED'}: {last[0][:200]}")
        # mlflow.db is on the schema of the Chronos env's MLflow (3.16, which logged the R&D
        # runs); the dashboard env's older MLflow cannot open it, so log from that env.
        self._write_info(self.dir)
        p = subprocess.run([_chronos_python(), os.path.abspath(__file__), "--log-mlflow", self.dir],
                           cwd=_REPO, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode == 0 and "@@MLFLOW@@" in out:
            notes["mlflow"] = {"ok": True, **json.loads(out[out.index("@@MLFLOW@@") + 10:].strip().splitlines()[0])}
            print(f"[mlflow] run {notes['mlflow']['mlflow_run_id']}, versions {notes['mlflow']['versions']}")
        else:
            last = [l for l in out.splitlines() if l.strip()][-1:] or [f"exit {p.returncode}"]
            notes["mlflow"] = {"ok": False, "detail": last[0][:300]}
            print(f"[mlflow] FAILED: {notes['mlflow']['detail']}")
        self.info["after"] = notes
        self._write_info(self.dir)

    def _write_info(self, d):
        with open(os.path.join(d, "run.json"), "w", encoding="utf-8") as fh:
            json.dump(self.info, fh, indent=1)

    def fail(self, exc):
        self.info["status"] = "failed"
        self.info["failed_step"] = getattr(exc, "step", "?")
        self.info["error"] = str(exc)[:2000]
        self.info["finished_at"] = datetime.now().isoformat(timespec="seconds")
        if os.path.isdir(self.dir):
            self._write_info(self.dir)
            failed = os.path.join(ec.RUNS_DIR, self.id + ".failed")
            os.replace(self.dir, failed)
            self.dir = failed


class _Logged:
    """A published run re-read from its run.json, for log_mlflow in another env."""
    def __init__(self, run_dir):
        with open(os.path.join(run_dir, "run.json"), encoding="utf-8") as fh:
            self.info = json.load(fh)
        self.id, self.sources, self.dir = self.info["run_id"], self.info["sources"], run_dir


def log_mlflow(run):
    """One MLflow run per refresh (experiment ffb-forecast-refresh) and one registered
    model version per source refreshed (forecast-<source>). The page reads run.json,
    never MLflow, so this is the audit trail, not the serving path."""
    import mlflow
    from mlflow.tracking import MlflowClient
    mlflow.set_tracking_uri("sqlite:///" + os.path.join(_REPO, "mlflow.db").replace(os.sep, "/"))
    name = "ffb-forecast-refresh"
    exp = mlflow.get_experiment_by_name(name)
    exp_id = exp.experiment_id if exp else mlflow.create_experiment(
        name, artifact_location="file:///" + os.path.join(_REPO, "mlartifacts").replace(os.sep, "/"))
    versions = {}
    with mlflow.start_run(experiment_id=exp_id, run_name=run.id) as r:
        mlflow.set_tags({"refresh_run": run.id, "sources": ",".join(run.sources),
                         "checkpoint_revision": run.info.get("checkpoint_revision") or "",
                         "inference": run.info["inference"]})
        params = {"checkpoint": run.info.get("checkpoint"), "checkpoint_revision": run.info.get("checkpoint_revision")}
        for sid, s in run.info.get("selection", {}).items():
            params[f"{sid}_rule"] = s["rule"]
            params[f"{sid}_served"] = s["served"]
        for key, o in (run.info.get("overrides") or {}).items():
            params[f"override_{key.replace('/', '_')}"] = o.get("model")
        for key, e in run.info.get("estates", {}).items():
            params[f"watermark_{key.replace('/', '_')}"] = e["last_day"]
        mlflow.log_params({k: v for k, v in params.items() if v is not None})
        metrics = {}
        for est, h in (run.info.get("health") or {}).get("served", {}).items():
            for k in ("smape_h1_3", "mase_h1_3"):
                if h.get(k) is not None:
                    metrics[f"{est.replace('/', '_')}_{k}"] = float(h[k])
        if metrics:
            mlflow.log_metrics(metrics)
        mlflow.log_artifact(os.path.join(run.dir, "run.json"))
        for sid in run.sources:
            p = os.path.join(run.dir, "lab", f"{sid}_selection.json")
            if os.path.exists(p):
                mlflow.log_artifact(p)
        client = MlflowClient()
        for sid in run.sources:
            model = f"forecast-{sid}"
            try:
                client.get_registered_model(model)
            except Exception:
                client.create_registered_model(model, description=(
                    f"Chronos-2 zero-shot forecast served on the {sid} source; one version per "
                    "refresh (forecast/refresh.py). Nothing is trained: a version is the checkpoint "
                    "revision, the served arm and the data watermark."))
            sel = run.info.get("selection", {}).get(sid, {})
            mv = client.create_model_version(
                model, source=f"{r.info.artifact_uri}", run_id=r.info.run_id,
                tags={"refresh_run": run.id, "rule": sel.get("rule") or "", "served": sel.get("served") or "",
                      "checkpoint_revision": run.info.get("checkpoint_revision") or ""})
            versions[sid] = mv.version
        return {"mlflow_run_id": r.info.run_id, "versions": versions}


# ── health (runs inside the unfinished run, through FORECAST_RUN_DIR) ────────

def check():
    """Every forecastable estate of every source is served by a selected model whose
    forecast starts after the estate's last complete month."""
    sys.path.insert(0, _REPO)
    import forecast_sources as fs
    reg = fs._lab()[0]
    problems, served, monitoring = [], {}, []
    for sid in ec.sources():
        try:
            sel = fs._selection(sid)[2]
        except fs.Unavailable as exc:
            problems.append(f"{sid}: {exc}")
            continue
        for pid, rid in fs.SOURCE_ESTATES[sid].items():
            ok, why, _n = reg.estate_grain_status(rid, "month")
            if not ok:
                served[f"{sid}/{pid}"] = {"served": False, "reason": why}
                continue
            try:
                d = fs.payload(pid, 3, source=sid)
            except fs.Unavailable as exc:
                problems.append(f"{sid}/{pid}: {exc}")
                continue
            if d.get("stale"):
                problems.append(f"{sid}/{pid}: {d.get('stale_note')}")
            pick = (d.get("serving") or {}).get("estates") or [{}]
            for err in (d.get("serving") or {}).get("errors") or []:   # page falls back; flag it
                monitoring.append({"source": sid, "estate": pid, "check": "manual_pick",
                                   "level": "alert", "detail": err})
            served[f"{sid}/{pid}"] = {"served": True, "model": d["model_id"], "anchor": d["anchor"]["month"],
                                      "chosen_by": pick[0].get("mode"),
                                      "smape_h1_3": d.get("headline_smape"), "mase_h1_3": d.get("headline_mase"),
                                      "flags": [f["kind"] for f in d.get("flags", [])]}
            monitoring += _monitor_estate(sid, pid, rid, d, reg)
    print("@@HEALTH@@" + json.dumps({"ok": not problems, "problems": problems, "served": served,
                                     "monitoring": monitoring}))


def _pool(sid, code, series_dir):
    """monitor.pool_months of the estate's seasonal pool (same source and region) read
    from series_dir(code) -- this run's folders or the previous run's -- keyed by page
    id (lower-case code), as the monitor rows are. {} when absent."""
    import pandas as pd
    import monitor
    if series_dir is None:
        return {}
    cfg = ec.estates(sid)
    region = cfg[code].get("region")
    frames = {}
    for c, e in cfg.items():
        if e.get("region") != region:
            continue
        p = os.path.join(series_dir(c), "monthly.csv")
        if os.path.exists(p):
            frames[c.lower()] = pd.read_csv(p, dtype={"month": str})
    return monitor.pool_months(frames)


def _monitor_estate(sid, pid, rid, payload, reg):
    """Phase 7.2 checks for one served estate (forecast/monitor.py), run inside the new run."""
    import importlib.util
    import pandas as pd
    import monitor
    code = pid.upper()
    d = ec.series_dir(sid, code)
    rows = []
    meta = {}
    if os.path.exists(os.path.join(d, "meta.json")):
        with open(os.path.join(d, "meta.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
    rows.append(monitor.freshness(sid, pid, meta.get("last_day")))
    monthly = (pd.read_csv(os.path.join(d, "monthly.csv"), dtype={"month": str})
               if os.path.exists(os.path.join(d, "monthly.csv")) else None)
    rows += monitor.volume(sid, pid, monthly)
    before = {}
    prev = ec.current_run()
    if prev:
        p = os.path.join(prev["dir"], "series", sid, code, "meta.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                before = json.load(fh).get("columns") or {}
    rows += monitor.column_drift(sid, pid, meta.get("columns") or {}, before)
    rows += monitor.seasonal_shape(sid, pid, _pool(sid, code, lambda c: ec.series_dir(sid, c)),
                                   _pool(sid, code, (lambda c: os.path.join(prev["dir"], "series", sid, c))
                                         if prev else None))
    spec = importlib.util.spec_from_file_location("epms_lab_shadow_m", os.path.join(_DIR, "lab", "shadow.py"))
    shadow = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shadow)
    resolved = shadow.resolved(rid)
    bands = {int(k): v["band_pct"] for k, v in (payload.get("interval") or {}).get("per_step", {}).items()}
    step1 = next((h["smape"] for h in (payload.get("accuracy_multih") or {}).get("per_horizon", [])
                  if h["step"] == 1), None)
    rows += monitor.realised_error(sid, pid, resolved, payload["model_id"], bands, step1)
    rows += monitor.chronos_vs_naive(sid, pid, resolved, payload["model_id"])
    return rows


# ── runs housekeeping ───────────────────────────────────────────────────────

def list_runs():
    if not os.path.isdir(ec.RUNS_DIR):
        return []
    out = []
    for name in sorted(os.listdir(ec.RUNS_DIR), reverse=True):
        p = os.path.join(ec.RUNS_DIR, name, "run.json")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as fh:
                info = json.load(fh)
            out.append((name, info))
        elif name.endswith(".killed") and os.path.isdir(os.path.join(ec.RUNS_DIR, name)):
            out.append((name, {"status": "killed"}))
    return out


def prune(keep=KEEP_RUNS):
    cur = (ec.current_run() or {}).get("run_id")
    runs = [n for n, _ in list_runs()]
    published = [n for n in runs if not n.endswith((".failed", ".killed"))]
    failed = [n for n in runs if n.endswith((".failed", ".killed"))]
    drop = [n for n in published[keep:] if n != cur] + failed[keep:]
    for n in drop:
        shutil.rmtree(os.path.join(ec.RUNS_DIR, n), ignore_errors=True)
    return drop


def rollback(run_id):
    p = os.path.join(ec.RUNS_DIR, run_id, "run.json")
    if not os.path.isfile(p) or run_id.endswith(".failed"):
        raise SystemExit(f"{run_id} is not a published run (see --list)")
    tmp = ec.CURRENT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"run_id": run_id, "published_at": datetime.now().isoformat(timespec="seconds"),
                   "rollback": True}, fh, indent=1)
    os.replace(tmp, ec.CURRENT_PATH)
    print(f"serving run {run_id} again")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", choices=["live", "staging", "all"])
    ap.add_argument("--remote", action="store_true", help="Chronos inference as a SageMaker job")
    ap.add_argument("--stub-inference", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--skip-after", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--keep", type=int, default=KEEP_RUNS, help="published runs to keep")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--rollback", metavar="RUN_ID")
    ap.add_argument("--check", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--log-mlflow", metavar="RUN_DIR", help=argparse.SUPPRESS)
    ap.add_argument("--precompute-ensemble", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args.check:
        check()
        return 0
    if args.precompute_ensemble:
        sys.path.insert(0, _REPO)
        import forecast_router
        files = forecast_router.precompute_ensemble(__import__("pathlib").Path(ec.lab_dir()) / "ensemble")
        print("wrote " + ", ".join(files))
        return 0
    if args.log_mlflow:
        print("@@MLFLOW@@" + json.dumps(log_mlflow(_Logged(args.log_mlflow))))
        return 0
    if args.list:
        cur = (ec.current_run() or {}).get("run_id")
        for name, info in list_runs():
            mark = "*" if name == cur else " "
            print(f"{mark} {name:40s} {info.get('status'):10s} {info.get('finished_at', '')}  "
                  f"{info.get('failed_step', '')}")
        return 0
    if args.rollback:
        rollback(args.rollback)
        return 0
    if not args.source:
        ap.error("--source is required")
    sources = ec.sources() if args.source == "all" else [args.source]
    os.makedirs(ec.RUNS_DIR, exist_ok=True)
    killed = acquire_lock()
    try:
        return _refresh(sources, args, killed)
    finally:
        release_lock()


def _refresh(sources, args, killed):
    run = Run(sources, remote=args.remote, stub=args.stub_inference, skip_after=args.skip_after)
    if killed:
        run.info["took_over_from_killed"] = killed
        print(f"[lock] an earlier refresh was killed; kept its folder as {', '.join(k.replace('.partial', '.killed') for k in killed)}")
    print(f"refresh {run.id}: {', '.join(sources)} (serving {run.info['previous_run'] or 'legacy paths'} until done)")
    try:
        run.snapshot()
        for sid in sources:
            run.extract(sid)
        for sid in sources:
            run.inference(sid)
            run.select(sid)
        if "live" in sources:
            run.ensemble()
        run.health()
        run.summarize()
        run.publish()
    except (StepFailed, Exception) as exc:
        run.fail(exc)
        print(f"\nFAILED at {getattr(exc, 'step', type(exc).__name__)} -- still serving "
              f"{run.info['previous_run'] or 'the legacy paths'}. Kept {run.dir} for inspection.")
        return 1
    if not run.skip_after:
        run.after()
    dropped = prune(args.keep)
    if dropped:
        print(f"[prune] removed {', '.join(dropped)}")
    print(f"\ndone: {run.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
