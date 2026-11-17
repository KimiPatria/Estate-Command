"""Broad questions answered with several measures (chat_overview.py and the
overview route in chat_router). The model and the database are stubbed."""
import concurrent.futures
from types import SimpleNamespace

import pytest

import chat_conversation as cc
import chat_overview as ov
import chat_router
import data_sources

LIVE = data_sources.get("live")
STAGING = data_sources.get("staging")
M = {m.id: m for m in STAGING.measures}
ML = {m.id: m for m in LIVE.measures}


# ── the trigger ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("q", [
    "how is Estate C performing?", "compare the operational efficiency of estate C againts other estates",
    "operational efficiency per estate", "How are the estates doing this year?",
    "bagaimana kinerja kebun EC tahun ini?", "ringkasan kinerja semua kebun bulan ini",
    "give me an overview of estate BA", "How did the estates perform last month?"])
def test_broad_questions_go_to_the_planner(q):
    assert cc.is_overview(q)


@pytest.mark.parametrize("q", [
    "Who is the best harvester?", "How is that block doing?", "Kinerja divisi mana yang paling buruk?",
    "which division performed best in 2025", "harvest efficiency in ripe bunches per hectare",
    "total harvest last month", "performance of the top 5 harvesters", "how are those estates doing?",
    "summarize ripe bunches per estate"])
def test_rankings_pointers_and_named_measures_do_not(q):
    assert not cc.is_overview(q)


def test_a_broad_question_is_not_asked_back(monkeypatch):
    called = []
    monkeypatch.setattr(cc.llm_client, "chat_json", lambda *a, **k: called.append(1))
    assert cc.clarify_gate("how is Estate C performing this year?", []) is None and called == []


# ── the plan ───────────────────────────────────────────────────────────────

@pytest.fixture
def planner(monkeypatch):
    def install(reply):
        calls = []
        monkeypatch.setattr(ov.llm_client, "chat_json", lambda m, **kw: calls.append(kw) or reply)
        monkeypatch.setattr(ov, "_last_full_year", lambda src: 2025)
        return calls
    return install


def test_the_plan_uses_the_reasoning_model_and_a_closed_list(planner):
    calls = planner({"measures": ["ripe_bunches", "absence"], "grain": "estate", "period": "September 2026"})
    p = ov.plan("How did the estates perform in September 2026?", STAGING, single_estate=False)
    assert [m.id for m in p.measures] == ["ripe_bunches", "absence"] and p.grain == "estate"
    assert p.period == " in September 2026"
    schema = calls[0]["schema"]["properties"]["measures"]["items"]
    assert calls[0]["tier"] == "reasoning" and set(schema["enum"]) == set(M)


def test_one_estate_is_broken_down_by_month(planner):
    planner({"measures": ["ripe_bunches", "mandays"], "grain": "estate", "period": ""})
    p = ov.plan("how is Estate C performing?", STAGING, single_estate=True)
    assert p.grain == "month" and p.period == " over the last 12 full months"


def test_a_breakdown_the_measures_cannot_share_falls_back(planner):
    planner({"measures": ["tonnes_delivered", "absence", "ripe_bunches"], "grain": "block", "period": ""})
    p = ov.plan("How are the estates doing?", STAGING, single_estate=False)
    assert p.grain == "estate" and len(p.measures) == 3


def test_no_plan_means_the_plain_pipeline(planner):
    planner(None)
    assert ov.plan("How are the estates doing?", STAGING, single_estate=False) is None


def test_live_defaults_to_the_last_full_year(planner):
    planner({"measures": ["ripe_bunches", "mandays"], "grain": "estate", "period": ""})
    p = ov.plan("How are the estates doing?", LIVE, single_estate=False)
    assert p.period == " in 2025" and p.period_label == "2025"


def test_sub_questions_are_written_by_code():
    p = ov.Plan([M["tonnes_delivered"]], "month", " in 2026", "2026")
    assert ov.sub_question(M["tonnes_delivered"], p, STAGING) == \
        "Tonnes delivered to the mill per month in 2026, counting each trip's weight once"
    # live estates are separate databases: no "per estate" in the question
    pl = ov.Plan([ML["ripe_bunches"]], "estate", " in 2025", "2025")
    assert ov.sub_question(ML["ripe_bunches"], pl, LIVE) == "Total ripe bunches harvested in 2025"


# ── merging ────────────────────────────────────────────────────────────────

def test_staging_parts_join_on_the_estate_whatever_they_call_it():
    parts = [
        (M["ripe_bunches"], ["estate", "ripe_bunches"],
         [{"estate": "ESTATE C", "ripe_bunches": 900}, {"estate": "PLASMA 1", "ripe_bunches": 100}]),
        (M["absence"], ["PRFNR", "mangkir_person_days"],
         [{"PRFNR": "ESTATE PLASMA 1", "mangkir_person_days": 7}, {"PRFNR": "ESTATE C", "mangkir_person_days": 3}]),
        (M["tonnes_delivered"], ["ESTNR", "trips", "tonnes"], [{"ESTNR": "EC", "trips": 40, "tonnes": 55.5}]),
    ]
    cols, rows, used = ov.merge(parts, "estate", STAGING)
    assert cols == ["Estate", "Ripe bunches", "Mangkir person-days", "Tonnes to the mill"]
    assert rows[0] == {"Estate": "Estate C (EC)", "Ripe bunches": 900, "Mangkir person-days": 3,
                       "Tonnes to the mill": 55.5}
    assert rows[1]["Tonnes to the mill"] is None and used == ["ripe_bunches", "absence", "tonnes_delivered"]


