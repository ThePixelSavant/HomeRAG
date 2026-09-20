# Architecture

A two-tier personal knowledge base. Open-tier documents are embedded into
Qdrant in plaintext; sensitive ones live in an encrypted, sealed-by-default
SQLCipher vault. A local LLM reaches both through MCP. Ingestion is driven from
the Makefile.

## The shape of it

```
                    ┌──────────────────────────────────────────┐
   you ──▶ Open WebUI (~/Dev/LLM) ──▶ local LLM                │
                    │                    │                      │
                    │                    ├─▶ mcp-server  :8000 ─┼─▶ Qdrant
                    │                    │   search_docs        │   (plaintext)
                    │                    │   fetch_context      │
                    │                    │   list_sources       │
                    │                    │   get_index_status   │
                    │                    │                      │
                    │                    └─▶ mcp-vault   :8001 ─┼─▶ vault.db
                    └──────────────────────┐ search_vault       │   (SQLCipher)
                                           │ fetch_context      │   + blobs/
   you ──▶ make ingest ──▶ ingestion-worker│ query_ledger       │
                              │            │ vault_status       │
                              │            └────────────────────┘
                              ├─▶ Qdrant (write)                  ▲
                              ├─▶ data/state/rag.db (write)       │
                              └─▶ vault.db (write, needs unlock) ─┘

   make unlock ──▶ unix socket ──▶ mcp-vault (the process holding the key)
```

Three containers plus a one-shot worker. `llm-net` is an external network owned
by `~/Dev/LLM`; this stack joins it rather than creating it.

## Control plane and query plane

They are deliberately separate.

- **Control plane**: the Makefile → `ingestion-worker`. Ingest, reindex, flag,
  retract, rebuild. Runs as a one-shot container, writes to everything.
- **Query plane**: Open WebUI → the local model → MCP. Reads only.

`make query` bypasses MCP and talks to Qdrant directly, so it works with the
servers down. That is a debugging path, not the intended interface.

A model has no route into the control plane. There is no MCP tool that ingests,
deletes, retracts or unlocks.

## Module map

| Module | Role |
|---|---|
| [app/domains.py](../app/domains.py) | The domain→tier map. Also the frozen `POINT_NAMESPACE` and `SCHEMA_VERSION`. |
| [app/documents.py](../app/documents.py) | Lifecycle states and citation formatting. Dependency-free — both tiers need it and neither may import the other. |
| [app/config.py](../app/config.py) | Settings from the environment. |
| [app/sources.py](../app/sources.py) | The source manifest plus implicit inbox sources. |
| [app/pipeline/embedder.py](../app/pipeline/embedder.py) | **The only module that imports fastembed.** |
| [app/pipeline/chunker.py](../app/pipeline/chunker.py) | Token-accurate chunking, table headers, char offsets. |
| [app/pipeline/extract/](../app/pipeline/extract/) | Per-type extractors: text/markdown, PDF, Claude Code transcripts. |
| [app/pipeline/classify.py](../app/pipeline/classify.py) | Sensitivity scan (open tier) and redaction (vault tier). |
| [app/pipeline/sync.py](../app/pipeline/sync.py) | Orchestration, tier routing, the sweep. |
| [app/pipeline/qdrant_store.py](../app/pipeline/qdrant_store.py) | Open tier only. |
| [app/pipeline/state.py](../app/pipeline/state.py) | Operational state: runs, sources, documents, quarantine. |
| [app/vault/](../app/vault/) | Crypto, key agent, identity, grants, audit, store, service, control socket. |
| [app/ingest.py](../app/ingest.py) | Control-plane CLI. |
| [app/vaultctl.py](../app/vaultctl.py) | Vault CLI. Separate so the vault image needs no qdrant-client. |
| [app/main.py](../app/main.py) | Open-tier MCP server. |
| [app/vault_server.py](../app/vault_server.py) | Vault MCP server. |

### Why `app/ingest.py` and `app/vaultctl.py` are separate

`ingest.py` imports `qdrant_store` at module level. The vault image
deliberately does not install `qdrant-client`, keeping the open tier's
dependencies out of the container that holds the key. So vault commands get
their own entry point that imports nothing from the open tier, and the
isolation is enforced by the dependency set rather than by convention.

### Why `embedder.py` is the only fastembed importer

Ingest-time and query-time vectors must be produced identically or retrieval
degrades silently — no error, just worse answers. Two verified fastembed
behaviours are encoded there and are easy to get wrong:

- Dense `query_embed`/`passage_embed` **do not apply prefixes**. In
  `text_embedding_base.py` both delegate to `embed`; only `JinaEmbeddingV3`
  overrides them. So prefixing is ours. `bge-*-v1.5` needs none, but the table
  stays so a model swap is one line.
- Sparse BM25 `query_embed` **is** genuinely different from `embed` — weights
  of 1.0 versus term frequency. Queries must use it.

Neither mistake raises.

## How a document becomes searchable

