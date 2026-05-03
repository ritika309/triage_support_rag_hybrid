"""Stage 3 — Grounded responder.

Single LLM call (Gemini Flash by default) that takes the ticket plus retrieved
chunks and produces a grounded response with citations.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .config import PROMPTS_DIR, SETTINGS
from .corpus import product_area_enum
from .llm import call_structured
from .retrieve import Hit, load_index


Confidence = Literal["low", "med", "high"]


class GroundedResult(BaseModel):
    response: str = Field(description="user-facing answer")
    cited_chunk_ids: list[str] = Field(
        description="chunk IDs from the retrieved set; >=1 unless insufficient_corpus"
    )
    product_area: str = Field(description="must come from the provided enum")
    confidence: Confidence
    insufficient_corpus: bool
    can_act_unilaterally: bool
    reasoning: str


_SYSTEM_PROMPT: str | None = None
_ENUM_CACHE: dict[str, list[str]] | None = None


def _system_prompt() -> str:
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        _SYSTEM_PROMPT = (PROMPTS_DIR / "respond.md").read_text(encoding="utf-8")
    return _SYSTEM_PROMPT


def _enum_for(company: str | None) -> list[str]:
    global _ENUM_CACHE
    if _ENUM_CACHE is None:
        _ENUM_CACHE = product_area_enum(load_index().chunks)
    if company and company in _ENUM_CACHE:
        return _ENUM_CACHE[company]
    out: set[str] = set()
    for v in _ENUM_CACHE.values():
        out.update(v)
    return sorted(out)


def _truncate(text: str, max_chars: int = 1800) -> str:
    return text if len(text) <= max_chars else text[:max_chars] + " ..."


def _format_chunks(hits: list[Hit]) -> str:
    if not hits:
        return "(no chunks retrieved)"
    parts: list[str] = []
    for h in hits:
        c = h.chunk
        parts.append(
            f"[id={c.chunk_id} pa={c.product_area} doc={c.doc_path}]\n"
            f"{_truncate(c.text)}\n"
        )
    return "\n---\n".join(parts)


def _format_user(
    issue: str,
    subject: str,
    company: str | None,
    sub_requests: list[str] | None,
    enum_values: list[str],
    hits: list[Hit],
) -> str:
    co = company if company else "None"
    sub = sub_requests or [issue.strip()]
    return (
        f"ticket:\n"
        f"  company: {co}\n"
        f"  subject: {subject.strip()!r}\n"
        f"  issue: {issue.strip()!r}\n"
        f"  sub_requests: {sub}\n\n"
        f"product_area_enum (pick exactly one for product_area):\n"
        f"  {enum_values}\n\n"
        f"retrieved_chunks (cite by id):\n"
        f"{_format_chunks(hits)}"
    )


def ground(
    issue: str,
    subject: str,
    company: str | None,
    sub_requests: list[str] | None,
    hits: list[Hit],
) -> GroundedResult:
    enum_values = _enum_for(company)
    user = _format_user(issue, subject, company, sub_requests, enum_values, hits)
    return call_structured(
        system=_system_prompt(),
        user=user,
        schema=GroundedResult,
        model=SETTINGS.respond_model,
    )
