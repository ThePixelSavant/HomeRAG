"""The in-memory key holder.

Holds the derived key for a bounded time and nowhere else. Nothing here writes
a key to disk, logs one, or puts one in an environment variable.

A model cannot unlock the vault, because unlocking requires a passphrase typed
at a terminal. That is the mechanism behind "cannot be queried by a model
unless I deliberately allow it" -- it is enforced, not policy.
"""

from __future__ import annotations

import logging
import threading
import time

from app.config import settings
from app.vault import crypto
from app.vault.crypto import VaultSealed

logger = logging.getLogger(__name__)


class KeyAgent:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: bytearray | None = None
        self._expires_at: float = 0.0

    # -- state ------------------------------------------------------------

    def _expired(self) -> bool:
        return self._key is None or time.time() >= self._expires_at

    def is_unlocked(self) -> bool:
        with self._lock:
            if self._expired():
                self._wipe_locked()
                return False
            return True

    def seconds_remaining(self) -> int:
        with self._lock:
            if self._expired():
                return 0
            return max(0, int(self._expires_at - time.time()))

    # -- lifecycle --------------------------------------------------------

    def unlock(self, passphrase: str, ttl_seconds: int | None = None) -> None:
        key = crypto.derive_key(passphrase)
        if not crypto.check_verifier(key):
            raise crypto.WrongPassphrase("That passphrase does not match this vault.")
        ttl = ttl_seconds if ttl_seconds is not None else settings.vault_key_ttl_seconds
        with self._lock:
            self._wipe_locked()
            self._key = bytearray(key)
            self._expires_at = time.time() + ttl
        logger.info("Vault unlocked for %ds", ttl)

    def _wipe_locked(self) -> None:
        if self._key is not None:
            # Overwrite before dropping the reference. Python offers no
            # guarantee the allocator will not have copied it, but zeroing the
            # buffer we control is strictly better than dropping it intact.
            for i in range(len(self._key)):
                self._key[i] = 0
        self._key = None
        self._expires_at = 0.0

    def lock(self) -> None:
        with self._lock:
            had_key = self._key is not None
            self._wipe_locked()
        if had_key:
            logger.info("Vault sealed; key wiped")

    def key(self) -> bytes:
        with self._lock:
            if self._expired():
                self._wipe_locked()
                raise VaultSealed(
                    "Vault is sealed. Run `make unlock` at a terminal -- the passphrase "
                    "cannot be supplied any other way."
                )
            return bytes(self._key)

    def connect(self):
        return crypto.connect(self.key())


AGENT = KeyAgent()
