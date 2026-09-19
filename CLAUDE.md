# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A self-hosted RAG stack that ingests documentation (web / local files / git repos), embeds it via Ollama, stores vectors in Qdrant, and exposes retrieval as an **MCP server over Streamable HTTP** at `/mcp`. Both Open WebUI and Claude clients consume the same endpoint. See [README.md](README.md) for user-facing setup and source-manifest syntax.

## Build state — read this first

The repo is **partially implemented**. Only the MCP server skeleton and the Qdrant store exist.

- The three MCP tools in [app/main.py](app/main.py) are stubs returning empty results, each marked with the phase that should implement it (`# Phase 2:`, `# Phase 5:`).
- There is **no `app/ingest.py` and no `ingestion-worker` service in [docker-compose.yml](docker-compose.yml)**, yet `make ingest`, `make reindex`, `make status`, and `make rebuild-index` all shell out to `docker compose run --rm ingestion-worker python -m app.ingest ...`. Those targets fail today; the README documents the intended end state, not current behaviour.
- [tests/](tests/) is empty and no test runner is declared in [requirements.mcp.txt](requirements.mcp.txt). There is no lint config. Adding any of these means choosing the tooling, not discovering it.

When implementing ingestion, the contract already fixed by [app/pipeline/qdrant_store.py](app/pipeline/qdrant_store.py) is: single unnamed vector, cosine distance, and a payload carrying `content`, `source_url`, `source_id`, `source_type`, `last_indexed`. `source_id` and `source_type` have keyword payload indexes created at collection init — filterable fields beyond those need new indexes.

## Commands

Everything runs in Docker; there is no local virtualenv.

```bash
cd ~/Dev/LLM && docker compose up -d   # REQUIRED FIRST — creates the external llm-net network
make up                                 # start qdrant, ollama, mcp-server
make pull-models                        # one-time: pull EMBED_MODEL into the ollama volume
make logs                               # tail all containers
make down
```

Per-container logs go to journald, not compose: `journalctl -t mcp-server -f` (also `qdrant`, `ollama`, `ingestion-worker`).

After editing anything under `app/`, `make up` alone will not pick it up — the image bakes the code in ([Dockerfile.mcp](Dockerfile.mcp) has no bind mount). Rebuild with `docker compose up -d --build mcp-server`.

## Configuration

[app/config.py](app/config.py) uses pydantic-settings, so every field maps to the uppercased env var (`embed_dim` ← `EMBED_DIM`) and unknown keys in `.env` are ignored. Defaults in that file assume **Docker DNS** (`http://qdrant:6333`, `http://ollama:11434`), and docker-compose re-sets `QDRANT_URL`/`OLLAMA_URL` in `environment:` specifically to win over anything in `.env` — so a host-oriented `.env` value stays correct for host-side scripts without breaking the container.

`MCP_PORT` only controls the host side of the port mapping (bound to `127.0.0.1`); inside the container the server always listens on 8000.

## The embedding-dimension invariant

`EMBED_MODEL` / `EMBED_DIM` are frozen at collection creation. `init_collection()` runs on every MCP server start and **raises on mismatch** (also on a collection using named vectors), so a dimension change turns into a startup crash rather than silent retrieval corruption. Changing the model is a three-step operation: edit `.env`, `make pull-models`, `make rebuild-index`. Never work around the check by relaxing the comparison — the rebuild is the intended path.

## Sources

[sources.yaml](sources.yaml) is the single source of truth for what gets indexed: each entry has `id`, `type` (`web` | `local` | `git`), a location, a cron `cadence`, and a `chunking` profile. Nothing reads this file yet — `list_sources` and the ingestion scheduler are both unimplemented. `local` sources reference `/docs`, which is the in-container mount of the host `LOCAL_DOCS_PATH`.
