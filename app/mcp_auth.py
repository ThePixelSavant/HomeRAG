"""Bearer-token gate in front of the open-tier MCP server (ADR-033).

Each approved client has its own named token in MCP_CLIENT_TOKENS:

    MCP_CLIENT_TOKENS=open-webui:<token>,claude-code:<token>

A request without one of those tokens in `Authorization: Bearer` gets a 401
before it reaches the MCP app. With no tokens configured every request is
refused: an unset variable must not quietly reopen the server.

Separate names so one client can be revoked without re-keying the others, and
so a refusal or an acceptance can say which client it was. The token identifies
a client configuration, not a model: every model that client runs gets it.

Pure ASGI rather than Starlette's BaseHTTPMiddleware, which buffers responses
and breaks the streamable-HTTP transport's SSE stream.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
from collections.abc import Mapping

logger = logging.getLogger(__name__)

# Same floor as REPORT_TOKEN. `openssl rand -hex 32` gives 64.
MIN_TOKEN_LENGTH = 32
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ClientTokenError(ValueError):
    """MCP_CLIENT_TOKENS is malformed. Raised at startup, never per request."""


def parse_client_tokens(raw: str | None) -> dict[str, str]:
    """Parse `name:token,name:token` into {name: token}.

    Raises on anything doubtful rather than skipping it: a mistyped entry that
    is silently dropped looks exactly like a client that was revoked.
    """
    tokens: dict[str, str] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, sep, token = entry.partition(":")
        name, token = name.strip(), token.strip()
        if not sep or not name:
            raise ClientTokenError(
                "MCP_CLIENT_TOKENS entries must be name:token "
                "(e.g. claude-code:<openssl rand -hex 32>)"
            )
        if not _NAME.match(name):
            raise ClientTokenError(
                f"MCP_CLIENT_TOKENS client name {name!r} must be lowercase letters, "
                "digits, '-' or '_'"
            )
        if len(token) < MIN_TOKEN_LENGTH:
            raise ClientTokenError(
                f"MCP_CLIENT_TOKENS token for {name!r} is under {MIN_TOKEN_LENGTH} "
                "characters; generate one with: openssl rand -hex 32"
            )
        if name in tokens:
            raise ClientTokenError(f"MCP_CLIENT_TOKENS names {name!r} twice")
        if token in tokens.values():
            raise ClientTokenError(
                f"MCP_CLIENT_TOKENS gives {name!r} another client's token; "
                "each client needs its own so it can be revoked alone"
            )
        tokens[name] = token
    return tokens


def client_for(authorization: str | None, tokens: Mapping[str, str]) -> str | None:
    """The client name an Authorization header belongs to, or None."""
    scheme, _, supplied = (authorization or "").strip().partition(" ")
    supplied = supplied.strip()
    if scheme.lower() != "bearer" or not supplied:
        return None
    match = None
    # Compare against every token, without stopping early, so timing says
    # nothing about which entry came close.
    for name, token in tokens.items():
        if hmac.compare_digest(supplied.encode(), token.encode()):
            match = name
    return match


class BearerAuth:
    """ASGI middleware: refuse any HTTP request without an approved token."""

    def __init__(self, app, tokens: Mapping[str, str]):
        self.app = app
        self.tokens = dict(tokens)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            # Startup/shutdown of the MCP session manager, not a request.
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            # Nothing here serves websockets; refuse rather than pass through.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        name = client_for(headers.get("authorization"), self.tokens)
        if name is None:
            client = scope.get("client") or ("?", 0)
            logger.warning(
                "refused %s %s from %s: %s",
                scope.get("method"),
                scope.get("path"),
                client[0],
                "no approved client token" if self.tokens else "MCP_CLIENT_TOKENS is unset",
            )
            await _unauthorized(send)
            return

        scope.setdefault("state", {})["rag_client"] = name
        logger.debug("%s %s from client %s", scope.get("method"), scope.get("path"), name)
        await self.app(scope, receive, send)


async def _unauthorized(send) -> None:
    body = json.dumps({"error": "unauthorized"}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b'Bearer realm="rag-docs"'),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
