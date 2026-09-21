"""The gates must fail closed.

These are the tests that matter most: every one of them asserts that something
is REFUSED. A regression here does not break a feature, it silently exposes
financial and personal data, so each failure mode gets its own case.
"""

from __future__ import annotations

import time

import pytest

from app.vault import grants, identity, service
from app.vault.crypto import VaultSealed, WrongPassphrase
from app.vault.keyagent import AGENT


def mcp_ctx(tok, chat="c1", message="m1"):
    return service.Context(
        transport=service.MCP,
        headers={"X-OpenWebUI-User-Jwt": tok},
        chat_id=chat,
        message_id=message,
    )


# --- gate 1: sealed --------------------------------------------------------


def test_sealed_vault_denies_model(vault_dir, token):
    with pytest.raises(VaultSealed):
        service.search("anything", ctx=mcp_ctx(token()))


def test_sealed_vault_denies_cli_too(vault_dir):
    """The CLI skips only the approval gate, never the key."""
    with pytest.raises(VaultSealed):
        service.search("anything", ctx=service.Context())


def test_status_readable_while_sealed(vault_dir):
    status = service.status()
    assert status["unlocked"] is False
    assert "chunks_by_domain" not in status  # reveals no contents


def test_wrong_passphrase_rejected(unlocked):
    AGENT.lock()
    with pytest.raises(WrongPassphrase):
        AGENT.unlock("not the passphrase")


def test_key_ttl_wipes(vault_dir):
    from app.vault import crypto, store

    AGENT.unlock("test passphrase", ttl_seconds=600)
    conn = AGENT.connect()
    store.init(conn)
    conn.close()
    crypto.write_verifier(AGENT.key())

    AGENT.unlock("test passphrase", ttl_seconds=1)
    time.sleep(1.1)
    assert AGENT.is_unlocked() is False
    with pytest.raises(VaultSealed):
        AGENT.key()


# --- gate 2: identity ------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"secret": "wrong-secret-entirely-0000000000"}, id="forged-signature"),
        pytest.param({"ttl": -10}, id="expired"),
        pytest.param({"email": "someone@else.com"}, id="email-not-allowlisted"),
        pytest.param({"role": "user"}, id="role-not-permitted"),
    ],
)
def test_identity_rejected(unlocked, token, kwargs):
    with pytest.raises(identity.IdentityError):
        service.search("receipts", ctx=mcp_ctx(token(**kwargs)))


def test_missing_jwt_rejected(unlocked):
    ctx = service.Context(transport=service.MCP, headers={}, chat_id="c1", message_id="m1")
    with pytest.raises(identity.IdentityError):
        service.search("receipts", ctx=ctx)


def test_plaintext_headers_are_not_identity(unlocked):
    """Open WebUI's unsigned X-OpenWebUI-User-* headers must carry no weight.

    Anything that can reach the port can set them, which is exactly why the
    signed assertion is the only thing checked.
    """
    ctx = service.Context(
        transport=service.MCP,
        headers={"X-OpenWebUI-User-Email": "owner@example.com", "X-OpenWebUI-User-Role": "admin"},
        chat_id="c1",
        message_id="m1",
    )
    with pytest.raises(identity.IdentityError):
        service.search("receipts", ctx=ctx)


def test_empty_allowlist_denies_everyone(unlocked, token, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "vault_allowed_emails", "")
    with pytest.raises(identity.IdentityError):
        service.search("receipts", ctx=mcp_ctx(token()))


# --- gate 3: per-request approval -----------------------------------------


def test_valid_identity_still_needs_approval(unlocked, token):
    grants.REGISTRY.clear()
    with pytest.raises(grants.PendingApproval) as excinfo:
        service.search("receipts", ctx=mcp_ctx(token()))
    grant = excinfo.value.grant
    assert grant.code.isdigit() and len(grant.code) == 6
    # The approver must be able to see who and what they are releasing.
    assert "owner@example.com" in grant.principal
    assert "receipts" in grant.query_preview


def test_approval_releases_then_cannot_be_replayed(unlocked, token):
    grants.REGISTRY.clear()
    tok = token()
    with pytest.raises(grants.PendingApproval) as excinfo:
        service.search("receipts", ctx=mcp_ctx(tok))
    grants.REGISTRY.approve(excinfo.value.grant.code)

    service.search("receipts", ctx=mcp_ctx(tok))  # released

    with pytest.raises(grants.PendingApproval):
        service.search("receipts", ctx=mcp_ctx(tok))  # single use


