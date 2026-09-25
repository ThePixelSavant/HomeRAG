# Command reference

Every command, both forms, with the behaviour that is not obvious from the
syntax.

## Two ways to run everything

```bash
rag list                    # from anywhere
make list                   # from ~/Dev/RAG
```

`rag` is [scripts/rag](../scripts/rag), symlinked into `~/.local/bin`. It
forwards to the Makefile, which stays the single definition of what anything
does — so the two forms cannot drift apart.

Install it once:

```bash
ln -s ~/Dev/RAG/scripts/rag ~/.local/bin/rag
```

It resolves through the symlink, so moving the repo does not break it.

### Why a script and not an alias

Both matter in practice:

- **An alias cannot fix a relative path.** `rag add manual.pdf manuals` run
  from `~/Documents` has to resolve that filename against *your* directory
  before make runs in the repo. An alias would hand make a path that means
  something else, or nothing.
- **Aliases do not exist outside interactive shells.** No cron, no systemd
  units, no scripts, no other shell. A file on `PATH` works everywhere.

### Shorthand versus pass-through

Five commands take positional arguments, because they are the ones used from
other directories:

```bash
rag add <file> <domain> [--move]
rag query <text> [--limit N] [--domain d] [--stale] [--superseded] [--dense]
rag list [--domain d] [--match x] [--all]
rag remove <uri> "<reason>"
rag restore <uri>
```

Everything else passes straight through to make, so a target added tomorrow
works here tomorrow with no change to the script:

```bash
rag status
rag ingest SOURCE=inbox:manuals
rag vault-audit N=20
```

Pass-through uses make's `NAME=value` form. A bare word there is a make
*target*, not an argument — `rag stale foo.md` would silently fail to set
`URI`. The script catches that and tells you the right form rather than
letting make answer confusingly.

`rag help` prints the shorthands plus every Makefile target with its
description.

---

## Getting documents in

### `rag add` — file one document and index it

```bash
rag add <file> <domain> [--move]
make add FILE=<path> DOMAIN=<domain> [MOVE=1]
```

Copies the file into `data/inbox/<domain>/` and runs an ingest for that source.
`--move` removes the original instead of copying.

```bash
rag add ~/Documents/pump-manual.pdf manuals
rag add scan.pdf receipts --move          # relative to your cwd
```

**The domain is always required and never guessed**, because it decides whether
the document is stored in the clear or in the encrypted vault. An unknown
domain is an error rather than a default — a typo that quietly became open-tier
would publish a plaintext vector, and a published vector cannot be
un-published.

The file may live anywhere on the host. The container only mounts `./data`,
`LOCAL_DOCS_PATH` and `CLAUDE_PROJECTS_PATH`, so `make add` binds the file's own
directory in for the life of the command — read-only, or read-write when
`MOVE=1` needs to unlink the original.

Filing into a vault domain needs the vault passphrase, which is prompted for.

### Drop-folder alternative

The directory name **is** the domain, so `add` is a convenience rather than the
only route:

```bash
cp ~/Downloads/manual.pdf ~/Dev/RAG/data/inbox/manuals/
rag ingest
```

### `rag ingest` — sync sources

```bash
rag ingest [SOURCE=id] [DOMAIN=d] [FORCE=1]
```

| Variable | Effect |
|---|---|
| `SOURCE=inbox:manuals` | One source only |
| `DOMAIN=notes` | Every source in that domain |
| `FORCE=1` | Override the sweep's 50% deletion floor |

Unchanged documents are skipped by content hash, not re-embedded. Vault-tier
sources prompt for the passphrase; with no terminal they queue and say so, and
the next interactive run takes them.

