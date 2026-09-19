"""Key derivation, encrypted database handles, and blob encryption.

The design point is that the key is never on disk. A key sitting in a file
beside the database it protects would be theater: anything that can read the
database can read the key. Here the passphrase is typed at a terminal, the key
is derived with Argon2id, and it lives only in process memory with a TTL.

The disk is already LUKS-encrypted, so powered-off theft is covered without any
of this. What this adds is the sealed-by-default property on a *running*
system: while locked, the data is unreadable even to root, and a model cannot
unlock it because unlocking needs a human at a TTY.
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import settings

KEY_BYTES = 32
SALT_BYTES = 16
NONCE_BYTES = 12
SALT_FILENAME = "vault.salt"
VERIFIER_FILENAME = "vault.verifier"


class VaultSealed(RuntimeError):
    """Raised whenever plaintext is requested and no key is held."""


class WrongPassphrase(ValueError):
    pass


def salt_path() -> Path:
    return settings.vault_db_path.parent / SALT_FILENAME


def verifier_path() -> Path:
    return settings.vault_db_path.parent / VERIFIER_FILENAME


def load_or_create_salt() -> bytes:
    """The salt is not secret; it only has to be stable and unique."""
    path = salt_path()
    if path.exists():
        salt = path.read_bytes()
        if len(salt) != SALT_BYTES:
            raise RuntimeError(f"{path} is corrupt: expected {SALT_BYTES} bytes, got {len(salt)}")
        return salt
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(SALT_BYTES)
    path.write_bytes(salt)
    os.chmod(path, 0o600)
    return salt


def derive_key(passphrase: str, salt: bytes | None = None) -> bytes:
    """Argon2id. Deliberately expensive: this is the only thing between a
    stolen vault file and its contents."""
    return hash_secret_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt if salt is not None else load_or_create_salt(),
        time_cost=settings.vault_kdf_time_cost,
        memory_cost=settings.vault_kdf_memory_kib,
        parallelism=settings.vault_kdf_parallelism,
        hash_len=KEY_BYTES,
        type=Type.ID,
    )


def _verifier_for(key: bytes) -> bytes:
    return hmac.new(key, b"rag-vault-verifier-v1", "sha256").digest()


def write_verifier(key: bytes) -> None:
    """Lets `unlock` reject a typo immediately rather than surfacing it later
    as an opaque database error."""
    path = verifier_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_verifier_for(key))
    os.chmod(path, 0o600)


def check_verifier(key: bytes) -> bool:
    path = verifier_path()
    if not path.exists():
        return True  # first unlock on a fresh vault
    return hmac.compare_digest(path.read_bytes(), _verifier_for(key))


def connect(key: bytes, path: Path | None = None):
    """Open the encrypted database with a raw key.

    `PRAGMA key = "x'<hex>'"` supplies the key material directly, so SQLCipher
    skips its own PBKDF2 and Argon2id above is the only KDF in play.
    """
    from sqlcipher3 import dbapi2 as sqlcipher

    target = path or settings.vault_db_path
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlcipher.connect(str(target), timeout=10.0)
    conn.row_factory = sqlcipher.Row
    conn.execute(f"PRAGMA key = \"x'{key.hex()}'\"")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    # Force a read so a wrong key fails here rather than at some later query.
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except Exception as exc:
        conn.close()
        raise WrongPassphrase("Could not decrypt the vault with that passphrase.") from exc
    return conn


# --------------------------------------------------------------------------
# Blob encryption -- original documents, kept as ciphertext sidecar files
# --------------------------------------------------------------------------


def _blob_key(master: bytes) -> bytes:
    """Separate subkey so blob encryption and database encryption never share
    key material."""
    return HKDF(
        algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=b"rag-vault-blob-v1"
    ).derive(master)


def encrypt_blob(master: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
    nonce = secrets.token_bytes(NONCE_BYTES)
    return nonce, AESGCM(_blob_key(master)).encrypt(nonce, plaintext, None)


def decrypt_blob(master: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    return AESGCM(_blob_key(master)).decrypt(nonce, ciphertext, None)