def test_grant_redeems_on_the_next_turn_in_the_same_chat(unlocked, token):
    """The approval is typed at a terminal after the triggering turn has ended.

    So the next turn -- a new message_id, same chat, same query -- is the one
    that must redeem it. Binding to message_id instead would leave the grant
    permanently unredeemable.
    """
    grants.REGISTRY.clear()
    tok = token()
    with pytest.raises(grants.PendingApproval) as excinfo:
        service.search("receipts", ctx=mcp_ctx(tok, message="m1"))
    grants.REGISTRY.approve(excinfo.value.grant.code)

    service.search("receipts", ctx=mcp_ctx(tok, message="m2"))  # released

    with pytest.raises(grants.PendingApproval):
        service.search("receipts", ctx=mcp_ctx(tok, message="m3"))  # still single use


def test_grant_does_not_carry_to_another_chat(unlocked, token):
    grants.REGISTRY.clear()
    tok = token()
    with pytest.raises(grants.PendingApproval) as excinfo:
        service.search("receipts", ctx=mcp_ctx(tok, chat="c1"))
    grants.REGISTRY.approve(excinfo.value.grant.code)

    with pytest.raises(grants.PendingApproval):
        service.search("receipts", ctx=mcp_ctx(tok, chat="c2"))


def test_grant_does_not_carry_to_another_query(unlocked, token):
    grants.REGISTRY.clear()
    tok = token()
    with pytest.raises(grants.PendingApproval) as excinfo:
        service.search("receipts", ctx=mcp_ctx(tok))
    grants.REGISTRY.approve(excinfo.value.grant.code)

    with pytest.raises(grants.PendingApproval):
        service.search("something else entirely", ctx=mcp_ctx(tok))


def test_grant_expires(unlocked, token, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "vault_grant_ttl_seconds", 1)
    grants.REGISTRY.clear()
    tok = token()
    with pytest.raises(grants.PendingApproval) as excinfo:
        service.search("receipts", ctx=mcp_ctx(tok))
    code = excinfo.value.grant.code
    time.sleep(1.2)
    with pytest.raises(KeyError):
        grants.REGISTRY.approve(code)


def test_cli_path_skips_approval_only(unlocked):
    """A human at a terminal is already the approval."""
    assert service.search("receipts", ctx=service.Context()) == []


# --- ledger SQL ------------------------------------------------------------


def test_ledger_rejects_unknown_filter(unlocked):
    with pytest.raises(ValueError, match="Unsupported filter"):
        service.query_ledger(evil="1; DROP TABLE ledger", ctx=service.Context())


def test_ledger_rejects_unknown_group_by(unlocked):
    with pytest.raises(ValueError, match="Unsupported group_by"):
        service.query_ledger(group_by="1; DROP TABLE ledger", ctx=service.Context())


# --- encryption at rest ----------------------------------------------------


def test_vault_file_is_ciphertext(unlocked):
    from app.vault import store
    from app.vault.keyagent import AGENT as agent

    conn = agent.connect()
    store.upsert_document(
        conn,
        doc_id="d1",
        source_id="inbox:receipts",
        domain="receipts",
        uri="r.jpg",
        title="Distinctive Merchant Name",
        content_hash="h",
        extractor="vlm",
        status="indexed",
    )
    conn.commit()
    conn.close()

    raw = (unlocked / "vault.db").read_bytes()
    assert b"Distinctive Merchant Name" not in raw
    assert not raw.startswith(b"SQLite format 3")


def test_open_tier_domain_rejected_by_vault_search(unlocked):
    with pytest.raises(PermissionError, match="open-tier"):
        service.search("x", domains=["manuals"], ctx=service.Context())


# --- fetch_context is not a bypass ------------------------------------------


def test_fetch_context_denied_while_sealed(vault_dir, token):
    """A context fetch returns vault chunk text, so it is gated exactly like a
    search. Anything less would be a hole straight through the boundary."""
    with pytest.raises(VaultSealed):
        service.fetch_context("d1", 0, ctx=mcp_ctx(token()))


def test_fetch_context_denied_without_identity(unlocked):
    with pytest.raises(identity.IdentityError):
        service.fetch_context("d1", 0, ctx=service.Context(transport=service.MCP, headers={}))


def test_fetch_context_needs_its_own_approval(unlocked, token):
    grants.REGISTRY.clear()
    with pytest.raises(grants.PendingApproval):
        service.fetch_context("d1", 0, ctx=mcp_ctx(token()))


def test_search_approval_does_not_release_fetch_context(unlocked, token):
    """Otherwise a model could walk a whole document out one neighbour at a
    time on the strength of a single approved search."""
    grants.REGISTRY.clear()
    tok = token()
    with pytest.raises(grants.PendingApproval) as excinfo:
        service.search("receipts", ctx=mcp_ctx(tok))
    grants.REGISTRY.approve(excinfo.value.grant.code)

    with pytest.raises(grants.PendingApproval):
        service.fetch_context("d1", 0, ctx=mcp_ctx(tok))


