# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A two-tier personal knowledge base. Open-tier documents are embedded into Qdrant
in plaintext; sensitive ones live in an encrypted, sealed-by-default SQLCipher
vault. A local LLM reaches both through MCP; ingestion is driven from the
Makefile. See [README.md](README.md) for user-facing setup.

This file is the terse list of invariants and gotchas. The reasoning behind them
lives in [docs/](docs/) — start with [docs/architecture.md](docs/architecture.md),
then [docs/status.md](docs/status.md) for what is built and
[docs/decisions.md](docs/decisions.md) for why. Before changing anything
security-related, read [docs/security-model.md](docs/security-model.md).

**Consumers**: Open WebUI (`~/Dev/LLM`) is the query plane. The Makefile is the
control plane. `make query` bypasses MCP and talks to Qdrant directly, so it
works with the servers down.

## The invariant that governs everything

**Extract → scan → route → embed, in that order.** Embedding an open-tier
document writes a plaintext vector, and inversion attacks reconstruct most of
the source text from a vector. So a misfiled sensitive document that reaches
`embed_passages` is effectively published; there is no cleaning it up after.

Consequences you must preserve when changing [app/pipeline/sync.py](app/pipeline/sync.py):

- No code path may reach the embedder before the tier is resolved.
- `tier_of()` raises on an unknown domain rather than defaulting — a typo must
  never silently become open-tier.
- Sensitive data never touches Qdrant. Encrypting a payload while leaving its
  vector searchable protects the wrong half.

## Commands

```bash
make build && make up      # llm-net must exist first: cd ~/Dev/LLM && docker compose up -d
make doctor                # run this before trusting anything
make ingest / query / list / status / add / reindex / rebuild-index
make eval                  # retrieval eval: tests/retrieval/questions.yaml
make lifecycle / supersede / stale / retract / restore
make unlock / lock / vault-list / vault-query / vault-lifecycle / approve / vault-audit
make test                  # pytest inside the worker image
```

[scripts/rag](scripts/rag) forwards all of these from any directory
(`rag list`, `rag add <file> <domain>`, `rag query "..."`). It is a script
rather than an alias because an alias cannot resolve a relative path against
the caller's cwd before make cd's into the repo, and does not exist in cron or
systemd units. **Full reference: [docs/cli.md](docs/cli.md)** — update it when
you add a target, along with the README command list.

