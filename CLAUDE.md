# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A two-tier personal knowledge base. Open-tier documents are embedded into Qdrant
in plaintext; sensitive ones live in an encrypted, sealed-by-default SQLCipher
vault. A local LLM reaches both through MCP; ingestion is driven from the
Makefile. See [README.md](README.md) for user-facing setup.

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
make ingest / query / status / add / reindex / rebuild-index
make unlock / lock / vault-query / approve / vault-audit
make test                  # pytest inside the worker image
```

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
the token budget.

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
identity → per-request approval bound to `(subject, chat_id, message_id,
query_hash)`, single-use. CLI skips only gate 3. **Claude Code gets no
exemption** — it is a model.

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
| Fingerprint guard | Dimension alone cannot detect a model swap — bge-base, nomic-v1.5, gte-base and arctic-m are all 768-dim. |

## Storage layout

Everything under `data/` as bind mounts, so the stack is one rsync-able tree
(named volumes are root-owned and need sudo even to inspect):

```
data/inbox/<domain>/   the drop folder; directory name IS the domain
data/quarantine/       held back by the sensitivity scan
data/state/rag.db      SQLite WAL: runs, sources, documents, quarantine
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
