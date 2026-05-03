"""Terminal entry point.

Usage:
    python -m code.main --build-index            # build retrieval index (one-time)
    python -m code.main --smoke                  # phase-1+2 smoke test (no LLM)
    python -m code.main --eval                   # full eval (requires LLM stages)
    python -m code.main                          # run on support_tickets.csv
"""
from __future__ import annotations

import argparse
import json


def cmd_build_index() -> int:
    from .retrieve import build_index
    from .corpus import product_area_enum

    idx = build_index(verbose=True)
    enum = product_area_enum(idx.chunks)
    print("\n[index] product_area enum (per company):")
    print(json.dumps(enum, indent=2))
    return 0


def cmd_smoke() -> int:
    """Phase 1+2 smoke: load index, run sample queries, run retrieval-only eval."""
    from .corpus import stats
    from .retrieve import load_index, retrieve
    from .eval import retrieval_smoke

    idx = load_index()
    print("=== corpus stats ===")
    print(json.dumps(stats(idx.chunks), indent=2))

    print("\n=== sample queries (top-5 hits each) ===")
    cases = [
        ("HackerRank", "How long do tests stay active in the system?"),
        ("HackerRank", "delete my account signed up via google login"),
        ("Claude", "delete a private claude conversation"),
        ("Claude", "API connection error 429"),
        ("Visa", "lost traveller cheques in Lisbon"),
        ("Visa", "report a stolen visa card from India"),
    ]
    for company, q in cases:
        hits = retrieve(q, company, top_k=5)
        print(f"\n  [{company}] {q!r}")
        for h in hits:
            print(
                f"    score={h.score:.4f} bm25={h.bm25_rank} dense={h.dense_rank}"
                f"  pa={h.chunk.product_area:<28} {h.chunk.doc_path}"
            )

    print("\n=== retrieval-only eval on sample CSV ===")
    print(json.dumps(retrieval_smoke(), indent=2))
    return 0


def cmd_triage(issue: str, subject: str, company: str | None) -> int:
    """One-off triage: --triage 'some issue text' [--subject ...] [--company ...]"""
    from .triage import triage

    result = triage(issue, subject or "", company)
    print(json.dumps(result.model_dump(), indent=2, ensure_ascii=False))
    return 0


