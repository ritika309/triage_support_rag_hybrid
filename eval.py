"""Eval harness.

Phase 3 (now): retrieval-only smoke metrics from sample_support_tickets.csv.
Phase 8 (later): full pipeline scoring (status / request_type / product_area / response).
"""
from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass

from .config import SAMPLE_CSV
from .retrieve import retrieve


@dataclass
class SampleRow:
    issue: str
    subject: str
    company: str | None
    expected_response: str
    expected_product_area: str
    expected_status: str
    expected_request_type: str


def load_sample(limit: int | None = None) -> list[SampleRow]:
    rows: list[SampleRow] = []
    with SAMPLE_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            company = (r.get("Company") or "").strip()
            rows.append(
                SampleRow(
                    issue=(r.get("Issue") or "").strip(),
                    subject=(r.get("Subject") or "").strip(),
                    company=None if not company or company.lower() == "none" else company,
                    expected_response=(r.get("Response") or "").strip(),
                    expected_product_area=(r.get("Product Area") or "").strip(),
                    expected_status=(r.get("Status") or "").strip(),
                    expected_request_type=(r.get("Request Type") or "").strip(),
                )
            )
            if limit and len(rows) >= limit:
                break
    return rows


def retrieval_smoke(limit: int | None = None) -> dict:
    """For rows with a labeled company AND product_area, check whether the top-k
    hits surface a chunk whose product_area matches the expected one.
    """
    rows = load_sample(limit=limit)
    total = 0
    top1_match = 0
    top5_match = 0
    company_match = 0
    misses: list[dict] = []
    pa_distribution: Counter[str] = Counter()

    for row in rows:
        if not row.company or not row.expected_product_area:
            continue
        if row.expected_status.lower() == "escalated":
            continue
        total += 1
        query = (row.subject + "\n" + row.issue).strip()
        hits = retrieve(query, row.company, top_k=5)
        if not hits:
            misses.append({"issue": row.issue[:120], "reason": "no hits"})
            continue
        if hits[0].chunk.company == row.company:
            company_match += 1
        expected_norm = row.expected_product_area.replace("-", "_").lower()
        top_pas = [h.chunk.product_area for h in hits]
        pa_distribution.update(top_pas)
        if expected_norm in top_pas[:1]:
            top1_match += 1
            top5_match += 1
        elif expected_norm in top_pas[:5]:
            top5_match += 1
        elif any(expected_norm in pa or pa in expected_norm for pa in top_pas[:5]):
            top5_match += 1  # substring credit
        else:
            misses.append({
                "issue": row.issue[:120],
                "expected_pa": expected_norm,
                "got_pas": top_pas[:5],
                "company": row.company,
            })

    return {
        "scored_rows": total,
        "top1_product_area_match": top1_match,
        "top5_product_area_match": top5_match,
        "top1_pct": round(100 * top1_match / total, 1) if total else 0.0,
        "top5_pct": round(100 * top5_match / total, 1) if total else 0.0,
        "company_match_top1": company_match,
        "misses_sample": misses[:10],
        "pa_distribution_top10": pa_distribution.most_common(10),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(retrieval_smoke(), indent=2))
