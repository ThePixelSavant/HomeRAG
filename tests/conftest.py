from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _fast_kdf():
    """Argon2id is deliberately expensive in production; tests would crawl."""
    os.environ.setdefault("VAULT_KDF_MEMORY_KIB", "8192")
    os.environ.setdefault("VAULT_KDF_TIME_COST", "1")


@pytest.fixture
def vault_dir(monkeypatch):
    """A throwaway vault, isolated from any real one."""
    from app.config import settings
    from app.vault.keyagent import AGENT

    tmp = Path(tempfile.mkdtemp())
    monkeypatch.setattr(settings, "vault_db_path", tmp / "vault.db")
    monkeypatch.setattr(settings, "vault_blobs_path", tmp / "blobs")
    monkeypatch.setattr(settings, "vault_jwt_secret", "test-secret-value-32-bytes-long!!")
    monkeypatch.setattr(settings, "vault_allowed_emails", "owner@example.com")
    AGENT.lock()
    yield tmp
    AGENT.lock()


@pytest.fixture
def unlocked(vault_dir):
    from app.vault import crypto, store
    from app.vault.keyagent import AGENT

    AGENT.unlock("test passphrase", ttl_seconds=600)
    conn = AGENT.connect()
    store.init(conn)
    conn.close()
    crypto.write_verifier(AGENT.key())
    return vault_dir


@pytest.fixture
def token():
    import time

    import jwt

    from app.config import settings

    def _make(email="owner@example.com", role="admin", secret=None, ttl=300, sub="u1"):
        now = int(time.time())
        return jwt.encode(
            {
                "sub": sub,
                "email": email,
                "name": "Owner",
                "role": role,
                "iss": "open-webui",
                "iat": now,
                "exp": now + ttl,
            },
            secret or settings.vault_jwt_secret,
            algorithm="HS256",
        )

    return _make