Local dev without Docker (`.venv` is gitignored):

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.ingest.txt
FASTEMBED_CACHE_PATH=/tmp/fecache .venv/bin/python -m pytest tests/ -q
.venv/bin/python -m pytest tests/test_security.py::test_sealed_vault_denies_model -q   # single test
```

Tests need a writable `FASTEMBED_CACHE_PATH` (the default `/models` is the
in-image path). Argon2id is capped low in `conftest.py`, or the vault tests crawl.

## Architecture

| Module | Role |
|---|---|
| [app/domains.py](app/domains.py) | The single domain→tier map. Also the frozen `POINT_NAMESPACE` and `SCHEMA_VERSION`. |
| [app/documents.py](app/documents.py) | Lifecycle states and citation formatting. Dependency-free, because both tiers need it and neither may import the other. |
| [app/pipeline/embedder.py](app/pipeline/embedder.py) | **The only module that imports fastembed.** |
| [app/pipeline/classify.py](app/pipeline/classify.py) | Sensitivity scan (open tier) + redaction (vault tier). |
| [app/pipeline/sync.py](app/pipeline/sync.py) | Orchestration, tier routing, the sweep. |
| [app/pipeline/qdrant_store.py](app/pipeline/qdrant_store.py) | Open tier only. |
| [app/vault/](app/vault/) | Crypto, key agent, identity, grants, audit. Not importable from the open MCP server. |
| [app/ingest.py](app/ingest.py) | Control-plane CLI the Makefile drives. |

### Why embedder.py is the only fastembed importer

Ingest-time and query-time vectors must be produced identically or retrieval
degrades silently. Two verified fastembed behaviours are encoded there and are
easy to get wrong:

- Dense `query_embed`/`passage_embed` **do not apply prefixes**. In
  `text_embedding_base.py` both delegate to `embed`; only `JinaEmbeddingV3`
  overrides them. So prefixing is ours. `bge-*-v1.5` needs none, but the table
  stays so a model swap is one line.
- Sparse BM25 `query_embed` **is** genuinely different from `embed` — weights of
  1.0 versus term frequency. Queries must use it.

Neither mistake raises. Do not call fastembed directly elsewhere.

### Chunking

Token-based, using the model's own tokenizer, via a **truncation-free clone** —
fastembed's instance truncates at 512, so counting with it would cap every
measurement and hide the rest of a document. Tokenize once and slice by token
index; the naive recursive splitter is O(n²) and takes minutes on a large PDF.

Markdown prepends its heading breadcrumb to the embedded text and charges it to
the token budget. A **split markdown table repeats its header** on every
continuation chunk, charged the same way — otherwise chunk 2 of a torque table
is bare numbers with nothing saying which column is torque.

`chunk_blocks` **accumulates** page numbers across a chunk rather than letting
the last block win. A chunk built from pages 11 and 12 that cited only 12 sends
the reader past the answer.

**PDF sections get their own chunks.** The extractor cuts each page at its
headings, found by **font size** via `pdftohtml -xml` (as text a heading is
indistinguishable from a table cell). `chunk_blocks` starts a chunk at every
`section_start` block unless what is pending is under `SECTION_MIN_TOKENS`
(40 — a bare chapter title), and prefixes `title > heading path`. Blocks
without `heading_path` (transcript turns) pack exactly as before. Tune against
`make eval`, not intuition: 120 looked safer and dropped hit@3 from 79% to
57%. ADR-025.

### PDF extraction: `-raw`, re-spaced from reading order

Every page is `pdftotext -raw`: text in the order the PDF stores it. That keeps
columns whole, numbered steps in sequence and most table rows on one line.
`-layout` interleaves the columns of any multi-column page, and default reading
order scrambles dense ones (Roland's A3 sheets split the POLY row from its
table). The old whitespace classifier choosing between those two was retired
after `-raw` beat both on the whole corpus — see ADR-024.

`-raw`'s one defect is **glued words** in letter-spaced text
(`INJURYORDEATH.THISPUMP`). Reading order has the same characters with the
spaces restored, so each page is still extracted twice and `_respace` splits a
raw token back into reading-order words — only when reading order never
produced the token itself, and in the fewest pieces.

Do not reintroduce a per-page choice of mode: it was measured, and no
threshold separates the glued pages from good ones. A PDF whose *stored* order
is scrambled would defeat this; none seen yet, and the Phase 2 VLM pass is the
answer if one appears. The extractor no longer sets `flagged_pages`; the column
and `make status` line remain for whatever flags pages next.

### Citations

Payloads carry a generic `locator` (`{"page": 12, "pages": [12,13]}`,
`{"lines": [40,58]}`, `{"anchor": ...}`), not a page field, so git and web
sources need no second rebuild. `search()` returns it plus a prebuilt
`citation`. Both MCP servers' tool docstrings tell the model to quote numeric
specs verbatim and cite — ugly-but-checkable beats fluent-but-wrong for a
torque figure.

The MCP servers do **not** send that full row: `documents.for_model` cuts it
to content, citation, `doc_id`/`chunk_index` and score, plus lifecycle only
when it isn't `active`. Every tool-result token is prefilled on a CPU-only
llama-server at ~70 tokens/s, and the dropped fields were 37% of a search
result. `search_docs` defaults to 3 hits; `search_vault` keeps 5 because a
follow-up costs the owner another approval. The open `fetch_context` caps its
window at 2 either side. The CLI still prints everything.

### Document lifecycle

`documents.lifecycle` is **separate from `documents.status`**. `status` is
whether the pipeline could read the file (`indexed`, `ocr_required`, …);
`lifecycle` is whether its contents should still be believed (`active`,
`superseded`, `stale`, `retracted`). One column cannot answer both.

- `superseded` and `stale` are filtered out by default and opt-in-able. A stale
  hit's warning banner is added **at read time** in `_hit`, never stored —
  storing it would change the embedded text and make flipping a flag a re-embed.
- `retracted` has **no opt-in**: its points are deleted, so "never returned"
  holds by absence rather than by a filter three query paths must remember.

The file is still on disk, so a flag has to survive the next run:

- `sync_source` reads tombstones **before** `extract.extract(path)`, so a
  retracted document costs no parsing and has no path to the embedder.
- `documents_missing_run` **excludes tombstones**. A retracted document never
  gets the run marker, so collecting it would delete the row that keeps it out
  and the next run would re-index it.
- Re-indexing preserves the existing lifecycle: editing a superseded file must
  not quietly make it current.
- `schedule_review` sets only the date. Reusing `set_lifecycle` for it
  overwrote `lifecycle_reason` with `None` and erased why a document was
  flagged.

**Supersession and staleness are never inferred.** Ingest names likely pairs
and stops. Candidates must share a directory and carry no date — matching bare
filenames flags every `CLAUDE.md` in a tree against every other, and a March
statement is not a newer version of February's.

### Schema migrations

`CREATE TABLE IF NOT EXISTS` is a no-op against an existing file, so a new
column in `SCHEMA` never reaches a live database — `_ADDED_COLUMNS` +
`_migrate()` do. Two consequences:

- A `CREATE INDEX` on a new column **cannot live in `SCHEMA`**: the script runs
  before the column is added and the whole thing fails. Create it in `_migrate`.
- **Readers cannot migrate** — the MCP servers mount `data/state` read-only —
  so reader-side helpers (`count_by_lifecycle`, `flagged_pages`) catch
  `OperationalError` and return empty rather than crashing a server that is
  waiting on the worker.

### Deletion — two mechanisms, both required

- **Shrink** (10 chunks → 6): `delete_doc_tail` runs unconditionally after every
  upsert. A no-op when nothing shrank, and it repairs prior partial failures.
- **Disappearance**: every run stamps `last_seen_run`; unchanged docs get it
  refreshed without re-embedding, then `sweep_source` deletes what the run did
  not touch.

`sweep_source` is **the most dangerous function here**. It refuses without
`enumeration_complete`, and refuses to delete more than 50% of a source without
`--force`, because a source that enumerates to zero (bad mount, unreadable path,
`sources.yaml` typo) is indistinguishable from one that genuinely lost its
files. Do not relax either guard.

### The vault

Three gates in [app/vault/service.py](app/vault/service.py): unlocked → verified
identity → per-request approval bound to `(subject, chat_id,
query_hash)`, single-use. CLI skips only gate 3. **Claude Code gets no
exemption** — it is a model.

**Vault extraction must never run in a process holding the key.**
`app/pipeline/parse_worker.py` parses, redacts, chunks and embeds with no key;
`sync.py` holds the key and writes. Do not import `sync` from `parse_worker`
(it pulls in `keyagent`), do not switch the transport to `multiprocessing`
(fork inherits the key) or to `pickle` (the child is the untrusted half). The
shared chunking helper lives at `extract.base.chunks_for` for exactly this
reason. `make ingest` prompts for the passphrase; it is never read from a
flag, a file or the environment.

`fetch_context` runs the same three gates as `search`, and an approval for one
does not release the other. Otherwise a whole document could be walked out one
neighbour at a time on the strength of a single approved search. There is
deliberately **no MCP tool for lifecycle**: retraction decides what the system
believes, which is a human's call at a terminal.

The key lives only in the vault server's memory with a TTL. `make unlock` reaches
that specific process over a **Unix socket** ([app/vault/control.py](app/vault/control.py)),
not HTTP — an unlock endpoint here would be reachable from every container on
`llm-net`, including Open WebUI, handing a model a way to try passphrases. A
one-shot container cannot unlock the vault: it would derive a key into its own
memory and exit.

Identity comes from Open WebUI's **signed HS256 assertion**, not the plaintext
`X-OpenWebUI-User-*` headers, which anything reaching the port could set. There
is no model identifier in any forwarded header, so keeping vault tools off
frontier presets is configuration, and the approval prompt is the backstop.

## Gotchas that have already bitten

| Thing | Why |
|---|---|
| `mcp` major version | 1.x `FastMCP` became 2.x `MCPServer`. Pinned `<3`. |
| `qdrant-client` floor | `client.search()` was **removed** in 1.12+. Use `query_points`, read `.points`. Pinned `>=1.12,<2`. |
| Qdrant healthcheck | The image has no `curl`/`wget`/`nc`, only bash, and the endpoint is `/healthz`. Uses a `/dev/tcp` probe against `127.0.0.1` (not `localhost`, which may resolve `::1`). |
| Qdrant image digest | Snapshot restore needs a matching minor version, so `:latest` breaks migration. Pinned by digest with a rollback comment. |
| Glob matching | `fnmatch("a.md", "**/*")` is **False** — `**/` needs a literal slash. `_matches` handles the prefix; an empty `include` means everything. |
| Bind-mounted files | Docker creates a *directory* when a bind-mounted file is missing. Mount directories: `data/state/` and `data/vault/` are separate so `mcp-server` gets state read-only and no vault mount at all. |
| `FORCE=1` does not re-embed | It only overrides the sweep's deletion floor. A document re-embeds when its **extracted text** changes, so a chunking-only change reaches the index through `rag reindex`, which blanks the stored hashes (never deletes rows: that dropped the retracted tombstone and every lifecycle flag). |
| Fingerprint guard | Dimension alone cannot detect a model swap — bge-base, nomic-v1.5, gte-base and arctic-m are all 768-dim. |
| `rag.db` journal mode | **Not WAL.** A WAL file opens read-only only if its `-wal`/`-shm` exist or can be created; the worker deletes them on exit and the readers' `:ro` mount can't recreate them, so every MCP state read failed. `_connect` sets `DELETE` on each write open, which also converts an old file. |

## Storage layout

Everything under `data/` as bind mounts, so the stack is one rsync-able tree
(named volumes are root-owned and need sudo even to inspect):

```
data/inbox/<domain>/   the drop folder; directory name IS the domain
data/quarantine/       held back by the sensitivity scan
data/state/rag.db      SQLite (rollback journal): runs, sources, documents, quarantine
data/vault/vault.db    SQLCipher: rows, chunk text, vectors, ledger, audit
data/vault/blobs/      AES-256-GCM originals
data/qdrant/           open-tier index (derived; rebuildable)
```

Qdrant is a **derived index, not the source of truth** — deterministic point IDs
mean `make rebuild-index` reconstructs it exactly. Snapshots are an optimisation
over that.

## Phase 2 (not yet built)

VLM receipt extraction + `ledger` + `query_ledger`; git/web sources with Crawl4AI
(**ingest image only** — Playwright is 1-2 GB); tesseract for bulk scanned OCR.
`sources.yaml` rejects `git`/`web` types until then rather than failing obscurely.

The vision model added for receipts is also a layout extractor for manuals:
render a page with `pdftoppm` (already installed), ask for markdown, and the
table-aware chunker handles the result. `-raw` removed most of its original
work queue; what is left is pages with no text layer and whitespace-aligned
tables, and it needs a new trigger since the extractor no longer flags pages. Tesseract keeps the narrower job — bulk
plain-text OCR where structure is not the content.
