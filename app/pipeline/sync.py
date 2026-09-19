"""Ingestion orchestration.

Enforces the ordering that the whole tiering scheme rests on:

    extract -> scan -> route -> embed

Embedding an open-tier document writes a plaintext vector, and a vector is
recoverable back to its source text, so classification must be settled before
anything reaches the embedder. There is deliberately no code path from
extraction straight to `embed_passages`.
"""

from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import logging
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.domains import Tier
from app.pipeline import chunker, classify, embedder, extract, qdrant_store, state
from app.pipeline.extract.base import OCR_REQUIRED, OK, doc_id_for
from app.sources import INBOX, LOCAL, TRANSCRIPTS, Source
from app.vault import blobs as vault_blobs
from app.vault import store as vault_store
from app.vault.crypto import VaultSealed
from app.vault.keyagent import AGENT

logger = logging.getLogger(__name__)

LOCK_NAME = ".sync.lock"

# Excluded by default: 140 MB of scanned game books would take days through the
# vision model for near-zero retrieval value. Revisit when bulk OCR lands.
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
    if doc.strategy == extract.MARKDOWN:
        return chunker.chunk_markdown(doc.text, extra=doc.extra)
    if doc.strategy == extract.BLOCKS:
        return chunker.chunk_blocks(doc.blocks, extra=doc.extra, prefix=prefix)
    return chunker.chunk_text(doc.text, extra=doc.extra, prefix=prefix)


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
    )
    result.docs_indexed += 1


# --------------------------------------------------------------------------
# Vault tier
# --------------------------------------------------------------------------


def _sync_vault_doc(vconn, source: Source, path: Path, doc, run_id: str, result: SourceResult) -> None:
    doc_id = doc_id_for(source.id, doc.rel_uri)

    # Redaction before embedding: the vault is encrypted anyway, but a secret
    # that never enters a vector is one fewer thing to reason about.
    if doc.strategy == extract.BLOCKS:
        for block in doc.blocks:
            block.text = classify.redact(block.text)
    else:
        doc.text = classify.redact(doc.text)

    content_hash = doc.content_hash()
    previous = vault_store.get_document(vconn, doc_id)
    if previous and previous["content_hash"] == content_hash:
        vault_store.touch_document(vconn, doc_id, run_id)
        result.docs_skipped += 1
        return

    title = doc.title or path.stem
    # Mid-session chunks are contextless without the session title.
    prefix = f"{title}\n\n" if source.type == TRANSCRIPTS else ""
    chunks = _chunks_for(doc, prefix=prefix)
    if not chunks:
        result.docs_skipped += 1
        return

    vault_store.upsert_document(
        vconn, doc_id=doc_id, source_id=source.id, domain=source.domain,
        uri=doc.rel_uri, title=title, content_hash=content_hash, chunk_count=len(chunks),
        extractor=doc.extractor, status="indexed", status_detail=doc.status_detail,
        raw_extraction=json.dumps(doc.extra, default=str), mtime=doc.mtime,
        size_bytes=doc.size_bytes, last_seen_run=run_id,
    )

    texts = [c.text for c in chunks]
    vectors = embedder.embed_passages(texts)
    vault_store.replace_chunks(
        vconn, doc_id,
        [
            {
                "chunk_id": f"{doc_id}-{chunk.index}", "doc_id": doc_id, "domain": source.domain,
                "chunk_index": chunk.index, "text": chunk.text,
                "content_hash": hashlib.sha256(chunk.text.encode()).hexdigest(),
                "vector": vault_store.pack_vector(vectors[n]), "token_count": chunk.token_count,
                "heading_path": json.dumps(chunk.heading_path),
                "indexed_at": vault_store.utcnow(),
            }
            for n, chunk in enumerate(chunks)
        ],
    )

    # The original is sensitive too. Inbox drops are consumed; sources indexed
    # in place (transcripts, mounted dirs) are left alone -- we do not own them.
    if source.type == INBOX:
        vault_blobs.consume(vconn, AGENT.key(), doc_id, path)

    result.chunks_upserted += len(chunks)
    result.docs_indexed += 1


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

            for path in files:
                doc = extract.extract(path)
                if doc is None:
                    continue
                if doc.status == OCR_REQUIRED:
                    result.ocr_required += 1
                    if not source.is_vault:
                        state.upsert_document(
                            conn, doc_id=doc_id_for(source.id, doc.rel_uri), source_id=source.id,
                            domain=source.domain, tier=Tier.OPEN.value, uri=doc.rel_uri,
                            title=doc.title or path.stem, content_hash=doc.content_hash(),
                            chunk_count=0, extractor=doc.extractor, status=state.OCR_REQUIRED,
                            status_detail=doc.status_detail, mtime=doc.mtime,
                            size_bytes=doc.size_bytes, last_seen_run=run_id,
                        )
                    continue
                if doc.status != OK or not doc.usable:
                    result.failed += 1
                    continue

                try:
                    if source.is_vault:
                        _sync_vault_doc(vconn, source, path, doc, run_id, result)
                    else:
                        _sync_open_doc(conn, source, path, doc, run_id, result)
                except VaultSealed:
                    raise
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

            state.record_source_finish(
                conn, source.id, "ok" if not result.error else "error",
                error=result.error, docs_seen=result.docs_seen,
                chunks_upserted=result.chunks_upserted, chunks_deleted=result.chunks_deleted,
            )
    finally:
        if vconn is not None:
            vconn.close()

    return result
