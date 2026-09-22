# Security model

What the vault protects, what it does not, and why each mechanism is shaped the
way it is.

## The requirement

Financial and personal material must be queryable by the account holder and by
nobody and nothing else. A model may reach it only when the account holder
deliberately allows that specific request. Everything sensitive is stored
encrypted.

## Threat model

Defended against:

| Threat | Mechanism |
|---|---|
| A model deciding on its own to read financial records | Per-request approval, granted at a TTY |
| A prompt-injected model exfiltrating vault data | Same, plus a typed confirmation naming the principal and query |
| Another container on `llm-net` reaching vault data | `mcp-vault` is not on `llm-net`; identity gate; no unlock endpoint on the network |
| A compromised `mcp-server` ARP-spoofing the identity assertion | `mcp-vault` sits on a separate `vault-net` bridge with only `open-webui`; `mcp-server` runs `cap_drop: [NET_RAW]` |
| Someone reading `vault.db` off the disk or a backup | SQLCipher AES-256; the key is never on disk |
| Sensitive text recovered from a vector | Sensitive data never gets a Qdrant vector at all |
| A misfiled sensitive document reaching the open tier | Pre-embedding sensitivity scan → quarantine |
| Forged `X-OpenWebUI-User-*` headers | Identity comes from a signed HS256 assertion, not plaintext headers |
| An approval replayed for a second query | Grants are single-use and bound to `(subject, chat_id, query_hash)` |
| A model walking a document out one chunk at a time | `fetch_context` is gated separately from `search` |
| A malicious PDF or transcript exploiting a parser to reach the key | Vault extraction runs in a child process that holds no key; only the parent writes |

**Not** defended against, and you should know it:

- **Root on this host.** Root can read the key out of `mcp-vault`'s memory
  while the vault is unlocked. There is no defence against this and none is
  attempted.
- **A compromised `~/Dev/LLM`.** Open WebUI holds the JWT signing secret. Code
  execution there lets an attacker mint a valid identity. They still need a
  per-request approval from a terminal, which is why that gate exists.
- **You approving a bad request.** The approval prompt shows the principal, the
  tool, the chat, and the query, and requires typing `yes` — but it is the last
  line and it trusts your reading of it.
- **Traffic analysis.** The audit log records that a query happened and its
  hash, which reveals timing and volume.
- **The open tier.** It is plaintext by design. Anything in `manuals`,
  `sdk-docs`, `notes` or `infra` is readable by any model with MCP access.

### Ingestion runs across a process boundary

Vault-tier extraction happens in `app/pipeline/parse_worker.py`, a child
process that never opens `vault.db` and holds no key. The parent
(`app/pipeline/sync.py`) holds the key, does the SQLCipher writes, and calls no
extractor.

The reason is that parsers are the largest attack surface here -- they read
whatever lands in `data/inbox/receipts/` -- while the key decrypts every
document ever stored. Sharing a process between them turns a parser exploit
into total vault disclosure. Split, it costs the one document being parsed.

The transport is JSON over pipes. `multiprocessing` would `fork` and inherit
the parent's address space, key included; `pickle` would let a compromised
child execute code in the parent on the way back. A test asserts in a fresh
interpreter that `parse_worker` imports neither `keyagent` nor `crypto`.

**The worker gets its key from a passphrase typed at the terminal**, not from
the control socket. `make ingest` prompts when a vault-tier source is in the
run. A `key` action on the socket would have kept the scheduled cadence
working for vault sources, but it would make the socket key-equivalent --
anything able to open it could decrypt the vault without a human. The rule
that holds the design up is that a passphrase is typed at a terminal and a
model has no terminal, so vault-tier sources simply do not ingest unattended.
They queue until someone runs `make ingest` interactively.

### Network segmentation

`mcp-vault` is deliberately **not** on `llm-net`. It sits on `vault-net`, a
bridge carrying exactly two containers: `open-webui` and `mcp-vault`.

The reason is that every container on a bridge shares a broadcast domain, and
Docker's default capability set includes `NET_RAW`. A compromised peer could
therefore ARP-spoof its way onto a path it is not part of — and the peer most
likely to be compromised is `mcp-server`, because parsing untrusted open-tier
documents is its whole job. Left on the same bridge it could have sat between
`open-webui` and `mcp-vault` and read the identity assertion, or the contents
of a reply to a request you had legitimately approved.

