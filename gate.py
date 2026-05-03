"""Stage 4 — Deterministic gate.

The single point of truth for final output values. Pure-Python rules that
merge signals from triage + retrieval + grounding. No LLM calls.
"""
from __future__ import annotations

from dataclasses import dataclass

from .corpus import product_area_enum
from .respond import GroundedResult
from .retrieve import Hit, load_index
from .triage import TriageResult


_ENUM_CACHE: dict[str, list[str]] | None = None


def _allowed_product_areas(company: str | None) -> set[str]:
    global _ENUM_CACHE
    if _ENUM_CACHE is None:
        _ENUM_CACHE = product_area_enum(load_index().chunks)
    if company and company in _ENUM_CACHE:
        return set(_ENUM_CACHE[company])
    out: set[str] = set()
    for v in _ENUM_CACHE.values():
        out.update(v)
    return out

# Canned response text — matches the style used in sample_support_tickets.csv
ACK_RESPONSE = "Happy to help"
OUT_OF_SCOPE_RESPONSE = "I am sorry, this is out of scope from my capabilities"
ESCALATE_RESPONSE = "Escalate to a human"


@dataclass
class FinalRow:
    status: str            # replied | escalated
    product_area: str
    response: str
    justification: str
    request_type: str      # product_issue | feature_request | bug | invalid


def _resolve_product_area(
    triage_res: TriageResult,
    hits: list[Hit],
    grounded: GroundedResult | None,
) -> str:
    """Final product_area selection, clamped to the company's enum.

    Priority: grounded.product_area (if in enum) > top hit's product_area
    > triage product_area_hint (if in enum) > 'uncategorized'.
    """
    company = (
        triage_res.detected_company
        if triage_res.detected_company not in (None, "None", "")
        else None
    )
    allowed = _allowed_product_areas(company)

    if grounded and grounded.product_area:
        pa = grounded.product_area
        if pa in allowed:
            return pa
        # off-enum: fall through to top hit's product_area which is enum-derived
    if hits:
        return hits[0].chunk.product_area
    hint = triage_res.product_area_hint
    if hint and hint in allowed:
        return hint
    return "uncategorized"


def _safe_request_type(rt: str | None) -> str:
    valid = {"product_issue", "feature_request", "bug", "invalid"}
    return rt if rt in valid else "invalid"


def decide(
    triage_res: TriageResult,
    hits: list[Hit],
    grounded: GroundedResult | None,
) -> FinalRow:
    """Produce the final row from upstream stage signals.

    Decision precedence (first matching rule wins):

      1. short_circuit == ack            -> replied, invalid, canned ack
      2. short_circuit == off_topic      -> replied, invalid, canned out-of-scope
      3. short_circuit == malicious      -> escalated, invalid
      4. short_circuit == unilateral     -> escalated, triage request_type
      5. grounding says cannot act       -> escalated, triage request_type
      6. grounding says insufficient     -> escalated, triage request_type
      7. low confidence + high risk      -> escalated (defensive)
      8. else                            -> replied, grounded response
    """
    sc = triage_res.short_circuit

    # 1) Pure ack / thank-you
    if sc == "ack":
        return FinalRow(
            status="replied",
            product_area="",
            response=ACK_RESPONSE,
            justification="Triage detected a pure acknowledgement; no request to handle.",
            request_type="invalid",
        )

    # 2) Off-topic
    if sc == "off_topic":
        return FinalRow(
            status="replied",
            product_area="",
            response=OUT_OF_SCOPE_RESPONSE,
            justification="Triage detected an off-topic ticket outside the three supported ecosystems.",
            request_type="invalid",
        )

    # 3) Malicious / prompt-injection
    if sc == "malicious":
        return FinalRow(
            status="escalated",
            product_area="",
            response=ESCALATE_RESPONSE,
            justification="Triage flagged the ticket as malicious or abusive; escalating to a human.",
            request_type="invalid",
        )

    # 4) Unilateral action demand caught at triage
    if sc == "unilateral":
        pa = _resolve_product_area(triage_res, hits, None)
        return FinalRow(
            status="escalated",
            product_area=pa,
            response=ESCALATE_RESPONSE,
            justification=(
                "Ticket demands an action support cannot grant from documentation alone. "
                + triage_res.reasoning
            ),
            request_type=_safe_request_type(triage_res.request_type_hint),
        )

    # Past short-circuit -> retrieval + grounding ran. `grounded` should be set.
    if grounded is None:
        pa = _resolve_product_area(triage_res, hits, None)
        return FinalRow(
            status="escalated",
            product_area=pa,
            response=ESCALATE_RESPONSE,
            justification="Internal: grounding stage produced no result; defaulting to escalation.",
            request_type=_safe_request_type(triage_res.request_type_hint),
        )

    pa = _resolve_product_area(triage_res, hits, grounded)
    rt = _safe_request_type(triage_res.request_type_hint)

    # 5) Responder caught a unilateral case post-retrieval
    if not grounded.can_act_unilaterally:
        return FinalRow(
            status="escalated",
            product_area=pa,
            response=ESCALATE_RESPONSE,
            justification=(
                "Grounded responder determined the request requires unauthorized unilateral action. "
                + grounded.reasoning
            ),
            request_type=rt,
        )

    # 6) Insufficient corpus -> escalate
    if grounded.insufficient_corpus:
        return FinalRow(
            status="escalated",
            product_area=pa,
            response=ESCALATE_RESPONSE,
            justification=(
                "No grounded answer in the corpus for this ticket. " + grounded.reasoning
            ),
            request_type=rt,
        )

    # 7) Defensive: low confidence + high risk -> escalate rather than reply
    if grounded.confidence == "low" and triage_res.risk == "high":
        return FinalRow(
            status="escalated",
            product_area=pa,
            response=ESCALATE_RESPONSE,
            justification=(
                "High-risk ticket with low retrieval confidence; escalating defensively. "
                + grounded.reasoning
            ),
            request_type=rt,
        )

    # 8) Grounded reply
    n_cites = len(grounded.cited_chunk_ids)
    return FinalRow(
        status="replied",
        product_area=pa,
        response=grounded.response,
        justification=(
            f"Grounded reply from {n_cites} chunk(s) at confidence={grounded.confidence}. "
            + grounded.reasoning
        ),
        request_type=rt,
    )
