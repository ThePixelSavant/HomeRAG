# Local RAG + MCP Knowledge Service

A self-hosted knowledge base over your own documents — manuals, SDK docs, notes,
infra plans, past agent sessions, receipts — exposed to a local LLM through MCP.

Documents go in from the command line. You query them from Open WebUI. Anything
sensitive is encrypted, sealed by default, and reachable by a model only with
your explicit per-request approval.

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

Drop a document in and search for it:

```bash
cp ~/Downloads/pump-manual.pdf data/inbox/manuals/
make ingest
make query Q="how do I prime the pump"
```

`data/inbox/<domain>/` **is** the classification — the directory name is the
domain. `make add FILE=x.pdf DOMAIN=manuals` does the same thing explicitly. An
unrecognised domain is an error rather than a default, because a typo that
quietly became open-tier would publish a plaintext vector you cannot un-publish.

## Commands

```bash
make doctor                      # preflight: reachability, paths, fingerprint, backlogs
make ingest [SOURCE=id] [DOMAIN=d] [FORCE=1]
make add FILE=x.pdf DOMAIN=manuals [MOVE=1]
make query Q="..." [DOMAINS=a,b] [LIMIT=5]
make status [JSON=1]
make reindex SOURCE=id
make rebuild-index               # after changing EMBED_MODEL/EMBED_DIM
make review-quarantine           # documents the sensitivity scan held back

make unlock [TTL=900]            # prompts; key lives in memory only
make lock
make vault-status
make vault-query Q="..."         # human path: no approval needed
make approve [CODE=123456]       # release one pending model request
make vault-audit [N=50]

make test
```

`make query` talks to Qdrant directly rather than through MCP, so it still works
with the servers down.

## Quarantine

Filing is declarative, but misfiling happens. Before any open-tier document is
embedded its text is scanned for Luhn-valid card numbers, SSN/IBAN/account
shapes, key and token prefixes, and co-occurring financial vocabulary. A hit
means **quarantine, not index** — the file moves to `data/quarantine/` and
nothing is embedded until you decide:

```bash
make review-quarantine
make add FILE=data/quarantine/statement.pdf DOMAIN=financial
```

Card numbers are Luhn-checked so ordinary order numbers don't flood the queue
into noise. This catches formatted identifiers, not "my password is hunter2" in
prose — it is a backstop for misfiling, not a classifier.

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
3. **Per-request approval** — bound to `(subject, chat_id, message_id, query)`,
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
FORWARD_USER_INFO_HEADER_JWT_EXPIRES_SECONDS=60
ENABLE_PLUGINS=false
```

Then **Admin Settings → External Tools → MCP → Add Server**:

- Open tier: `http://mcp-server:8000/mcp`
- Vault: `http://mcp-vault:8001/mcp` (attach to the Finance preset only)

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

## Logs

```bash
journalctl -t mcp-server -f
journalctl -t mcp-vault -f
journalctl -t ingestion-worker -f
journalctl -t qdrant -f
journalctl -t rag-sync -f
```