Segmenting is a stronger guarantee than encrypting traffic the attacker can
still see, and it costs no certificates. `mcp-vault` makes no outbound network
calls and `vaultctl` reaches it over the bind-mounted `control.sock`, so it
loses nothing by being unreachable from everything else.

`mcp-server` and `ingestion-worker` additionally run with
`cap_drop: [NET_RAW]`, which removes the spoofing primitive from the two
containers that touch untrusted input.

Verified by connecting from inside the containers: `mcp-server` cannot resolve
`mcp-vault`, and cannot reach it by raw IP either — the packets are dropped
between bridges, so this is isolation and not merely a missing DNS record.

> **TLS is not used on any of these hops, on purpose.** Everything is bound to
> `127.0.0.1`, so nothing crosses a wire. Capturing on loopback requires
> `CAP_NET_RAW`, i.e. root, which is already out of scope above. This changes
> the moment anything binds off-loopback — then TLS is mandatory, terminated by
> a reverse proxy at the edge rather than per-service on the bridge. Note that
> `AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL` is `True` in the Open WebUI image,
> so it verifies tool-server certificates; a self-signed cert must be trusted
> inside that container, and setting the flag to `false` to "make it work"
> buys encryption without authentication, which does not stop the MITM the TLS
> was for.

## The three gates

All in one place, [app/vault/service.py](../app/vault/service.py), applied in
order by `_gate()`. Every model-initiated read of vault data passes all three.

### Gate 1 — the vault is unlocked

Checked first, so a sealed vault never reveals whether an identity would have
been accepted.

The key exists only in `mcp-vault`'s memory, derived by Argon2id from a
passphrase, with a TTL after which it is wiped. It is never written to disk, an
environment variable, or a config file.

**`make unlock` reaches that specific process over a Unix socket**
([app/vault/control.py](../app/vault/control.py)), not HTTP. An unlock endpoint
would be reachable from every container on `llm-net`, including Open WebUI —
handing a model a way to try passphrases. A one-shot container cannot unlock
the vault either: it would derive a key into its own memory and exit.

The passphrase is read only from a TTY. It is deliberately not accepted from a
flag, a file or an environment variable. **That is the mechanism that stops a
model unlocking the vault: a model has no terminal.**

### Gate 2 — verified identity

Identity comes from Open WebUI's **signed HS256 assertion**
(`FORWARD_USER_INFO_HEADER_JWT_SECRET`), verified against a shared secret in
[app/vault/identity.py](../app/vault/identity.py).

The plaintext `X-OpenWebUI-User-Email` / `-Name` / `-Role` headers are **not**
identity. Anything that can reach port 8001 can set them.

`chat_id` comes from a plaintext header and is used only to bind an approval
to one conversation; `message_id` is recorded for the audit log and the
approval prompt but is not matched on. A forged value can make an approval
harder to obtain, never easier.

With `VAULT_JWT_SECRET` unset, no caller's identity can be verified and every
model-initiated vault call is denied. `make doctor` warns about this; it is the
safe direction, so it is a warning and not a hard failure.

### Gate 3 — a human approved *this* request

A grant is bound to `(subject, chat_id, query_hash)` and is **single-use**.
The first call returns an approval code instead of data:

```
make approve CODE=123456
```

The grant runs on two clocks. Before approval it expires in
`VAULT_GRANT_TTL_SECONDS` (120s) -- short on purpose, so a request a human has
not looked at does not sit around waiting to be rubber-stamped. Once approved,
the clock restarts for `VAULT_REDEEM_TTL_SECONDS` (300s), because the owner has
now made a decision and needs time to act on it.

A single clock from creation made approvals unredeemable in practice: by the
time the confirmation prompt had been read and `yes` typed, too little of the
window remained to re-ask in, and the grant expired between the approval and
the redemption.

You approve, then ask again in the same chat and that turn is released. The
binding stops at the chat deliberately: Open WebUI mints a new `message_id`
every turn, and the approval is typed after the turn that triggered it has
already ended, so a message binding could never be redeemed at all.

The prompt prints the principal, the tool, the chat and message, and **every
argument of the call**, with the query first, then requires the word `yes`
typed in full. A reflexive y/n is no defence against a prompt-injected model,
which is the one thing this gate exists to catch — and neither is a prompt that
buries what was asked behind boilerplate, which is why the ordering is
deliberate rather than alphabetical.

