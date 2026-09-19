# Local RAG + MCP Documentation Service

A self-hosted pipeline that ingests documentation (web pages, local files, git repos), creates embeddings, and exposes retrieval as an **MCP server over Streamable HTTP** — consumable by both Open WebUI and Claude (Desktop / Code) from a single shared endpoint.

## Architecture

```
  Sources                  RAG Stack (~/Dev/RAG)           Consumers
  -------                  ---------------------           ---------
  Web pages ──┐                                        ┌── Open WebUI
  Local files─┤── Ingestion worker                     │   (Admin → External Tools)
  Git repos ──┘   │ Crawl4AI / file walker / git pull  │
                  │ Chunk → Embed (Ollama)              │
                  ▼                                     │
               Qdrant ◄── MCP server (FastMCP) ────────┤
               (vectors +   http://localhost:8000/mcp   │
                metadata)                               └── Claude Desktop / Code
```

All services share `llm-net` with the existing LLM stack (Open WebUI, llama-server), so Open WebUI can reach the MCP server over Docker DNS without exposing extra ports.

## Services

| Container | Image | Purpose |
|---|---|---|
| `qdrant` | `qdrant/qdrant` | Vector database with persistent storage |
| `ollama` | `ollama/ollama` | Embedding model server (`nomic-embed-text` by default) |
| `mcp-server` | local build | FastMCP Streamable HTTP server — exposes `search_docs`, `list_sources`, `get_index_status` |
| `ingestion-worker` | local build | Crawlers + chunker + embedder + scheduler |

## Setup

### Prerequisites

The LLM stack (`~/Dev/LLM`) must be running first — it creates the `llm-net` Docker network that this stack joins.

```bash
cd ~/Dev/LLM && docker compose up -d
```

### First-time setup

**1. Copy and edit the env file:**

```bash
cp .env.example .env
```

Edit `.env` and set `LOCAL_DOCS_PATH` to the directory you want to index as local files. All other defaults are ready to use.

**2. Start the stack:**

```bash
make up
```

**3. Pull the embedding model into Ollama:**

```bash
make pull-models
```

This downloads `nomic-embed-text` (~274 MB) into the `ollama-data` volume. Only needed once.

**4. Index your sources:**

```bash
make ingest
```

Re-running is safe — unchanged chunks are skipped and nothing is duplicated.

## MCP tools

| Tool | Description |
|---|---|
| `search_docs` | Semantic search over indexed documentation. Args: `query`, `limit` (default 5), optional `source_id` to restrict to one source. |
| `list_sources` | Returns all configured sources and their last sync status. |
| `get_index_status` | Returns last successful sync time and chunk count per source. |

## Connecting Open WebUI

In Open WebUI: **Admin Settings → External Tools → MCP → Add Server**

- **URL:** `http://mcp-server:8000/mcp` (Docker DNS, since Open WebUI is on `llm-net`)

> **WEBUI_SECRET_KEY:** Set this in `~/Dev/LLM/.env` and keep it stable. If it changes, Open WebUI invalidates all saved tool credentials on the next restart. Generate with: `openssl rand -hex 32`

## Connecting Claude Desktop / Claude Code

Add to your MCP client config (e.g. `~/.claude/mcp_servers.json` or Claude Desktop `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "rag-docs": {
      "type": "streamable-http",
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

### mcpo fallback (stdio-only clients)

If your client requires stdio transport, run the server behind [mcpo](https://github.com/open-webui/mcpo):

```bash
uvx mcpo --port 8001 -- python -m app.main
```

Then point your client at `http://localhost:8001`.

## Managing sources

Sources are defined in [`sources.yaml`](sources.yaml). Each entry has a type, location, refresh cadence (cron expression), and chunking profile.

**Source types:**

```yaml
# Web page / site (Crawl4AI, JS rendering, sitemap-aware)
- id: my-docs
  type: web
  url: https://docs.example.com
  cadence: "0 */6 * * *"   # every 6 hours
  chunking:
    strategy: markdown
    chunk_size: 512
    chunk_overlap: 64

# Local files (bind-mounted from LOCAL_DOCS_PATH in .env)
- id: my-notes
  type: local
  path: /docs
  cadence: "0 * * * *"     # hourly

# Git repository (indexes only changed files on each pull)
- id: my-repo
  type: git
  url: https://github.com/org/repo
  branch: main
  paths: ["*.md", "docs/**"]
  cadence: "0 0 * * *"     # daily
```

To add a source: edit `sources.yaml`, then run `make ingest`.

## Common commands

```bash
make up              # Start all services
make down            # Stop all services
make logs            # Tail all logs
make pull-models     # Pull/update the embedding model in Ollama
make ingest          # Ingest all sources (skips unchanged chunks)
make reindex SOURCE=my-source-id   # Force reindex one source
make status          # Show last sync time and chunk count per source
make rebuild-index   # Drop and rebuild the entire Qdrant collection
```

## Changing the embedding model

The embedding model and its vector dimension are **frozen at collection creation**. Mixing models in one collection produces nonsense retrieval results.

To switch models:

1. Update `EMBED_MODEL` and `EMBED_DIM` in `.env`
2. Run `make pull-models` to download the new model
3. Run `make rebuild-index` — this drops the collection and re-embeds everything

## Logs

All containers log to journald:

```bash
journalctl -t mcp-server -f
journalctl -t qdrant -f
journalctl -t ollama -f
journalctl -t ingestion-worker -f
```
