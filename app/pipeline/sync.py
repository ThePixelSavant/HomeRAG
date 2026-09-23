"""Ingestion orchestration.

Enforces the ordering that the whole tiering scheme rests on:

    extract -> scan -> route -> embed

Embedding an open-tier document writes a plaintext vector, and a vector is
recoverable back to its source text, so classification must be settled before
anything reaches the embedder. There is deliberately no code path from
extraction straight to `embed_passages`.
"""

from __future__ import annotations

import datetime as dt
import difflib
import fcntl
import fnmatch
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from app import documents
from app.config import settings
from app.domains import Tier
from app.pipeline import chunker, classify, embedder, extract, parse_worker, qdrant_store, state
from app.pipeline.extract.base import EMPTY, OCR_REQUIRED, OK, doc_id_for
from app.sources import INBOX, LOCAL, TRANSCRIPTS, Source
from app.vault import blobs as vault_blobs
from app.vault import store as vault_store
from app.vault.crypto import VaultSealed
from app.vault.keyagent import AGENT

logger = logging.getLogger(__name__)

LOCK_NAME = ".sync.lock"


class ParseWorkerFailed(RuntimeError):
    """The keyless extraction child did not return a usable result."""

# Noise that is never worth indexing, whatever the source. This does NOT cover
# large scanned PDFs -- those are kept out by not pointing a source at them,
# since no pattern here would distinguish a scanned manual worth OCRing from a
# scanned book that would cost days of vision inference for nothing.
DEFAULT_EXCLUDES = ["**/.*", "**/node_modules/**", "**/.git/**", "**/.venv/**"]


@dataclass
class SourceResult:
    source_id: str
    docs_seen: int = 0
    docs_indexed: int = 0
    docs_skipped: int = 0
    chunks_upserted: int = 0
    chunks_deleted: int = 0
    quarantined: int = 0
    ocr_required: int = 0
    queued_sealed: int = 0
    skipped_retracted: int = 0
    # Extracted fine, contained nothing worth indexing. NOT a failure: a
    # subagent transcript that is all tool bookkeeping is empty every run, and
    # counting it as failed makes `failed` mean nothing.
    docs_empty: int = 0
    flagged_pages: int = 0
    failed: int = 0
    enumeration_complete: bool = False
    error: str | None = None
    notes: list[str] = field(default_factory=list)


@contextmanager
def sync_lock(data_root: Path | None = None):
    """Serialise runs.

    The systemd timer and the inbox path unit can fire at once, and two
    concurrent sweeps with different run ids would delete each other's work.
    """
    root = data_root or settings.data_root
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / LOCK_NAME).open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError("Another sync is already running (holding data/.sync.lock).") from None
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _matches(rel: str, patterns: list[str]) -> bool:
    """Glob match that treats a leading `**/` as "at any depth, including zero".

    fnmatch alone does not: `**/*.md` expands to a regex needing a literal
    slash, so it silently fails to match a file sitting directly in the source
    root. That is exactly where inbox drops land.
    """
    name = rel.rsplit("/", 1)[-1]
    for pattern in patterns:
        if fnmatch.fnmatch(rel, pattern):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatch(rel, pattern[3:]):
            return True
        if "/" not in pattern and fnmatch.fnmatch(name, pattern):
            return True
    return False


def enumerate_files(source: Source) -> tuple[list[Path], bool]:
    """Walk a source. Returns (files, enumeration_complete).

    The flag is load-bearing: the sweep refuses to delete anything unless the
    walk finished cleanly, because an empty result from a broken mount is
    indistinguishable from a source that genuinely lost all its files.
    """
    root = source.path
    if root is None or not root.exists() or not root.is_dir():
        return [], False

    include = source.include or (["*.jsonl"] if source.type == TRANSCRIPTS else [])
    exclude = [*DEFAULT_EXCLUDES, *source.exclude]

    files = []
    try:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            rel = str(path.relative_to(root))
            if _matches(rel, exclude):
                continue
            # An empty include list means "everything not excluded".
            if include and not _matches(rel, include):
                continue
            files.append(path)
    except OSError as exc:
        logger.warning("Enumeration of %s failed: %s", root, exc)
        return files, False
    return files, True


