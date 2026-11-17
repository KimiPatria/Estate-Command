"""The chat reads the forecast (buildplan_chat_centre.md Part B): routing, scope,
the facts block, the figure check and the chart. No model, no files: the payload
is a fake with the forecast page's shape."""
from types import SimpleNamespace

import pytest

import chat_forecast as cf
import chat_intent
import llm_client


# ── fixtures ───────────────────────────────────────────────────────────────

def _add(ym, n):
    return cf._add_months(ym, n)


def payload(months=3, anchor="2026-06", label="EC", **over):
    hist = [{"month": _add(anchor, -i), "value": 1_000_000 + i * 1_000} for i in range(17, -1, -1)]
    fc = [{"month": _add(anchor, s), "step": s, "ensemble": 1_100_000 + s,
           "lower": 1_000_000 + s, "upper": 1_200_000 + s,
           "lower80": 950_000 + s, "upper80": 1_250_000 + s} for s in range(1, months + 1)]
    p = {"forecast": fc, "history": hist, "anchor": {"month": anchor, "value": hist[-1]["value"]},
         "model_label": "Chronos-2 · monthly (zero-shot)", "serving": {"mode": "rule"},
         "data_through": "2026-07-22", "series_label": label,
         "accuracy_multih": {"per_horizon": [{"step": 1, "smape": 13.8}], "max_step": 3},
         "headline_smape": 17.6, "track_record": {"n": 12, "within": 7, "tol_pct": 10},
         "interval": {"outer": {"coverage": 0.81, "n": 21}}}
    p.update(over)
    return p


def turn(route="forecast", meta=None, question="forecast for EC"):
    return {"pos": 0, "question": question, "answer": "...", "route": route, "sql": None,
            "columns": [], "rows": [], "row_count": 0, "stats": {}, "meta": meta or {}}


FORECAST_TURN = turn(meta={"forecast": {"source": "live", "estates": ["ec"], "every": False, "months": 3}})
DB_LIVE = SimpleNamespace(id="live", label="Live estates")
DB_STG = SimpleNamespace(id="staging", label="EPMS staging")


@pytest.fixture
def no_model(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("no model call expected")
    monkeypatch.setattr(llm_client, "chat_json", fail)
    monkeypatch.setattr(llm_client, "chat", fail)


@pytest.fixture
def fake_read(monkeypatch):
    """_read answers from a dict of (source, estate) -> payload | exception."""
    def install(table):
        def read(estate, horizon, source):
            v = table.get((source, estate))
            if v is None:
                raise RuntimeError(f"{estate.upper()}: no forecast model")
            if isinstance(v, Exception):
                raise v
            return v
        monkeypatch.setattr(cf, "_read", read)
    return install


# ── routing ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "What is the production outlook for EC next quarter?",
    "forecast for K3",
    "Perkiraan produksi 3 bulan ke depan untuk Estate C",
    "how much harvest is expected next month?",
    "proyeksi panen bulan depan",
    "forecast vs actual last month",
    "will production go up in the coming months?",
])
def test_a_forecast_question_is_recognised_without_a_model(message, no_model):
    assert cf.wants_forecast(message)
    assert chat_intent.classify(message, []) == (chat_intent.FORECAST, False)


@pytest.mark.parametrize("message", [
    "Total harvest in the last 30 days",
    "what does ripeness rate mean?",
    "how many employees next to the mill?",
    "harvest next week",                 # a week is not a forecast month
])
def test_other_questions_are_not_forecast(message):
    assert not cf.wants_forecast(message)


def test_a_how_does_it_work_question_goes_to_explain_with_the_forecast_notes(no_model, monkeypatch):
    import data_sources
    msg = "how does the forecast work?"
    assert chat_intent.classify(msg, []) == (chat_intent.EXPLAIN, False)
    src = SimpleNamespace(label="Live", description="d", topics="t", measures=[], entity_labels=lambda: ["BA"],
                          data_through=lambda: None)
    monkeypatch.setattr(data_sources, "active", lambda: src)
    monkeypatch.setattr(chat_intent, "explain_system", lambda s: "SYSTEM")
    text = chat_intent.explain_messages(msg, [])[1]["content"]
    assert "ABOUT THE FORECAST" in text and "Chronos-2" in text


def test_why_lower_after_a_forecast_stays_on_the_forecast(no_model):
    assert chat_intent.classify("explain why the forecast is lower than last year", [FORECAST_TURN]) == (chat_intent.FORECAST, False)


