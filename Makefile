-include .env
export

COMPOSE    = docker compose
WORKER     = $(COMPOSE) run --rm ingestion-worker
# Vault control commands must reach the RUNNING vault server, because that is
# the process holding the key in memory. A one-shot container would derive a
# key into its own memory and then exit.
VAULTEXEC  = docker exec -it mcp-vault python -m app.ingest

.PHONY: up down logs build doctor warm-cache ingest reindex add query status \
        rebuild-index review-quarantine unlock lock vault-status vault-query \
        approve vault-audit test

## --- stack ---------------------------------------------------------------

up:                     ## Start qdrant, mcp-server and mcp-vault
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

build:
	$(COMPOSE) --profile tools build

logs:
	$(COMPOSE) logs -f

## --- open tier -----------------------------------------------------------

doctor:                 ## Preflight: reachability, paths, fingerprint, backlogs
	$(WORKER) doctor

warm-cache:             ## Download and load the embedding models
	$(WORKER) warm-cache

ingest:                 ## Sync all sources (SOURCE=id DOMAIN=d FORCE=1 to narrow)
	$(WORKER) ingest $(if $(SOURCE),--source $(SOURCE)) $(if $(DOMAIN),--domain $(DOMAIN)) $(if $(FORCE),--force)

reindex:                ## Force a full re-embed of one source: make reindex SOURCE=id
	@test -n "$(SOURCE)" || { echo "usage: make reindex SOURCE=<source-id>"; exit 2; }
	$(WORKER) reindex $(SOURCE)

add:                    ## File a document and ingest it: make add FILE=x.pdf DOMAIN=manuals
	@test -n "$(FILE)" -a -n "$(DOMAIN)" || { echo "usage: make add FILE=<path> DOMAIN=<domain>"; exit 2; }
	$(WORKER) add "$(FILE)" --domain $(DOMAIN) $(if $(MOVE),--move)

query:                  ## Search the open tier: make query Q="priming the pump"
	@test -n "$(Q)" || { echo 'usage: make query Q="..."'; exit 2; }
	$(WORKER) query "$(Q)" $(if $(DOMAINS),--domains $(DOMAINS)) $(if $(LIMIT),--limit $(LIMIT)) $(ARGS)

status:                 ## Index health, per-domain counts, vault state, backlogs
	$(WORKER) status $(if $(JSON),--json)

review-quarantine:      ## Documents the sensitivity scan held back from indexing
	$(WORKER) review-quarantine

# Drops the collection and re-embeds every open-tier document. Required after
# changing EMBED_MODEL or EMBED_DIM. The vault is NOT touched.
rebuild-index:
	$(WORKER) rebuild-index $(if $(YES),--yes)

## --- vault ---------------------------------------------------------------
##
## The passphrase is only ever read from a terminal. It is deliberately not
## accepted from a flag, a file or an environment variable -- that is the
## mechanism that stops a model from unlocking the vault on its own.

unlock:                 ## Unlock the vault (prompts; key lives in memory with a TTL)
	@$(VAULTEXEC) unlock $(if $(TTL),--ttl $(TTL))

lock:                   ## Seal the vault now and wipe the key
	@docker exec mcp-vault python -m app.ingest lock

vault-status:
	@docker exec mcp-vault python -m app.ingest vault-status

vault-query:            ## Search the vault as a human: make vault-query Q="..."
	@test -n "$(Q)" || { echo 'usage: make vault-query Q="..."'; exit 2; }
	@docker exec mcp-vault python -m app.ingest vault-query "$(Q)" $(if $(LIMIT),--limit $(LIMIT))

approve:                ## Release one pending request: make approve CODE=123456
	@$(VAULTEXEC) approve $(if $(CODE),--code $(CODE))

vault-audit:            ## Every access attempt, allowed or denied
	@docker exec mcp-vault python -m app.ingest vault-audit $(if $(N),--limit $(N))

## --- development ---------------------------------------------------------

test:                   ## Run the test suite inside the worker image
	$(COMPOSE) run --rm --entrypoint pytest ingestion-worker -q
