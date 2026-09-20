# Decisions

Why the system is shaped the way it is. Each entry records what was chosen,
what was rejected, and what would have to change for the decision to be
revisited.

These are not style preferences. Most were made because the obvious
alternative fails in a way that is silent.

---

## ADR-001: FastEmbed over Ollama for embeddings

**Chosen:** FastEmbed (ONNX, in-process), `BAAI/bge-small-en-v1.5`, 384-dim,
plus `Qdrant/bm25` sparse.

**Rejected:** Ollama's embedding endpoint.

Embeddings run in the same process as the chunker, so there is no HTTP hop per
batch and no second service to keep alive during a long ingest. This box
already runs a resident `llama-server` that saturates it; adding a second model
server competing for the same cores makes ingestion slower, not faster.

`bge-small-en-v1.5` at 384 dimensions is a deliberate size choice — the index
is small enough to hold comfortably in RAM alongside the LLM, and quality at
this corpus size is not the bottleneck.

**Revisit if:** the corpus grows past a few hundred thousand chunks, or
measurement shows embedding quality limiting answers. A model swap is one line
in `_PREFIXES` plus `make rebuild-index`; the fingerprint guard enforces it.

---

## ADR-002: Sensitive data never reaches Qdrant

**Chosen:** Two physically separate stores. Sensitive documents get no Qdrant
point at all.

**Rejected:** One Qdrant collection with encrypted payloads for sensitive rows.

A vector cannot be encrypted and remain searchable. Embedding inversion attacks
reconstruct the large majority of short source texts from their embeddings, so
a receipt's vector leaks the receipt regardless of what the payload says.
Encrypting the payload while leaving the vector searchable protects the wrong
half.

This is the decision every other security mechanism rests on, and it is why
`tier_of()` raises rather than defaults: a typo that silently routed a document
to the open tier would write a plaintext vector, and a published vector cannot
be unpublished.

**Revisit if:** never, on current cryptography.

---

## ADR-003: Tier is decided by domain, never by content

**Chosen:** `DOMAIN_TIERS` in [app/domains.py](../app/domains.py) is the single
map. The directory a file is dropped into decides its tier.

**Rejected:** Classifying tier from document content.

A content classifier has a false-negative rate, and a false negative here
writes a plaintext vector of a bank statement. Declarative routing has a
failure mode too — misfiling — but it is a human error that is visible and
correctable *before* embedding, because the sensitivity scan runs as a backstop
and quarantines rather than indexes.

The scan is defence in depth, not the routing mechanism. Confusing the two
would be a mistake.

---

## ADR-004: SQLCipher, not MySQL

**Chosen:** SQLCipher (SQLite with AES-256 page-level encryption).

**Rejected:** MySQL, despite more operator familiarity with it.

1. **Encryption at rest.** SQLCipher encrypts every page with a key that exists
   only in memory. MySQL's equivalents either need enterprise features or a
   keyring on disk — and a key on disk next to the data it protects is not
   protecting it from the threat that matters here (someone reading the
   filesystem or a backup).
2. **Attack surface.** A file is not a listening service. MySQL means a second
   daemon on `llm-net` with its own auth to get right.
3. **Portability.** The whole point of the bind-mount layout is that the stack
   is one `rsync` away from new hardware. `vault.db` plus `blobs/` is a
   directory. A MySQL instance is a dump, a restore, and a version-compatibility
   question.
4. **Concurrency is not needed.** One writer (the ingestion worker) and a
   handful of readers, which is exactly what WAL is designed for.

**Revisit if:** the vault ever needs concurrent writers from multiple hosts.
It does not; it is a personal knowledge base.

---

## ADR-005: Unlock over a Unix socket only

**Chosen:** `make unlock` reaches `mcp-vault` over a Unix socket on the data
volume, and the passphrase is read only from a TTY.

**Rejected:** An HTTP unlock endpoint on the vault server.

An HTTP endpoint on `llm-net` is reachable from every container on that
network, including Open WebUI. That hands a model a way to try passphrases.

The TTY requirement is not ergonomics — **it is the mechanism that stops a
model unlocking the vault.** A model has no terminal. The passphrase is
therefore deliberately not accepted from a flag, a file or an environment
variable, and `cmd_unlock` refuses outright when stdin is not a TTY.

