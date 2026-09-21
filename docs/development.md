# Development

How to work on this repo without breaking the things that are easy to break
silently.

## Running the tests

Inside the worker image, which is what CI-equivalent means here:

```bash
make test
```

Locally, without Docker:

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.ingest.txt
FASTEMBED_CACHE_PATH=/tmp/fecache .venv/bin/python -m pytest tests/ -q

# one test
FASTEMBED_CACHE_PATH=/tmp/fecache .venv/bin/python -m pytest \
  tests/test_security.py::test_sealed_vault_denies_model -q
```

`FASTEMBED_CACHE_PATH` must be writable. The default `/models` is the in-image
path and will fail with `PermissionError` on the host. Argon2id is capped low
in [tests/conftest.py](../tests/conftest.py) or the vault tests crawl.

86 tests: 57 pipeline, 29 security. Every security test asserts that something
is **refused**.

## Unit tests are necessary and not sufficient

The single most important habit in this repo: **run the actual stack**. Of the
bugs found so far, the majority passed every unit test.

```bash
make build && make up
make doctor          # before trusting anything
make ingest
make status
make query Q="..."
```

### Bugs the tests did not catch

Kept as a list because the pattern repeats, and it is always the boundary
between a component and its environment.

| Bug | Why tests missed it |
|---|---|
| `client.search()` removed in qdrant-client ≥ 1.12 | Tests mocked the client |
| Qdrant healthcheck used `curl` (absent from the image) against `/health` (wrong path) — `mcp-server` sat in `Created` for six weeks | Healthchecks are not unit-testable |
| `mcp` 1.x `FastMCP` → 2.x `MCPServer` rename | Import-time failure in a container that tests never start |
| `fnmatch("pump.md", "**/*")` is **False** — every inbox drop enumerated empty | Fixtures used nested paths |
| `mcp-server` crash-looped calling `state.init()` against its read-only mount | Tests write to a tmpdir |
| Vault CLI unusable in the vault container — `app/ingest.py` imports qdrant_store at module level | Tests import modules directly |
| `make test` found no tests: `tests/` and `pyproject.toml` were not copied into the image | Circular |
| doc_id collisions: `rel_uri` was the basename, so three `CLAUDE.md` collapsed into one document | Fixtures had unique names |
| `CREATE INDEX` on a new column inside `SCHEMA` | Tests always built a fresh database |
| `count_by_lifecycle` crashed a read-only reader on an unmigrated database | Tests migrate implicitly via `writer()` |
| Page classifier measured from column 0, so every indented step scored as "prose" | Synthetic fixtures were unindented |
| Version warning flagged every `CLAUDE.md` against every other — five false pairs on the first real run | Fixtures had two files, not twenty |
| `schedule_review` erased `lifecycle_reason` | No test combined a reason with a date |
| Vault-tier ingestion could never run: `sync` checked its own in-process `AGENT`, but `make unlock` unlocks the vault *server* in another container | Tests unlock in the same process, so the boundary does not exist for them |
| A grant expired 120s after creation whether or not it was approved, so approvals could not be redeemed in time | Every test approved and redeemed within milliseconds — the gate was fully covered and unusable at the same time |
| `EMPTY` counted as a failure, so three subagent transcripts reported `failed: 3` on every run | Fixtures had content; nothing exercised a document that legitimately extracts to nothing |
| An approval binds to the exact argument string, but the model rewords its search each turn, so a legitimate re-ask mints a new grant | Tests pass the same query object twice |

The last two share a shape worth naming: **a test that acts instantly cannot
catch a window that is too short for a human**, and **a test that reuses one
input cannot catch a caller that varies its input**. Both passed for months.


The common thread is that each involves a real filesystem, a real container, a
real dependency version, real data at real scale — or a real human, moving at
human speed. When you add a feature, budget time to run it against the live
stack, and if a human is in the loop, walk through it at their pace rather than
asserting the two ends in one function call.

## Things that will bite you

| Thing | Why |
|---|---|
| `mcp` major version | 1.x `FastMCP` became 2.x `MCPServer`. Pinned `<3`. |
| `qdrant-client` floor | `client.search()` was **removed** in 1.12+. Use `query_points`, read `.points`. Pinned `>=1.12,<2`. |
| Qdrant healthcheck | The image has no `curl`/`wget`/`nc`, only bash, and the endpoint is `/healthz`. Uses a `/dev/tcp` probe against `127.0.0.1`, not `localhost` (which may resolve `::1`). |
| Qdrant image digest | Snapshot restore needs a matching minor version, so `:latest` breaks migration. Pinned by digest with a rollback comment. |
| Glob matching | `fnmatch("a.md", "**/*")` is **False** — `**/` needs a literal slash. An empty `include` means everything. |
| Bind-mounted files | Docker creates a *directory* when a bind-mounted file is missing. Mount directories. |
| Fingerprint guard | Dimension alone cannot detect a model swap — bge-base, nomic-v1.5, gte-base and arctic-m are all 768-dim. |
| `git commit -m` with backticks | Shell substitution silently ate a word from a commit message. Use `-F -` with a quoted heredoc. |

## Common tasks

### Add a domain

1. Add it to `DOMAIN_TIERS` in [app/domains.py](../app/domains.py) with an
   explicit tier. There is no default — see
   [ADR-003](decisions.md#adr-003-tier-is-decided-by-domain-never-by-content).
2. `mkdir data/inbox/<domain>` if you want a drop folder. The directory name
   *is* the domain; no config edit is needed beyond step 1.
3. `make doctor` to confirm it resolves.

### Add an extractor

1. Implement the `Extractor` protocol in
   [app/pipeline/extract/base.py](../app/pipeline/extract/base.py): `matches(path)`
   and `extract(path) -> RawDoc`.
2. Set `strategy` to `TEXT`, `MARKDOWN` or `BLOCKS`. That picks the chunker.
3. Populate `extra` with anything the payload should carry. If the source has a
   meaningful position, emit a `locator` — `{"lines": [...]}`,
   `{"page": n, "pages": [...]}` or `{"anchor": "..."}` — so citations work.
4. Register it in `_REGISTRY` in
   [app/pipeline/extract/\_\_init\_\_.py](../app/pipeline/extract/__init__.py).
5. Never call fastembed. Only [embedder.py](../app/pipeline/embedder.py) does.

An extractor that raises is caught and turned into an `extract_failed` RawDoc —
one bad file must not kill a run.

### Migrations

`CREATE TABLE IF NOT EXISTS` is a **no-op** against an existing file, so a new
column added to `SCHEMA` never reaches a live database. Two rules follow:

1. **Add the column to `_ADDED_COLUMNS` as well**, in both
   [state.py](../app/pipeline/state.py) and, if it applies,
   [vault/store.py](../app/vault/store.py). `_migrate()` runs `ALTER TABLE` for
   anything missing.
2. **A `CREATE INDEX` on a new column cannot live in `SCHEMA`.** The script runs
   before the column is added and the whole thing fails. Create it inside
   `_migrate()`, which works for both fresh and existing databases.

And the constraint that makes this awkward:

3. **Readers cannot migrate.** The MCP servers mount `data/state` read-only on
   purpose. Any helper a reader calls must catch `sqlite3.OperationalError` and
   degrade — returning `{}` or `[]` — rather than crash a server that is merely
   waiting for the worker to catch up. `count_by_lifecycle` and `flagged_pages`
   are the pattern to copy.

Test a migration against a *real* old database, not a fresh one. Build a v1
schema by hand, insert a row, then open it with the new code and assert the
columns arrived and the existing row got its default.

### Change the payload schema

Bump `SCHEMA_VERSION` in [app/domains.py](../app/domains.py). It is part of the
embedding fingerprint, so the next run refuses the collection with a clear
message and `make rebuild-index` is required. At current corpus size that is
minutes, which is why payload changes should be batched and landed together.

### Add an MCP tool

- Open tier: [app/main.py](../app/main.py). Anything returning chunk text must
  respect lifecycle filtering.
- Vault: [app/vault_server.py](../app/vault_server.py). **Any tool returning
  vault content must call `_gate()` via a `service.*` function**, and needs its
  own approval — an approval for another tool must not release it. Add a test
  to [tests/test_security.py](../tests/test_security.py) asserting it fails
  closed while sealed, without identity, and without approval.
- Write the docstring for the model, not for a developer. It is the only
  instruction the model gets — which is where the "quote numeric specs
  verbatim and cite the locator" rule lives.
- **Do not add a tool that writes, deletes or unlocks.** See
  [ADR-018](decisions.md#adr-018-the-makefile-is-the-only-control-plane).

## Debugging

```bash
make doctor                       # reachability, paths, fingerprint, backlogs
make status JSON=1                # machine-readable, includes qdrant_error
make lifecycle                    # documents that are not plainly active
make review-quarantine            # what the sensitivity scan held back
make vault-audit N=50             # every vault access attempt

docker compose logs mcp-server --tail 50
docker compose logs mcp-vault --tail 50
journalctl -t mcp-server -f
```

`make query` talks to Qdrant directly rather than through MCP, so it isolates
retrieval problems from transport problems.

To probe the MCP servers over the wire, initialize a session, send
`notifications/initialized`, then call `tools/list` — the transport is
streamable-http and responses come back as SSE `data:` lines.

## Style

Match the surrounding code. Specifically:

- Comments explain **why**, and especially why the obvious alternative is
  wrong. A comment restating the code is noise; a comment saying "this looks
  redundant but removing it re-introduces bug X" is the reason the file is
  maintainable.
- Docstrings on non-obvious functions carry the reasoning. `sweep_source` and
  `point_id` are the model.
- When you fix a bug that a test would not have caught, add the test **and**
  note it in the table above.
