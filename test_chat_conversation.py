"""Asking back, suggesting next and keeping estate scope on the /chat page.
The model and the pipeline are stubbed; no Bedrock or database calls."""
import json
from types import SimpleNamespace

import pytest

import chat_conversation as cc
import chat_router
import llm_client
from rag_sources import RetrievalResult


def _turn(question="q", answer="a", route="sql", **meta):
    return {"question": question, "answer": answer, "route": route, "sql": None,
            "columns": [], "rows": [], "row_count": 0, "stats": {}, "meta": meta}


def _reply(text):
    return SimpleNamespace(text=text, usage={}, model_id="stub")


def _ask(question, options=None):
    return json.dumps({"clarify": True, "question": question, "options": options or []})


@pytest.fixture
def model(monkeypatch):
    """Answer gate and suggestion calls with canned text, recording them."""
    calls = []

    def install(gate='{"clarify": false}', followups='{"suggestions": []}'):
        def fake(messages, **kw):
            calls.append({"messages": messages, **kw})
            answer = gate if kw["task"] == "chat_clarify" else followups
            if isinstance(answer, Exception):
                raise answer
            return _reply(answer)

        monkeypatch.setattr(llm_client, "chat", fake)
        return calls

    return install


# ── the gate ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("q", ["Total harvest last 30 days",
                               "How many active employees per estate?",
                               "Harvest ripeness rate by division",
                               "tampilkan total panen bulan januari 2025"])
def test_complete_questions_skip_the_model(model, q):
    calls = model(gate=_ask("Which?"))
    assert cc.clarify_gate(q, []) is None and calls == []


@pytest.mark.parametrize("q", ["Who is the best harvester?", "How is that block doing?",
                               "Kinerja divisi mana yang paling buruk?", "Bandingkan itu"])
def test_vague_questions_reach_the_gate(model, q):
    calls = model(gate=_ask("Which measure?", ["Ripe bunches", "Ripeness rate"]))
    assert cc.clarify_gate(q, []) == {"question": "Which measure?",
                                      "options": ["Ripe bunches", "Ripeness rate"]}
    assert calls[0]["tier"] == "reasoning"


@pytest.mark.parametrize("q, language", [
    ("Who is the best harvester?", "English"),
    ("Compare them.", "English"),
    ("Siapa pemanen terbaik?", "Indonesian"),
    ("Apakah itu buruk?", "Indonesian"),
])
def test_gate_is_told_the_language(model, q, language):
    calls = model()
    cc.clarify_gate(q, [])
    assert calls[0]["messages"][-1]["content"].endswith(f"REPLY LANGUAGE: {language}")


def test_never_asks_twice_running(model):
    calls = model(gate=_ask("Which?"))
    assert cc.clarify_gate("that one", [_turn(route=cc.ROUTE_CLARIFY)]) is None and calls == []


@pytest.mark.parametrize("gate", [RuntimeError("throttled"), "not json", "[1]",
                                  '{"clarify": "yes", "question": "x?"}',
                                  '{"clarify": false, "question": "x?"}'])
def test_broken_gate_means_no_question(model, gate):
    model(gate=gate)
    assert cc.clarify_gate("who is the best?", []) is None


def test_question_with_invented_figure_is_held_back(model):
    model(gate=_ask("Do you mean the 4,821 bunch block?"))
    assert cc.clarify_gate("how is that block?", []) is None


def test_question_may_repeat_figures_and_years(model):
    model(gate=_ask("The 47 ha block in 2024 or 2025?"))
    assert cc.clarify_gate("how is that 47 ha block?", []) is not None


@pytest.mark.parametrize("raw, expected", [
    (["a", "b", "A"], ["a", "b"]),
    (["only one"], []),
    (None, []),
    (["1", "2", "3", "4", "5"], ["1", "2", "3", "4"]),
    (["Please name the block", "Please give the label"], []),
    (["Silakan pilih", "Biaya", "Hasil"], ["Biaya", "Hasil"]),
])
def test_options_are_tidied(raw, expected):
    assert cc.parse_clarification(
        {"clarify": True, "question": "q?", "options": raw})["options"] == expected


def test_column_names_never_reach_the_user():
    assert cc.parse_clarification({
        "clarify": True, "question": "Use oph_ripe?", "options": ["oph_ripe", "oph_unripe"]
    }) == {"question": "Use oph ripe?", "options": ["oph ripe", "oph unripe"]}


# ── replies ────────────────────────────────────────────────────────────────

ASKED = _turn("Who is the best harvester?", "By what?", cc.ROUTE_CLARIFY,
              options=["Total ripe bunches", "A very long option that has more than eight words in it"])


