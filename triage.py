"""Stage 1 — Triage.

Single Gemini Flash call with structured output. Classifies the ticket and
decides whether to short-circuit (skip retrieval/grounding) or proceed.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .config import PROMPTS_DIR
from .llm import call_structured


Company = Literal["HackerRank", "Claude", "Visa", "None"]
Scope = Literal["in_scope", "off_topic", "social", "malicious"]
Risk = Literal["low", "med", "high"]
RequestType = Literal["product_issue", "feature_request", "bug", "invalid"]
ShortCircuit = Literal["ack", "off_topic", "malicious", "unilateral", "none"]


class TriageResult(BaseModel):
    detected_company: Company = Field(description="HackerRank | Claude | Visa | None")
    sub_requests: list[str] = Field(description="atomic asks; one ticket can have several")
    scope: Scope
    risk: Risk
    request_type_hint: RequestType
    product_area_hint: str = Field(description="lowercase + underscores; '' if N/A")
    asks_unilateral_action: bool
    short_circuit: ShortCircuit
    reasoning: str


_SYSTEM_PROMPT: str | None = None


def _system_prompt() -> str:
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        _SYSTEM_PROMPT = (PROMPTS_DIR / "triage.md").read_text(encoding="utf-8")
    return _SYSTEM_PROMPT


def _format_user(issue: str, subject: str, company: str | None) -> str:
    co = company if company else "None"
    return (
        f"issue: {issue.strip()!r}\n"
        f"subject: {subject.strip()!r}\n"
        f"company: {co}"
    )


def triage(issue: str, subject: str, company: str | None) -> TriageResult:
    user = _format_user(issue, subject, company)
    return call_structured(
        system=_system_prompt(),
        user=user,
        schema=TriageResult,
    )
