"""The shared kit for the four forecasting models: fit, score, and say it plainly.

Four models feed tomorrow's plan (buildplan_ml.md): rain, headcount, work
done (slippage) and crew rates. They share three things, kept here so each
one is held to the same standard.

Fitting
-------
numpy only. Ridge least squares and a ridge logistic regression by iteratively
reweighted least squares. Nothing a supervisor could not have written on a
whiteboard, which is the stance productivity.py already takes.

Scoring
-------
Every model is judged against the guess it replaces, on days it had not seen,
with only what was known the evening before. `grade()` turns that comparison
into one of four words, so the screen can say "Reliable" rather than
"skill 0.21".

Saying it plainly
-----------------
The app is used by managers and mandors, not statisticians. Every figure a
model returns travels with a sentence a non-specialist can act on: "a 38%
chance of rain heavy enough to wash off spraying", "expect 19 to 23 men".
The wording helpers below are the one place those phrasings are decided.

The answer key
--------------
The generated crews file carries each crew's true speed in a `skill` column,
because the generator needed it. A model that read it would score perfectly
and prove nothing. `ANSWER_KEYS` names it; `strip()` removes it from any row
before a model sees it; the recovery tests are the only readers.
"""

import math
from datetime import date, timedelta

import numpy as np

# Columns in the generated feeds that ARE the answer. Never a feature.
# `skill` is each crew's hidden speed (ec_crews.csv); `delay_cause` is why a
# purchase order ran late (ec_mm_purchase_orders.csv).
ANSWER_KEYS = frozenset({"skill", "delay_cause"})


def strip(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in ANSWER_KEYS}


def assert_clean(names) -> None:
    bad = ANSWER_KEYS & set(names)
    if bad:
        raise AssertionError(f"model features include the answer key {sorted(bad)}")


# ── fitting ────────────────────────────────────────────────────────────────

def ridge(X: np.ndarray, y: np.ndarray, w: np.ndarray | None = None,
          lam: float | np.ndarray = 1e-3) -> np.ndarray:
    """Weighted ridge least squares. `lam` may be a per-column vector, so an
    intercept goes unpenalised while crew dummies are pulled toward zero."""
    n, p = X.shape
    w = np.ones(n) if w is None else w
    L = np.diag(np.broadcast_to(np.asarray(lam, dtype=float), (p,)))
    Xw = X * w[:, None]
    return np.linalg.solve(X.T @ Xw + L, Xw.T @ y)


def sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def logistic(X: np.ndarray, y: np.ndarray, w: np.ndarray | None = None,
             lam: float | np.ndarray = 1e-3, iters: int = 30) -> np.ndarray:
    """Ridge logistic regression by IRLS.

    `y` is a proportion in [0, 1] and `w` the number of trials behind it, so
    the same routine fits "did the order get worked" (w = 1) and "how many of
    the roll turned up" (y = present / roll, w = roll).
    """
    n, p = X.shape
    w = np.ones(n) if w is None else w
    L = np.diag(np.broadcast_to(np.asarray(lam, dtype=float), (p,)))
    beta = np.zeros(p)
    for _ in range(iters):
        mu = sigmoid(X @ beta)
        s = np.maximum(mu * (1 - mu), 1e-6) * w
        z = X @ beta + (y - mu) / np.maximum(mu * (1 - mu), 1e-6)
        new = np.linalg.solve(X.T @ (X * s[:, None]) + L, X.T @ (s * z))
        if np.max(np.abs(new - beta)) < 1e-7:
            beta = new
            break
        beta = new
    return beta


# ── scoring ────────────────────────────────────────────────────────────────

def weekly_origins(first: date, last: date, weekday: int = 0) -> list[date]:
    """Mondays from `first` to `last`: each is the start of one held-out week."""
    d = first + timedelta(days=(weekday - first.weekday()) % 7)
    out = []
    while d <= last:
        out.append(d)
        d += timedelta(days=7)
    return out


def mae(a, b) -> float | None:
    a, b = np.asarray(a, float), np.asarray(b, float)
    return float(np.mean(np.abs(a - b))) if len(a) else None


def brier(p, y) -> float | None:
    p, y = np.asarray(p, float), np.asarray(y, float)
    return float(np.mean((p - y) ** 2)) if len(p) else None


def improvement_pct(model_err: float | None, baseline_err: float | None) -> float | None:
    """How much smaller the model's error is than the old method's, in percent."""
    if model_err is None or not baseline_err:
        return None
    return round(100.0 * (baseline_err - model_err) / baseline_err, 1)


def grade(improvement: float | None) -> dict:
    """One word for how far to trust a model, from its held-out improvement.

    The bar the plan set: a model that does not beat the method it replaces is
    not used, whatever else it does well.
    """
    if improvement is None:
        return {"label": "Not yet measured", "tone": "low", "passes": False,
                "meaning": "There is not enough history to check this forecast yet."}
    if improvement <= 0:
        return {"label": "Not better than the old way", "tone": "low", "passes": False,
                "meaning": ("On past days this forecast did no better than the simple "
                            "method it would replace, so the plan keeps the simple method.")}
    if improvement < 10:
        return {"label": "Rough guide", "tone": "mid", "passes": True,
                "meaning": ("Slightly better than the old method on past days. Use it as a "
                            "guide and keep your own judgement close.")}
    if improvement < 25:
        return {"label": "Fairly reliable", "tone": "ok", "passes": True,
                "meaning": ("Clearly better than the old method on past days, though it "
                            "still misses on some.")}
    return {"label": "Reliable", "tone": "ok", "passes": True,
            "meaning": "Much better than the old method on past days."}


# ── saying it plainly ──────────────────────────────────────────────────────

def pct(p: float | None, digits: int = 0) -> str:
    if p is None:
        return "unknown"
    v = round(100 * p, digits)
    return f"{v:.{digits}f}%"


def chance_words(p: float | None) -> str:
    """The words a forecaster would use for a probability."""
    if p is None:
        return "unknown"
    if p < 0.10:
        return "very unlikely"
    if p < 0.30:
        return "unlikely"
    if p < 0.55:
        return "possible"
    if p < 0.80:
        return "likely"
    return "very likely"


def men(n: float | int) -> str:
    n = int(round(n))
    return f"{n} {'person' if n == 1 else 'people'}"


def signed_pct(frac: float, digits: int = 0) -> str:
    """+7% / -12% from a multiplier-minus-one."""
    v = round(100 * frac, digits)
    return f"{'+' if v > 0 else ''}{v:.{digits}f}%"


def faster_words(factor: float) -> str:
    d = factor - 1.0
    if abs(d) < 0.03:
        return "about the same pace as the average crew"
    return f"about {abs(round(100 * d))}% {'faster' if d > 0 else 'slower'} than the average crew"


def safe(v, digits=3):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return None
    return round(float(v), digits)
