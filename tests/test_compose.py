"""What each container is given, read from docker-compose.yml (ADR-033).

Compose files are not otherwise tested, and every rule here is one line of
YAML away from being undone: an `env_file: .env` pasted back onto mcp-server
hands the model-facing server every secret in it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.yml"

pytestmark = pytest.mark.skipif(not COMPOSE.exists(), reason="docker-compose.yml not present")

# Only these services may hold the identity secret.
VAULT_SERVICES = {"mcp-vault"}
VAULT_ONLY = {
    "VAULT_JWT_SECRET",
    "VAULT_ALLOWED_EMAILS",
    "VAULT_ALLOWED_ROLES",
    "VAULT_KEY_TTL_SECONDS",
    "VAULT_GRANT_TTL_SECONDS",
    "VAULT_REDEEM_TTL_SECONDS",
}
# Services that get an explicit list, never a whole env file.
ALLOWLISTED = {"mcp-server", "rag-admin"}


@pytest.fixture(scope="module")
def services() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]


def _env_files(service: dict) -> list[str]:
    files = service.get("env_file") or []
    if isinstance(files, (str, dict)):
        files = [files]
    return [f["path"] if isinstance(f, dict) else f for f in files]


def _env_keys(service: dict) -> set[str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        return {item.split("=", 1)[0] for item in env}
    return set(env)


def _env_file_keys(path: Path) -> set[str]:
    return {m.group(1) for m in re.finditer(r"^([A-Z][A-Z0-9_]*)=", path.read_text(), re.M)}


@pytest.mark.parametrize("name", sorted(ALLOWLISTED))
def test_allowlisted_services_get_no_env_file(services, name):
    assert not _env_files(services[name]), f"{name} must list its variables, not read an env file"


@pytest.mark.parametrize("name", sorted(ALLOWLISTED))
def test_allowlisted_variables_are_real_settings(services, name):
    # A typo here would silently fall back to a default.
    fields = {f.upper() for f in Settings.model_fields}
    unknown = _env_keys(services[name]) - fields
    assert not unknown, f"{name} sets variables no setting reads: {sorted(unknown)}"


def test_only_the_vault_reads_env_vault(services):
    for name, service in services.items():
        if name in VAULT_SERVICES:
            assert ".env.vault" in _env_files(service)
        else:
            assert ".env.vault" not in _env_files(service), name


def test_no_other_service_is_handed_a_vault_secret(services):
    for name, service in services.items():
        if name not in VAULT_SERVICES:
            assert not (_env_keys(service) & VAULT_ONLY), name


def test_vault_settings_live_in_env_vault_not_env():
    # .env reaches the worker and is read by make; the vault's settings must
    # not drift back into it.
    env_example = ROOT / ".env.example"
    vault_example = ROOT / ".env.vault.example"
    assert not (_env_file_keys(env_example) & VAULT_ONLY)
    assert VAULT_ONLY <= _env_file_keys(vault_example)


def test_qdrant_keys_are_required(services):
    # `:?` makes compose refuse to start without them; `:-` let an empty key
    # through, which is an unauthenticated Qdrant on a shared bridge.
    qdrant = services["qdrant"]["environment"]
    for var in ("QDRANT__SERVICE__API_KEY", "QDRANT__SERVICE__READ_ONLY_API_KEY"):
        assert ":?" in qdrant[var], var


@pytest.mark.parametrize("name", sorted(ALLOWLISTED))
def test_readers_get_the_read_only_qdrant_key(services, name):
    assert services[name]["environment"]["QDRANT_API_KEY"].startswith(
        "${QDRANT_READ_ONLY_API_KEY:?"
    )


def test_mcp_server_gets_its_client_tokens(services):
    assert "MCP_CLIENT_TOKENS" in _env_keys(services["mcp-server"])


def test_env_vault_is_gitignored():
    gitignore = ROOT / ".gitignore"
    if not gitignore.exists():
        pytest.skip(".gitignore is not in the image")
    assert ".env.vault" in gitignore.read_text().split()
