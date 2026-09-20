-include .env
export

COMPOSE    = docker compose
WORKER     = $(COMPOSE) run --rm ingestion-worker
# Vault control commands must reach the RUNNING vault server, because that is
# the process holding the key in memory. A one-shot container would derive a
# key into its own memory and then exit.
VAULTEXEC  = docker exec -it mcp-vault python -m app.vaultctl
VAULTRUN   = docker exec mcp-vault python -m app.vaultctl

.PHONY: up down logs build doctor warm-cache ingest reindex add query status \
        rebuild-index review-quarantine supersede stale retract restore lifecycle \
        unlock lock vault-status vault-query vault-lifecycle \
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
	$(WORKER) query "$(Q)" $(if $(DOMAINS),--domains $(DOMAINS)) $(if $(LIMIT),--limit $(LIMIT)) \
	  $(if $(SUPERSEDED),--include-superseded) $(if $(STALE),--include-stale) $(ARGS)

status:                 ## Index health, per-domain counts, vault state, backlogs
	$(WORKER) status $(if $(JSON),--json)

review-quarantine:      ## Documents the sensitivity scan held back from indexing
	$(WORKER) review-quarantine

# Drops the collection and re-embeds every open-tier document. Required after
# changing EMBED_MODEL or EMBED_DIM. The vault is NOT touched.
rebuild-index:
	$(WORKER) rebuild-index $(if $(YES),--yes)

## --- document lifecycle --------------------------------------------------
##
## Four states: active, superseded (replaced by a named document), stale (out
## of date, no replacement) and retracted (wrong, or unwanted here).
##
## Supersession is never inferred. Ingest names likely pairs and stops there,
## because periodic documents are not versions of each other -- a March
## statement does not retire February's, and auto-detecting would silently
## retire live financial records.

lifecycle:              ## Documents that are not plainly active
	$(WORKER) lifecycle $(if $(JSON),--json)

supersede:              ## make supersede OLD=<uri> NEW=<uri> [DOMAIN=d]
	@test -n "$(OLD)" -a -n "$(NEW)" || { echo "usage: make supersede OLD=<uri> NEW=<uri>"; exit 2; }
	$(WORKER) supersede "$(OLD)" "$(NEW)" $(if $(DOMAIN),--domain $(DOMAIN))

stale:                  ## make stale URI=<uri> [AFTER=YYYY-MM-DD] [REASON="..."]
	@test -n "$(URI)" || { echo 'usage: make stale URI=<uri> [AFTER=<date>] [REASON="..."]'; exit 2; }
	$(WORKER) stale "$(URI)" $(if $(AFTER),--after $(AFTER)) $(if $(REASON),--reason "$(REASON)") \
	  $(if $(DOMAIN),--domain $(DOMAIN))

# Deletes the vectors and leaves a tombstone. Deleting the file alone is not a
# removal: a copy elsewhere in the tree, or a restored backup, is re-indexed on
# the next run. The tombstone is what makes it stick.
retract:                ## make retract URI=<uri> REASON="..."
	@test -n "$(URI)" -a -n "$(REASON)" || { echo 'usage: make retract URI=<uri> REASON="..."'; exit 2; }
	$(WORKER) retract "$(URI)" --reason "$(REASON)" $(if $(DOMAIN),--domain $(DOMAIN))

restore:                ## Clear a flag: make restore URI=<uri>  (then make ingest)
	@test -n "$(URI)" || { echo "usage: make restore URI=<uri>"; exit 2; }
	$(WORKER) restore "$(URI)" $(if $(DOMAIN),--domain $(DOMAIN))

## --- vault ---------------------------------------------------------------
##
## The passphrase is only ever read from a terminal. It is deliberately not
## accepted from a flag, a file or an environment variable -- that is the
## mechanism that stops a model from unlocking the vault on its own.

unlock:                 ## Unlock the vault (prompts; key lives in memory with a TTL)
	@$(VAULTEXEC) unlock $(if $(TTL),--ttl $(TTL))

lock:                   ## Seal the vault now and wipe the key
	@$(VAULTRUN) lock

vault-status:
	@$(VAULTRUN) status

vault-query:            ## Search the vault as a human: make vault-query Q="..."
	@test -n "$(Q)" || { echo 'usage: make vault-query Q="..."'; exit 2; }
	@$(VAULTRUN) query "$(Q)" $(if $(LIMIT),--limit $(LIMIT))

# Same four states as the open tier. Retraction here also destroys the
# encrypted original, which `make retract` has no equivalent of.
vault-lifecycle:        ## make vault-lifecycle URI=<uri> STATE=retracted REASON="..."
	@test -n "$(URI)" -a -n "$(STATE)" || { echo 'usage: make vault-lifecycle URI=<uri> STATE=<state> [REASON="..."]'; exit 2; }
	@$(VAULTRUN) lifecycle "$(URI)" $(STATE) $(if $(REASON),--reason "$(REASON)") \
	  $(if $(NEW),--superseded-by "$(NEW)") $(if $(DOMAIN),--domain $(DOMAIN))

approve:                ## Release one pending request: make approve CODE=123456
	@$(VAULTEXEC) approve $(if $(CODE),--code $(CODE))

vault-audit:            ## Every access attempt, allowed or denied
	@$(VAULTRUN) audit $(if $(N),--limit $(N))

## --- development ---------------------------------------------------------

test:                   ## Run the test suite inside the worker image
	$(COMPOSE) run --rm --entrypoint pytest ingestion-worker -q
