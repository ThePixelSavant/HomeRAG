"""Parse, chunk and embed vault-tier documents in a process that holds no key.

Every parser in this pipeline reads attacker-influenced bytes. A PDF, a
transcript, a spreadsheet dropped into `data/inbox/receipts/` -- none of it is
trusted, and the libraries that read it are the largest attack surface in the
project. The vault key must not be in that process: a parser exploit should
cost the document being parsed, not every document ever stored.

So ingestion is split. This module is the untrusted half. It receives a job on
stdin, extracts and redacts and chunks and embeds, and writes records to
stdout. It never opens `vault.db`, never asks for a key, and has no way to
obtain one -- `app.vault.keyagent.AGENT` in this process is empty and stays
empty. The parent half, in `app.pipeline.sync`, holds the key, does the
SQLCipher writes, and never calls an extractor.

The transport is JSON over pipes rather than `multiprocessing`, for two
reasons. A `fork` would inherit the parent's address space, key included,
which is the entire thing being avoided; `subprocess` + `exec` gives a clean
interpreter. And JSON cannot execute code on the way back in, where `pickle`
could -- the whole point is that this process may be the compromised one.

Vectors travel base64-encoded rather than as JSON float arrays: it is ~6x
smaller and avoids a decimal round-trip on values the parent stores verbatim.

Run directly for debugging:

    echo '{"source": {...}, "files": [...]}' | python -m app.pipeline.parse_worker
"""

from __future__ import annotations

import base64
import json
import logging
import sys
from pathlib import Path

from app.pipeline import classify, embedder, extract
from app.pipeline.extract.base import OCR_REQUIRED, OK, doc_id_for
from app.sources import TRANSCRIPTS
from app.vault import store as vault_store

logger = logging.getLogger(__name__)

# Outcomes the parent switches on. Anything it does not recognise is a bug
# here, and the parent treats it as a failure rather than silently dropping
# the document.
INDEXED = "indexed"
SKIPPED_UNCHANGED = "skipped_unchanged"
SKIPPED_RETRACTED = "skipped_retracted"
SKIPPED_EMPTY = "skipped_empty"
OCR = "ocr_required"
FAILED = "failed"


def _parse_one(source: dict, rel_uri: str, known_hash: str | None) -> dict:
    """Extract one file. Returns a record the parent can write, or a reason not to."""
    path = Path(source["path"]) / rel_uri
    record: dict = {"rel_uri": rel_uri, "doc_id": doc_id_for(source["id"], rel_uri)}

    doc = extract.extract(path)
    if doc is None:
        record["outcome"] = SKIPPED_EMPTY
        return record

    # Extractors only see one file and default rel_uri to the basename. Only
    # the caller knows the source root, and doc_id derives from rel_uri --
    # leaving it a basename collides every CLAUDE.md under a tree into one
    # document, each silently overwriting the last.
    doc.rel_uri = rel_uri
    record["flagged_pages"] = len(doc.extra.get("flagged_pages") or ())

    if doc.status == OCR_REQUIRED:
        record["outcome"] = OCR
        record["status_detail"] = doc.status_detail
        return record
    if doc.status != OK or not doc.usable:
        record["outcome"] = FAILED
        record["error"] = doc.status_detail or f"status={doc.status}"
        return record

    # Redaction before embedding: the vault is encrypted anyway, but a secret
    # that never enters a vector is one fewer thing to reason about.
    if doc.strategy == extract.BLOCKS:
        for block in doc.blocks:
            block.text = classify.redact(block.text)
    else:
        doc.text = classify.redact(doc.text)

    content_hash = doc.content_hash()
    if known_hash is not None and known_hash == content_hash:
        # Unchanged since the last run. Say so before embedding -- the parent
        # only needs to touch the row, and embedding would be pure waste.
        record["outcome"] = SKIPPED_UNCHANGED
        record["content_hash"] = content_hash
        return record

    title = doc.title or path.stem
    # Mid-session chunks are contextless without the session title.
    prefix = f"{title}\n\n" if source["type"] == TRANSCRIPTS else ""
    chunks = extract.chunks_for(doc, prefix=prefix)
    if not chunks:
        record["outcome"] = SKIPPED_EMPTY
        return record

    vectors = embedder.embed_passages([c.text for c in chunks])

    record.update(
        outcome=INDEXED,
        title=title,
        content_hash=content_hash,
        extractor=doc.extractor,
        status_detail=doc.status_detail,
        raw_extraction=json.dumps(doc.extra, default=str),
        mtime=doc.mtime,
        size_bytes=doc.size_bytes,
        chunks=[
            {
                "index": chunk.index,
                "text": chunk.text,
                "token_count": chunk.token_count,
                "heading_path": chunk.heading_path,
                "locator": chunk.extra.get("locator", {}),
                "vector_b64": base64.b64encode(vault_store.pack_vector(vectors[n])).decode(),
            }
            for n, chunk in enumerate(chunks)
        ],
    )
    return record


def handle(job: dict) -> dict:
    """Process one source's worth of files. Never raises for a single bad file."""
    source = job["source"]
    known: dict[str, str] = job.get("known_hashes") or {}
    tombstoned = set(job.get("tombstoned") or ())

    records = []
    for rel_uri in job.get("files") or []:
        doc_id = doc_id_for(source["id"], rel_uri)
        if doc_id in tombstoned:
            records.append({"rel_uri": rel_uri, "doc_id": doc_id, "outcome": SKIPPED_RETRACTED})
            continue
        try:
            records.append(_parse_one(source, rel_uri, known.get(doc_id)))
        except Exception as exc:  # noqa: BLE001 - one bad file must not kill the run
            logger.exception("Failed on %s", rel_uri)
            records.append(
                {
                    "rel_uri": rel_uri,
                    "doc_id": doc_id,
                    "outcome": FAILED,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return {"records": records}


def main() -> int:
    # stderr, never stdout: stdout is the protocol.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        job = json.load(sys.stdin)
    except Exception as exc:  # noqa: BLE001
        json.dump({"error": f"unreadable job: {type(exc).__name__}: {exc}"}, sys.stdout)
        return 2
    json.dump(handle(job), sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
