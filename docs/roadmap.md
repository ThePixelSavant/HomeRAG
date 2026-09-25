# Roadmap

The plan, in the order it is meant to happen. [status.md](status.md) says what
is actually built; this says what is intended and why it is ordered this way.

## The end state

A local LLM, authenticated through Open WebUI, that can query this knowledge
base over MCP — and can also reach approved frontier models when given proper
credentials. Documents enter through the command line and the Makefile. Nothing
sensitive is readable without the account holder deliberately allowing that
specific request.

Concretely, the corpus is meant to cover:

- Electronics and appliance manuals, so they can be queried instead of guessed at
- Bills and receipts, with exact arithmetic over them
- Software SDK documentation
- Network topology, server and container orchestration plans
- Past conversations, meetings and prompt sessions

The first four are open-tier. Receipts and transcripts are vault-tier.

## Phase 1 — the two-tier base · **done**

Everything in [status.md](status.md#component-status) marked done. The core
result is that the tier boundary holds: sensitive data never gets a Qdrant
vector, and three independent gates stand between a model and the vault.

## Phase 1.5 — retrieval quality · **done** (commit `ccddc0c`)

Four additive changes, prompted by reviewing a fintech RAG retrospective
against this stack. Landed together because each alters the payload, and they
should cost one `make rebuild-index` rather than four.

Three of the four gaps were confirmed by measurement, not taken on faith.

### 1. Per-page adaptive PDF extraction

`pdftotext -layout` was corrupting manuals in the index *at the time the
problem was found*, not hypothetically. Neither extraction mode is correct for
a mixed document, which is every manual in the corpus. Each page is now
extracted both ways and classified by the shape of its text.

| Page | Median left | Verdict | Kept |
|---|---:|---|---|
| IntelliFlo p2 (the scrambled safety page) | 59 | columns | reading order |
| IntelliFlo p5 | 4 | rows | `-layout` |
| IntelliFlo p9 / p14 | 25.5 / 32.5 | **ambiguous** | reading order, flagged |
| XPS p3 (contents, dot leaders) | 69 | columns | reading order |
| XPS p86 (numbered steps) | 1 | rows | `-layout` |
| TPM guide p1–2 (pure prose) | no gutters | prose | reading order |

*Since replaced:* the classifier kept `-layout` for Roland's dense A3 sheets,
which scrambled them. Every page is now `pdftotext -raw`, re-spaced from
reading order — [ADR-024](decisions.md#adr-024-pdf-pages-use--raw-re-spaced-from-reading-order).

### 2. Table-aware chunking

A split markdown table repeats its header on every continuation chunk. The
torque fixture went from 1 of 5 chunks carrying column labels to 5 of 5.
Whitespace-aligned tables are deliberately out of scope — see
[ADR-009](decisions.md#adr-009-whitespace-tables-are-not-detected).

### 3. Generic locators and verbatim citations

A `locator` rather than a page field, so Phase 2's git and web sources slot in
without a second rebuild. Both MCP servers now instruct the model to quote
numeric specs verbatim and cite them.

### 4. Document lifecycle

`active` / `superseded` / `stale` / `retracted`, plus tombstones, scheduled
review dates, and a same-directory version-candidate warning that never acts on
its own.

## Phase 1.6 — measured retrieval · **done** (commits `3c203df`–`70c9720`)

Prompted by Open WebUI answers taking 75 s to 3.5 min on the CPU-only
llama-server, where nearly all of that is prefilling tool results.

- **Trimmed results** — the model gets content, citation and ids, 3 hits by
  default ([ADR-023](decisions.md#adr-023-the-model-gets-a-trimmed-result-row)).
- **`fetch_context` capped** at 2 chunks either side.
- **`pdftotext -raw`**, re-spaced from reading order, replacing the per-page
  classifier ([ADR-024](decisions.md#adr-024-pdf-pages-use--raw-re-spaced-from-reading-order)).
- **A chunk per PDF section**, headings found by font size ([ADR-025](decisions.md#adr-025-pdf-chunks-follow-section-headings)).
- **`make eval`**, the evaluation harness Phase 3 called for, built first so
  each of the above could be measured rather than assumed.

## Phase 2 — vision, aggregation and more sources · **not started**

This is the next block of work. Items are roughly in dependency order.

### a. VLM receipt extraction and the ledger

The largest single gap: `query_ledger` is built and gated but nothing writes
rows.

- Render a receipt image or PDF, send it to the vision model, get structured
  JSON back: merchant, date, total, line items, payment method, category.
- Validate before inserting. Amounts are stored as **integer cents**; a total
  that does not match the sum of line items sets `needs_review` rather than
  being silently accepted.
- `needs_review_count` is already surfaced on every `query_ledger` response, so
  an unvalidated reading can never quietly inflate a total the caller trusts.
- The original image is encrypted into `data/vault/blobs/` and the plaintext
  deleted (`blobs.consume()` already does this).

Receipts are structurally unaffected by Phase 1.5: they get `locator: {}` and
never participate in supersession. A corrected receipt is a **duplicate**
problem — same merchant, date and total — which is a separate check, not a
version problem.

### b. VLM page transcription — *changes the OCR plan*

The vision model added for receipts is also the right layout extractor for
manuals. Render a page with `pdftoppm` (already installed, no new dependency),
ask for markdown, feed the result to the table chunker from Phase 1.5.

One mechanism fixes column scrambling, broken tables, and scanned pages with no
text layer at once.

Scope it to pages that need it. `-raw` extraction (ADR-024) fixed the column
scrambling this was first meant for, and the extractor no longer flags pages,
so the queue is now pages marked `ocr_required` plus whitespace-aligned tables,
which still need a detector.

### c. Tesseract, in a narrower role

Bulk plain-text OCR where structure does not matter and per-page VLM cost is
prohibitive. The VLM handles the pages where structure *is* the content. This
is a smaller job than originally planned, because (b) absorbed most of it.

Tooling (`tesseract`, `ocrmypdf`, `ghostscript`) goes in `Dockerfile.ingest`
**only** — never the MCP or vault images.

### d. Git and web sources

`sources.yaml` already rejects both types rather than failing obscurely.

- **Git**: clone or fetch, walk include globs, index at a pinned SHA. Line-range
  locators come free from Phase 1.5's design.
- **Web**: Crawl4AI. **Ingest image only** — Playwright pulls 1–2 GB, which has
  no business in the vault container. Anchor locators, also free.

Both use implicit versioning: re-crawling overwrites by path or URL, so
latest-wins is correct by construction and they need none of the lifecycle
machinery.

AST-aware chunking for source files stays a nice-to-have. The stated corpus is
SDK *documentation*, which is markdown, which the existing chunker serves.

### e. Duplicate detection for receipts

Distinct from supersession. Same merchant + date + total is probably the same
transaction photographed twice, and double-counting it corrupts every sum the
ledger produces. Flag, do not auto-merge.

## Phase 3 — retrieval tuning · **harness built; the rest not started**

- **Cross-encoder reranking.** Moved *behind* Phase 2's parsing work. Reranking
  cannot recover a chunk whose column headers were discarded or whose two
  columns were interleaved — see
  [ADR-011](decisions.md#adr-011-parsing-quality-before-retrieval-tuning).
- **Query expansion / HyDE**, if measurement shows recall is the bottleneck.
- **Chunk-level correction notes.** Retracting a 200-page manual because one
  torque figure is mistyped is the wrong tool. Today's mitigation is the
  verbatim-citation rule: a cited figure can be checked. A real fix needs a
  per-chunk annotation surfaced at read time, like the stale banner.
- **Evaluation harness** · *done* — `make eval` over
  `tests/retrieval/questions.yaml`. Grow the set whenever a real question comes
  back wrong; 15 questions is enough to catch a regression, not to tune on.
- **A larger embedding model.** bge-small misses synonyms ("polysynth" for
  "Polyphonic"); bge-base is the first thing to measure. It needs
  `rag rebuild-index` (384 to 768 dimensions; the fingerprint guard refuses
  the old collection until then).

## Explicitly not planned

Recording these so they are not re-litigated. Full reasoning in
[decisions.md](decisions.md).

| Rejected | Why |
|---|---|
| MySQL for the vault | No page-level encryption equivalent to SQLCipher; a server process is a second attack surface and breaks portability ([ADR-004](decisions.md#adr-004-sqlcipher-not-mysql)) |
| Encrypting Qdrant payloads for sensitive data | Protects the wrong half — the vector still inverts ([ADR-002](decisions.md#adr-002-sensitive-data-never-reaches-qdrant)) |
| An HTTP unlock endpoint | Reachable from every container on `llm-net`, including Open WebUI ([ADR-005](decisions.md#adr-005-unlock-over-a-unix-socket-only)) |
| Auto-detecting supersession | Would silently retire live financial records ([ADR-010](decisions.md#adr-010-supersession-is-never-inferred)) |
| An MCP tool for lifecycle | A model may not decide what counts as true |
| Named Docker volumes | Root-owned, need sudo to inspect, and break the one-rsync migration story |
| Ollama in this stack | `~/Dev/LLM` owns model serving; this stack joins its network |
| Scanned RPG books in the corpus | ~140 MB of scanned PDFs, days of vision inference, near-zero retrieval value. Not excluded by a pattern — they simply live outside every configured source, and should stay that way |