def _chunks_for(doc: extract.RawDoc, prefix: str = "") -> list[chunker.Chunk]:
    # Shared with app.pipeline.parse_worker, which cannot import this module --
    # see extract.base.chunks_for.
    return extract.chunks_for(doc, prefix=prefix)


# Version-ish decoration in a filename: v2, rev3, -2, (1), _final.
_VERSIONISH_RE = re.compile(r"(?:[._\-\s(]|^)(?:v|ver|rev|r|draft|final|copy)?\s*\d{0,3}\)?$", re.I)
_DATEISH_RE = re.compile(r"\d{4}[-_]?\d{2}(?:[-_]?\d{2})?")

# How alike two normalised stems must be before the pair is worth mentioning.
SIMILAR_RATIO = 0.85


def _normalised_stem(uri: str) -> str:
    stem = _VERSIONISH_RE.sub("", Path(uri).stem.lower())
    return re.sub(r"[^a-z0-9]+", " ", stem).strip()


def similar_documents(rows) -> list[tuple[str, str]]:
    """Pairs of documents that look like versions of each other.

    Surfaces a candidate for `make supersede` and stops there. Acting on this
    automatically would retire live records: a March statement is not a newer
    version of February's, and both must stay queryable. Anything carrying a
    date is therefore excluded outright -- periodic documents are the case this
    heuristic gets wrong, and they are exactly the ones in `financial`.

    Candidates must share a DIRECTORY. Comparing bare filenames matches every
    CLAUDE.md and README.md in a source tree against every other, which is a
    warning on every run about files that have nothing to do with each other --
    and a warning nobody reads is worse than no warning. A document and its
    replacement live in the same folder.
    """
    candidates = [
        (row["uri"], str(Path(row["uri"]).parent), _normalised_stem(row["uri"]))
        for row in rows
        if row["lifecycle"] == documents.ACTIVE and not _DATEISH_RE.search(row["uri"])
    ]
    pairs = []
    for i, (uri_a, dir_a, stem_a) in enumerate(candidates):
        if not stem_a:
            continue
        for uri_b, dir_b, stem_b in candidates[i + 1 :]:
            if dir_a != dir_b:
                continue
            if difflib.SequenceMatcher(None, stem_a, stem_b).ratio() >= SIMILAR_RATIO:
                pairs.append((uri_a, uri_b))
    return pairs


def _apply_review_dates(conn, result: SourceResult) -> None:
    """Flip documents whose review date has passed to `stale`.

    An explicit, recorded transition rather than date arithmetic at query time:
    the flag is visible in `make status` and in the payload, so a document
    going quiet is something you can see rather than infer from a bad answer.
    """
    today = dt.date.today().isoformat()
    for row in state.due_for_review(conn, today):
        reason = f"review date {row['review_after']} passed"
        state.set_lifecycle(conn, row["doc_id"], documents.STALE, reason=reason)
        state.clear_review(conn, row["doc_id"])
        if row["tier"] == Tier.OPEN.value:
            qdrant_store.set_lifecycle(
                row["doc_id"],
                {
                    "lifecycle": documents.STALE,
                    "lifecycle_reason": reason,
                    "lifecycle_set_at": state.utcnow(),
                },
            )
        result.notes.append(f"{row['uri']}: marked stale ({reason})")


def _quarantine(conn, source: Source, path: Path, result: SourceResult, scan) -> None:
    settings.quarantine_path.mkdir(parents=True, exist_ok=True)
    target = settings.quarantine_path / path.name
    if target.exists():
        target = settings.quarantine_path / f"{path.stem}.{hashlib.md5(str(path).encode()).hexdigest()[:6]}{path.suffix}"
    shutil.move(str(path), target)
    state.add_quarantine(
        conn,
        path=str(target),
        origin_path=str(path),
        domain=source.domain,
        rules=",".join(f.rule for f in scan.findings),
        reason=scan.reason(),
    )
    result.quarantined += 1
    logger.warning("Quarantined %s from %s: %s", path.name, source.id, scan.reason())