@pytest.mark.parametrize("message", ["and EB?", "how sure is it?", "compare with last year",
                                     "which model is that?", "chart that", "seberapa yakin?"])
def test_a_follow_up_after_a_forecast_stays_on_the_forecast(message, no_model):
    assert cf.follows_up(message, [FORECAST_TURN])
    assert chat_intent.classify(message, [FORECAST_TURN]) == (chat_intent.FORECAST, False)


def test_a_question_about_what_happened_leaves_the_forecast():
    assert not cf.follows_up("what did we produce last month?", [FORECAST_TURN])
    assert not cf.follows_up("and EB?", [turn(route="sql")])
    assert not cf.follows_up("and EB?", [])


# ── scope and months ───────────────────────────────────────────────────────

def test_named_estates_on_the_live_source():
    sc = cf.scope("forecast for EC and K3", [], "live")
    assert sc["estates"] == ["k3", "ec"] and sc["named"] == ["k3", "ec"] and not sc["default"]


def test_estate_c_is_ec():
    assert cf.mentioned_estates("outlook for Estate C") == ["ec"]
    assert cf.mentioned_estates("kebun 3") == ["k3"]


def test_the_default_scope_is_the_page_default():
    assert cf.scope("production outlook", [], "staging")["estates"] == ["all"]
    live = cf.scope("production outlook", [], "live")
    assert live["estates"] == ["k3"] and live["default"]


def test_every_estate_reads_the_roster():
    assert cf.scope("forecast for all estates", [], "staging")["estates"] == ["all", "ea", "eb", "ec"]
    assert cf.scope("compare the estates", [], "live")["estates"] == ["k3", "ec", "eb", "ea", "ba"]
    assert not cf.scope("compare with last year", [], "live")["every"]


def test_an_estate_on_the_other_source_is_reported_not_read():
    sc = cf.scope("forecast for K3", [], "staging")
    assert sc["elsewhere"] == ["k3"] and sc["estates"] == ["all"]


def test_a_follow_up_keeps_the_last_forecast_scope():
    sc = cf.scope("how sure is it?", [FORECAST_TURN], "live")
    assert sc["estates"] == ["ec"] and not sc["default"]
    # another database: the kept scope does not carry
    assert cf.scope("how sure is it?", [FORECAST_TURN], "staging")["estates"] == ["all"]


@pytest.mark.parametrize("message,months,month", [
    ("outlook for the next 6 months", 6, None),
    ("forecast next quarter", 3, None),
    ("perkiraan 12 bulan ke depan", 12, None),
    ("forecast for March", 3, (3, None)),
    ("forecast for May 2027", 3, (5, 2027)),
    ("may I see the forecast?", 3, None),
    ("forecast", 3, None),
])
def test_how_far_ahead_the_question_looks(message, months, month):
    assert cf.requested(message, []) == {"months": months, "month": month}


def test_a_follow_up_keeps_the_months():
    t = turn(meta={"forecast": {"source": "live", "estates": ["ec"], "every": False, "months": 6}})
    assert cf.requested("how sure is it?", [t])["months"] == 6


# ── facts ──────────────────────────────────────────────────────────────────

def test_facts_quote_the_payload(fake_read):
    fake_read({("live", "ec"): payload()})
    entries = cf.read_all(["ec"], 3, "live")
    fx = cf.facts(entries, cf.requested("forecast for EC", []), cf.scope("forecast for EC", [], "live"), "Live estates")
    text = fx["text"]
    assert "ESTATE EC" in text
    assert "2026-07 (step 1): expected 1,100,001; likely range 950,001 to 1,250,001; last year 2025-07: 1,011,000." in text
    assert "Total for the 3 months (2026-07 to 2026-09): 3,300,006 bunches; same months last year: 3,030,000; change vs last year: +8.9%." in text
    assert "one month ahead 13.8%" in text and "7 of 12" in text and "held 81%" in text
    assert "Tonnes and budget: not available yet" in text and "DATA RUNS TO: 2026-07-22" in text
    assert fx["shown"][0]["total"] == 3_300_006


def test_an_unavailable_estate_is_listed_with_its_reason(fake_read):
    fake_read({("live", "ec"): payload(), ("live", "ea"): RuntimeError("EA: only 1 complete month of data (needs 3).")})
    entries = cf.read_all(["ec", "ea", "ba"], 3, "live")
    fx = cf.facts(entries, cf.requested("x", []), cf.scope("forecast for EC, EA and BA", [], "live"), "Live estates")
    assert "UNAVAILABLE" in fx["text"]
    assert "- EA: EA: only 1 complete month of data (needs 3)." in fx["text"]
    assert "- BA: BA: no forecast model" in fx["text"]
    assert [e["id"] for e in fx["missing"]] == ["ea", "ba"]


