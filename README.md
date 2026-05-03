# Multi-Domain Support Triage Agent

Terminal-based agent that resolves real support tickets across three ecosystems (**HackerRank**, **Claude**, **Visa**) using only the provided corpus, with explicit escalation for high-risk and unsupported cases.

> Built for HackerRank Orchestrate (May 2026, 24h hackathon). Final output: [`support_tickets/output.csv`](../support_tickets/output.csv). Per-row trace JSON in `code/.runs/`.

---

## TL;DR

A **deterministic 4-stage pipeline** — Triage → Retrieve → Ground → Gate — over a metadata-filtered hybrid RAG index. All grounded answers cite specific corpus chunks; the gate is pure-Python and is the only place final output values are stamped. No agent loops, no fine-tuning, no live web calls. Free-tier provider stack: Groq (Llama 3.1 8B + 3.3 70B) with local sentence-transformer embeddings.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                ticket row (issue, subject, company)                      │
└────────────────────────────┬─────────────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ STAGE 1 — TRIAGE  (LLM, structured output)                               │
│  - detect company if input is None / wrong                               │
│  - split into atomic sub-requests                                        │
│  - scope: in_scope | off_topic | social | malicious                      │
│  - risk: low | med | high                                                │
│  - request_type_hint, product_area_hint                                  │
│  - asks_unilateral_action, short_circuit                                 │
└────────────────────────────┬─────────────────────────────────────────────┘
                             ▼
                short_circuit ≠ none ?  ─── yes ──▶ skip directly to GATE
                             │ no
                             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ STAGE 2 — RETRIEVE  (no LLM — hybrid)                                    │
│  - filter by detected_company                                            │
│  - BM25 (rank_bm25) top-20 + dense (bge-small-en-v1.5) top-20            │
│  - Reciprocal Rank Fusion (RRF) → top-K (default 8)                      │
└────────────────────────────┬─────────────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ STAGE 3 — GROUND  (LLM, structured output)                               │
│  - response, cited_chunk_ids[], product_area (from enum)                 │
│  - confidence: low | med | high                                          │
│  - insufficient_corpus, can_act_unilaterally                             │
│  - hard rule: NO unauthorized promises (refunds, bans, score overrides)  │
└────────────────────────────┬─────────────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ STAGE 4 — GATE  (pure Python — single point of truth)                    │
│  Decision precedence (first matching rule wins):                         │
│    1. ack            → replied / invalid / canned ack                    │
│    2. off_topic      → replied / invalid / canned out-of-scope           │
│    3. malicious      → escalated / invalid                               │
│    4. unilateral     → escalated / triage request_type                   │
│    5. cannot_act     → escalated (LLM caught it post-retrieval)          │
│    6. insufficient   → escalated                                         │
│    7. low conf+high risk → escalated (defensive)                         │
│    8. else           → replied / grounded response                       │
│  Also clamps product_area to the company's canonical enum.               │
└────────────────────────────┬─────────────────────────────────────────────┘
                             ▼
                       output.csv row
