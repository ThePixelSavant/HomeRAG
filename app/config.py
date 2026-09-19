from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    qdrant_url: str = "http://qdrant:6333"
    ollama_url: str = "http://ollama:11434"
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768
    collection_name: str = "docs"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000


settings = Settings()