# --------------------------------------------------------------------------
# Open tier
# --------------------------------------------------------------------------


def _sync_open_doc(conn, source: Source, path: Path, doc, run_id: str, result: SourceResult) -> None:
    doc_id = doc_id_for(source.id, doc.rel_uri)
    content_hash = doc.content_hash()

    # Gate: scan before anything is embedded.
    scan = classify.scan(doc.full_text())
    if scan.sensitive:
        _quarantine(conn, source, path, result, scan)
        state.upsert_document(
            conn, doc_id=doc_id, source_id=source.id, domain=source.domain,
            tier=Tier.OPEN.value, uri=doc.rel_uri, title=doc.title or path.stem,
            content_hash=content_hash, chunk_count=0, extractor=doc.extractor,
            status=state.QUARANTINED, status_detail=scan.reason(), mtime=doc.mtime,
            size_bytes=doc.size_bytes, last_seen_run=run_id,
        )
        return

    previous = state.get_document(conn, doc_id)
    if previous and previous["content_hash"] == content_hash and previous["status"] == state.INDEXED:
        # Unchanged: refresh the run marker so the sweep does not treat it as
        # disappeared, but do not re-embed.
        qdrant_store.touch_doc(doc_id, run_id)
        state.touch_document(conn, doc_id, run_id)
        result.docs_skipped += 1
        return

    chunks = _chunks_for(doc)
    if not chunks:
        state.upsert_document(
            conn, doc_id=doc_id, source_id=source.id, domain=source.domain,
            tier=Tier.OPEN.value, uri=doc.rel_uri, title=doc.title or path.stem,
            content_hash=content_hash, chunk_count=0, extractor=doc.extractor,
            status=state.EMPTY, status_detail="no chunks produced", mtime=doc.mtime,
            size_bytes=doc.size_bytes, last_seen_run=run_id,
        )
        return

    # A re-indexed document keeps whatever lifecycle it already carried: an
    # edit to a superseded file does not quietly make it current again.
    lifecycle = previous["lifecycle"] if previous else documents.ACTIVE
    lifecycle_fields = {
        "lifecycle": lifecycle,
        "lifecycle_reason": previous["lifecycle_reason"] if previous else None,
        "lifecycle_set_at": previous["lifecycle_set_at"] if previous else None,
        "superseded_by": previous["superseded_by"] if previous else None,
        "effective_date": previous["effective_date"] if previous else None,
    }

    existing = qdrant_store.existing_chunks(doc_id)
    texts = [c.text for c in chunks]
    hashes = [hashlib.sha256(t.encode()).hexdigest() for t in texts]
    changed = [i for i, h in enumerate(hashes) if existing.get(chunks[i].index) != h]

    if changed:
        dense = embedder.embed_passages([texts[i] for i in changed])
        sparse = embedder.sparse_passages([texts[i] for i in changed])
        points = []
        for n, i in enumerate(changed):
            chunk = chunks[i]
            points.append(
                qdrant_store.make_point(
                    source_id=source.id, doc_id=doc_id, chunk_index=chunk.index,
                    dense=dense[n], sparse=sparse[n],
                    payload={
                        "domain": source.domain, "source_id": source.id, "doc_id": doc_id,
                        "chunk_index": chunk.index, "content": chunk.text,
                        "content_hash": hashes[i], "doc_hash": content_hash,
                        "chunk_count": len(chunks), "source_type": source.type,
                        "uri": doc.rel_uri, "title": doc.title or path.stem,
                        "heading_path": chunk.heading_path, "token_count": chunk.token_count,
                        "indexed_at": state.utcnow(), "doc_mtime": doc.mtime,
                        "extractor": doc.extractor, "last_seen_run": run_id,
                        # Lifted out of `extra` to a top-level key: a citation
                        # is read on every hit, and search() should not have to
                        # go digging through a free-form bag for it.
                        "locator": chunk.extra.get("locator", {}),
                        **lifecycle_fields,
                        "extra": json.loads(json.dumps(chunk.extra, default=str)),
                    },
                )
            )
        qdrant_store.upsert_chunks(points)
        result.chunks_upserted += len(points)

    # Unconditional: a no-op when nothing shrank, and it repairs a prior
    # partial failure.
    qdrant_store.delete_doc_tail(doc_id, len(chunks))
    if not changed:
        qdrant_store.touch_doc(doc_id, run_id)

    state.upsert_document(
        conn, doc_id=doc_id, source_id=source.id, domain=source.domain, tier=Tier.OPEN.value,
        uri=doc.rel_uri, title=doc.title or path.stem, content_hash=content_hash,
        chunk_count=len(chunks), extractor=doc.extractor, status=state.INDEXED,
        status_detail=None, mtime=doc.mtime, size_bytes=doc.size_bytes, last_seen_run=run_id,
        flagged_pages=len(doc.extra.get("flagged_pages") or ()),
    )
    # Counted here rather than at extraction, so the run summary reports pages
    # flagged in what this run actually INDEXED. Counting every document that
    # was merely re-extracted makes an unchanged corpus report the same figure
    # forever, which reads as new work and is not.
    result.flagged_pages += len(doc.extra.get("flagged_pages") or ())
    result.docs_indexed += 1


