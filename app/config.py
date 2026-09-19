from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Qdrant (open tier) ---
    qdrant_url: str = "http://qdrant:6333"
    qdrant_api_key: str | None = None
    collection_name: str = "docs"

    # --- Embedding ---
    # Frozen at collection creation. Changing either requires `make rebuild-index`;
    # the fingerprint stored alongside the vectors enforces this.
    embed_model: str = "BAAI/bge-small-en-v1.5"
    embed_dim: int = 384
    # onnxruntime otherwise spawns one thread per core and thrashes against
    # llama-server, which is already saturating this box.
    embed_threads_ingest: int = 8
    embed_threads_query: int = 4
    fastembed_cache_path: Path = Path("/models")

    # --- Chunking (tokens, measured with the embedding model's own tokenizer) ---
    chunk_target_tokens: int = 400
    chunk_overlap_tokens: int = 64
    chunk_max_tokens: int = 480

    # --- Paths ---
    # Split so that state/ and vault/ can be mounted independently: mcp-server
    # gets state/ read-only and no vault mount at all, so it cannot open
    # vault.db even if compromised. Directories rather than single files,
    # because Docker silently creates a *directory* when a bind-mounted file
    # does not exist yet.
    data_root: Path = Path("/data")
    inbox_path: Path = Path("/data/inbox")
    archive_path: Path = Path("/data/archive")
    quarantine_path: Path = Path("/data/quarantine")
    state_db_path: Path = Path("/data/state/rag.db")
    vault_db_path: Path = Path("/data/vault/vault.db")
    vault_blobs_path: Path = Path("/data/vault/blobs")
    sources_file: Path = Path("/app/sources.yaml")

    # --- MCP servers ---
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000
    vault_port: int = 8001

    # --- Vault access control ---
    # Shared with Open WebUI's FORWARD_USER_INFO_HEADER_JWT_SECRET. Without it,
    # identity cannot be verified and every model-initiated vault call is denied.
    vault_jwt_secret: str | None = None
    vault_jwt_header: str = "X-OpenWebUI-User-Jwt"
    vault_jwt_issuer: str = "open-webui"
    vault_allowed_emails: str = ""
    vault_allowed_roles: str = "admin"
    vault_key_ttl_seconds: int = 900
    vault_grant_ttl_seconds: int = 120
    vault_kdf_time_cost: int = 3
    vault_kdf_memory_kib: int = 262144
    vault_kdf_parallelism: int = 4

    # --- Vision model (receipt extraction) ---
    llama_url: str = "http://llama-server:8080"
    vision_model: str = "local"
    vision_timeout_seconds: int = 180

    @property
    def allowed_emails(self) -> list[str]:
        return [e.lower() for e in _csv(self.vault_allowed_emails)]

    @property
    def allowed_roles(self) -> list[str]:
        return [r.lower() for r in _csv(self.vault_allowed_roles)]


settings = Settings()