A one-shot container cannot unlock the vault either: it would derive a key into
its own memory and then exit. That is why vault commands run via
`docker exec` against the running server, which is the process holding the key.

---

## ADR-006: Three gates, and the CLI skips only one

**Chosen:** unlocked → verified identity → per-request approval. The CLI path
skips gate 3 only.

**Rejected:** Trusting a verified identity on its own.

Gate 2 proves *who* is asking. It cannot prove that the human whose identity is
attached actually wanted this particular query — a prompt-injected model runs
under exactly the same verified identity as a legitimate one.

Gate 3 binds an approval to `(subject, chat_id, query_hash)`,
single-use, and requires the word `yes` typed at a terminal after showing the
principal, tool and query. A reflexive y/n is no defence against the one thing
this gate exists to catch.

The CLI skips gate 3 because a human is already at the terminal. It does not
skip gate 1.

**Claude Code gets no exemption.** It is a model.

---

## ADR-007: Two deletion mechanisms, both with guards

**Chosen:** `delete_doc_tail` for shrinkage, `sweep_source` for disappearance,
with two independent guards on the latter.

Qdrant has no cascade, so a document that shrinks from 10 chunks to 6 leaves
chunks 6–9 in the index forever, and they keep surfacing in results. That is
the shrink case, handled unconditionally after every upsert so it also repairs
a prior partial failure.

Disappearance is different and much more dangerous: a source that enumerates to
zero — a bad mount, an unreadable path, a `sources.yaml` typo — is
*indistinguishable* from one that genuinely lost every file. So `sweep_source`
refuses without an explicit `enumeration_complete` assertion from the caller,
and refuses to delete more than 50% of a source without `--force`.

**Do not relax either guard.** The failure they prevent is the silent
destruction of an index that looks fine until someone searches for something
that used to be there.

---

## ADR-008: PDF pages are extracted twice

**Chosen:** Run `pdftotext` both with and without `-layout` on every page, and
keep whichever fits the page's shape.

**Rejected:** Picking one mode per document.

`-layout` keeps a table row or a numbered step on one line and interleaves
two-column pages, splicing a safety warning into the middle of unrelated body
text. Dropping it does the reverse. Measured on the real corpus, a single
manual contains both kinds of page — so any per-document choice is wrong for
part of every document.

The discriminator is the median width of the text left of the first 3+ space
gutter, measured from the first non-space character. Short means rows; long
means column prose.

The cost is one extra subprocess per page, milliseconds against an embedding
pass measured in seconds.

Pages that fall between the thresholds are **recorded, not guessed**. Reading
order is the safer default because manuals are mostly prose, but the page goes
into `flagged_pages` so the choice is visible and Phase 2 has a work queue.

---

## ADR-009: Whitespace tables are not detected

**Chosen:** Detect markdown pipe tables. Do not attempt whitespace-aligned
tables in PDF text.

**Rejected:** A whitespace-gutter heuristic for tables.

ADR-008's measurements show exactly why: a stable whitespace gutter cannot
distinguish a data table from two-column prose — both have one. A heuristic
here would reintroduce the failure mode ADR-008 exists to fix, in a place where
it is harder to notice.

The correct fix is the Phase 2 vision model transcribing the page to markdown,
at which point the existing pipe-table detector handles it with no new code.

---

## ADR-010: Supersession is never inferred

**Chosen:** Ingest names likely version pairs and stops. A human runs
`make supersede`.

**Rejected:** Auto-detecting versions from filenames or dates.

Periodic documents are not versions of each other. A March statement does not
retire February's, and auto-retiring it would take a live financial record out
of reach of every default query — in the one domain where that matters most.

Two guards on the warning itself:

- Anything carrying a date is excluded outright.
- Candidates must share a **directory**. Matching bare filenames flagged every
  `CLAUDE.md` and `README.md` in the source tree against every other — five
  false pairs on the first real run. A warning nobody reads is worse than no
  warning.

`review_after` is not an exception to this rule: a human sets the date.

---

## ADR-011: Parsing quality before retrieval tuning

**Chosen:** Phase 2's extraction work comes before Phase 3's cross-encoder
reranking.

**Rejected:** Reranking next, as originally sequenced.

Reranking reorders candidates. It cannot recover a chunk whose column headers
were discarded, or one whose two columns were interleaved into nonsense — those
chunks are wrong in the index, and a better ranking of wrong chunks is still
wrong.

