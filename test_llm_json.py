"""Structured output (llm_client.chat_json): a forced tool, a schema check, one repair.
The model is stubbed; no Bedrock calls."""
from types import SimpleNamespace

import pytest

import chat_router
import error_handler
import llm_client

ROUTE = {"type": "object", "required": ["route"],
         "properties": {"route": {"type": "string", "enum": ["context", "sql"]}}}


def _tool(args, name="route"):
    return SimpleNamespace(text="", message=SimpleNamespace(tool_calls=[{"name": name, "args": args}]))


def _text(text):
    return SimpleNamespace(text=text, message=None)


@pytest.fixture
def model(monkeypatch):
    """Replies in order from `replies`; records every call's kwargs and messages."""
    calls = []

    def install(*replies, provider="bedrock"):
        monkeypatch.setattr(llm_client, "LLM_PROVIDER", provider)
        queue = list(replies)

        def fake(messages, **kw):
            calls.append({"messages": messages, **kw})
            reply = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(reply, Exception):
                raise reply
            return reply

        monkeypatch.setattr(llm_client, "chat", fake)
        return calls

    return install


def _ask(**kw):
    return llm_client.chat_json_ex([{"role": "user", "content": "q"}], name="route",
                                   schema=kw.pop("schema", ROUTE), task="t", **kw)


def test_forced_tool_args_are_returned(model):
    calls = model(_tool({"route": "context"}))
    assert _ask() == ({"route": "context"}, "tool")
    assert calls[0]["tool_choice"] == "route" and calls[0]["tools"][0]["function"]["name"] == "route"
    assert calls[0]["json_mode"] is False and len(calls) == 1


def test_a_bad_value_gets_one_repair_that_says_what_was_wrong(model):
    calls = model(_tool({"route": "database"}), _tool({"route": "sql"}))
    assert _ask() == ({"route": "sql"}, "repair")
    assert len(calls) == 2
    fix = calls[1]["messages"][-1]["content"]
    assert "must be one of context|sql" in fix and "database" in fix


def test_text_reply_is_parsed_when_no_tool_was_called(model):
    model(_text('Sure. {"route": "sql"}'))
    assert _ask() == ({"route": "sql"}, "text")


def test_two_bad_replies_give_nothing(model):
    calls = model(_text("no idea"))
    assert _ask() == (None, "fail") and len(calls) == 2


def test_a_broken_off_tool_call_is_asked_again_as_text(model):
    calls = model(RuntimeError("ModelErrorException: Model produced invalid sequence as part of ToolUse"),
                  _text('{"route": "sql"}'))
    assert _ask() == ({"route": "sql"}, "repair")
    assert calls[1]["tools"] is None and "JSON schema" in calls[1]["messages"][-1]["content"]


def test_a_failed_call_is_not_retried(model):
    calls = model(RuntimeError("throttled"))
    assert _ask() == (None, "fail") and len(calls) == 1


@pytest.mark.parametrize("raw, expected", [
    ({"clarify": "true", "options": '["a", "b"]'}, {"clarify": True, "options": ["a", "b"]}),
    ({"clarify": False, "options": ["a", "b", "c", "d", "e", "f", "g"]},
     {"clarify": False, "options": ["a", "b", "c", "d", "e", "f"]}),
])
def test_accidental_shapes_are_coerced(model, raw, expected):
    schema = {"type": "object", "required": ["clarify"], "properties": {
        "clarify": {"type": "boolean"},
        "options": {"type": "array", "items": {"type": "string"}, "maxItems": 6}}}
    model(_tool(raw))
    assert _ask(schema=schema)[0] == expected


@pytest.mark.parametrize("raw", [{"clarify": "yes"}, {}, {"clarify": None}])
def test_unclear_values_are_not_guessed(model, raw):
    schema = {"type": "object", "required": ["clarify"], "properties": {"clarify": {"type": "boolean"}}}
    model(_tool(raw))
    assert _ask(schema=schema) == (None, "fail")


def test_enum_case_is_normalised(model):
    model(_tool({"route": "SQL"}))
    assert _ask()[0] == {"route": "sql"}


def test_without_bedrock_it_asks_for_json_text(model):
    calls = model(_text('{"route": "context"}'), provider="groq")
    assert _ask() == ({"route": "context"}, "text")
    assert calls[0]["tools"] is None and calls[0]["json_mode"] is True
    assert "JSON schema" in calls[0]["messages"][-1]["content"]


# ── the callers keep their safe defaults ────────────────────────────────────

def test_router_falls_back_to_a_new_query(model):
    model(_text("hmm"))
    turns = [{"question": "q", "answer": "a", "rows": [], "columns": [], "row_count": 0}]
    assert chat_router._route_followup("is that a lot?", turns) == "sql"


def test_router_takes_the_tool_decision(model):
    model(_tool({"route": "context"}))
    turns = [{"question": "q", "answer": "a", "rows": [], "columns": [], "row_count": 0}]
    assert chat_router._route_followup("is that a lot?", turns) == "context"


def test_domain_check_never_rejects_on_a_broken_reply(model):
    model(RuntimeError("down"))
    assert error_handler.is_off_domain("write me a poem") is False


def test_domain_check_reads_the_tool_decision(model):
    model(_tool({"related": False}, name="domain_check"))
    assert error_handler.is_off_domain("write me a poem") is True