def test_live_parts_join_on_the_estate_database():
    parts = [(ML["ripe_bunches"], ["v", "_source_estate"], [{"v": 5, "_source_estate": "BA"}, {"v": 3, "_source_estate": "K3"}]),
             (ML["bunches_per_ha"], ["bunches", "ha", "bunches_per_ha", "_source_estate"],
              [{"bunches": 10, "ha": 2, "bunches_per_ha": 5.0, "_source_estate": "BA"},
               {"bunches": 9, "ha": 3, "bunches_per_ha": 3.0, "_source_estate": "K3"}])]
    cols, rows, _ = ov.merge(parts, "estate", LIVE)
    assert cols == ["Estate", "Ripe bunches", "Bunches per mature ha"]
    assert rows == [{"Estate": "BA", "Ripe bunches": 5, "Bunches per mature ha": 5.0},
                    {"Estate": "K3", "Ripe bunches": 3, "Bunches per mature ha": 3.0}]


def test_months_join_however_the_period_is_written():
    parts = [(M["ripe_bunches"], ["year", "month", "ripe"], [{"year": 2026, "month": 9, "ripe": 1}, {"year": 2026, "month": 10, "ripe": 2}]),
             (M["mandays"], ["ym", "man_days"], [{"ym": "2026-10", "man_days": 8}, {"ym": "2026-09", "man_days": 7}])]
    cols, rows, _ = ov.merge(parts, "month", STAGING)
    assert [r["Month"] for r in rows] == ["2026-09", "2026-10"] and rows[1]["Field man-days"] == 8


def test_a_part_broken_down_finer_is_left_out_not_fatal():
    parts = [(M["ripe_bunches"], ["estate", "ripe"], [{"estate": "EC", "ripe": 1}, {"estate": "EA", "ripe": 2}]),
             (M["mandays"], ["estate", "man_days"], [{"estate": "EC", "man_days": 4}, {"estate": "EA", "man_days": 5}]),
             (M["absence"], ["estate", "month", "n"], [{"estate": "EC", "month": 1, "n": 1}, {"estate": "EC", "month": 2, "n": 1}])]
    _, _, used = ov.merge(parts, "estate", STAGING)
    assert used == ["ripe_bunches", "mandays"]


def test_parts_with_nothing_in_common_are_not_merged():
    parts = [(M["ripe_bunches"], ["estate", "ripe"], [{"estate": "EC", "ripe": 1}]),
             (M["mandays"], ["estate", "man_days"], [{"estate": "EA", "man_days": 5}])]
    assert ov.merge(parts, "estate", STAGING) is None


def test_the_value_is_the_measure_not_its_parts():
    cols = ["estate", "bunches_per_harvester_day", "harvester_days"]
    row = {"estate": "EC", "bunches_per_harvester_day": 60.9, "harvester_days": 3000}
    assert ov._value_column(M["harvester_output"], cols, [row], ["estate"]) == "bunches_per_harvester_day"


# ── the route end to end ───────────────────────────────────────────────────

@pytest.fixture
def overview(monkeypatch):
    plan = ov.Plan([ML["ripe_bunches"], ML["mandays"]], "estate", " in 2025", "2025")
    monkeypatch.setattr(chat_router.chat_overview, "plan", lambda *a, **k: plan)
    monkeypatch.setattr(chat_router, "retrieve_tables", lambda q, k=5: ([SimpleNamespace(name="t_oph")], False))
    monkeypatch.setattr(chat_router, "retrieve_examples", lambda q, k=3: [])
    prompts = []
    monkeypatch.setattr(chat_router, "_answer_llm", lambda m, **kw: prompts.append((m, kw)) or
                        ("BA leads.", "bedrock", {"input_tokens": 0, "output_tokens": 0, "model": "nova-pro"}))
    done = concurrent.futures.Future()
    done.set_result(None)
    monkeypatch.setattr(chat_router, "_start_chart", lambda *a: done)
    saved = []
    monkeypatch.setattr(chat_router, "record_query", lambda *a, **k: saved.append(k))

    def fake_run(sql_q, q, cards, ex, hist, estate_ids, *, step_prefix="", what=""):
        value = {"Total ripe bunches harvested in 2025": {"ba": 5, "k3": 3},
                 "Total mandays of field work done in 2025": {"ba": 40, "k3": 50}}.get(q, {"ba": 1, "k3": 1})
        return chat_router.RunResult(sql=f"SELECT -- {what}", outcome="first_ok", ok_estates=["ba", "k3"],
                                     run_step=f"{step_prefix}run",
                                     exec_results={e: (["v"], [{"v": value[e]}], None) for e in ("ba", "k3")})

    monkeypatch.setattr(chat_router, "generate_and_run", fake_run)
    return SimpleNamespace(prompts=prompts, saved=saved)


def test_a_broad_question_gets_one_merged_table(overview):
    resp, _ = chat_router._run_sql_pipeline("s1", "How are the estates doing?", [], 0.0)
    assert resp.route == "overview" and resp.columns == ["Estate", "Ripe bunches", "Work mandays"]
    assert resp.rows == [{"Estate": "BA", "Ripe bunches": 5, "Work mandays": 40},
                         {"Estate": "K3", "Ripe bunches": 3, "Work mandays": 50}]
    assert [p["measure"] for p in resp.parts] == ["ripe_bunches", "mandays"]
    messages, kw = overview.prompts[0]
    assert kw["tier"] == "reasoning" and "Measures looked at" in messages[1]["content"]
    assert overview.saved == []          # template sub-questions never become examples


def test_a_plain_question_never_reaches_the_planner(overview, monkeypatch):
    monkeypatch.setattr(chat_router.chat_overview, "plan", lambda *a, **k: pytest.fail("planned"))
    resp, _ = chat_router._run_sql_pipeline("s1", "total ripe bunches in 2025", [], 0.0)
    assert resp.route == "sql"