```

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

## File map

```
code/
├── config.py                  # env-driven settings; auto-enables HF offline mode if model is cached
├── corpus.py                  # markdown loader + H2 chunker + product_area canonicalization
├── retrieve.py                # hybrid retriever (BM25 + dense + RRF), persisted index
├── llm.py                     # provider abstraction (Gemini + Groq), rate limiter, 429 backoff
├── triage.py                  # stage 1 — TriageResult + few-shot prompt
├── respond.py                 # stage 3 — GroundedResult + citation enforcement
├── gate.py                    # stage 4 — deterministic decision rules (single point of truth)
├── pipeline.py                # wires stages 1–4 + per-row trace dump
├── eval.py                    # sample-CSV loader + retrieval-only smoke metrics
├── main.py                    # CLI entry point (all commands)
├── prompts/
│   ├── triage.md              # system prompt with 9 few-shot examples
│   └── respond.md             # system prompt with 6 few-shot examples
├── requirements.txt
├── README.md                  # this file
├── .cache/                    # gitignored — index files
└── .runs/                     # gitignored — per-run trace dumps
```

---

## Why this architecture

### Why no agent loop / framework

Output schema is fixed and deterministic. Free-form ReAct or LangGraph adds debugging surface area without delivering value here. A hand-written DAG of typed dataclasses ships faster, is auditable, and is easier to defend in the AI judge interview.

### Why no fine-tuning

109 labeled rows is too few to fine-tune on without overfitting. The base LLM already knows English, classification, and JSON schema. The hard work is **retrieval + grounding + safety**, which is data-shaped, not weight-shaped. In-context few-shots + structured output replace SFT for a fraction of the effort, with full citation traceability.

### Why hybrid retrieval

Support docs use exact phrases ("reset password", "429 errors", "traveller's cheques") that BM25 catches reliably; dense embeddings catch semantic paraphrase ("my chat has private info" → conversation deletion docs). Reciprocal Rank Fusion is parameter-free and combines both signals robustly.

### Why local embeddings

Free, deterministic, offline (after first download), and ~750 chunks fits in <10MB of vectors. No API rate limits, no DNS dependencies during runs.

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

## Submission

The submission has three artifacts (per the repo's top-level `README.md`):

### 1. Predictions CSV

```powershell
# from repo root, .venv activated, .env populated, index built
python -m code.main
```

Writes `support_tickets/output.csv` with one row per input row (57 rows expected). Columns: `status, product_area, response, justification, request_type`.

Verify:

```powershell
(Get-Content .\support_tickets\output.csv).Count   # expect 58 (header + 57 rows)
```

### 2. Code zip

Zip the `code/` directory **excluding** `.cache/` and `.runs/`:

```powershell
$exclude = @('.cache', '.runs', '__pycache__')
Get-ChildItem -Path code -Recurse |
  Where-Object {
    $p = $_.FullName
    -not ($exclude | Where-Object { $p -like "*\$_\*" -or $p -like "*\$_" })
  } | Compress-Archive -DestinationPath code-submission.zip -Force
```

Or simpler — `Compress-Archive` and just delete `.cache/` and `.runs/` before zipping:

```powershell
Remove-Item -Recurse -Force code\.cache, code\.runs -ErrorAction SilentlyContinue
Compress-Archive -Path code\* -DestinationPath code-submission.zip -Force
```

### 3. Chat transcript

The `AGENTS.md`-driven log file:

```
%USERPROFILE%\hackerrank_orchestrate\log.txt
```

Upload as-is.

### Submit

https://www.hackerrank.com/contests/hackerrank-orchestrate-may26/challenges/support-agent/submission

Upload all three:
- Code zip
- Predictions CSV (`support_tickets/output.csv`)
- Chat transcript (`log.txt` from above)

---

## AI judge interview crib

Topics most likely to come up and the answers I'd give:

| Question | Answer |
|---|---|
| "Walk me through your architecture." | 4-stage deterministic pipeline; only the gate sets final values. |
| "Why a deterministic gate instead of an LLM critic?" | Auditability + 0 added latency + crisp rules I can defend per-case. |
| "Why hybrid retrieval?" | BM25 catches exact phrases (reset password, 429), dense catches paraphrase. RRF combines them parameter-free. |
| "Why no fine-tuning?" | 109 labels too few; SFT on a grounded-retrieval task degrades citation traceability. In-context few-shots achieve same effect cheaper and with full audit. |
| "What's your biggest failure mode?" | LLM misclassifying "self-service action" as `unilateral` — caught and patched after eval iteration; example in trace. |
| "How do you handle multi-request tickets?" | Triage emits `sub_requests`; gate uses most-severe routing — any sub-request needing escalation escalates the whole ticket. |
| "How do you prevent hallucinated answers?" | Mandatory `cited_chunk_ids` from retrieved set; `insufficient_corpus=true` triggers escalation; `--respond` checks every cited ID against the retrieved set. |
| "Determinism?" | `temperature=0`, structured output via schema, signature-cached index, pinned deps, per-row traces. |
| "Why split LLM models?" | Triage volume × TPM cap meant we needed a fast small model (8B); grounding is lower volume but reasoning-heavy, so 70B. Free-tier-fit. |
