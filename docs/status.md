# Implementation status

As of `feat/ingestion-pipeline-and-vault` after commit `70c9720`, 2026-09-25.

**Phase 1 is built and running. Phase 2 is designed and not started.**

## Summary

| | |
|---|---|
| Branch | `feat/ingestion-pipeline-and-vault` (34 commits ahead of `master`) |
| Tests | **132 passing** (80 pipeline, 39 security, 13 vault ingest) |
| Open tier | 22 active documents (+1 retracted), 1,182 chunks; `manuals` and `notes` populated |
| Retrieval | `make eval`: hit@3 **73%**, hit@1 53%, MRR 0.652 over 15 questions; a model reads ~2,980 chars per search |
| Vault | 9 documents, 500 chunks at last count (2026-09-21); all three gates exercised end to end. Sealed at time of writing |
| Stack | 3 containers up, running as the host user; `make doctor` reports 10 ok, 6 warn, 0 fail |

The warnings are six empty inbox domains, which is expected. Nothing is
waiting on a human any more — see [Blocked on you](#blocked-on-you).

## Component status

Legend: **done** = built, tested and exercised against real data ·
**partial** = works with a documented gap · **not started** = design only.

### Ingestion

| Component | Status | Notes |
|---|---|---|
| Source manifest + implicit inbox sources | done | Directory name is the domain |
| Enumeration with include/exclude globs | done | `**/` prefix bug fixed and regression-tested |
| Text / markdown extraction | done | |
| PDF extraction | done | `pdftotext -raw`, re-spaced from reading order (ADR-024); see [gaps](#known-gaps) |
| Claude Code transcript extraction | done | Tool calls collapsed, results dropped except errors |
| Token-accurate chunking | done | Truncation-free tokenizer clone |
| PDF section chunking | done | Headings by font size (`pdftohtml -xml`); a chunk per section with a `title > heading path` breadcrumb ([ADR-025](decisions.md#adr-025-pdf-chunks-follow-section-headings)) |
| Markdown table header repetition | done | Whitespace tables deliberately excluded |
| Sensitivity scan + quarantine | done | Luhn-checked; findings never echo the secret |
| Tier routing | done | `tier_of()` raises rather than defaults |
| Shrink deletion (`delete_doc_tail`) | done | Unconditional, self-healing |
| Disappearance sweep (`sweep_source`) | done | Two guards, tombstone-aware |
| Git sources | not started | `sources.yaml` rejects the type on purpose |
| Web sources | not started | Same |
| OCR for scanned PDFs | not started | Documents marked `ocr_required`, backlog reported |
| Receipt/VLM extraction | not started | Phase 2 |

### Storage and retrieval

| Component | Status | Notes |
|---|---|---|
| Qdrant hybrid search (dense + BM25, RRF) | done | `Modifier.IDF`, server-side corpus stats |
| Deterministic point IDs | done | `rebuild-index` reconstructs exactly |
| Embedding fingerprint guard | done | Catches model swaps dimension alone would miss |
| Payload indexes | done | 6 fields |
| Citations (`locator` + `citation`) | done | Pages, page ranges, line ranges, anchors |
| `fetch_context` (open tier) | done | Ordered neighbours, capped at 2 either side, refuses vault doc_ids |
| Model-facing result trimming | done | `documents.for_model`: content, citation, ids, score; `search_docs` returns 3 ([ADR-023](decisions.md#adr-023-the-model-gets-a-trimmed-result-row)) |
| Deterministic result order | done | RRF ties broken by position in the document |
| Retrieval eval (`make eval`) | done | Known-answer ranks, hit@1/hit@3/MRR; `tests/retrieval/questions.yaml` |
| Document lifecycle | done | 4 states, tombstones, scheduled review dates |
| Cross-encoder reranking | not started | Phase 3, deprioritised — see [roadmap](roadmap.md) |
| Snapshot/restore for migration | partial | Qdrant pinned by digest; rebuild is the real recovery path |

### Vault

| Component | Status | Notes |
|---|---|---|
| SQLCipher schema + migrations | done | Documents, chunks, FTS5, ledger, audit |
| Argon2id key derivation | done | Memory-only key with TTL |
| Unix-socket control channel | done | Never HTTP |
| Gate 1 — sealed | done | 6 tests; checked first, so a sealed vault reveals nothing about gates 2-3 |
| Gate 2 — signed identity | done | 5 tests, plus 7 cases driven over real MCP against the running server |
| Gate 3 — per-request approval | done | 10 tests. Bound to `(subject, chat_id, query_hash)`; two clocks, see [ADR-021](decisions.md#adr-021-two-clocks-on-a-grant-not-one) |
| Vault-tier ingestion | done | Parses in a keyless child process ([ADR-019](decisions.md#adr-019-vault-extraction-runs-in-a-process-that-holds-no-key)); 13 tests |
| Brute-force vector search | done | Sub-millisecond at this scale |
| FTS5 keyword search | done | Malformed queries return empty, not an error |
| AES-256-GCM blobs | done | `consume()` deletes the plaintext |
| Audit log | done | Records denials too |
| `fetch_context` (vault) | done | Separately gated, 4 tests |
| Lifecycle / retraction | done | Destroys chunks, vectors and the blob |
| Ledger + `query_ledger` | **partial** | **Schema, service, CLI and MCP tool exist and are tested. Nothing writes rows yet** — that is the Phase 2 VLM extractor. |

### Interfaces

| Component | Status | Notes |
|---|---|---|
| Makefile control plane | done | 29 targets, reachable from any directory via `rag` |
| `make doctor` preflight | done | |
| Open-tier MCP server | done | 4 tools, verified over the wire |
| Vault MCP server | done | 4 tools, verified to fail closed over the wire |
| Open WebUI integration | done | Verified end to end through a real browser session, 2026-09-21 |
| systemd timers | done | Units written; `croniter` cadences honoured in code |

## What was verified, and how

Everything below was run against the live stack on this machine, not asserted
from unit tests.

| Claim | Evidence |
|---|---|
| Two-column PDF pages are no longer scrambled | IntelliFlo p2 extracted as contiguous prose; the `-layout` output for comparison splices "General Warnings" into the middle of the body text |
| Row structure survives | XPS p86 keeps `1 Turn on the computer.` on one line under `-raw`, as `-layout` did |
| Dense multi-column sheets read in order | SH-01A p2 extracted with `-raw`: "Selecting Assign Mode" is followed by its steps and the MONO/UNISON/POLY/CHORD table, where `-layout` interleaved four columns (ADR-024) |
| Glued words are re-spaced | IntelliFlo p2's warning box reads "INJURY OR DEATH. THIS PUMP SHOULD BE INSTALLED" instead of `-raw`'s `INJURYORDEATH.THISPUMP…` |
| Table headers survive splitting | Torque fixture: 1 of 5 chunks carried column labels before, 5 of 5 after, all under the 480-token ceiling |
| Small tables are untouched | 1 chunk, exactly 1 header |
| Page citations point at the right page | `…install-guide.pdf, page 21` — `pdftotext -f 21` contains the quoted step |
| Line citations point at the right lines | `RAG/CLAUDE.md, lines 92-102` — `sed -n '92,102p'` matches the chunk |
| Supersede filters by default | v1 hidden, `SUPERSEDED=1` returns it tagged `[SUPERSEDED]` |
| Stale banner is read-time, not stored | Payload `content` unchanged; banner appears only in the hit |
| Retraction is absolute | 0 points; invisible with both `SUPERSEDED=1` and `STALE=1` |
| Tombstones survive ingestion | Two further `make ingest` runs, file still on disk, `skipped(retracted): 1` both times, row intact after the run that would otherwise sweep it |
| Restore works | `make restore` + `make ingest` → back as active with its original chunk count |
| Review dates flip only when past | Yesterday's date flipped; tomorrow's did not |
| Schema bump forces a rebuild | Fingerprint refused the stale collection with `schema_version: stored=1 configured=2` |
| v1→v2 migration works on a real old database | All 7 columns added, existing rows defaulted to `active`, index created |
| `fetch_context` returns ordered neighbours | Chunks 54, 55, 56 in order, with citations |
| Vault fails closed over MCP | Both `search_vault` and `fetch_context` return `VAULT_SEALED` over streamable-http |
| Both servers expose the new tools | `tools/list` over the wire confirms 4 tools each |
| Gate 2 discriminates, not just denies | 7 cases over real MCP against an unlocked vault: no header, plaintext-only, wrong secret, wrong email, wrong role and expired all `IDENTITY_REJECTED`; valid identity reached gate 3 |
| The full three-gate path releases data | Audit: `20:57:49 pending_approval` → `make approve` at a TTY → `20:59:08 allowed … rows=5` from Open WebUI, 2026-09-21 |
| An approval survives into the next turn | Redeemed 79s after being granted, in a later turn with a different `message_id` |
| An approval does not cross a chat or a query | Same question in a new chat, and a reworded search in the same chat, both minted fresh `PENDING` grants rather than redeeming |
| An approval is redeemable without guesswork | `PENDING_APPROVAL` echoes `retry_with`; re-sending it unchanged redeems, re-sending it reworded does not. Unit-level against the real service layer — the live MCP round-trip needs the vault unlocked |
| Vault ingestion writes real documents | 12 transcripts in: 9 indexed, 500 chunks, 3 empty; `vault: unlocked docs=9` |
| Extraction holds no key | A fresh interpreter importing `parse_worker` pulls in neither `keyagent` nor `crypto` |
| `mcp-server` cannot reach the vault | No DNS, and a raw-IP connect to `mcp-vault` times out between bridges |
| Section chunking improves retrieval | `make eval` before/after on 14 questions: hit@3 71%→79%, hit@1 36%→57%, top-3 size −36%; SH-01A poly-mode answer 8th→2nd (ADR-025) |
| Trimming shortens a real answer | Same SH-01A question in Open WebUI: 3 min 26 s before, 88 s after (the cap, trimming and sections together) |
| Reindex keeps tombstones | `rag reindex SOURCE=inbox:manuals`: 872 chunks re-embedded, `skipped(retracted): 1`, IntelliFlo row still `retracted` |
| MCP servers read state on a read-only mount | `fetch_context`, `list_sources`, `get_index_status` all failed while `rag.db` was WAL; all return data after the switch to a rollback journal |
| `list_sources` shows inbox sources | `inbox:manuals` listed with 7 documents from `mcp-server`, which has no inbox mount |
| Search order is repeatable | Two consecutive `make eval` runs produce identical output |

## Known gaps

Ordered by how likely they are to bite.

1. **The ledger has no writer.** `query_ledger` works and is gated, but no code
   path inserts a row. Receipts ingested today are chunked and searchable, not
   summable. Phase 2.
2. **PDF text follows the file's stored order.** Right for every manual
   indexed so far; a PDF whose stored order is itself scrambled would come out
   scrambled, and nothing detects that yet. A token `-raw` glued that reading
   order cannot spell is left glued (Roland's `KEYTRANSPOSE`).
3. **Whitespace-aligned tables in PDFs are not detected as tables.** Deliberate
   — a whitespace gutter cannot distinguish a data table from two-column prose,
   and guessing reintroduces the exact failure just fixed. The Phase 2 vision
   pass is the fix.
4. **Scanned PDFs are not indexed at all.** Below 100 chars/page there is no
   text layer; those documents are marked `ocr_required` and reported rather
   than silently making the index look complete.
5. **Vault-tier sources do not ingest on a schedule.** The worker derives the
   key from a passphrase typed at a terminal ([ADR-020](decisions.md#adr-020-the-ingestion-worker-prompts-for-the-passphrase)),
   so a cron run queues them and says so. Open-tier sources are unaffected.
6. **The similar-document warning only covers documents in one source and
   domain.** Two versions filed into different domains are not compared.
7. **Vault lifecycle has no `restore`.** `make vault-lifecycle STATE=active`
   clears the flag, but a retracted vault document's chunks, vectors and blob
   are destroyed — the original file is gone too, so there is nothing to
   re-ingest. This is intentional but worth knowing before you retract.
8. **No reranking.** Hybrid RRF only. Deliberately deprioritised behind parsing
   quality; see [ADR-011](decisions.md#adr-011-parsing-quality-before-retrieval-tuning).
9. **The small embedding model misses synonyms.** "SH-01A polysynth setup" —
   the query a model actually sent — does not find the Assign Mode section,
   because the manual says "Polyphonic". `sh01a-poly-model` and `tr06-write`
   miss the top 10 in `make eval`. A larger model (bge-base) is the candidate
   fix; it needs `rag rebuild-index`, so measure it with `make eval` first.
10. **A chunking change needs `rag reindex`.** `rag ingest` re-embeds a document
    only when its extracted text changes, and `FORCE=1` does not change that.

## Blocked on you

Nothing is blocking the system any more. What remains is optional.

1. **Swap in a vision model** — Qwen3-VL-30B-A3B-Instruct Q4_K_M plus its
   mmproj, ~18.6 GB — which Phase 2's receipt extraction depends on. It would
   also cover the PDF cases in gap 2 that text extraction cannot.
2. **Remove the orphaned Docker volumes.** `rag_qdrant-data` and
   `rag_ollama-data` are left over from the pre-bind-mount layout; no container
   uses them and together they hold under 2 KB. The unused ollama image is
   already gone. `docker volume rm rag_qdrant-data rag_ollama-data`.
3. **Re-ingest vault sources by hand as they change.** `claude-sessions` has a
   4-hourly cadence that will keep queueing; take it with an interactive
   `make ingest` when you want the newer transcripts.

Resolved on 2026-09-21: the vault has been unlocked and ingested,
`VAULT_JWT_SECRET` is set and matching, and Open WebUI is configured and
verified end to end.

## History

| Commit | What |
|---|---|
| `70c9720` | PDF chunks follow section headings |
| `3c8bda2` | Add `make eval`; stop reindex from resurrecting retracted documents |
| `d66ba12` | PDF extraction: `-raw`, re-spaced from reading order |
| `91e3153` | Cap `fetch_context` at 2 chunks either side |
| `a4da69d` | `list_sources` includes inbox sources the server cannot see |
| `787ebeb` | State db: rollback journal, not WAL |
| `3c203df` | Send the model a trimmed search result |
| `613acd6` | Document the `rag` command and the listing commands |
| `87d3d2b` | Run the containers as the host user instead of root |
| `339669c` | Document inventory, and a `rag` command that works from anywhere |
| `ea8fcc5` | `make add`: bind the file's own directory in for the run |
| `58d8166` | Vault: tell the caller which call an approval covers |
| `9868fe3` | Correct three claims the Open WebUI integration disproved |
| `2ee146e` | Bring status and the bug log up to date |
| `37b405c` | Restart the grant clock on approval |
| `5545cd2` | An empty document is not a failed one |
| `c4763b0` | Prompt for the vault passphrase instead of exporting the key |
| `a94ea60` | Parse vault documents in a process that holds no key |
| `3b0eacf` | Isolate mcp-vault on its own bridge, drop NET_RAW |
| `6a772c0` | Bind grants to the chat, not the message |
| `ccddc0c` | Adaptive PDF extraction, table headers, citations, lifecycle |
| `b9e41fb` | Fix doc_id collisions between same-named files in different directories |
| `ea56506` | Split the vault CLI and fix read-only state access |
| `c4edb7d` | Rewrite README and CLAUDE.md for the built system |
| `b5196bb` | Add test suite and systemd units |
| `81e97bf` | Add sync orchestration, control-plane CLI and both MCP servers |
| `a283de9` | Add the encrypted vault and its three access gates |
| `ba9696d` | Add extractors: text, PDF, and Claude Code transcripts |
| `d6a7d81` | Add classification, state store and source manifest |
| `29bbeda` | Rewrite Qdrant store and fix the infrastructure |
| `0929424` | Add config, domain-tier registry, embedder and chunker |
| `4dc60a9` | Initial commit: RAG stack skeleton |

Bugs found by running the stack rather than by the tests are catalogued in
[development.md](development.md#bugs-the-tests-did-not-catch). That list is
worth reading before adding a feature — the pattern repeats.
