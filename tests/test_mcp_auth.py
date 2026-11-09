"""The client-token gate in front of the open-tier MCP server (ADR-033)."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app import mcp_auth
from app.config import settings

WEBUI = "w" * 64
CLAUDE = "c" * 64
TOKENS = {"open-webui": WEBUI, "claude-code": CLAUDE}


def _echo(request):
    return PlainTextResponse(request.scope["state"]["rag_client"])


@pytest.fixture
def client():
    inner = Starlette(routes=[Route("/mcp", _echo, methods=["GET", "POST"])])
    return TestClient(mcp_auth.BearerAuth(inner, TOKENS))


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------
# Parsing MCP_CLIENT_TOKENS
# --------------------------------------------------------------------------


def test_parses_named_tokens():
    raw = f" open-webui:{WEBUI} , claude-code:{CLAUDE} ,"
    assert mcp_auth.parse_client_tokens(raw) == TOKENS


@pytest.mark.parametrize("raw", [None, "", " , "])
def test_unset_means_no_clients(raw):
    assert mcp_auth.parse_client_tokens(raw) == {}


@pytest.mark.parametrize(
    "raw",
    [
        WEBUI,  # no name
        f":{WEBUI}",  # empty name
        f"Open WebUI:{WEBUI}",  # bad name
        "claude-code:short",  # too short
        f"a:{WEBUI},a:{CLAUDE}",  # duplicate name
        f"a:{WEBUI},b:{WEBUI}",  # shared token: cannot revoke one alone
    ],
)
def test_malformed_entries_raise_rather_than_being_skipped(raw):
    with pytest.raises(mcp_auth.ClientTokenError):
        mcp_auth.parse_client_tokens(raw)


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize("token,name", [(WEBUI, "open-webui"), (CLAUDE, "claude-code")])
def test_each_approved_client_gets_in_under_its_own_name(client, token, name):
    r = client.post("/mcp", headers=_auth(token))
    assert r.status_code == 200 and r.text == name


@pytest.mark.parametrize(
    "headers",
    [
        {},
        _auth("x" * 64),
        _auth(WEBUI[:-1]),
        _auth(WEBUI + "x"),
        {"Authorization": WEBUI},  # no scheme
        {"Authorization": f"Basic {WEBUI}"},
        {"Authorization": "Bearer "},
        {"X-OpenWebUI-User-Jwt": WEBUI},  # the identity assertion is not a client token
    ],
)
def test_anything_else_is_refused_before_the_app(client, headers):
    r = client.post("/mcp", headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")
    assert WEBUI not in r.text and CLAUDE not in r.text


def test_scheme_is_case_insensitive(client):
    assert client.get("/mcp", headers={"Authorization": f"bearer {CLAUDE}"}).status_code == 200


def test_no_tokens_refuses_everyone():
    inner = Starlette(routes=[Route("/mcp", _echo)])
    c = TestClient(mcp_auth.BearerAuth(inner, {}))
    assert c.get("/mcp", headers=_auth(WEBUI)).status_code == 401


def test_every_path_is_gated(client):
    # Unknown routes too: a 404 would confirm what exists to an unauthenticated caller.
    assert client.get("/.well-known/oauth-protected-resource").status_code == 401


# --------------------------------------------------------------------------
# The real server
# --------------------------------------------------------------------------


def test_real_mcp_app_refuses_an_unauthenticated_initialize(monkeypatch):
    from app import main

    monkeypatch.setattr(settings, "mcp_client_tokens", f"claude-code:{CLAUDE}")
    app = main.build_app()
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    }
    accept = {"Accept": "application/json, text/event-stream"}
    with TestClient(app, base_url="http://localhost:8000") as c:
        assert c.post("/mcp", json=body, headers=accept).status_code == 401
        r = c.post("/mcp", json=body, headers={**accept, **_auth(CLAUDE)})
        assert r.status_code == 200
        assert "rag-docs" in r.text


def test_real_server_will_not_start_on_a_malformed_setting(monkeypatch):
    from app import main

    monkeypatch.setattr(settings, "mcp_client_tokens", "claude-code:short")
    with pytest.raises(mcp_auth.ClientTokenError):
        main.build_app()
