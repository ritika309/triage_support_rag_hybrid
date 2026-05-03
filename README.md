# Multi-Domain Support Triage Agent

Terminal-based agent that resolves real support tickets across three ecosystems (**HackerRank**, **Claude**, **Visa**) using only the provided corpus, with explicit escalation for high-risk and unsupported cases.

> Built for HackerRank Orchestrate (May 2026, 24h hackathon). Final output: [`support_tickets/output.csv`](../support_tickets/output.csv). Per-row trace JSON in `code/.runs/`.

---

## TL;DR

A **deterministic 4-stage pipeline** — Triage → Retrieve → Ground → Gate — over a metadata-filtered hybrid RAG index. All grounded answers cite specific corpus chunks; the gate is pure-Python and is the only place final output values are stamped. No agent loops, no fine-tuning, no live web calls. Free-tier provider stack: Groq (Llama 3.1 8B + 3.3 70B) with local sentence-transformer embeddings.

---

## Architecture

![RAG pipeline](./rag%20pipeline.png)

---

## Setup

### Requirements
- Python 3.12+ (3.10+ should work)
- ~500MB disk for `sentence-transformers` + `torch`
- ~130MB for the cached `bge-small-en-v1.5` embedding model (downloads once)
- A free Groq API key (or Gemini, see provider section)

### Install

```powershell
# from repo root
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r code\requirements.txt
```

### Configure

Create `.env` at the repo root with your provider keys:

```
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_your_key_here

LLM_TRIAGE_MODEL=llama-3.1-8b-instant
LLM_RESPOND_MODEL=llama-3.3-70b-versatile

LLM_MAX_RPM=10
RETRIEVE_TOP_K=8
```

Get a free Groq key at https://console.groq.com/keys (no card). To use Gemini instead, set `LLM_PROVIDER=gemini` and `GOOGLE_API_KEY=...`.

`.env` is gitignored — never commit it.

### One-time index build

```powershell
python -m code.main --build-index
```

Walks `data/{hackerrank,claude,visa}/**/*.md`, strips YAML frontmatter, chunks files >2500 chars by H2 sections, builds BM25, and encodes all chunks with `bge-small-en-v1.5`. Persists to `code/.cache/`. First run is ~5 minutes (CPU encoding + ~130MB model download). Subsequent runs are instant — the build skips re-encoding when chunk texts are unchanged (signature-cached).

After this step `code/.cache/` contains `chunks.jsonl`, `bm25.pkl`, `dense.npy`, `texts.sig`. None of it is committed.

---

## Run

| Command | Purpose |
|---|---|
| `python -m code.main --build-index` | (re)build the retrieval index |
| `python -m code.main --smoke` | phase 1+2 smoke test (no LLM): corpus stats + 6 canonical queries + retrieval-only eval |
| `python -m code.main --triage "<issue>" --company HackerRank` | one-off triage for a single ticket |
| `python -m code.main --triage-batch --limit 10` | run triage over the first N sample rows; reports company / request_type accuracy |
| `python -m code.main --respond "<issue>" --company Visa` | end-to-end on one ticket: prints all 4 stages + citation sanity check |
| `python -m code.main --eval --limit 20` | full pipeline on N sample rows; scores status / request_type / product_area against expected |
| `python -m code.main --eval` | full pipeline on all 109 sample rows |
| `python -m code.main` | submission run on `support_tickets.csv` → `support_tickets/output.csv` |

Per-row trace JSON for every pipeline run is dumped to `code/.runs/<timestamp>-<label>/row_NNNN.json` with the full triage + retrieval + grounded + final state. Gold for debugging and the AI judge interview.

---

### Why hybrid retrieval

Support docs use exact phrases ("reset password", "429 errors", "traveller's cheques") that BM25 catches reliably; dense embeddings catch semantic paraphrase ("my chat has private info" → conversation deletion docs). Reciprocal Rank Fusion is parameter-free and combines both signals robustly.


### Why a deterministic gate

The gate is the single auditable point where final output values are set. An LLM critic adds another failure mode without adding signal — the rules I encoded are crisp and provable. This is the part to point at for the AI judge interview to demonstrate that the agent isn't hallucinating decisions.

### Why split-tier LLM (8B triage + 70B grounding)

Triage is high-volume but conceptually simple (classification). 8B handles it at high TPM. Grounding is lower-volume (many tickets short-circuit) but reasoning-heavy and citation-critical. 70B excels there. Splitting hits the free-tier rate limits comfortably while keeping quality where it matters.

---

## Determinism & reproducibility

- All LLM calls use `temperature=0`.
- Structured output is enforced via Pydantic schemas (Gemini native `responseSchema` or Groq `json_object` + Pydantic validation).
- Pinned dependencies in `requirements.txt`.
- Embedding model pinned by ID; cached weights are content-hashed.
- Retrieval index is signature-checked: rebuilds only when `chunks.jsonl` text changes.
- Per-row trace JSON makes any decision reproducible from the captured inputs.

---

## Failure modes & mitigations

| Failure mode | Mitigation |
|---|---|
| LLM returns malformed JSON | Pydantic validation + automatic retry with longer backoff. Final fallback: gate sees no `grounded` and escalates. |
| LLM picks an off-enum `product_area` | Gate clamps to the company's enum, falling back to top retrieval hit's `product_area`. |
| 429 rate limit | Token-bucket rate limiter at `LLM_MAX_RPM`; 429-aware backoff schedule (15/30/60/90/120s) with jitter; respects server `Retry-After` if present. |
| Daily quota exhaustion | Multi-provider abstraction (Gemini ↔ Groq) — switch one env var. |
| HF model download fails (no network) | `config.py` auto-detects cached model and forces `HF_HUB_OFFLINE=1` so retrieval works fully offline after first build. |
| Triage misclassifies "self-service" as `unilateral` | Few-shot example + explicit rule clarifying account deletion / password reset / settings changes are NOT unilateral; gate then routes them to grounding. |
| Hallucinated citations | The `--respond` debug command flags any `cited_chunk_ids` that aren't in the retrieved set. |
| Multi-request tickets | Triage emits `sub_requests` and the gate's "most-severe wins" rule routes through escalation if any sub-request needs it. |

---

## Eval

Run on the labeled sample CSV before the submission run:

```powershell
python -m code.main --eval --limit 20      # quick check
python -m code.main --eval                  # full 109 rows
```

Reports:
- `status_accuracy` — replied vs escalated
- `request_type_accuracy` — product_issue / feature_request / bug / invalid
- `product_area_top1_accuracy` — substring-tolerant match against expected
- `errors` — pipeline failures (count + first 5 with messages)
- `failures_sample` — first 10 disagreements with expected values
- `trace_dir` — pointer to the dumped per-row JSONs for inspection

---