def test_a_month_beyond_the_forecast_is_a_note(fake_read):
    fake_read({("live", "ec"): payload(months=6)})
    req = cf.requested("forecast for EC in May 2027", [])
    fx = cf.facts(cf.read_all(["ec"], 3, "live"), req, cf.scope("forecast for EC", [], "live"), "Live")
    assert any("2027-05 is beyond the forecast, which reaches 2026-12" in n for n in fx["notes"])


def test_a_month_already_in_the_actuals_is_a_note(fake_read):
    fake_read({("live", "ec"): payload()})
    req = cf.requested("forecast for EC in April 2026", [])
    fx = cf.facts(cf.read_all(["ec"], 3, "live"), req, cf.scope("forecast for EC", [], "live"), "Live")
    assert any("2026-04 is already in the actuals: 1,002,000 bunches" in n for n in fx["notes"])


def test_a_named_month_widens_the_months_shown(fake_read):
    fake_read({("live", "ec"): payload(months=6)})
    req = cf.requested("forecast for EC in November", [])
    fx = cf.facts(cf.read_all(["ec"], 6, "live"), req, cf.scope("forecast for EC", [], "live"), "Live")
    assert fx["shown"][0]["months"] == ["2026-07", "2026-08", "2026-09", "2026-10", "2026-11"]


def test_more_than_six_months_is_capped_with_a_note(fake_read):
    fake_read({("live", "ec"): payload(months=6)})
    req = cf.requested("forecast for EC next 12 months", [])
    fx = cf.facts(cf.read_all(["ec"], 12, "live"), req, cf.scope("forecast for EC", [], "live"), "Live")
    assert len(fx["shown"][0]["months"]) == 6 and any("reaches 6 months ahead" in n for n in fx["notes"])


def test_past_months_say_only_whether_the_forecast_landed(fake_read):
    tr = {"n": 3, "within": 2, "tol_pct": 10, "months": ["2026-04", "2026-05", "2026-06"], "hits": [True, False, True]}
    fake_read({("live", "ec"): payload(track_record=tr)})
    fx = cf.facts(cf.read_all(["ec"], 3, "live"), cf.requested("x", []), cf.scope("forecast for EC", [], "live"), "Live")
    assert ("Past one-month-ahead forecasts, within 10% of that month's actual? 2026-04: yes, 2026-05: no, "
            "2026-06: yes. The forecast figures made for past months are not available here.") in fx["text"]


@pytest.mark.parametrize("message", ["how does the forecast work?", "how is the forecast made?",
                                     "bagaimana cara kerja perkiraan ini?", "what does forecast mean?"])
def test_how_it_works_questions_are_explain(message):
    assert cf.asks_how_it_works(message)


@pytest.mark.parametrize("message", ["how sure is the forecast?", "how does the forecast compare with last year?",
                                     "how accurate has the forecast been?"])
def test_figure_questions_about_the_forecast_are_not_explain(message):
    assert not cf.asks_how_it_works(message)


def test_a_manual_pick_and_a_fallback_are_said(fake_read):
    fake_read({("live", "k3"): payload(label="K3", serving={"mode": "manual"},
                                       fallback={"model": "served_ensemble", "reason": "the Chronos forecast is stale"})})
    fx = cf.facts(cf.read_all(["k3"], 3, "live"), cf.requested("x", []), cf.scope("forecast for K3", [], "live"), "Live")
    assert "a manual pick by the forecasting team" in fx["text"] and "the fallback model, because the Chronos forecast is stale" in fx["text"]


# ── chart ──────────────────────────────────────────────────────────────────

def test_one_estate_is_a_fan_chart(fake_read):
    fake_read({("live", "ec"): payload()})
    fx = cf.facts(cf.read_all(["ec"], 3, "live"), cf.requested("x", []), cf.scope("forecast for EC", [], "live"), "Live")
    c = cf.chart(fx["shown"])
    assert c["type"] == "forecast" and len(c["labels"]) == 12 + 3
    actual, fc = c["series"]
    assert actual["values"][11] == 1_000_000 and actual["values"][12] is None
    assert fc["values"][11] == 1_000_000 and fc["values"][12] == 1_100_001      # the fan starts at the anchor
    assert c["band"]["low"][12] == 950_001 and c["band"]["high"][14] == 1_250_003


