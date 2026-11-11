"""Live progress of one chat request: the steps the page ticks off while it waits.

The pipeline announces what it is doing ("Finding the right tables", "Running
on EPMS staging") through the module-level helpers below. They reach whatever
Recorder is in force for the request (a ContextVar, like data_sources.use and
llm_client.model_override) and do nothing when there is none, so code that has
never heard of streaming (evals, the dashboard) is unaffected.

A Recorder always keeps the finished steps, so they can be stored with the
turn and shown again when the chat is reopened. With a `sink` it also forwards
every event as it happens: that is what /chat/stream sends to the browser.

Stopping: when the browser goes away the endpoint sets `cancel`, and the next
event raises Cancelled, which unwinds the pipeline before it stores anything.
Cancelled derives from BaseException so the pipeline's own `except Exception`
blocks cannot swallow it.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Optional


class Cancelled(BaseException):
    """The client stopped the request."""


class Recorder:
    def __init__(self, sink: Optional[Callable[[str, dict], None]] = None,
                 cancel: Optional[threading.Event] = None):
        self.sink = sink
        self.cancel = cancel
        self.extras: dict = {}          # request-scoped results: chart, query id ...
        self._steps: dict[str, dict] = {}
        self._lock = threading.Lock()

    def send(self, event: str, data: dict) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()
        if self.sink is not None:
            self.sink(event, data)

    def step(self, step_id: str, label: str) -> None:
        with self._lock:
            self._steps[step_id] = {"id": step_id, "label": label, "status": "running",
                                    "_t0": time.monotonic()}
        self.send("step", {"id": step_id, "label": label, "status": "running"})

    def done(self, step_id: str, detail: str | None = None, status: str = "done",
             label: str | None = None, **extra) -> None:
        with self._lock:
            s = self._steps.get(step_id)
            if s is None:
                return
            s["status"] = status
            if "_t0" in s:
                s["ms"] = int((time.monotonic() - s.pop("_t0")) * 1000)
            if label:
                s["label"] = label
            if detail:
                s["detail"] = detail
            s.update({k: v for k, v in extra.items() if v is not None})
            payload = dict(s)
        self.send("step", payload)

    def summary(self) -> list[dict]:
        """Finished steps, in order, for storing with the turn."""
        with self._lock:
            out = []
            for s in self._steps.values():
                item = {k: v for k, v in s.items() if not k.startswith("_")}
                if item["status"] == "running":      # never closed: the request ended there
                    item["status"] = "done"
                out.append(item)
            return out


_recorder: ContextVar[Optional[Recorder]] = ContextVar("chat_recorder", default=None)


def current() -> Optional[Recorder]:
    return _recorder.get()


def streaming() -> bool:
    """True when someone is watching this request live."""
    rec = _recorder.get()
    return rec is not None and rec.sink is not None


@contextmanager
def recording(sink: Optional[Callable[[str, dict], None]] = None,
              cancel: Optional[threading.Event] = None):
    rec = Recorder(sink, cancel)
    token = _recorder.set(rec)
    try:
        yield rec
    finally:
        _recorder.reset(token)


def step(step_id: str, label: str) -> None:
    rec = _recorder.get()
    if rec is not None:
        rec.step(step_id, label)


def done(step_id: str, detail: str | None = None, status: str = "done",
         label: str | None = None, **extra) -> None:
    rec = _recorder.get()
    if rec is not None:
        rec.done(step_id, detail, status, label, **extra)


def send(event: str, **data) -> None:
    rec = _recorder.get()
    if rec is not None:
        rec.send(event, data)


def attach(key: str, value) -> None:
    rec = _recorder.get()
    if rec is not None:
        rec.extras[key] = value
