"""Encrypted storage for original documents.

"Anything sensitive stored encrypted" covers the source image, not only the
fields extracted from it -- a receipt photo sitting in plaintext next to an
encrypted ledger would make the encryption pointless.

Blobs are ciphertext sidecar files rather than database rows: a few thousand
receipts at 100 KB-2 MB each would push vault.db into gigabytes and slow the
brute-force vector load, which reads the whole vector set into memory.

Keeping the original also means a later parser improvement can re-extract
without re-photographing anything.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from app.config import settings
from app.vault import crypto
from app.vault.store import utcnow


def blob_path(doc_id: str) -> Path:
    return settings.vault_blobs_path / f"{doc_id}.enc"


def store(conn, key: bytes, doc_id: str, source: Path, *, content_type: str | None = None) -> Path:
    """Encrypt `source` into the vault and return the ciphertext path."""
    plaintext = source.read_bytes()
    nonce, ciphertext = crypto.encrypt_blob(key, plaintext)

    target = blob_path(doc_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(ciphertext)
    os.chmod(target, 0o600)

    conn.execute(
        """INSERT INTO blobs (doc_id, rel_path, content_type, size_bytes, nonce, sha256, created_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(doc_id) DO UPDATE SET
               rel_path=excluded.rel_path, content_type=excluded.content_type,
               size_bytes=excluded.size_bytes, nonce=excluded.nonce,
               sha256=excluded.sha256, created_at=excluded.created_at""",
        (
            doc_id,
            target.name,
            content_type or source.suffix.lstrip("."),
            len(plaintext),
            nonce,
            hashlib.sha256(plaintext).hexdigest(),
            utcnow(),
        ),
    )
    return target


def load(conn, key: bytes, doc_id: str) -> bytes:
    row = conn.execute("SELECT * FROM blobs WHERE doc_id=?", (doc_id,)).fetchone()
    if row is None:
        raise KeyError(f"No stored blob for {doc_id}")
    ciphertext = (settings.vault_blobs_path / row["rel_path"]).read_bytes()
    plaintext = crypto.decrypt_blob(key, row["nonce"], ciphertext)
    if hashlib.sha256(plaintext).hexdigest() != row["sha256"]:
        raise ValueError(f"Blob {doc_id} failed its integrity check.")
    return plaintext


def consume(conn, key: bytes, doc_id: str, source: Path, *, content_type: str | None = None) -> Path:
    """Encrypt the original into the vault, then remove the plaintext.

    `make add` into a vault domain deliberately CONSUMES the file. Leaving the
    original behind would leave the sensitive content readable on a running
    box regardless of whether the vault is sealed.
    """
    target = store(conn, key, doc_id, source, content_type=content_type)
    source.unlink()
    return target


def delete(conn, doc_id: str) -> None:
    row = conn.execute("SELECT rel_path FROM blobs WHERE doc_id=?", (doc_id,)).fetchone()
    if row:
        (settings.vault_blobs_path / row["rel_path"]).unlink(missing_ok=True)
        conn.execute("DELETE FROM blobs WHERE doc_id=?", (doc_id,))