```
  file on disk
       │
       ▼
  enumerate_files()          glob include/exclude, report completeness
       │
       ▼
  tombstone check            retracted? skip here, before any parsing
       │
       ▼
  extract.extract()          → RawDoc(text | blocks, strategy, extra)
       │
       ▼
  classify.scan()            open tier only; a hit means QUARANTINE, not index
       │
       ▼
  tier_of(domain)            raises on unknown; never defaults
       │
       ├──── open ────▶ chunk → embed → Qdrant upsert → delete_doc_tail
       │
       └──── vault ───▶ redact → chunk → embed → SQLCipher → blob the original
```

The tombstone check sits above `extract` on purpose: a retracted document costs
no parsing, and more importantly there is then no code path from it to the
embedder at all.

### Extraction strategies

`RawDoc.strategy` picks the chunker:

| Strategy | Source | Chunker | Locator produced |
|---|---|---|---|
| `MARKDOWN` | `.md` | `chunk_markdown` — heading breadcrumb prepended and charged to the budget | `{"lines": [40, 58]}` |
| `TEXT` | `.txt`, anything plain | `chunk_text` | `{"lines": [...]}` |
| `BLOCKS` | PDF pages, transcript turns | `chunk_blocks` — packs atomic units, never splits one unless it must | `{"page": 12, "pages": [12, 13]}` |

### PDF extraction is done twice per page

`pdftotext -layout` preserves the visual grid, which keeps a table row or a
numbered step on one line. On a two-column page it interleaves the columns line
by line, splicing a safety warning into the middle of unrelated body text.
Dropping `-layout` gives reading order, which fixes the columns and breaks the
rows. A manual is both, so each page is extracted both ways and classified.

`_classify_page` measures the median width of the text left of the first 3+
space gutter, **measured from the first non-space character** — an indented
step (`   1    Turn on...`) has its first gutter at column 0, and counting that
scores the line as having nothing on the left.

| Median left width | Verdict | Extraction kept |
|---|---|---|
| ≤ 20 | rows | `-layout` |
| ≥ 40 | columns | reading order |
| between | **ambiguous** | reading order, and the page is recorded |
| no gutters at all | prose | reading order |

Ambiguous pages are recorded in `flagged_pages`, never guessed silently. That
list is the Phase 2 vision pass's work queue and `make status` reports the
count. See [decisions.md](decisions.md#adr-008-pdf-pages-are-extracted-twice).

## Deletion: two mechanisms, both required

Qdrant has no cascade and no foreign keys, so a document's stale chunks have to
be removed explicitly. Two different failures need two different mechanisms.

- **Shrink** (10 chunks → 6). `delete_doc_tail` runs unconditionally after
  every upsert, removing chunks at index ≥ the new count. A no-op when nothing
  shrank, and it repairs a prior partial failure.
- **Disappearance** (the file is gone). Every run stamps `last_seen_run` on
  everything it touched; unchanged documents get it refreshed without
  re-embedding. `sweep_source` then deletes what the run did not touch.

`sweep_source` is **the most dangerous function in the repo**. It refuses
without `enumeration_complete`, and refuses to delete more than 50% of a source
without `--force`, because a source that enumerates to zero — a bad mount, an
unreadable path, a typo in `sources.yaml` — is indistinguishable from one that
genuinely lost its files. Do not relax either guard.

Tombstoned documents are excluded from the disappearance sweep. They never get
the run marker (they are skipped before extraction), so collecting them would
delete the row that keeps them out.

## Storage layout

Everything under `data/` as bind mounts, so the stack is one rsync-able tree.
Named volumes are root-owned and need sudo even to inspect, which makes moving
the stack to new hardware unnecessarily painful.

```
data/inbox/<domain>/   the drop folder; directory name IS the domain
data/quarantine/       held back by the sensitivity scan
data/state/rag.db      SQLite WAL: runs, sources, documents, quarantine
data/vault/vault.db    SQLCipher: rows, chunk text, vectors, ledger, audit
data/vault/blobs/      AES-256-GCM originals
data/qdrant/           open-tier index (derived; rebuildable)
```

Qdrant is a **derived index, not the source of truth**. Point IDs are
`uuid5(POINT_NAMESPACE, "source_id|doc_id|chunk_index")` — deterministic and
positional — so `make rebuild-index` reconstructs it exactly. Snapshots are an
optimisation over that, not the recovery plan.

Point IDs are deliberately **not** a hash of chunk content: that makes a
document's stale chunks unenumerable, and collides whenever two documents share
a boilerplate paragraph.

## Container boundaries

| Container | Qdrant | state.db | vault.db | Key in memory |
|---|---|---|---|---|
| `ingestion-worker` | read-write | read-write | read-write (if unlocked) | no |
| `mcp-server` | read | **read-only mount** | **not mounted** | no |
| `mcp-vault` | not installed | read-only mount | read-write | **yes** |

`mcp-server` has no vault mount at all — a compromise there cannot reach
`vault.db` regardless of what the process does. The read-only mount on
`data/state` is why reader-side helpers must degrade rather than migrate; see
[development.md](development.md#migrations).

`data/state/` and `data/vault/` are separate directories precisely so these
mounts can differ. Mount directories, never files: Docker creates a *directory*
when a bind-mounted file is missing, and you get a confusing failure later.