def cmd_triage_batch(limit: int | None) -> int:
    """Run triage on every row in sample_support_tickets.csv. Prints a compact
    table + agreement metrics against expected fields where applicable."""
    from .eval import load_sample
    from .triage import triage

    rows = load_sample(limit=limit)
    print(f"[triage-batch] running triage on {len(rows)} sample rows ...\n")
    short_circuit_counter: dict[str, int] = {}
    company_match = 0
    company_total = 0
    request_type_match = 0
    request_type_total = 0
    misses: list[dict] = []
    for i, row in enumerate(rows, start=1):
        try:
            t = triage(row.issue, row.subject, row.company)
        except Exception as e:
            print(f"  {i:>3}. [ERROR] {type(e).__name__}: {e}")
            continue
        sc = t.short_circuit
        short_circuit_counter[sc] = short_circuit_counter.get(sc, 0) + 1
        co_marker = ""
        if row.company:
            company_total += 1
            if t.detected_company == row.company:
                company_match += 1
                co_marker = "[co=ok]"
            else:
                co_marker = f"[co!{t.detected_company}]"
        if row.expected_request_type:
            request_type_total += 1
            if t.request_type_hint == row.expected_request_type:
                request_type_match += 1
            else:
                misses.append({
                    "i": i,
                    "expected_rt": row.expected_request_type,
                    "got_rt": t.request_type_hint,
                    "expected_status": row.expected_status,
                    "short_circuit": sc,
                    "issue": row.issue[:80],
                })
        print(
            f"  {i:>3}. sc={sc:<10} risk={t.risk:<4} rt={t.request_type_hint:<15}"
            f" pa={t.product_area_hint:<22} {co_marker}  {row.issue[:60]}"
        )
    print("\n=== summary ===")
    print(json.dumps({
        "rows": len(rows),
        "short_circuit_distribution": short_circuit_counter,
        "company_detection_accuracy": (
            round(100 * company_match / company_total, 1)
            if company_total else None
        ),
        "request_type_hint_accuracy": (
            round(100 * request_type_match / request_type_total, 1)
            if request_type_total else None
        ),
        "request_type_misses_sample": misses[:10],
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_respond(issue: str, subject: str, company: str | None) -> int:
    """Run triage + retrieve + ground for a single ticket and print all three."""
    from .triage import triage
    from .retrieve import retrieve
    from .respond import ground

    print("=== STAGE 1: triage ===")
    t = triage(issue, subject or "", company)
    print(json.dumps(t.model_dump(), indent=2, ensure_ascii=False))

    if t.short_circuit != "none":
        print(f"\n[short-circuit={t.short_circuit}] skipping retrieval + grounding.")
        return 0

    detected = t.detected_company if t.detected_company != "None" else None
    query = ((subject or "") + "\n" + issue).strip()

    print("\n=== STAGE 2: retrieve (top-8) ===")
    hits = retrieve(query, detected, top_k=8)
    for h in hits:
        print(
            f"  score={h.score:.4f}  pa={h.chunk.product_area:<25}"
            f"  id={h.chunk.chunk_id}  {h.chunk.doc_path}"
        )

    print("\n=== STAGE 3: ground ===")
    g = ground(issue, subject or "", detected, t.sub_requests, hits)
    print(json.dumps(g.model_dump(), indent=2, ensure_ascii=False))

    print("\n=== citation check ===")
    cited = set(g.cited_chunk_ids)
    retrieved_ids = {h.chunk.chunk_id for h in hits}
    bogus = cited - retrieved_ids
    if bogus:
        print(f"  WARN: {len(bogus)} cited IDs not in retrieved set: {bogus}")
    else:
        print(f"  ok: all {len(cited)} cited IDs are from retrieval")
    return 0


CSV_HEADER = ["status", "product_area", "response", "justification", "request_type"]


def _csv_writer(out_path, append: bool = False):
    import csv
    mode = "a" if append else "w"
    f = out_path.open(mode, encoding="utf-8", newline="")
    writer = csv.writer(f)
    if not append:
        writer.writerow(CSV_HEADER)
    return writer, f


def cmd_eval(limit: int | None) -> int:
    """Run full pipeline on sample CSV; score against expected fields."""
    from .config import SAMPLE_CSV
    from .eval import load_sample
    from .pipeline import TicketInput, make_run_dir, run_one

    rows = load_sample(limit=limit)
    run_dir = make_run_dir("eval")
    print(f"[eval] {len(rows)} rows; traces -> {run_dir}\n")

    status_match = 0
    status_total = 0
    rt_match = 0
    rt_total = 0
    pa_top1 = 0
    pa_top1_total = 0
    error_count = 0
    failures: list[dict] = []
    errors: list[dict] = []

    for i, row in enumerate(rows, start=1):
        try:
            final = run_one(
                TicketInput(issue=row.issue, subject=row.subject, company=row.company),
                row_index=i,
                run_dir=run_dir,
            )
        except Exception as e:
            error_count += 1
            errors.append({"i": i, "err": f"{type(e).__name__}: {e}", "issue": row.issue[:80]})
            print(f"  {i:>3}. [ERROR] {type(e).__name__}: {e}")
            continue

        line = (
            f"  {i:>3}. {final.status:<9} {final.request_type:<14}"
            f" pa={final.product_area:<22}  {row.issue[:50]}"
        )

        if row.expected_status:
            status_total += 1
            if final.status.lower() == row.expected_status.lower():
                status_match += 1
                line += "  [s=ok]"
            else:
                line += f"  [s!{row.expected_status}]"
                failures.append({
                    "i": i,
                    "kind": "status",
                    "expected": row.expected_status,
                    "got": final.status,
                    "issue": row.issue[:80],
                })
        if row.expected_request_type:
            rt_total += 1
            if final.request_type == row.expected_request_type:
                rt_match += 1
            else:
                line += f"  [rt!{row.expected_request_type}]"
        if row.expected_product_area:
            pa_top1_total += 1
            exp = row.expected_product_area.replace("-", "_").lower()
            got = final.product_area
            if exp == got or exp in got or got in exp:
                pa_top1 += 1
            else:
                line += f"  [pa!{exp}]"
        print(line)

    print("\n=== eval summary ===")
    print(json.dumps({
        "rows": len(rows),
        "errors": error_count,
        "scored_rows": len(rows) - error_count,
        "status_accuracy": (
            round(100 * status_match / status_total, 1) if status_total else None
        ),
        "request_type_accuracy": (
            round(100 * rt_match / rt_total, 1) if rt_total else None
        ),
        "product_area_top1_accuracy": (
            round(100 * pa_top1 / pa_top1_total, 1) if pa_top1_total else None
        ),
        "failures_sample": failures[:10],
        "errors_sample": errors[:5],
        "trace_dir": str(run_dir),
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_run(limit: int | None, start: int = 1, end: int | None = None) -> int:
    """Run full pipeline on support_tickets.csv -> output.csv.

    `start` is 1-indexed inclusive; when start > 1, output.csv is opened in
    append mode (no new header). Useful for resuming with a different API
    key after a quota exhaustion.
    """
    import csv
    from .config import INPUT_CSV, OUTPUT_CSV
    from .pipeline import TicketInput, make_run_dir, run_one

    if not INPUT_CSV.exists():
        print(f"[run] input not found: {INPUT_CSV}")
        return 1

    run_dir = make_run_dir("submission")
    print(f"[run] traces -> {run_dir}")
    print(f"[run] writing predictions -> {OUTPUT_CSV} (start={start}, end={end or 'end'})\n")

    rows: list[tuple[str, str, str | None]] = []
    with INPUT_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            company = (r.get("Company") or "").strip()
            rows.append((
                (r.get("Issue") or "").strip(),
                (r.get("Subject") or "").strip(),
                None if not company or company.lower() == "none" else company,
            ))
            if limit and len(rows) >= limit:
                break

    append_mode = start > 1
    writer, out_f = _csv_writer(OUTPUT_CSV, append=append_mode)
    written = 0
    try:
        for i, (issue, subject, company) in enumerate(rows, start=1):
            if i < start:
                continue
            if end is not None and i > end:
                break
            try:
                final = run_one(
                    TicketInput(issue=issue, subject=subject, company=company),
                    row_index=i,
                    run_dir=run_dir,
                )
            except Exception as e:
                print(f"  {i:>3}. [ERROR] {type(e).__name__}: {e}; defaulting to escalation")
                from .gate import ESCALATE_RESPONSE
                writer.writerow(["escalated", "uncategorized", ESCALATE_RESPONSE,
                                 f"pipeline error: {type(e).__name__}", "invalid"])
                out_f.flush()
                written += 1
                continue
            writer.writerow([
                final.status, final.product_area, final.response,
                final.justification, final.request_type,
            ])
            out_f.flush()
            written += 1
            print(f"  {i:>3}. {final.status:<9} {final.request_type:<14}"
                  f" pa={final.product_area:<22}  {issue[:50]}")
    finally:
        out_f.close()

    print(f"\n[run] wrote {written} rows to {OUTPUT_CSV}")
    return 0


def cmd_merge_traces(run_dir_arg: str | None) -> int:
    """Build output.csv from per-row trace JSONs in a run dir."""
    from pathlib import Path
    from .config import OUTPUT_CSV, SETTINGS

    if run_dir_arg:
        run_dir = Path(run_dir_arg)
    else:
        # latest -submission run dir
        candidates = sorted(
            [p for p in SETTINGS.runs_dir.iterdir()
             if p.is_dir() and p.name.endswith("-submission")],
            key=lambda p: p.name,
        )
        if not candidates:
            print(f"[merge] no -submission run dirs in {SETTINGS.runs_dir}")
            return 1
        run_dir = candidates[-1]

    print(f"[merge] reading traces from {run_dir}")
    traces = sorted(run_dir.glob("row_*.json"))
    if not traces:
        print(f"[merge] no row_*.json files in {run_dir}")
        return 1

    writer, out_f = _csv_writer(OUTPUT_CSV, append=False)
    try:
        for t in traces:
            d = json.loads(t.read_text(encoding="utf-8"))
            f = d.get("final", {})
            writer.writerow([
                f.get("status", ""),
                f.get("product_area", ""),
                f.get("response", ""),
                f.get("justification", ""),
                f.get("request_type", ""),
            ])
            out_f.flush()
    finally:
        out_f.close()

    print(f"[merge] wrote {len(traces)} rows to {OUTPUT_CSV}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="support-triage")
    parser.add_argument("--build-index", action="store_true",
                        help="(re)build retrieval index")
    parser.add_argument("--smoke", action="store_true",
                        help="phase 1+2 smoke test (corpus + retrieval, no LLM)")
    parser.add_argument("--triage", type=str, default=None,
                        help="one-off triage: pass the ticket issue text")
    parser.add_argument("--subject", type=str, default="",
                        help="(with --triage) subject line")
    parser.add_argument("--company", type=str, default=None,
                        help="(with --triage / --triage-batch) input company")
    parser.add_argument("--triage-batch", action="store_true",
                        help="run triage on every row in sample CSV")
    parser.add_argument("--respond", type=str, default=None,
                        help="end-to-end single ticket: triage + retrieve + ground")
    parser.add_argument("--eval", action="store_true",
                        help="full pipeline eval on sample CSV (phase 8+)")
    parser.add_argument("--limit", type=int, default=None,
                        help="process only N rows")
    parser.add_argument("--start", type=int, default=1,
                        help="resume run from row N (1-indexed); appends to output.csv")
    parser.add_argument("--end", type=int, default=None,
                        help="stop after row N (1-indexed inclusive)")
    parser.add_argument("--merge-traces", nargs="?", const="", default=None,
                        help="rebuild output.csv from per-row trace JSONs (defaults to latest -submission run)")
    args = parser.parse_args()

    if args.build_index:
        return cmd_build_index()
    if args.smoke:
        return cmd_smoke()
    if args.triage:
        return cmd_triage(args.triage, args.subject, args.company)
    if args.triage_batch:
        return cmd_triage_batch(args.limit)
    if args.respond:
        return cmd_respond(args.respond, args.subject, args.company)
    if args.eval:
        return cmd_eval(args.limit)
    if args.merge_traces is not None:
        return cmd_merge_traces(args.merge_traces or None)
    return cmd_run(args.limit, start=args.start, end=args.end)


if __name__ == "__main__":
    raise SystemExit(main())