def test_several_estates_are_bars_of_their_totals(fake_read):
    fake_read({("staging", "ea"): payload(label="EA"), ("staging", "eb"): payload(label="EB")})
    fx = cf.facts(cf.read_all(["ea", "eb"], 3, "staging"), cf.requested("x", []), cf.scope("EA and EB", [], "staging"), "S")
    c = cf.chart(fx["shown"])
    assert c["type"] == "bar" and c["labels"] == ["EA", "EB"]
    assert c["series"][0]["values"] == [3_300_006, 3_300_006] and c["series"][1]["values"] == [3_030_000, 3_030_000]


# ── answering ──────────────────────────────────────────────────────────────

def _ask(replies):
    calls = []

    def ask(messages, **kw):
        calls.append({"messages": messages, **kw})
        return replies[min(len(calls), len(replies)) - 1], "bedrock", {"model": "Nova 2 Lite"}
    ask.calls = calls
    return ask


def test_a_clean_answer_is_kept_with_its_chart_and_link(fake_read):
    fake_read({("live", "ec"): payload()})
    ask = _ask(["EC should bring in **3,300,006 bunches** over 2026-07 to 2026-09, up +8.9% on last year."])
    out = cf.respond("forecast for EC next 3 months", [], DB_LIVE, ask)
    assert out["answer"].startswith("EC should bring in")
    assert out["chart"]["type"] == "forecast" and out["link"] == "[Open in Forecast](/forecast?db=live&estate=ec)"
    assert out["scope"] == {"source": "live", "estates": ["ec"], "every": False, "months": 3, "shown": ["ec"]}
    assert out["provider_label"] == "Nova 2 Lite" and out["audit"]["clean"]
    assert "FACTS:" in ask.calls[0]["messages"][1]["content"] and ask.calls[0]["task"] == "chat_forecast"
    assert out["suggestions"] == ["How sure is it?", "Compare with last year", "What about the other estates?"]


def test_an_invented_figure_is_sent_back_once_then_the_facts_speak(fake_read):
    fake_read({("live", "ec"): payload()})
    ask = _ask(["EC will make about 3.3 million bunches.", "Still 3,500,000 bunches."])
    quiet = _ask(["EC: **3,300,006 bunches** over the next three months."])
    out = cf.respond("forecast for EC", [], DB_LIVE, ask, ask_quiet=quiet)
    assert out["answer"].startswith("EC: **3,300,006 bunches**")
    assert len(ask.calls) == 1 and len(quiet.calls) == 1
    assert "not in FACTS: 3.3" in quiet.calls[0]["messages"][-1]["content"]


def test_two_bad_drafts_fall_back_to_the_template(fake_read):
    fake_read({("live", "ec"): payload()})
    ask = _ask(["about 3.3 million", "roughly 3,500,000"])
    out = cf.respond("forecast for EC", [], DB_LIVE, ask)
    assert out["answer"].startswith("**EC**: expected **3,300,006 bunches** (2026-07 – 2026-09).")
    assert "- 2026-07: 1,100,001 (likely 950,001 – 1,250,001)" in out["answer"]


def test_nothing_available_needs_no_model(fake_read):
    fake_read({})
    ask = _ask(["should not be called"])
    out = cf.respond("forecast for EA", [], DB_LIVE, ask)
    assert not ask.calls and out["chart"] is None and out["link"] == ""
    assert out["answer"] == "**EA**: no forecast yet (EA: no forecast model)."


def test_chart_that_after_a_forecast_needs_no_model(fake_read):
    fake_read({("live", "ec"): payload()})
    ask = _ask(["should not be called"])
    out = cf.respond("chart that", [FORECAST_TURN], DB_LIVE, ask)
    assert not ask.calls and out["answer"] == "Here is the forecast chart." and out["chart"]["type"] == "forecast"


def test_the_staging_database_reads_the_staging_source(fake_read):
    seen = []
    fake_read({("staging", "all"): payload(label="EA + EB + EC")})
    ask = _ask(["The three estates together: **3,300,006 bunches**."])
    out = cf.respond("perkiraan produksi 3 bulan ke depan", [], DB_STG, ask)
    assert out["scope"]["source"] == "staging" and out["link"].endswith("db=staging&estate=all)")
    assert "REPLY LANGUAGE: Indonesian" in ask.calls[0]["messages"][1]["content"]
    assert out["suggestions"][0] == "Seberapa yakin perkiraan ini?"
