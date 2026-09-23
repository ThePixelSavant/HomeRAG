"""Control channel for the running vault server.

The key lives in the vault server's memory, and `make unlock` runs as a
separate process -- so the passphrase has to reach that server somehow.

It travels over a Unix domain socket on the bind-mounted data directory, never
over the network. That matters: an HTTP unlock endpoint on this container would
be reachable from every other container on llm-net, including Open WebUI, which
would hand the model a way to try passphrases. A socket file with mode 0600 is
reachable only by something already inside the trust boundary.

Commands are newline-delimited JSON.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

SOCKET_NAME = "control.sock"


def socket_path() -> Path:
    return settings.vault_db_path.parent / SOCKET_NAME


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------


def _handle(request: dict) -> dict:
    from app.vault import audit, grants, service
    from app.vault.crypto import WrongPassphrase
    from app.vault.keyagent import AGENT

    action = request.get("action")

    if action == "unlock":
        passphrase = request.get("passphrase") or ""
        if not passphrase:
            return {"ok": False, "error": "empty passphrase"}
        try:
            AGENT.unlock(passphrase, request.get("ttl"))
        except WrongPassphrase as exc:
            return {"ok": False, "error": str(exc)}
        # First unlock on a fresh vault establishes the schema and the
        # verifier that later attempts are checked against.
        from app.vault import crypto, store

        conn = AGENT.connect()
        try:
            store.init(conn)
        finally:
            conn.close()
        crypto.write_verifier(AGENT.key())
        return {"ok": True, "seconds_remaining": AGENT.seconds_remaining()}

    if action == "lock":
        AGENT.lock()
        return {"ok": True}

    if action == "status":
        return {"ok": True, "status": service.status()}

    if action == "pending":
        return {
            "ok": True,
            "pending": [
                {
                    "code": g.code,
                    "principal": g.principal,
                    "tool": g.tool,
                    "chat_id": g.chat_id,
                    "message_id": g.message_id,
                    "query_preview": g.query_preview,
                    "arguments": g.arguments,
                }
                for g in grants.REGISTRY.pending()
            ],
        }

    if action == "approve":
        try:
            grant = grants.REGISTRY.approve(str(request.get("code", "")))
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "grant": {
                "code": grant.code,
                "principal": grant.principal,
                "tool": grant.tool,
                "chat_id": grant.chat_id,
                "query_preview": grant.query_preview,
                "arguments": grant.arguments,
            },
        }

    if action == "audit":
        if not AGENT.is_unlocked():
            return {"ok": False, "error": "vault sealed; the audit log is encrypted with it"}
        conn = AGENT.connect()
        try:
            rows = audit.tail(conn, int(request.get("limit", 50)))
            return {"ok": True, "rows": [dict(r) for r in rows]}
        finally:
            conn.close()

    return {"ok": False, "error": f"unknown action {action!r}"}


def _serve(server: socket.socket) -> None:
    while True:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        try:
            with conn, conn.makefile("rwb") as stream:
                line = stream.readline()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    response = {"ok": False, "error": "malformed request"}
                else:
                    try:
                        response = _handle(request)
                    except Exception as exc:  # noqa: BLE001 - never kill the channel
                        logger.exception("Control command failed")
                        response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                stream.write((json.dumps(response) + "\n").encode())
                stream.flush()
        except OSError:
            continue


def start() -> socket.socket:
    path = socket_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, 0o600)
    server.listen(4)
    threading.Thread(target=_serve, args=(server,), daemon=True, name="vault-control").start()
    logger.info("Vault control socket at %s", path)
    return server


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class ControlUnavailable(RuntimeError):
    pass


class ControlForbidden(ControlUnavailable):
    """The socket is there; this process is not allowed to use it.

    Its own type because it is the expected answer for every unprivileged
    container, not a fault. Callers report it differently -- see cmd_doctor.
    """


def send(request: dict, timeout: float = 120.0) -> dict:
    path = socket_path()
    if not path.exists():
        raise ControlUnavailable(
            f"No vault control socket at {path}. Is mcp-vault running? "
            "Try: docker compose up -d mcp-vault"
        )
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(path))
        with client.makefile("rwb") as stream:
            stream.write((json.dumps(request) + "\n").encode())
            stream.flush()
            line = stream.readline()
        if not line:
            raise ControlUnavailable("Vault server closed the connection without replying.")
        return json.loads(line)
    except PermissionError:
        # The socket is 0600 and owned by the vault server's user. Every other
        # container now runs unprivileged, so this is the expected answer from
        # them rather than a fault -- and a bare "Permission denied" reads like
        # a bug, which sends people looking in the wrong place.
        raise ControlForbidden(
            f"No permission on {path}. The vault control socket is restricted to the "
            "vault server's own user, so unprivileged containers cannot read vault "
            "state. This is expected. Use `make vault-status`, which runs inside "
            "that container."
        ) from None
    except (ConnectionError, socket.timeout) as exc:
        raise ControlUnavailable(f"Could not reach the vault server: {exc}") from None
    finally:
        client.close()
