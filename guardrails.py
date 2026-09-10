"""
Guardrails seam (no-op placeholder — Bedrock migration plan #7).

These two pass-throughs mark the exact points in the request pipeline where a
Bedrock Guardrails call (bedrock-runtime ApplyGuardrail) will be inserted once
AWS access exists:

  * apply_input_guardrails  — user text, before retrieval/generation
  * apply_output_guardrails — model text, before it is returned to the client

Deliberately no filtering logic today: the functions exist so that wiring in
Guardrails later touches only this file (read GUARDRAIL_ID/GUARDRAIL_VERSION
from config, call ApplyGuardrail with source="INPUT"/"OUTPUT", handle the
blocked-action response), not every router.

Wired into: chat_router.chat_message, report_router.generate_text_report,
dashboard_server.generate_dashboard.
"""

import logging

log = logging.getLogger(__name__)


def apply_input_guardrails(text: str, *, context: str = "general") -> str:
    """Pass-through. Future: Bedrock ApplyGuardrail(source="INPUT")."""
    return text


def apply_output_guardrails(text: str, *, context: str = "general") -> str:
    """Pass-through. Future: Bedrock ApplyGuardrail(source="OUTPUT")."""
    return text
