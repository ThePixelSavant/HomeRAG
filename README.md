# Local RAG + MCP Knowledge Service

A self-hosted knowledge base over your own documents — manuals, SDK docs, notes,
infra plans, past agent sessions, receipts — exposed to a local LLM through MCP.

Documents go in from the command line. You query them from Open WebUI. Anything
sensitive is encrypted, sealed by default, and reachable by a model only with
your explicit per-request approval.

> Developer documentation is in **[docs/](docs/)** — the full
> [command reference](docs/cli.md), design, security model,
> [implementation status](docs/status.md) and the [roadmap](docs/roadmap.md).

```
                                     ┌─ OPEN TIER ──────────────────────┐
  data/inbox/<domain>/ ─> extract ─> │  Qdrant: dense + BM25, plaintext │ ─┐
  ~/Dev (notes)          scan        │  manuals · sdk-docs · notes      │  │   Open WebUI
                         chunk       └──────────────────────────────────┘  ├─> (Qwen3-VL,
                         embed                                             │    or Claude)
  ~/.claude/projects ──> extract ─>  ┌─ VAULT TIER ─────────────────────┐  │
  data/inbox/receipts/   redact      │  SQLCipher: rows, text, vectors  │ ─┘
                         chunk       │  receipts · financial ·          │
                         embed       │  transcripts · personal          │
                                     │  SEALED until `make unlock`      │
                                     └──────────────────────────────────┘
```

## The two tiers

The tier a document lands in is decided by **the folder you put it in**, never
inferred from its contents.

| Tier | Domains | Storage | Who can read it |
|---|---|---|---|
| **Open** | `manuals`, `sdk-docs`, `notes`, `infra` | Qdrant, plaintext | Any local model, freely |
| **Vault** | `receipts`, `financial`, `transcripts`, `personal` | SQLCipher, encrypted | Unlocked vault **+** verified identity **+** per-request approval |

Sensitive data never reaches Qdrant. A vector has to be plaintext floats to be
searchable, and inversion attacks reconstruct most of the source text from an
embedding — so encrypting a payload while leaving its vector searchable would
protect the wrong half. Vault vectors live inside the encrypted database and are
brute-forced in memory while unlocked.

## Quick start

The LLM stack must be up first — it owns the `llm-net` network:

```bash
cd ~/Dev/LLM && docker compose up -d
```

Then:

```bash
cp .env.example .env     # set LOCAL_DOCS_PATH, VAULT_ALLOWED_EMAILS, VAULT_JWT_SECRET
make build
make up
make doctor              # everything should be green before you ingest
```

Install the `rag` command so you can drive it from any directory:

```bash
ln -s ~/Dev/RAG/scripts/rag ~/.local/bin/rag     # needs ~/.local/bin on PATH
```

Drop a document in and search for it:

```bash
rag add ~/Downloads/pump-manual.pdf manuals
rag query "how do I prime the pump"
rag list
```

Every `rag` command is a Makefile target underneath, so the two forms are
interchangeable — `rag list` and `make list` do the same thing, and `rag`
simply works from outside the repo. Full reference: **[docs/cli.md](docs/cli.md)**.

Vault-tier domains need you present: `rag ingest` prompts for the vault
passphrase when the run includes one, because the worker derives the key
itself rather than being handed one. A scheduled run has no terminal, so it
queues those sources and says so -- the next interactive `rag ingest` takes
them. Open-tier sources ingest unattended as usual.

`data/inbox/<domain>/` **is** the classification — the directory name is the
domain. `rag add x.pdf manuals` does the same thing explicitly. An
unrecognised domain is an error rather than a default, because a typo that
quietly became open-tier would publish a plaintext vector you cannot un-publish.

## Commands

Shown as `rag`; every one also works as `make` from inside the repo.

```bash
rag up / down / logs / build     # stack control (start ~/Dev/LLM first)
rag doctor                       # preflight: reachability, paths, fingerprint, backlogs
rag warm-cache                   # download and load the embedding models

rag add <file> <domain> [--move]
rag ingest [SOURCE=id] [DOMAIN=d] [FORCE=1]
rag reindex SOURCE=id            # after changing chunking or extraction
rag eval                         # rank known answers: hit@1 / hit@3 / MRR
rag list [--domain d] [--match x] [--all]
rag query "..." [--limit N] [--domain d] [--stale] [--superseded] [--dense]
rag status [JSON=1]
rag rebuild-index YES=1          # after changing EMBED_MODEL/EMBED_DIM
rag review-quarantine            # documents the sensitivity scan held back

rag lifecycle                    # documents that are not plainly active
rag remove <uri> "<reason>"      # retract: deletes vectors, leaves a tombstone
rag restore <uri>                # then: rag ingest
rag supersede OLD=<uri> NEW=<uri>
rag stale URI=<uri> [AFTER=YYYY-MM-DD] [REASON="..."]

rag unlock [TTL=900]             # prompts; key lives in memory only
rag lock
rag vault-status
rag vault-list [DOMAIN=d] [ALL=1]
rag vault-query Q="..."          # human path: no approval needed
rag vault-lifecycle URI=<uri> STATE=<state> [REASON="..."]
rag approve [CODE=123456]        # release one pending model request
rag vault-audit [N=50]

rag test
rag help                         # shorthands plus every Makefile target
```