The `PENDING_APPROVAL` response hands the caller those same arguments back as
`retry_with`, because the hash it is bound to is opaque and a caller that has
to guess what it asked a turn ago will guess wrong. That echo **widens
nothing**: the hash, the single use, the chat scope and both clocks are
unchanged, and a reworded retry still mints a fresh request needing its own
approval. See [ADR-022](decisions.md#adr-022-the-pending-response-echoes-the-call-it-covers).

The CLI path (`make vault-query`) skips **only** gate 3, because a human is
already at the terminal. It does not skip gate 1.

**Claude Code gets no exemption.** It is a model, so it passes all three
exactly as Open WebUI does.

### `fetch_context` is gated identically

Returning the chunks around a hit returns vault chunk text, so it runs the same
three gates and needs its own approval. An approval for the search does not
release it. Otherwise a whole document could be walked out one neighbour at a
time on the strength of a single approved query.

### There is no lifecycle tool

Retraction decides what the system believes is true. That is a human's call,
made at a terminal. No MCP tool on either server can flag, retract or restore a
document — `test_model_cannot_change_what_counts_as_true` asserts it.

## Why sensitive data never reaches Qdrant

A vector cannot be encrypted and remain searchable. Encrypting a payload while
leaving its vector searchable protects the wrong half.

Embedding inversion attacks (Vec2Text and successors) reconstruct the large
majority of short source texts from their embeddings. A receipt's vector in
Qdrant leaks the receipt, whatever the payload says. So sensitive documents get
no Qdrant point at all.

Semantic search still works in the vault: vectors are stored as BLOBs inside
the encrypted database and brute-forced in memory while unlocked. At ~10k
chunks × 384 float32 that is ~15 MB and sub-millisecond, which comfortably
covers six figures of chunks. FTS5 rides along for keyword search, and the
ledger answers aggregation in exact SQL.

No ANN index on the vault side, deliberately: an on-disk ANN structure is one
more thing holding derived plaintext.

## Defence in depth around the tier boundary

Routing is declarative — the domain decides the tier, never the content. But
misfiling happens, so:

1. `tier_of()` **raises** on an unknown domain rather than defaulting. A typo
   that silently became open-tier would write a plaintext vector.
2. Every open-tier document is scanned before embedding for Luhn-valid card
   numbers, SSN/IBAN/routing/account shapes, key and token prefixes, and
   co-occurring financial vocabulary. A hit means **quarantine, not index**.
3. Vault-tier text is **redacted** before embedding. The vault is encrypted
   anyway, but a secret that never enters a vector is one fewer thing to reason
   about.
4. Findings never echo the live secret, so the quarantine queue and the logs do
   not become a second copy of it.

Card numbers are Luhn-checked so ordinary order numbers do not flood the queue
into noise. This catches formatted identifiers, not "my password is hunter2" in
prose — it is a backstop for misfiling, not a classifier.

## Encryption details

| What | How |
|---|---|
| `vault.db` | SQLCipher, AES-256, page-level, `PRAGMA key = "x'<hex>'"` |
| Key derivation | Argon2id from the passphrase (memory/time cost configurable; capped low in tests) |
| Original files | AES-256-GCM per-blob in `data/vault/blobs/` |
| Subkeys | HKDF from the master key |
| Key lifetime | Memory only, TTL-bounded, wiped on lock or expiry |

`blobs.consume()` deletes the plaintext original after encrypting it, so
ingesting a receipt from the inbox does not leave a cleartext copy behind.

## Audit

Every access attempt is recorded — allowed, denied, or pending — with the
principal, tool, transport, chat and message ids, query hash and row count.
Denials are recorded too, including denials that happened because the vault was
sealed.

```bash
make vault-audit N=50
```

The query itself is stored as a hash, not as text, so the audit log is not a
second unencrypted copy of what you searched for.

## Verifying it still holds

[tests/test_security.py](../tests/test_security.py) is 29 tests and every one
asserts that something is **refused**. A regression there does not break a
feature; it silently exposes financial and personal data. Each failure mode
gets its own case.

```bash
make test
.venv/bin/python -m pytest tests/test_security.py -q     # just these
```
