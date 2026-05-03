"""End-to-end per-ticket pipeline: triage -> retrieve -> ground -> gate.

Also handles per-row trace dumps for debugging and the AI judge interview.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import SETTINGS
from .gate import FinalRow, decide
from .respond import GroundedResult, ground
from .retrieve import Hit, retrieve
from .triage import TriageResult, triage


@dataclass
class TicketInput:
    issue: str
    subject: str
    company: str | None


@dataclass
class TraceRecord:
    row_index: int
    input: dict
    triage: dict
    retrieval: list[dict]
    grounded: dict | None
    final: dict
    timings_ms: dict


def _write_trace(run_dir: Path, trace: TraceRecord) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    out = run_dir / f"row_{trace.row_index:04d}.json"
    out.write_text(
        json.dumps(asdict(trace), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def run_one(
    ticket: TicketInput,
    row_index: int = 0,
    run_dir: Path | None = None,
) -> FinalRow:
    """Run the full pipeline on one ticket. Returns the final row.

    If `run_dir` is provided, writes a per-row trace JSON.
    """
    timings: dict[str, float] = {}

    t0 = time.perf_counter()
    t_triage = triage(ticket.issue, ticket.subject, ticket.company)
    timings["triage_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    detected = t_triage.detected_company
    detected = None if detected in (None, "None", "") else detected

    hits: list[Hit] = []
    grounded: GroundedResult | None = None

    if t_triage.short_circuit == "none":
        query = (ticket.subject + "\n" + ticket.issue).strip()

        t1 = time.perf_counter()
        hits = retrieve(query, detected, top_k=SETTINGS.top_k)
        timings["retrieve_ms"] = round((time.perf_counter() - t1) * 1000, 1)

        if hits:
            t2 = time.perf_counter()
            grounded = ground(
                ticket.issue,
                ticket.subject,
                detected,
                t_triage.sub_requests,
                hits,
            )
            timings["ground_ms"] = round((time.perf_counter() - t2) * 1000, 1)

    t3 = time.perf_counter()
    final = decide(t_triage, hits, grounded)
    timings["gate_ms"] = round((time.perf_counter() - t3) * 1000, 1)
    timings["total_ms"] = round(
        (time.perf_counter() - t0) * 1000, 1
    )

    if run_dir is not None:
        trace = TraceRecord(
            row_index=row_index,
            input=asdict(ticket),
            triage=t_triage.model_dump(),
            retrieval=[
                {
                    "chunk_id": h.chunk.chunk_id,
                    "doc_path": h.chunk.doc_path,
                    "product_area": h.chunk.product_area,
                    "score": round(h.score, 6),
                    "bm25_rank": h.bm25_rank,
                    "dense_rank": h.dense_rank,
                }
                for h in hits
            ],
            grounded=grounded.model_dump() if grounded else None,
            final=asdict(final),
            timings_ms=timings,
        )
        _write_trace(run_dir, trace)

    return final


def make_run_dir(label: str) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    p = SETTINGS.runs_dir / f"{ts}-{label}"
    p.mkdir(parents=True, exist_ok=True)
    return p