`rag query` talks to Qdrant directly rather than through MCP, so it still works
with the servers down — which is what makes it useful for telling a retrieval
problem apart from a transport one.

Every flag and the reasoning behind each command: **[docs/cli.md](docs/cli.md)**.

## Quarantine

Filing is declarative, but misfiling happens. Before any open-tier document is
embedded its text is scanned for Luhn-valid card numbers, SSN/IBAN/account
shapes, key and token prefixes, and co-occurring financial vocabulary. A hit
means **quarantine, not index** — the file moves to `data/quarantine/` and
nothing is embedded until you decide:

```bash
rag review-quarantine
rag add data/quarantine/statement.pdf financial
```

Card numbers are Luhn-checked so ordinary order numbers don't flood the queue
into noise. This catches formatted identifiers, not "my password is hunter2" in
prose — it is a backstop for misfiling, not a classifier.

## When a document stops being true

Deleting the file is not enough — a copy elsewhere in the tree, or a restored
backup, is re-indexed on the next `rag ingest`. Four states cover the cases:

| State | Meaning | Retrieval |
|---|---|---|
| `active` | Default | Returned |
| `superseded` | A *named* newer document replaced it | `--superseded` |
| `stale` | Out of date, nothing replaced it | `--stale`, with a warning in the text |
| `retracted` | Wrong, or should not be here | Never — the vectors are deleted |

```bash
rag supersede OLD=topology-v1.md NEW=topology-v2.md
rag stale URI=rack-plan.md REASON="hardware decommissioned"
rag stale URI=firmware-notes.md AFTER=2027-01-01    # flips on that date
rag remove bad-spec.md "torque figure was wrong"
rag restore bad-spec.md && rag ingest               # undo
```

Retraction deletes the chunks and leaves a **tombstone** in the state database.
That row is what keeps the document out of later runs, and it survives the
disappearance sweep on purpose. `rag restore` clears it; because the vectors
are gone, the document only comes back on the next ingest.

Citations come back with every hit (`pump.pdf, page 21`, `CLAUDE.md, lines
92-102`), so a torque figure can be checked rather than trusted.

**Versions are never guessed.** Ingest points out filenames in the same folder
that look like versions of each other and leaves the decision to you — a March
statement is not a newer version of February's, and auto-retiring it would take
a live financial record out of reach.

## The vault

Sealed by default. `make unlock` prompts on a terminal, derives the key with
Argon2id, and holds it in the vault server's memory with a TTL. **No key is ever
written to disk** — not in `.env`, not in an image layer.

The disk is already LUKS-encrypted, so powered-off theft is covered without any
of this. What the vault adds is sealed-by-default on a *running* system: while
locked the data is unreadable even to root, and **a model cannot unlock it**,
because unlocking requires a human at a TTY. That is what makes the rule
enforced rather than declared.

Three gates on every model-initiated read:

1. **Unlocked** — else `VAULT_SEALED`, before identity is even considered.
2. **Verified identity** — Open WebUI mints a signed HS256 assertion per tool
   call; the vault checks the signature, issuer, expiry, email and role. The
   plaintext `X-OpenWebUI-User-*` headers carry no weight, because anything that
   can reach the port could set them.
3. **Per-request approval** — bound to `(subject, chat_id, query)`,
   single-use. The first call returns a code, not data:

```
you  > what did I spend at Home Depot in Q2?
model> PENDING_APPROVAL, code 481920

$ make approve CODE=481920
  principal : jnovick@pixelsavant.net (admin)
  query     : SUM by month, Q2
  Type 'yes' to approve:
```

Approving one request does not approve the next one, and the grant cannot be
replayed on another turn.

**Claude Code gets no exemption** — it is a model, so it passes all three gates.
Only `make vault-query`, typed by a human, skips the approval step.

Every attempt is recorded, allowed or denied: `make vault-audit`.

### Frontier models

Vault data released into a chat with a frontier model leaves this machine. The
vault cannot detect that on its own — Open WebUI forwards no model identifier —
so the control is **per-model tool scoping**: give the vault tool to a local-only
"Finance" preset and to nothing else.

| Preset | Model | Tools |
|---|---|---|
| Finance | local Qwen3-VL | `mcp-server` + `mcp-vault` |
| General | local Qwen3-VL | `mcp-server` |
| Claude | remote | `mcp-server` only |

This is enforced configuration, not a cryptographic boundary. Re-check the
preset tool lists after an Open WebUI upgrade, and remember the approval prompt
is the backstop — it names the chat so you can confirm before releasing.

## Connecting Open WebUI

Set these in `~/Dev/LLM/.env` first, or identity verification cannot work:

```
WEBUI_AUTH=true
WEBUI_SECRET_KEY=<openssl rand -hex 32>                     # stable, or registrations are wiped
ENABLE_FORWARD_USER_INFO_HEADERS=true
FORWARD_USER_INFO_HEADER_JWT_SECRET=<openssl rand -hex 32>  # must equal VAULT_JWT_SECRET here
ENABLE_PLUGINS=false
```