`FORCE=1` exists for one case: you genuinely deleted most of a source and the
sweep is refusing. If you did not, the refusal is correct — see
[`sweep_source`](architecture.md#deletion-two-mechanisms-both-required).

### `rag reindex` — re-embed one source from scratch

```bash
rag reindex SOURCE=<id>
```

Drops the state rows for that source and re-reads every file. Use after
changing chunking or extraction; for an embedding-model change use
`rebuild-index`.

---

## Finding things

### `rag list` — the inventory

```bash
rag list [--domain d] [--match x] [--all]
make list [DOMAIN=d] [MATCH=x] [ALL=1] [JSON=1]
```

Open tier only. Vault documents are in the encrypted database — see
`rag vault-list`.

```
  analog-lab_Manual_5_11_1_EN.pdf           manuals   118 chunks  2026-09-23
  intelliflo3-pro3-vsf-install-guide.pdf    manuals    67 chunks  2026-09-20

  19 active document(s), 567 chunks.
```

| Flag | Effect |
|---|---|
| `--domain manuals` | One domain |
| `--match TB-03` | Substring of the uri |
| `--all` | Include superseded, stale and retracted |
| `JSON=1` | Machine-readable (make form only) |

Only exceptions are annotated — a lifecycle that is not `active`, a status that
is not `indexed`, pages flagged for review. A column reading "active" on every row would
hide the one row that is not.

### `rag query` — search the open tier

```bash
rag query "<text>" [--limit N] [--domain d] [--stale] [--superseded] [--dense]
make query Q="<text>" [LIMIT=5] [DOMAINS=a,b] [STALE=1] [SUPERSEDED=1] [ARGS=--dense]
```

```bash
rag query "how do I prime the pump"
rag query "torque spec" --domain manuals --limit 3
```

Hybrid by default: dense embeddings plus BM25, fused with reciprocal rank.
Dense retrieval is systematically weak on exact rare tokens — a part number, a
function name — which is most of what gets asked of a manual. `rag query …
--dense` (make: `ARGS=--dense`) disables the keyword half, for comparison only.

Every hit carries a **citation**: `pump.pdf, page 21`, `CLAUDE.md, lines
92-102`. A chunk spanning pages cites the first, because that is where the
passage starts.

Superseded and stale documents are excluded unless asked for. Retracted ones
cannot be asked for — their vectors are gone.

This talks to Qdrant directly rather than through MCP, so it still works with
the MCP servers down. That makes it the tool for telling a retrieval problem
apart from a transport problem.

---

## When a document stops being true

Four states. Full reasoning in
[ADR-012](decisions.md#adr-012-lifecycle-is-separate-from-status) and
[ADR-013](decisions.md#adr-013-retraction-deletes-it-does-not-filter).

| State | Meaning | Retrieval |
|---|---|---|
| `active` | Default | Returned |
| `superseded` | A *named* newer document replaced it | `--superseded` |
| `stale` | Out of date, nothing replaced it | `--stale`, with a warning in the text |
| `retracted` | Wrong, or should not be here | **Never** — vectors deleted |

### `rag remove` — retract

```bash
rag remove <uri> "<reason>"
make retract URI=<uri> REASON="<reason>" [DOMAIN=d]
```

```bash
rag remove bad-spec.md "torque figure was wrong"
```

Deletes the chunks and vectors, and writes a **tombstone** in the state
database.

**The reason is required.** It is recorded on the tombstone and is what
explains the absence to whoever notices later.

**Deleting the file is not a removal.** A copy elsewhere in the tree, or a
restored backup, is re-indexed on the next run. The tombstone is what makes it
stick: it is read *before* extraction, so a retracted document costs no parsing
and has no path to the embedder, and it is excluded from the disappearance
sweep so the row that keeps it out is not itself collected.

Retraction deletes rather than filters so that "never returned" holds by
absence, not by three query paths each remembering to exclude it.

### `rag restore` — clear a flag

```bash
rag restore <uri>
make restore URI=<uri> [DOMAIN=d]
```

Returns a document to `active`. For a retracted one the vectors are gone, so it
only comes back on the next ingest:

```bash
rag restore bad-spec.md && rag ingest
```

### `make supersede` — one document replaced by another

```bash
make supersede OLD=<uri> NEW=<uri> [DOMAIN=d]
rag supersede OLD=topology-v1.md NEW=topology-v2.md
```

Links both directions. The old document leaves default search; results for it
carry `superseded_by`.

### `make stale` — out of date, nothing replaced it

```bash
make stale URI=<uri> [REASON="..."] [AFTER=YYYY-MM-DD] [DOMAIN=d]
```

```bash
rag stale URI=rack-plan.md REASON="hardware decommissioned"
rag stale URI=firmware-notes.md AFTER=2027-01-01     # schedule it
```

With `AFTER` it schedules instead of flipping: the transition happens on the
ingest run that first sees the date has passed, and is recorded and visible in
`rag lifecycle`. A human sets the date, so nothing is inferred.

Stale content is returned with a `[STALE since <date>: <reason>]` prefix added
at **read** time. It is never stored — storing it would change the embedded
text and make flipping a flag a re-embed.

### `rag lifecycle` — what is not plainly active

```bash
rag lifecycle
make lifecycle [JSON=1]
```

```
active=22  retracted=1
  retracted   intelliflo3-pro3-vsf-install-guide.pdf   (manuals)  wrong pump
```

### Versions are never inferred

Ingest points out filenames **in the same directory** that look like versions of
each other and stops there. A March statement is not a newer version of
February's, and auto-retiring it would take a live financial record out of
reach of every default query. Anything carrying a date is excluded outright.
See [ADR-010](decisions.md#adr-010-supersession-is-never-inferred).

---

## The vault

Sealed by default. Every model-initiated read passes three gates; see
[security-model.md](security-model.md).

### `rag unlock` / `rag lock`

```bash
rag unlock [TTL=900]
rag lock
```

Prompts on a terminal, derives the key with Argon2id, holds it in the vault
server's memory with a TTL. **No key is written to disk.**

The passphrase is deliberately not accepted from a flag, a file or an
environment variable. That is the mechanism that stops a model unlocking the
vault: a model has no terminal.

### `rag vault-list` — the vault inventory

```bash
rag vault-list [DOMAIN=d] [ALL=1] [JSON=1]
```

Needs an unlocked vault, because the inventory is inside the encrypted database
— listing it *is* reading it.

### `rag vault-query` — search the vault as a human

```bash
rag vault-query Q="<text>" [LIMIT=5]
```

The CLI path skips only the **approval** gate, because you are already at a
terminal. It does not skip the key: a sealed vault returns `VAULT_SEALED`.

### `rag approve` — release one model request

```bash
rag approve              # list what is pending
rag approve CODE=123456  # release one
```

A model's first vault call returns an approval code instead of data. The prompt
prints the principal, the tool, the chat, and **every argument of the call**
with the query first, then requires the word `yes` typed in full.

The `PENDING_APPROVAL` response hands the model back `retry_with` — the exact
arguments — because the binding is a hash and a caller that has to guess what
it asked a turn ago will guess wrong. A grant is single-use and scoped to one
chat; a reworded retry needs its own approval.

### `rag vault-lifecycle` — flag a vault document

```bash
rag vault-lifecycle URI=<uri> STATE=<state> [REASON="..."] [NEW=<uri>]
```

`STATE` is `active`, `stale`, `superseded` or `retracted`. Retraction here also
destroys the encrypted original, which the open-tier `retract` has no
equivalent of — there is nothing to re-ingest afterwards.

There is deliberately **no MCP tool** for any of this. Retraction decides what
the system believes is true, which is a human's call at a terminal.

### `rag vault-audit`

```bash
rag vault-audit [N=50]
```

Every access attempt — allowed, denied, or pending — with principal, tool,
transport, chat and message ids, and row count. Denials are recorded too. The
query is stored as a hash, not as text, so the audit log is not a second
unencrypted copy of what you searched for.

---

## Health and maintenance

### `rag doctor` — preflight

```bash
rag doctor
```

Reachability, paths, embedding fingerprint, per-source file counts, backlogs.
**Run this before trusting anything.** An empty source is a warning rather than
a failure, because that is also what a broken mount looks like and the
distinction matters to the sweep.

Vault state reads `not readable from here (by design)` — the control socket is
`0600` and owned by the vault container's user, while the worker now runs
unprivileged. Use `rag vault-status`.

### `rag status` — index health

```bash
rag status [JSON=1]
```

Point counts per domain, documents by status and lifecycle, pages flagged
for review, quarantine backlog, source history.

### `rag review-quarantine`

```bash
rag review-quarantine
```

Documents the sensitivity scan held back before embedding. A hit means
quarantine, **not index** — the file moves to `data/quarantine/` and nothing is
embedded until you decide:

```bash
rag add data/quarantine/statement.pdf financial
```

### `rag rebuild-index`

```bash
rag rebuild-index YES=1
```

Drops the collection and re-embeds every open-tier document. Required after
changing `EMBED_MODEL`, `EMBED_DIM` or `SCHEMA_VERSION`; the fingerprint stored
with the vectors enforces it rather than letting two embedding spaces mix
silently. **The vault is not touched.**

### Stack control

```bash
rag up            # start qdrant, mcp-server, mcp-vault
rag down
rag logs
rag build
rag warm-cache    # download and load the embedding models
rag test
```

`~/Dev/LLM` must be up first — it owns the `llm-net` and `vault-net` networks.

---

## Exit codes

`0` success · `1` the operation failed · `2` usage error (missing or
malformed arguments, file not found).

Suitable for scripting:

```bash
rag doctor || echo "preflight failed"
```

---

## Where things live

| | |
|---|---|
| Wrapper | [scripts/rag](../scripts/rag) |
| Targets | [Makefile](../Makefile) |
| Open-tier CLI | [app/ingest.py](../app/ingest.py) |
| Vault CLI | [app/vaultctl.py](../app/vaultctl.py) |

The vault CLI is a separate entry point on purpose: `app/ingest.py` imports
`qdrant_store` at module level, and the vault image deliberately does not
install `qdrant-client`. The isolation is enforced by the dependency set rather
than by convention.