# --- lifecycle is not a model-facing control --------------------------------


def test_model_cannot_change_what_counts_as_true():
    """There is deliberately no MCP tool for lifecycle. Retraction decides what
    the system believes; that is a human's call, made at a terminal."""
    import app.vault_server as vault_server

    exposed = {
        name
        for name, value in vars(vault_server).items()
        if callable(value) and not name.startswith("_")
    }
    assert "set_lifecycle" not in exposed
    assert "retract" not in exposed


def test_vault_lifecycle_change_requires_an_unlocked_vault(vault_dir):
    with pytest.raises(VaultSealed):
        service.set_lifecycle("receipt.pdf", "retracted", reason="wrong")


def test_retracting_a_vault_document_destroys_its_chunks(unlocked):
    from app import documents
    from app.vault import store

    conn = AGENT.connect()
    store.upsert_document(
        conn, doc_id="d1", source_id="s", domain="receipts", uri="receipt.pdf",
        title="Receipt", content_hash="h", extractor="vlm", status="indexed",
    )
    store.replace_chunks(conn, "d1", [{
        "chunk_id": "d1-0", "doc_id": "d1", "domain": "receipts", "chunk_index": 0,
        "text": "Distinctive Merchant Name 42.00", "content_hash": "c",
        "vector": store.pack_vector([0.1] * 384), "token_count": 8,
        "heading_path": "[]", "locator": "{}", "indexed_at": store.utcnow(),
    }])
    conn.commit()
    conn.close()

    service.set_lifecycle("receipt.pdf", documents.RETRACTED, reason="wrong merchant")

    conn = AGENT.connect()
    try:
        assert conn.execute("SELECT COUNT(*) n FROM chunks WHERE doc_id='d1'").fetchone()["n"] == 0
        row = store.get_document(conn, "d1")
        # The row stays: it IS the exclusion that keeps re-ingestion out.
        assert row is not None
        assert row["lifecycle"] == documents.RETRACTED
        assert not store.search_keyword(conn, "Distinctive", domains=["receipts"])
    finally:
        conn.close()


def test_approved_grant_survives_the_pending_window(unlocked, token, monkeypatch):
    """The clock must restart on approval, or approvals are unredeemable.

    Measured from creation, the window had to cover reading the chat, running
    `make approve`, reading the confirmation, typing `yes`, returning to the
    chat, re-asking, and the local model prefilling and generating. It did not,
    and the first real end-to-end attempt expired between the approval and the
    re-ask.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "vault_grant_ttl_seconds", 1)
    monkeypatch.setattr(settings, "vault_redeem_ttl_seconds", 600)
    grants.REGISTRY.clear()
    tok = token()

    with pytest.raises(grants.PendingApproval) as excinfo:
        service.search("receipts", ctx=mcp_ctx(tok, message="m1"))
    grants.REGISTRY.approve(excinfo.value.grant.code)

    time.sleep(1.2)  # past the pending window, well inside the redeem window
    service.search("receipts", ctx=mcp_ctx(tok, message="m2"))  # released


def test_unapproved_grant_still_expires_fast(unlocked, token, monkeypatch):
    """The short window is the point for anything a human has NOT approved."""
    from app.config import settings

    monkeypatch.setattr(settings, "vault_grant_ttl_seconds", 1)
    monkeypatch.setattr(settings, "vault_redeem_ttl_seconds", 600)
    grants.REGISTRY.clear()

    with pytest.raises(grants.PendingApproval) as excinfo:
        service.search("receipts", ctx=mcp_ctx(token()))
    code = excinfo.value.grant.code

    time.sleep(1.2)
    with pytest.raises(KeyError):
        grants.REGISTRY.approve(code)


def test_approved_grant_does_eventually_expire(unlocked, token, monkeypatch):
    """Restarting the clock must not mean never expiring."""
    from app.config import settings

    monkeypatch.setattr(settings, "vault_grant_ttl_seconds", 60)
    monkeypatch.setattr(settings, "vault_redeem_ttl_seconds", 1)
    grants.REGISTRY.clear()
    tok = token()

    with pytest.raises(grants.PendingApproval) as excinfo:
        service.search("receipts", ctx=mcp_ctx(tok, message="m1"))
    grants.REGISTRY.approve(excinfo.value.grant.code)

    time.sleep(1.2)
    with pytest.raises(grants.PendingApproval):
        service.search("receipts", ctx=mcp_ctx(tok, message="m2"))
