"""Escalation rule for the chat pipeline (model_routing.needs_reasoning)."""
import pytest

from model_routing import answer_tier, needs_reasoning

LOOKUPS = [
    "Total harvest in 2025",
    "berapa panen yang dihasilkan pada Q2 2025",
    "how many employees are there per estate?",
    "list all estates with their names",
    "top 5 blocks by ripe bunches in 2025",
    "tampilkan total work duration pekerja di bulan januari 2025",
]

ANALYTIC = [
    "Compare total harvest in 2025 between BA and K3",
    "bandingkan produksi bunches tiap bulan antar estate",
    "bagaimana jika dibandingkan dengan bulan februari",
    "why did harvest drop in March?",
    "mengapa panen turun di bulan maret",
    "explain the trend in unripe bunches",
    "is that good or bad?",
    "analisis hubungan antara tandan mentah dan total tandan",
    "should we add more harvesters to division 2?",
    "give me a summary of production by estate",
]


@pytest.mark.parametrize("q", LOOKUPS)
def test_lookup_stays_on_main(q):
    assert not needs_reasoning(q)
    assert answer_tier(q) == "main"


@pytest.mark.parametrize("q", ANALYTIC)
def test_analysis_escalates(q):
    assert needs_reasoning(q)
    assert answer_tier(q) == "reasoning"


def test_empty_message():
    assert not needs_reasoning("")
    assert not needs_reasoning(None)


def test_switch_off(monkeypatch):
    import model_routing
    monkeypatch.setattr(model_routing, "CHAT_ESCALATE_TO_REASONING", False)
    assert not model_routing.needs_reasoning("why did harvest drop?")