# --------------------------------------------------------------------------
# Vault tier
# --------------------------------------------------------------------------


def _run_parse_worker(source: Source, files: list[Path], known: dict, tombstoned) -> list[dict]:
    """Extract, chunk and embed in a child process that holds no vault key.

    Every parser here reads attacker-influenced bytes, and this process holds
    the key to every document ever stored. Keeping the two apart means a
    parser exploit costs one document rather than the whole vault.

    `subprocess` rather than `multiprocessing`: fork would inherit this
    address space, key included. JSON rather than pickle: the child is the
    half assumed to be compromised, and pickle would let it execute code here
    on the way back.
    """
    job = {
        "source": {
            "id": source.id,
            "type": source.type,
            "domain": source.domain,
            "path": str(source.path),
        },
        "files": [str(p.relative_to(source.path)) for p in files],
        "known_hashes": known,
        "tombstoned": sorted(tombstoned),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "app.pipeline.parse_worker"],
        input=json.dumps(job),
        capture_output=True,
        text=True,
        # The key is never in argv or the environment, so an inherited env is
        # not a leak -- but the child gets no reason to look, either.
        env={k: v for k, v in os.environ.items()},
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise ParseWorkerFailed(
            f"parse worker exited {proc.returncode}: {(proc.stderr or '').strip()[-500:]}"
        )
    payload = json.loads(proc.stdout)
    if "error" in payload:
        raise ParseWorkerFailed(payload["error"])
    if proc.stderr.strip():
        logger.debug("parse worker stderr: %s", proc.stderr.strip()[-2000:])
    return payload.get("records") or []


