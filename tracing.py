"""
Observability for the LLM layers (Stage-3 R&D): OpenTelemetry -> Arize Phoenix.

Self-hosted, no docker: `python -m phoenix.server.main serve` starts the
collector + trace UI at http://127.0.0.1:6006. All Groq chat calls are
auto-instrumented (OpenInference -> LLM spans with prompts/completions/token
counts); the investigator adds CHAIN/TOOL spans around the ReAct loop so a
whole investigation reads as one nested trace.

Fail-open design: if the phoenix packages are missing OR no collector responds
at PHOENIX_COLLECTOR_ENDPOINT (default http://127.0.0.1:6006), setup returns
None and `span()` degrades to a no-op — the app never logs export retries for
a collector that is not there. Probe result is cached for the process.
"""

import logging
import os
from contextlib import contextmanager

log = logging.getLogger("epms-tracing")

_TRACER = None
_INITIALIZED = False


def _collector_up(endpoint: str) -> bool:
    import httpx
    try:
        r = httpx.get(endpoint, timeout=1.5, follow_redirects=True)
        return r.status_code < 500
    except Exception:
        return False


def setup_tracing(project_name: str = "forecast-investigator"):
    """Idempotent. Returns an OTel tracer, or None when tracing is unavailable."""
    global _TRACER, _INITIALIZED
    if _INITIALIZED:
        return _TRACER
    _INITIALIZED = True

    endpoint = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://127.0.0.1:6006")
    if not _collector_up(endpoint):
        log.info("[tracing] no Phoenix collector at %s — tracing disabled "
                 "(run `python -m phoenix.server.main serve` and restart)", endpoint)
        return None
    try:
        from phoenix.otel import register
        tracer_provider = register(
            project_name=project_name,
            endpoint=endpoint.rstrip("/") + "/v1/traces",
            batch=True,
            set_global_tracer_provider=False,
            verbose=False,
        )
        try:  # LLM spans for every Groq chat call, tokens included
            from openinference.instrumentation.groq import GroqInstrumentor
            GroqInstrumentor().instrument(tracer_provider=tracer_provider)
        except Exception as exc:
            log.warning("[tracing] Groq auto-instrumentation unavailable: %s", exc)
        _TRACER = tracer_provider.get_tracer("epms-investigator")
        log.info("[tracing] Phoenix tracing active -> %s (project %s)",
                 endpoint, project_name)
    except Exception as exc:
        log.warning("[tracing] setup failed, tracing disabled: %s", exc)
    return _TRACER


def _clean(attributes: dict | None) -> dict:
    """OTel attribute values must be str/bool/int/float."""
    import json
    out = {}
    for k, v in (attributes or {}).items():
        if v is None:
            continue
        out[k] = v if isinstance(v, (str, bool, int, float)) \
            else json.dumps(v, default=str)[:2000]
    return out


@contextmanager
def span(name: str, kind: str = "CHAIN", attributes: dict | None = None):
    """OpenInference-flavoured span; yields the span (or None when disabled).

    kind: CHAIN | TOOL | LLM | AGENT — drives how Phoenix renders the row.
    """
    tracer = setup_tracing()
    if tracer is None:
        yield None
        return
    attrs = {"openinference.span.kind": kind, **_clean(attributes)}
    with tracer.start_as_current_span(name, attributes=attrs) as s:
        try:
            yield s
        except Exception as exc:
            try:
                from opentelemetry.trace import Status, StatusCode
                s.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
            except Exception:
                pass
            raise


def set_attrs(s, attributes: dict):
    """Attach attributes to a live span; silently no-ops when disabled."""
    if s is not None:
        try:
            s.set_attributes(_clean(attributes))
        except Exception:
            pass
