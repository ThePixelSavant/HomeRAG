-include .env
export

EMBED_MODEL     ?= nomic-embed-text
COLLECTION_NAME ?= docs
MCP_PORT        ?= 8000

COMPOSE = docker compose

.PHONY: up down logs pull-models ingest reindex status rebuild-index

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

# Run once after the first `make up` to download the embedding model into Ollama.
pull-models:
	$(COMPOSE) exec ollama ollama pull $(EMBED_MODEL)

# --- Ingestion targets (available after Phase 2) ---

ingest:
	$(COMPOSE) run --rm ingestion-worker python -m app.ingest ingest

# Reindex a single source by ID: make reindex SOURCE=my-source-id
reindex:
	$(COMPOSE) run --rm ingestion-worker python -m app.ingest reindex $(SOURCE)

status:
	$(COMPOSE) run --rm ingestion-worker python -m app.ingest status

# Drops the '$(COLLECTION_NAME)' Qdrant collection and re-embeds everything.
# Required when changing EMBED_MODEL or EMBED_DIM.
rebuild-index:
	$(COMPOSE) run --rm ingestion-worker python -m app.ingest rebuild-index
