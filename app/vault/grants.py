"""Gate 3: per-request human approval.

A verified identity is not sufficient. A model that has been prompt-injected by
a poisoned open-tier document is still a legitimately authenticated caller, so
gates 1 and 2 pass and only a human standing outside the loop can catch it.

A grant is bound to (subject, chat_id, message_id, query_hash) and is
single-use. The message binding is what makes "granted for that request only"
literal: the same approval cannot be redeemed on the next turn, and cannot be
redeemed for a different query.

Grants live in memory only. A restart drops every pending approval, which is
the safe direction.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass

from app.config import settings


def query_hash(tool: str, payload: str) -> str:
    return hashlib.sha256(f"{tool}\x00{payload}".encode()).hexdigest()


@dataclass
class Grant:
    code: str
    subject: str
    principal: str
    tool: str
    chat_id: str
    message_id: str
    query_hash: str
    query_preview: str
    created_at: float
    approved: bool = False
    redeemed: bool = False

    def expires_at(self, ttl: int) -> float:
        return self.created_at + ttl


class PendingApproval(Exception):
    """Raised to hand the caller a code instead of data."""

    def __init__(self, grant: Grant) -> None:
        self.grant = grant
        super().__init__(
            f"Approval required. Run `make approve CODE={grant.code}` at a terminal "
            "to release this specific request."
        )


class GrantRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._grants: dict[str, Grant] = {}

    def _ttl(self) -> int:
        return settings.vault_grant_ttl_seconds

    def _prune_locked(self) -> None:
        now = time.time()
        for code in [c for c, g in self._grants.items() if now > g.expires_at(self._ttl())]:
            del self._grants[code]

    def request(
        self,
        *,
        subject: str,
        principal: str,
        tool: str,
        chat_id: str,
        message_id: str,
        qhash: str,
        query_preview: str,
    ) -> Grant:
        """Find an approved, unredeemed grant for this exact request, or create
        a pending one and raise."""
        with self._lock:
            self._prune_locked()
            for grant in self._grants.values():
                if (
                    grant.subject == subject
                    and grant.tool == tool
                    and grant.chat_id == chat_id
                    and grant.message_id == message_id
                    and grant.query_hash == qhash
                ):
                    if grant.approved and not grant.redeemed:
                        grant.redeemed = True
                        return grant
                    if not grant.approved:
                        raise PendingApproval(grant)
                    # Approved but already redeemed: this is a replay.
                    break

            grant = Grant(
                code=f"{secrets.randbelow(10**6):06d}",
                subject=subject,
                principal=principal,
                tool=tool,
                chat_id=chat_id,
                message_id=message_id,
                query_hash=qhash,
                query_preview=query_preview,
                created_at=time.time(),
            )
            self._grants[grant.code] = grant
            raise PendingApproval(grant)

    def pending(self) -> list[Grant]:
        with self._lock:
            self._prune_locked()
            return [g for g in self._grants.values() if not g.approved]

    def get(self, code: str) -> Grant | None:
        with self._lock:
            self._prune_locked()
            return self._grants.get(code)

    def approve(self, code: str) -> Grant:
        with self._lock:
            self._prune_locked()
            grant = self._grants.get(code)
            if grant is None:
                raise KeyError(f"No pending request with code {code} (it may have expired).")
            if grant.approved:
                raise ValueError(f"Request {code} was already approved.")
            grant.approved = True
            return grant

    def clear(self) -> None:
        with self._lock:
            self._grants.clear()


REGISTRY = GrantRegistry()
