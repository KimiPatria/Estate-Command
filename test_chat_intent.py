"""The /chat intent router, the cached conversation window and the calls code can skip.
The model is stubbed; no Bedrock or database calls."""
from types import SimpleNamespace

import pytest

import chat_charts
import chat_conversation as conv
import chat_intent
import chat_router
import llm_client


def _turn(pos, question=None, rows=None, route="sql"):
    return {"pos": pos, "question": question or f"q{pos}", "answer": f"a{pos}", "route": route,
            "sql": f"SELECT {pos}", "columns": ["x"] if rows else [], "rows": rows or [],
            "row_count": len(rows or []), "stats": {}, "meta": {}}


@pytest.fixture
def no_model(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("no model call expected")
    monkeypatch.setattr(llm_client, "chat_json", fail)
    monkeypatch.setattr(llm_client, "chat", fail)


@pytest.fixture
def router(monkeypatch):
    """The routing call answers `route`; records each call."""
    calls = []

    def install(route):
        def fake(messages, **kw):
            calls.append({"messages": messages, **kw})
            return None if route is None else {"route": route}
        monkeypatch.setattr(llm_client, "chat_json", fake)
        return calls
    return install


# ── greetings ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "hello, what can you do??", "Hi! What can you help me with?", "thanks! bye",
    "hey there, who are you?", "ok great, thanks a lot", "terima kasih, sampai jumpa",
    "halo, kamu bisa apa?", "good morning! how are you?", "thank you so much"])
def test_several_pleasantries_are_small_talk(message):
    assert conv.is_smalltalk(message)


@pytest.mark.parametrize("message", [
    "hi, total harvest last month?", "help me compare EA and EB", "ok what about February?",
    "thanks, now show ripe bunches per block", "hello what is the harvest in 2025"])
def test_a_greeting_with_a_question_is_not_small_talk(message):
    assert not conv.is_smalltalk(message)


# ── meaning questions ──────────────────────────────────────────────────────

@pytest.mark.parametrize("message,expected", [
    ("what does ripeness rate mean?", True),
    ("apa itu brondolan?", True),
    ("what is brondolan?", True),
    ("how is bunches per hectare calculated?", True),
    ("what counts as a manday?", True),
    ("what is the total harvest in 2025?", False),
    ("total harvest 2025", False),
    ("top 5 blocks by ripe bunches", False),
])
def test_asks_meaning(message, expected):
    assert chat_intent.asks_meaning(message) is expected


# ── deciding ───────────────────────────────────────────────────────────────

def test_code_decides_without_a_model(no_model):
    assert chat_intent.classify("hello, what can you do??", []) == ("chat", False)
    assert chat_intent.classify("total ripe bunches in 2025 per estate", []) == ("data", False)
    assert chat_intent.classify("run it", [_turn(0)], agreeing=True) == ("data", False)
    assert chat_intent.classify("Specifically: ripe bunches", [], answering_us=True) == ("data", False)
    rows = [{"x": "A", "y": 1}, {"x": "B", "y": 2}]
    assert chat_intent.classify("plot that", [_turn(0, rows=rows)]) == ("context", False)


def test_a_meaning_question_is_asked(router):
    calls = router("explain")
    assert chat_intent.classify("what does ripeness rate mean?", []) == ("explain", True)
    assert calls[0]["tier"] == "routing" and calls[0]["task"] == "chat_intent"
    assert "this is the first message" in calls[0]["messages"][-1]["content"]


def test_context_needs_a_conversation(router):
    router("context")
    assert chat_intent.classify("is that a lot?", [])[0] == "data"
    assert chat_intent.classify("is that a lot?", [_turn(0)])[0] == "context"


def test_no_decision_means_a_query(router):
    router(None)
    assert chat_intent.classify("how about the others", [_turn(0)])[0] == "data"


def test_the_decision_reaches_the_follow_up_check(no_model):
    with chat_intent.preset("context"):
        assert chat_router._route_followup("is that a lot?", [_turn(0)]) == "context"
    with chat_intent.preset("data"):
        assert chat_router._route_followup("is that a lot?", [_turn(0)]) == "sql"


# ── the cached window ──────────────────────────────────────────────────────

def test_the_window_moves_in_steps(monkeypatch):
    monkeypatch.setattr(chat_intent, "WINDOW_SIZE", 4)
    monkeypatch.setattr(chat_intent, "WINDOW_STEP", 4)
    starts = {}
    for n in range(1, 13):
        turns = [_turn(p) for p in range(n)]
        starts[n] = chat_intent.window(turns)[0]["pos"]
    assert [starts[n] for n in range(1, 13)] == [0, 0, 0, 0, 0, 0, 0, 4, 4, 4, 4, 8]


def test_the_window_counts_turns_it_does_not_show(monkeypatch):
    monkeypatch.setattr(chat_intent, "WINDOW_SIZE", 4)
    monkeypatch.setattr(chat_intent, "WINDOW_STEP", 4)
    # Positions 4 and 8 were clarifying turns, left out of the pipeline's history:
    # the cut still falls at position 4, so the window is the same as with them.
    turns = [_turn(p) for p in (2, 3, 5, 6, 7, 9)]
    assert [t["pos"] for t in chat_intent.window(turns)] == [5, 6, 7, 9]