def _write_vault_record(vconn, source: Source, record: dict, run_id: str, result: SourceResult) -> None:
    """Write one parsed record into the encrypted store.

    This half never calls an extractor. Everything it touches arrived as JSON
    from `parse_worker`, so the only parsing here is `json.loads`.
    """
    doc_id = record["doc_id"]
    outcome = record.get("outcome")

    if outcome == parse_worker.SKIPPED_RETRACTED:
        result.skipped_retracted += 1
        return
    if outcome == parse_worker.SKIPPED_UNCHANGED:
        vault_store.touch_document(vconn, doc_id, run_id)
        result.docs_skipped += 1
        return
    if outcome == parse_worker.SKIPPED_EMPTY:
        result.docs_empty += 1
        return
    if outcome == parse_worker.OCR:
        # Vault-tier OCR candidates are counted but not recorded: an unindexed
        # row in the vault would be a document we cannot search and cannot
        # explain. Open-tier keeps a row because Qdrant is not the record.
        result.ocr_required += 1
        return
    if outcome == parse_worker.FAILED:
        result.failed += 1
        result.notes.append(f"{record['rel_uri']}: {record.get('error', 'extraction failed')}")
        return
    if outcome != parse_worker.INDEXED:
        result.failed += 1
        result.notes.append(f"{record['rel_uri']}: unknown parse outcome {outcome!r}")
        return

    # Past every skip: this document really is being written, so its flagged
    # pages are this run's work. See the matching note in _sync_open_doc.
    result.flagged_pages += record.get("flagged_pages", 0)

    chunks = record.get("chunks") or []
    vault_store.upsert_document(
        vconn, doc_id=doc_id, source_id=source.id, domain=source.domain,
        uri=record["rel_uri"], title=record["title"], content_hash=record["content_hash"],
        chunk_count=len(chunks), extractor=record["extractor"], status="indexed",
        status_detail=record.get("status_detail"), raw_extraction=record.get("raw_extraction"),
        mtime=record.get("mtime", 0.0), size_bytes=record.get("size_bytes", 0),
        last_seen_run=run_id,
    )
    vault_store.replace_chunks(
        vconn, doc_id,
        [
            {
                "chunk_id": f"{doc_id}-{chunk['index']}", "doc_id": doc_id, "domain": source.domain,
                "chunk_index": chunk["index"], "text": chunk["text"],
                "content_hash": hashlib.sha256(chunk["text"].encode()).hexdigest(),
                "vector": base64.b64decode(chunk["vector_b64"]),
                "token_count": chunk["token_count"],
                "heading_path": json.dumps(chunk["heading_path"]),
                "locator": json.dumps(chunk["locator"], default=str),
                "indexed_at": vault_store.utcnow(),
            }
            for chunk in chunks
        ],
    )

    # The original is sensitive too. Inbox drops are consumed; sources indexed
    # in place (transcripts, mounted dirs) are left alone -- we do not own them.
    if source.type == INBOX:
        vault_blobs.consume(vconn, AGENT.key(), doc_id, source.path / record["rel_uri"])

    result.chunks_upserted += len(chunks)
    result.docs_indexed += 1


