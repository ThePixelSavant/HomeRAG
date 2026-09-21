"""Gate 2: who is asking.

Open WebUI mints a signed HS256 JWT carrying sub, email, name, role, iss and
exp when FORWARD_USER_INFO_HEADER_JWT_SECRET is set.

It is minted ONCE PER TURN, not per tool call: `connect_mcp_server` opens the
MCP session inside `process_chat_payload` and hands the token to
`httpx.AsyncClient` as a static default header, reused for every call in that
turn. The clock therefore starts before the model prefills or generates a
single token, which is why the assertion's lifetime is left at its 300s default
-- a shorter window expires mid-turn on a long prompt and looks exactly like a
mismatched secret. Verified against the running 0.11.0 image. Because it is signed with a secret only Open WebUI
and this service share, it cannot be forged by anything else that can reach the
port -- unlike the plaintext X-OpenWebUI-User-* headers, which anyone could set.

What this canNOT tell us is which MODEL is asking. The full placeholder set
Open WebUI exposes is CHAT_ID, MESSAGE_ID, USER_MESSAGE_ID,
USER_MESSAGE_PARENT_ID, FILE_ID, FILE_NAME, FILE_CONTENT_TYPE, TASK, USER_ID,
USER_NAME, USER_EMAIL, USER_ROLE, USER_GROUPS, USER_GROUP_IDS, USER_AGENT --
there is no model field. Keeping vault tools off frontier model presets is
therefore a configuration control enforced by Open WebUI's per-model tool_ids,
and the per-request approval is what backstops it.
"""

from __future__ import annotations

from dataclasses import dataclass

import jwt

from app.config import settings


class IdentityError(PermissionError):
    """Identity absent, unverifiable, or not permitted."""


@dataclass(frozen=True)
class Principal:
    subject: str
    email: str
    name: str
    role: str

    def __str__(self) -> str:
        return f"{self.email} ({self.name}, {self.role})"


def verify(headers: dict[str, str]) -> Principal:
    """Verify the forwarded identity assertion, or raise."""
    if not settings.vault_jwt_secret:
        raise IdentityError(
            "VAULT_JWT_SECRET is not configured, so no caller's identity can be verified. "
            "Set it to the same value as FORWARD_USER_INFO_HEADER_JWT_SECRET in the "
            "Open WebUI environment."
        )

    lookup = {k.lower(): v for k, v in headers.items()}
    token = lookup.get(settings.vault_jwt_header.lower())
    if not token:
        raise IdentityError(
            f"No {settings.vault_jwt_header} header. Vault tools require a verified identity."
        )

    try:
        claims = jwt.decode(
            token,
            settings.vault_jwt_secret,
            algorithms=["HS256"],
            issuer=settings.vault_jwt_issuer,
            options={"require": ["exp", "iss", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise IdentityError("Identity assertion has expired.") from None
    except jwt.InvalidIssuerError:
        raise IdentityError("Identity assertion has the wrong issuer.") from None
    except jwt.InvalidTokenError as exc:
        raise IdentityError(f"Identity assertion failed verification: {exc}") from None

    email = str(claims.get("email", "")).strip().lower()
    role = str(claims.get("role", "")).strip().lower()

    allowed_emails = settings.allowed_emails
    if not allowed_emails:
        raise IdentityError(
            "VAULT_ALLOWED_EMAILS is empty, so no identity is permitted. This is "
            "deliberate: an unset allowlist must deny, not allow."
        )
    if email not in allowed_emails:
        raise IdentityError(f"{email or 'unknown'} is not permitted to reach vault data.")
    if role not in settings.allowed_roles:
        raise IdentityError(f"Role {role or 'unknown'!r} is not permitted.")

    return Principal(
        subject=str(claims["sub"]),
        email=email,
        name=str(claims.get("name", "")),
        role=role,
    )