@pytest.mark.parametrize("reply", ["Total ripe bunches", "by ripeness",
                                   "A very long option that has more than eight words in it"])
def test_reply_is_folded_into_the_question(reply):
    assert cc.resolve_reply(reply, [ASKED]) == f"Who is the best harvester? Specifically: {reply}"


@pytest.mark.parametrize("new", ["How many employees are there?",
                                 "show me the total harvest for every division across all of last year"])
def test_moving_on_is_left_alone(new):
    assert cc.resolve_reply(new, [ASKED]) == new


def test_only_a_clarification_folds():
    assert cc.resolve_reply("by ripeness", [_turn("q")]) == "by ripeness"


def test_pipeline_sees_whole_questions_and_no_clarifications():
    turns = [_turn("first"), ASKED, _turn("ripe", resolved="Who is best? (ripe)")]
    assert [t["question"] for t in cc.pipeline_turns(turns)] == ["first", "Who is best? (ripe)"]


# ── scope ──────────────────────────────────────────────────────────────────

BA_ONLY = [_turn("harvest in BA", estates=["ba"])]


@pytest.mark.parametrize("q", ["what about last month?", "bagaimana dengan bulan lalu",
                               "how many of those were unripe?", "by division",
                               "bandingkan dengan Q2"])
def test_follow_up_keeps_the_estate(q):
    assert cc.carried_estates(q, BA_ONLY) == ["ba"]


@pytest.mark.parametrize("q", ["how many active employees do we have in total today?",
                               "total panen bulan ini untuk seluruh divisi di perusahaan",
                               "what about K3?",
                               "what about all estates?",
                               "bagaimana dengan semua kebun?"])
def test_new_question_is_not_narrowed(q):
    assert cc.carried_estates(q, BA_ONLY) == []


@pytest.mark.parametrize("history", [
    [], [_turn()], [_turn(estates=["ba", "k3"])], [_turn(estates=["gone"])]])
def test_nothing_narrower_to_keep(history):
    assert cc.carried_estates("what about last month?", history) == []


def test_turn_without_a_query_keeps_the_scope():
    assert cc.scope_after([], BA_ONLY) == ["ba"]
    assert cc.scope_after(["K3"], BA_ONLY) == ["k3"]
    assert cc.scope_after([], []) == []


def test_follow_up_chain_is_walked_back_to_its_topic():
    history = [_turn("How many employees?"), _turn("Total ripe bunches in BA"),
               _turn("what about February?")]
    assert cc.retrieval_context("and March?", history).split("\n") == [
        "Total ripe bunches in BA", "what about February?", "and March?"]


def test_new_topic_keeps_only_the_previous_question():
    history = [_turn("Total ripe bunches in BA"), _turn("what about February?")]
    q = "How many active employees are registered right now?"
    assert cc.retrieval_context(q, history) == f"what about February?\n{q}"


def test_first_message_is_retrieved_as_typed():
    assert cc.retrieval_context("and March?", []) == "and March?"


def test_listed_options_are_cut_from_the_question():
    asked = cc.parse_clarification({
        "clarify": True, "question": "Please specify the measure. Options include: a, b.",
        "options": ["a", "b"]})
    assert asked["question"] == "Please specify the measure."


# ── suggestions ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["Why is block 99-99 so low?", "Is 4,821 bunches a lot?",
                                 "What is oph_ripe?", "total harvest?", "earlier one?", "Hm?", 12])
def test_bad_suggestions_are_dropped(bad):
    assert cc.clean_suggestions([bad], "total harvest?", "35-33 had 120 bunches.",
                                asked=["earlier one?"]) == []


def test_good_suggestions_are_capped():
    items = [f"Break that down by {w}?" for w in ["block", "division", "employee", "month"]]
    assert len(cc.clean_suggestions(items, "q", "a")) == cc.MAX_SUGGESTIONS


def test_suggestion_failure_means_no_buttons(model):
    model(followups=RuntimeError("throttled"))
    assert cc.suggest_followups("q", "a", [], [], []) == []


# ── through the endpoint ───────────────────────────────────────────────────

@pytest.fixture
def endpoint(monkeypatch, model):
    state = SimpleNamespace(turns=[], added=[], dispatched=[])
    monkeypatch.setattr(chat_router, "ensure_session", lambda sid, q: "s1")
    monkeypatch.setattr(chat_router, "get_turns", lambda sid, limit: state.turns)
    monkeypatch.setattr(chat_router, "add_turn",
                        lambda *a, **k: state.added.append((a, k)))

    def dispatch(source, question, session, sql_source):
        state.dispatched.append((question, session.turns))
        return RetrievalResult(answer="BA had 120 bunches.", source="sql", route="sql",
                               estates_used=["BA"], tables_used=["t_oph"])

    monkeypatch.setattr(chat_router, "dispatch_rag", dispatch)
    return state


