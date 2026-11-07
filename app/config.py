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
    # Inside the vault container only: a tmpfs, not the ./data/vault bind mount,
    # which the ingestion worker shares. See docker-compose.yml.
    vault_control_socket: Path = Path("/run/vault/control.sock")
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
    # How long an UNapproved request waits for a human. Short on purpose: a
    # prompt-injected request should not sit around hoping for a distracted
    # rubber stamp.
    vault_grant_ttl_seconds: int = 120
    # How long an APPROVED grant stays redeemable, measured from the approval.
    # This covers the human returning to the chat, re-asking, and the local
    # model prefilling and generating before the call goes out -- which at
    # ~15 t/s does not fit in the window above.
    vault_redeem_ttl_seconds: int = 300
    vault_kdf_time_cost: int = 3
    vault_kdf_memory_kib: int = 262144
    vault_kdf_parallelism: int = 4

    # --- Vision model (receipt extraction) ---
    llama_url: str = "http://llama-server:8080"
    # The id llama-server's router serves the vision model under. Coder-Next
    # stays the coding model; this one reads receipts and answers RAG chats.
    vision_model: str = "qwen3-vl"
    # A photographed receipt costs ~1,500 image tokens plus the projector's
    # encode, prefilled on the CPU at ~70 tokens/s: minutes, not seconds.
    vision_timeout_seconds: int = 600
    # The cap on one answer, enforced by llama-server. A receipt reading is a
    # few hundred tokens and a dense manual page ~2,000; a looping model would
    # otherwise generate until the 32k context fills -- most of an hour at
    # ~10 tokens/s -- because the router does not stop when the client leaves.
    vision_max_tokens: int = 4096
    # Long edge a receipt image is scaled to before it is sent. A 12 MP phone
    # photo at full size would be ~15k image tokens; 1536 px keeps small print
    # legible at a tenth of that.
    receipt_image_max_px: int = 1536
    # A scanned PDF past this many pages is not a receipt.
    receipt_max_pages: int = 3

    # --- Vision model (PDF page transcription) ---
    # Long edge a PDF page is rendered at. A letter page at 1536 px is ~140 dpi,
    # which keeps 8pt body text legible for ~1,800 image tokens.
    transcribe_image_max_px: int = 1536
    # Pages a single document may send to the model WITHOUT being asked to. A
    # page costs about a minute of CPU, so a scanned 400-page book would be a
    # day of inference nobody chose; past this the document waits for
    # `rag transcribe`.
    transcribe_max_auto_pages: int = 30

    @property
    def allowed_emails(self) -> list[str]:
        return [e.lower() for e in _csv(self.vault_allowed_emails)]

    @property
    def allowed_roles(self) -> list[str]:
        return [r.lower() for r in _csv(self.vault_allowed_roles)]


settings = Settings()