This was the clearest lesson from reviewing a fintech RAG retrospective against
this stack, and measuring the corpus confirmed it: the problems found were all
extraction problems.

---

## ADR-012: `lifecycle` is separate from `status`

**Chosen:** A new `lifecycle` column alongside the existing `status`.

**Rejected:** Reusing `status` with extra values.

`documents.status` already means "could the pipeline read this file" —
`indexed`, `ocr_required`, `extract_failed`, `empty`, `quarantined`,
`queued_vault_sealed`. It is read by `count_by_status` and has its own index.

Adding `active` / `superseded` to it would make "is this document extractable"
and "is this document true" the same question, and they are not. A document can
be perfectly extractable and completely wrong.

---

## ADR-013: Retraction deletes, it does not filter

**Chosen:** Retracting a document deletes its points from Qdrant (or its chunks
and vectors from the vault) and leaves a tombstone row in the state database.

**Rejected:** Keeping the points and excluding them at query time.

"Never returned" then holds by **absence**. A filter-based guarantee is only as
strong as every query path remembering to apply it — and there are three
(`search`, `fetch_context`, and the vault's own search), with more coming in
Phase 2. One forgotten filter silently un-retracts the document.

The trade is that un-retracting needs a re-ingest. The file is still on disk, so
`make restore && make ingest` recovers it.

**The tombstone is the point.** Deleting the file is not a removal: a copy
elsewhere in the tree, or a restored backup, is re-indexed on the next run. The
row is what makes removal stick, which is why it is read *before* extraction and
excluded from the disappearance sweep.

---

## ADR-014: The stale banner is added at read time

**Chosen:** `_hit()` prepends `[STALE since …]` to the content it returns.

**Rejected:** Storing the banner in the payload.

Storing it changes the embedded text and therefore the content hash, which
turns flipping a lifecycle flag into a re-embed of the whole document. At read
time it costs one string concatenation, and marking a 300-page manual stale
stays a single `set_payload` call.

---

## ADR-015: A generic `locator`, not a page number

**Chosen:** `{"page": 12, "pages": [12, 13]}` / `{"lines": [40, 58]}` /
`{"anchor": "install"}` in one payload field.

**Rejected:** A `page` field.

Phase 2 adds git sources (line ranges) and web sources (anchors). A `page`
field would need a payload migration and a full re-embed to accommodate them.
The generic shape costs nothing now and saves a rebuild later.

A chunk can span pages, so `pages` keeps the range and the citation names the
**first** one. Citing only the last sends the reader past the answer — which is
what `chunk_blocks` did before, because `dict.update` kept only the last
block's page.

---

## ADR-016: Deterministic, positional point IDs

**Chosen:** `uuid5(POINT_NAMESPACE, "source_id|doc_id|chunk_index")`.

**Rejected:** Hashing chunk content.

Positional IDs mean re-ingestion overwrites rather than duplicating, and
`make rebuild-index` reconstructs the index exactly — which is what makes
Qdrant a derived index rather than a source of truth.

Content hashing breaks both: a document's stale chunks become unenumerable (you
cannot ask "what are chunks 6-9 of this document" if the ID depends on text you
no longer have), and two documents sharing a boilerplate paragraph collide onto
one point.

`POINT_NAMESPACE` is frozen. Changing it re-IDs every point and orphans
everything already stored.

---

## ADR-017: Bind mounts, not named volumes

**Chosen:** Everything under `data/` as bind mounts.

**Rejected:** Docker named volumes.

The stack has to move to new home-server hardware without ceremony. A bind
mount tree is one `rsync`. Named volumes are root-owned, need sudo even to
inspect, and turn migration into a per-volume export/import.

Qdrant is additionally pinned **by digest** rather than `:latest`, because
snapshot restore requires a matching minor version — `:latest` silently breaks
migration at exactly the moment you need it to work.

---

## ADR-018: The Makefile is the only control plane

**Chosen:** Ingest, flag, retract, unlock and rebuild are Makefile targets
only. No MCP tool performs any of them.

A model can read the open tier freely, and the vault through three gates. It
cannot write, delete, retract or unlock anything. Retraction in particular
decides what the system believes is true, which is a human's call at a
terminal.

`test_model_cannot_change_what_counts_as_true` asserts the vault server exposes
no lifecycle function, so this cannot regress by someone adding a convenient
tool.