def test_a_transcript_only_grows_between_cut_points(monkeypatch):
    monkeypatch.setattr(chat_intent, "WINDOW_SIZE", 4)
    monkeypatch.setattr(chat_intent, "WINDOW_STEP", 4)
    cp = llm_client.CACHE_POINT
    render = lambda t: f"{t['question']}|{t['answer']}"
    for n in range(1, 7):                              # 0..6 share a start
        before = chat_intent.transcript([_turn(p) for p in range(n)], render)
        after = chat_intent.transcript([_turn(p) for p in range(n + 1)], render)
        assert after.count(cp) == 2 and before.count(cp) == min(2, n)
        # Where the earlier prompt put its last point, the later one has one too.
        upto_last = before.rsplit(cp, 1)[0].replace(cp, "")
        upto_first = after.split(cp, 1)[0].replace(cp, "")
        assert upto_last == upto_first


def test_context_answer_shows_every_turn_in_the_window_the_same_way():
    rows = [{"x": "A"}]
    turns = [_turn(0, rows=rows), _turn(1), _turn(2, rows=rows)]
    one = chat_router._build_context_messages("why?", turns)[1]["content"]
    two = chat_router._build_context_messages("and?", turns + [_turn(3)])[1]["content"]
    shared = one.rsplit(llm_client.CACHE_POINT, 1)[0].replace(llm_client.CACHE_POINT, "")
    assert two.replace(llm_client.CACHE_POINT, "").startswith(shared)
    assert one.count("Rows (first 1 of 1)") == 2


# ── turning markers into cache points ──────────────────────────────────────

def test_markers_become_cache_points_on_bedrock(monkeypatch):
    monkeypatch.setattr(llm_client, "LLM_PROVIDER", "bedrock")
    monkeypatch.setattr(llm_client, "PROMPT_CACHE", True)
    cp = llm_client.CACHE_POINT
    out = llm_client._prepare_messages([{"role": "system", "content": "rules"},
                                        {"role": "user", "content": f"schema{cp}question"}])
    assert out[0]["content"] == [{"type": "text", "text": "rules"}, {"cachePoint": {"type": "default"}}]
    assert out[1]["content"] == [{"type": "text", "text": "schema"}, {"cachePoint": {"type": "default"}},
                                 {"type": "text", "text": "question"}]


def test_at_most_four_points_and_the_latest_are_kept(monkeypatch):
    monkeypatch.setattr(llm_client, "LLM_PROVIDER", "bedrock")
    monkeypatch.setattr(llm_client, "PROMPT_CACHE", True)
    cp = llm_client.CACHE_POINT
    out = llm_client._prepare_messages([{"role": "system", "content": "s"},
                                        {"role": "user", "content": cp.join("abcde") + "f"}])
    points = [b for m in out for b in m["content"] if "cachePoint" in b]
    assert len(points) == 4
    # The system prompt's point was the earliest of five: it is the one dropped.
    assert out[0]["content"] == [{"type": "text", "text": "s"}]
    assert sum("cachePoint" in b for b in out[1]["content"]) == 4


@pytest.mark.parametrize("provider,cache", [("groq", True), ("bedrock", False)])
def test_markers_are_dropped_elsewhere(monkeypatch, provider, cache):
    monkeypatch.setattr(llm_client, "LLM_PROVIDER", provider)
    monkeypatch.setattr(llm_client, "PROMPT_CACHE", cache)
    cp = llm_client.CACHE_POINT
    out = llm_client._prepare_messages([{"role": "system", "content": "rules"},
                                        {"role": "user", "content": f"schema{cp}question"}])
    assert out == [{"role": "system", "content": "rules"}, {"role": "user", "content": "schemaquestion"}]


def test_cache_counts_reach_the_call_log():
    rec = llm_client._usage_record({"input_tokens": 900, "output_tokens": 5, "total_tokens": 905,
                                    "input_token_details": {"cache_read": 800, "cache_creation": 90}})
    assert rec["cache_read_tokens"] == 800 and rec["cache_write_tokens"] == 90


def test_the_sql_writer_caches_after_the_schema(monkeypatch):
    import prompt_builder
    monkeypatch.setattr(prompt_builder, "compact_ddl_for", lambda names: "CREATE TABLE t (a int);")
    msgs = prompt_builder.build_sql_messages("how many?", [SimpleNamespace(name="t")], [])
    user = msgs[1]["content"]
    assert user.index(llm_client.CACHE_POINT) < user.index("Q: how many?")


# ── calls code can skip ────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "top 5 blocks by ripe bunches", "which division had the best ripeness rate in 2025?",
    "who harvested the most bunches this month?", "pemanen terbaik berdasarkan janjang"])
def test_clarify_is_skipped_when_a_measure_is_named(no_model, message):
    assert conv.clarify_gate(message, []) is None


def test_clarify_still_asks_for_a_measure(router, monkeypatch):
    calls = []
    monkeypatch.setattr(llm_client, "chat_json", lambda m, **kw: calls.append(kw) or {"clarify": False})
    conv.clarify_gate("who is the best employee?", [])
    conv.clarify_gate("how many ripe bunches did that block have?", [])
    assert [c["task"] for c in calls] == ["chat_clarify", "chat_clarify"]


def test_one_label_one_measure_charts_without_a_model(monkeypatch):
    monkeypatch.setattr(chat_charts, "llm_plan", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    rows = [{"block": "A1", "ripe_bunches": 10}, {"block": "A2", "ripe_bunches": 20}]
    chart = chat_charts.plan_chart("ripe bunches per block", ["block", "ripe_bunches"], rows)
    assert chart and chart["type"] in ("bar", "hbar")


def test_wider_results_still_ask_the_model(monkeypatch):
    asked = []
    monkeypatch.setattr(chat_charts, "llm_plan", lambda *a, **k: asked.append(1) or None)
    rows = [{"block": "A1", "ripe": 10, "unripe": 2}, {"block": "A2", "ripe": 20, "unripe": 3}]
    chat_charts.plan_chart("ripe and unripe per block", ["block", "ripe", "unripe"], rows)
    assert asked
