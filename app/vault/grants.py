"""Gate 3: per-request human approval.

A verified identity is not sufficient. A model that has been prompt-injected by
a poisoned open-tier document is still a legitimately authenticated caller, so
gates 1 and 2 pass and only a human standing outside the loop can catch it.

A grant is bound to (subject, chat_id, query_hash) and is single-use. That is
what makes "granted for that request only" literal: an approval cannot be
redeemed for a different query, in a different chat, or a second time.

It is deliberately NOT bound to message_id. Open WebUI mints a new message id
for every turn, and the approval is typed at a terminal after the turn that
triggered it has already ended -- so a message binding could only ever be
redeemed by the model retrying mid-turn while a human raced it to the
terminal. Binding to the chat instead lets the owner approve and then re-ask,
which is the flow the approval gate is actually for. The originating
message_id is still recorded on the grant and shown to the approver.

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
    approved_at: float | None = None

    def expires_at(self) -> float:
        """When this grant stops being usable.

        Two windows, because they do different jobs. Before approval the clock
        runs from creation and is deliberately short. After approval it
        restarts, because the owner has now made a decision and needs time to
        act on it -- go back to the chat, re-ask, and wait for the model.

        Running one short clock from creation made approvals unredeemable in
        practice: by the time `make approve` had been read and confirmed,
        there was not enough of the window left to re-ask in.
        """
        if self.approved and self.approved_at is not None:
            return self.approved_at + settings.vault_redeem_ttl_seconds
        return self.created_at + settings.vault_grant_ttl_seconds


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

    def _prune_locked(self) -> None:
        now = time.time()
        for code in [c for c, g in self._grants.items() if now > g.expires_at()]:
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
                # message_id is recorded but not matched on -- see the module
                # docstring. Widening it to the chat is what makes an approval
                # redeemable on the turn after it was granted.
                if (
                    grant.subject == subject
                    and grant.tool == tool
                    and grant.chat_id == chat_id
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
            grant.approved_at = time.time()
            return grant

    def clear(self) -> None:
        with self._lock:
            self._grants.clear()


REGISTRY = GrantRegistry()
