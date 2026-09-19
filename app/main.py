import logging
from mcp.server.fastmcp import FastMCP
from app.config import settings
from app.pipeline import qdrant_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP("rag-docs")


@mcp.tool()
def search_docs(
    query: str,
    limit: int = 5,
    source_id: str | None = None,
) -> list[dict]:
    """Search indexed documentation by semantic similarity.

    Args:
        query: Natural language search query.
        limit: Maximum number of results (default 5).
        source_id: Restrict search to a single source from sources.yaml.
    """
    # Phase 2: embed query via Ollama, then call qdrant_store.search()
    return []


@mcp.tool()
def list_sources() -> list[dict]:
    """List all configured sources and their last sync status."""
    # Phase 5: read from sources.yaml + SQLite sync log
    return []


@mcp.tool()
def get_index_status() -> dict:
    """Return last successful sync time and chunk count per source."""
    # Phase 5: read from SQLite sync log
    return {"sources": []}


def main() -> None:
    logger.info("Initialising Qdrant collection...")
    qdrant_store.init_collection()
    logger.info(
        "Starting MCP server (streamable-http) on %s:%d — endpoint: /mcp",
        settings.mcp_host,
        settings.mcp_port,
    )
    mcp.run(
        transport="streamable-http",
        host=settings.mcp_host,
        port=settings.mcp_port,
    )


if __name__ == "__main__":
    main()