def _post(message, source="sql"):
    return chat_router.chat_message(chat_router.ChatRequest(
        message=message, session_id="s1", source=source))


def test_clarification_never_runs_the_pipeline(endpoint, model):
    model(gate=_ask("By what measure?", ["Ripe bunches", "Ripeness rate"]))
    resp = _post("Who is the best harvester?")
    assert resp.route == "clarify" and resp.options == ["Ripe bunches", "Ripeness rate"]
    assert endpoint.dispatched == []
    assert endpoint.added[0][1]["route"] == "clarify"


def test_reply_runs_as_the_whole_question_but_is_stored_as_typed(endpoint, model):
    model()
    endpoint.turns[:] = [ASKED]
    resp = _post("Total ripe bunches")
    question, history = endpoint.dispatched[0]
    assert question == "Who is the best harvester? Specifically: Total ripe bunches" and history == []
    args, kw = endpoint.added[0]
    assert args[1] == "Total ripe bunches" and kw["meta"]["resolved"] == question
    assert resp.route == "sql"


def test_answer_carries_suggestions_and_scope(endpoint, model):
    model(followups='{"suggestions": ["Break it down by division?"]}')
    endpoint.turns[:] = BA_ONLY
    resp = _post("what about last month?")
    assert resp.suggestions == ["Break it down by division?"]
    assert resp.estates_carried == ["BA"]
    meta = endpoint.added[0][1]["meta"]
    assert meta["estates"] == ["ba"] and meta["carried"] == ["BA"]


def test_knowledge_base_is_left_alone(endpoint, model):
    calls = model(gate=_ask("Which?"))
    resp = _post("what does that clause say?", source="knowledge_base")
    assert resp.route == "sql" and calls == []


# ── one estate set against the rest, "run it", the period ─────────────────

EC_ONLY = [_turn("harvest in estate C", "Estate C had 1.11 bunches per hectare. To compare it with "
                 "others, we need to run the query without filtering by estate code.", estates=["EC"])]


@pytest.mark.parametrize("q", [
    "compare the operational efficiency of estate C againts other estates",
    "How does BA compare with the other estates?",
    "Estate C vs the rest",
    "is K3 better than the others?",
    "bandingkan kebun C dengan kebun lainnya",
    "run it without the estate filter",
])
def test_set_against_the_rest_widens(q):
    assert cc.widens_scope(q)


@pytest.mark.parametrize("q", [
    "harvest in estate C", "the rest of the month in BA", "other activities in estate C",
    "harvest of other crops", "total ripe bunches per hectare"])
def test_naming_one_estate_does_not_widen(q):
    assert not cc.widens_scope(q)


def test_set_against_the_rest_drops_a_carried_estate(monkeypatch):
    assert cc.carried_estates("and against the other estates?", BA_ONLY) == []


@pytest.mark.parametrize("q", ["run it", "Run it.", "yes", "yes please", "ok, run it", "go ahead",
                               "do it", "run the query again", "jalankan", "iya"])
def test_go_ahead_is_a_confirmation(q):
    assert cc.is_confirmation(q)


@pytest.mark.parametrize("q", ["run it for BA", "yes but for last month", "run",
                               "what is it?", "total ripe bunches"])
def test_a_request_is_not_a_bare_confirmation(q):
    assert not cc.is_confirmation(q)


def test_run_it_becomes_the_question_plus_the_offer():
    q = cc.resolve_confirmation("run it", EC_ONLY)
    assert q.startswith("harvest in estate C. ") and "without filtering by estate code" in q
    assert cc.widens_scope(q)          # so the filter really comes off


def test_run_it_with_nothing_offered_repeats_the_question():
    assert cc.resolve_confirmation("run it", [_turn("Total harvest in BA?", "BA harvested 10 bunches.")]) \
        == "Total harvest in BA"
    assert cc.resolve_confirmation("run it", []) == "run it"
    assert cc.resolve_confirmation("what about BA?", EC_ONLY) == "what about BA?"


@pytest.mark.parametrize("q,named", [
    ("Total ripe bunches per hectare", False),
    ("compare estate C against the other estates", False),
    ("Total harvest in 2025", True),
    ("ripe bunches last month", True),
    ("panen bulan oktober", True),
    ("harvest per week in BA", True),
    ("latest harvest in EC", True),
])
def test_names_period(q, named):
    assert cc.names_period(q) is named