def _ingest_vault_source(
    vconn, source: Source, files: list[Path], tombstoned, run_id: str, result: SourceResult
) -> None:
    known = {
        row["doc_id"]: row["content_hash"]
        for row in vconn.execute(
            "SELECT doc_id, content_hash FROM documents WHERE source_id=?", (source.id,)
        )
    }
    for record in _run_parse_worker(source, files, known, tombstoned):
        try:
            _write_vault_record(vconn, source, record, run_id, result)
        except Exception as exc:  # noqa: BLE001 - one bad record must not kill the run
            logger.exception("Failed to store %s", record.get("rel_uri"))
            result.failed += 1
            result.notes.append(f"{record.get('rel_uri')}: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# Per-source driver
# --------------------------------------------------------------------------


def sync_source(source: Source, run_id: str, *, force: bool = False, dry_run: bool = False) -> SourceResult:
    result = SourceResult(source_id=source.id)
    files, complete = enumerate_files(source)
    result.enumeration_complete = complete
    result.docs_seen = len(files)

    if not complete:
        result.error = (
            f"enumeration of {source.path} did not complete (missing, unreadable, or not a "
            "directory). Nothing was deleted."
        )
        return result

    if dry_run:
        result.notes.append(f"dry run: {len(files)} file(s) would be processed")
        return result

    vault_sealed = source.is_vault and not AGENT.is_unlocked()
    if vault_sealed:
        # Nothing is read, extracted or written. Sealed means sealed.
        result.queued_sealed = len(files)
        result.notes.append(
            f"{len(files)} file(s) queued: vault sealed. Run `make unlock` to ingest them."
        )
        return result

    vconn = AGENT.connect() if source.is_vault else None
    if vconn is not None:
        vault_store.init(vconn)

    try:
        with state.writer() as conn:
            state.record_source_start(conn, source.id, source.domain, source.type, run_id)

            # Read once per source, not per file. A retracted document is
            # skipped BEFORE extraction: it costs no parsing, and more
            # importantly there is then no path from it to the embedder at all.
            # Vault documents are only ever recorded in the vault's own
            # database, so that is where their tombstones live too.
            if source.is_vault:
                # Vault-tier extraction happens in a child process that holds
                # no key -- see app.pipeline.parse_worker. Nothing below this
                # branch runs an extractor in a process that can decrypt.
                _ingest_vault_source(
                    vconn, source, files,
                    vault_store.tombstones(vconn, source.id), run_id, result,
                )
                files = []

            tombstoned = state.tombstones(conn, source.id) if not source.is_vault else {}
            if not source.is_vault:
                _apply_review_dates(conn, result)

            for path in files:
                rel_uri = str(path.relative_to(source.path))
                if doc_id_for(source.id, rel_uri) in tombstoned:
                    result.skipped_retracted += 1
                    continue

                doc = extract.extract(path)
                if doc is None:
                    continue
                # Extractors only see one file, so they default rel_uri to the
                # basename. Only this loop knows the source root, and doc_id is
                # derived from rel_uri -- leaving it a basename makes every
                # CLAUDE.md under a tree collide into one document, each
                # silently overwriting the last.
                doc.rel_uri = rel_uri
                if doc.status == OCR_REQUIRED:
                    result.ocr_required += 1
                    state.upsert_document(
                        conn, doc_id=doc_id_for(source.id, doc.rel_uri), source_id=source.id,
                        domain=source.domain, tier=Tier.OPEN.value, uri=doc.rel_uri,
                        title=doc.title or path.stem, content_hash=doc.content_hash(),
                        chunk_count=0, extractor=doc.extractor, status=state.OCR_REQUIRED,
                        status_detail=doc.status_detail, mtime=doc.mtime,
                        size_bytes=doc.size_bytes, last_seen_run=run_id,
                    )
                    continue
                if doc.status == EMPTY or not doc.usable:
                    result.docs_empty += 1
                    continue
                if doc.status != OK:
                    result.failed += 1
                    result.notes.append(
                        f"{doc.rel_uri}: {doc.status_detail or doc.status}"
                    )
                    continue

                try:
                    _sync_open_doc(conn, source, path, doc, run_id, result)
                except Exception as exc:  # noqa: BLE001 - one bad file must not kill the run
                    logger.exception("Failed on %s", path)
                    result.failed += 1
                    result.notes.append(f"{path.name}: {type(exc).__name__}: {exc}")

            if vconn is not None:
                vconn.commit()

            # Disappearance sweep, guarded.
            if not source.is_vault:
                try:
                    result.chunks_deleted = qdrant_store.sweep_source(
                        source.id, run_id, enumeration_complete=complete, force=force
                    )
                    for row in state.documents_missing_run(conn, source.id, run_id):
                        state.delete_document(conn, row["doc_id"])
                except qdrant_store.SweepRefused as exc:
                    result.notes.append(str(exc))
                    logger.warning("Sweep refused for %s: %s", source.id, exc)
            else:
                for row in vault_store.documents_missing_run(vconn, source.id, run_id):
                    vault_store.delete_document(vconn, row["doc_id"])
                    vault_blobs.delete(vconn, row["doc_id"])
                    result.chunks_deleted += 1
                vconn.commit()

            sibling_conn = vconn if source.is_vault and vconn is not None else conn
            rows = sibling_conn.execute(
                "SELECT uri, lifecycle FROM documents WHERE source_id=? AND domain=?",
                (source.id, source.domain),
            ).fetchall()
            for uri_a, uri_b in similar_documents(rows):
                result.notes.append(
                    f"{uri_a!r} and {uri_b!r} look like versions of each other. "
                    f"If one replaces the other: make supersede OLD=<old> NEW=<new>"
                )

            state.record_source_finish(
                conn, source.id, "ok" if not result.error else "error",
                error=result.error, docs_seen=result.docs_seen,
                chunks_upserted=result.chunks_upserted, chunks_deleted=result.chunks_deleted,
            )
    finally:
        if vconn is not None:
            vconn.close()

    return result
