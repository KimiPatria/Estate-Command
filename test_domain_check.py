"""The off-topic check's keyword shortcut. The routing model is stubbed."""
from types import SimpleNamespace

import pytest

import error_handler
import llm_client


@pytest.fixture
def model(monkeypatch):
    calls = []

    def fake(messages, **kw):
        calls.append(messages)
        return SimpleNamespace(text='{"related": false}')

    monkeypatch.setattr(llm_client, "chat", fake)
    return calls


@pytest.mark.parametrize("q", [
    "Who is the best harvester by total ripe bunches?",
    "How many blocks are in K3?",
    "How many active employees do we have in total today?",
    "How many workers per gang?",
    "Which divisions improved?",
    "Berapa panen bulan ini?",
    "siapa pemanen terbaik di kebun 3",
    "jumlah karyawan aktif",
    "kehadiran pekerja minggu lalu",
    "Total for BA",
])
def test_plantation_words_skip_the_model(model, q):
    assert error_handler.is_off_domain(q) is False
    assert model == []


@pytest.mark.parametrize("q", ["What is the capital of France?", "write me a poem",
                               "blockchain explained", "tell me about palmistry"])
def test_anything_else_goes_to_the_model(model, q):
    assert error_handler.is_off_domain(q) is True
    assert len(model) == 1
