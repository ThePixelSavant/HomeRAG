"""Encrypted, sealed-by-default storage for sensitive and financial data.

Nothing here is importable by the open-tier MCP server: mcp-server has no
vault mount and does not install the crypto dependencies, so a compromise there
cannot reach this code or the key it holds.
"""
