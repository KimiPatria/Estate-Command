"""Decide when the chat pipeline should escalate from the default model
(Nova 2 Lite, tier "main") to the reasoning model (Nova Pro, tier "reasoning").

Lookup-style questions ("total harvest 2025", "berapa karyawan") only need the
default model: it writes the SQL and states the figures. Questions that ask for
comparison, explanation, trend reading or a recommendation need the stronger
model to reason over the returned rows. The test is deliberately a transparent
keyword rule (English + Bahasa Indonesia), not another LLM call, so it adds no
latency or cost and can be unit-tested; it is cheap to extend from real misses.
"""
from __future__ import annotations

import re

from config import CHAT_ESCALATE_TO_REASONING

# Whole-word / prefix matches, case-insensitive.
_ANALYTIC = re.compile(
    r"\b("
    # English
    r"compar\w*|versus|vs|why|explain\w*|analy[sz]\w*|trends?|insights?|"
    r"reasons?|causes?|caused|driver\w*|recommend\w*|should|impact\w*|"
    r"correlat\w*|anomal\w*|outliers?|underperform\w*|good or bad|"
    r"is (?:it|that|this) (?:good|bad)|what (?:does|do) (?:it|this|that) mean|"
    r"summar[iy]s?e?|evaluate|assess\w*|"
    # Bahasa Indonesia
    r"bandingkan|membandingkan|dibandingkan|dibandingan|perbandingan|"
    r"mengapa|kenapa|jelaskan|menjelaskan|analisis|analisa|menganalisis|"
    r"tren|kecenderungan|penyebab|dampak|pengaruh|korelasi|anomali|"
    r"rekomendasi|saran|sebaiknya|ringkas\w*|evaluasi|bagus atau buruk|baik atau buruk"
    r")\b",
    re.IGNORECASE,
)


def needs_reasoning(message: str) -> bool:
    """True when the question calls for analysis rather than lookup."""
    if not CHAT_ESCALATE_TO_REASONING:
        return False
    return bool(_ANALYTIC.search(message or ""))


def answer_tier(message: str) -> str:
    return "reasoning" if needs_reasoning(message) else "main"