Leave `FORWARD_USER_INFO_HEADER_JWT_EXPIRES_SECONDS` at its 300 s default.
Open WebUI mints the assertion once, when it opens the MCP session at the start
of a turn (`utils/middleware.py:connect_mcp_server`), and reuses it for every
tool call in that turn — so the clock starts *before* the model prefills and
generates. A shorter window expires mid-turn on a long prompt and surfaces as
an intermittent `IDENTITY_REJECTED` that looks exactly like a mismatched
secret.

Then **Admin Panel → Settings → Tools**, adding each as **Connection type:
MCP**:

- Open tier: `http://mcp-server:8000/mcp`
- Vault: `http://mcp-vault:8001/mcp`

Container names, not `localhost` — inside that container `localhost` is itself.

Three settings decide whether this works at all, and getting any of them wrong
produces a silent failure that looks like the model simply choosing not to
search:

- **Auth: None.** The dialog defaults to Bearer. Left blank, Open WebUI sends
  `Authorization: Bearer ` — an empty header value, which `h11` rejects. The
  connection dies before a byte is sent, so **the MCP server logs nothing at
  all**, indistinguishable from never being called.
- **Connection type: MCP, not OpenAPI.** OpenAPI is the dialog default and is
  dispatched through a path that `ENABLE_PLUGINS=false` has already emptied.
  The symptom is `GET /mcp/openapi.json → 404` in the server log.
- **Turn off the model preset's `builtin_tools` capability** — the master
  switch, not a category under it. Otherwise ~15 of Open WebUI's own tools
  compete with these 8, and the model will search its (empty) Knowledge feature
  and report finding nothing.

Attach the **vault** server only to the local model's preset. No forwarded
header carries a model identifier, so the vault cannot tell a local caller from
a hosted one; which model may reach it is a configuration control enforced
there, backstopped by the per-request approval.

Selecting the raw base model instead of the preset gives you a bare LLM —
`tool_ids` lives on presets only.

**Verifying a tool actually ran:** a fluent answer is not evidence. Watch
`journalctl -t mcp-server -t mcp-vault -f`. Four requests on connect
(`initialize`, `notifications/initialized`, the SSE `GET`, `tools/list`) mean
the tools were *offered*; only a fifth `POST /mcp` proves one was *called*.

## Claude Desktop / Claude Code

```json
{
  "mcpServers": {
    "rag-docs": { "type": "streamable-http", "url": "http://localhost:8000/mcp" }
  }
}
```

The vault endpoint is intentionally omitted: without Open WebUI's signed
identity assertion every call is denied anyway. Use `make vault-query`.

## Sources

`data/inbox/` needs no configuration. Everything else is declared in
[`sources.yaml`](sources.yaml), where `domain` is **required**:

```yaml
sources:
  - id: dev-notes
    type: local
    domain: notes           # decides plaintext vs encrypted
    path: /docs
    cadence: "0 * * * *"
    include: ["**/*.md"]
```

## Scheduling

Host systemd timers rather than a scheduler container — journald integration for
free, no resident model idling, and missed runs are caught after a reboot:

```bash
sudo cp systemd/* /etc/systemd/system/
sudo systemctl enable --now rag-sync.timer rag-inbox.path
```

One timer covers every source; per-source `cadence` is honoured in code so it
lives in exactly one place. `rag-inbox.path` makes a dropped file searchable
without running anything by hand.

**Vault sources only sync while unlocked.** The timer skips them when sealed and
`make status` reports the backlog. That friction is the security property.

## Changing the embedding model

`EMBED_MODEL` and `EMBED_DIM` are frozen at collection creation. A fingerprint
stored alongside the vectors records the model, dimension and prefixes, and
startup fails loudly on a mismatch — dimension alone cannot catch it, since
bge-base, nomic-v1.5, gte-base and arctic-m are all 768-dim.

1. Edit `EMBED_MODEL` / `EMBED_DIM` in `.env`
2. `make rebuild-index` (the vault is not touched)

## Known gaps

- **Scanned PDFs are not indexed.** Below 100 chars/page there is no text layer,
  and an empty chunk becomes a high-similarity vector matching almost anything.
  Those documents are marked `ocr_required` and reported by `make status` rather
  than silently making the index look complete. OCR lands in Phase 2.
- **Receipts** are Phase 2: the VLM extractor, ledger and `query_ledger`.
- **git and web sources** are Phase 2 (Crawl4AI pulls Playwright, ingest image
  only).
- **PDF text follows the order the file stores it in** (`pdftotext -raw`).
  That keeps multi-column manuals and procedures intact, but a PDF whose
  stored order is itself scrambled would come out scrambled. A table aligned
  only with spaces may lose its row structure; the Phase 2 vision model is the
  fix for both.
- **Whitespace-aligned tables in PDFs** are not detected as tables, for the same
  reason. Markdown tables are, and keep their header across chunks.

## Logs

```bash
journalctl -t mcp-server -f
journalctl -t mcp-vault -f
journalctl -t ingestion-worker -f
journalctl -t qdrant -f
journalctl -t rag-sync -f
```
