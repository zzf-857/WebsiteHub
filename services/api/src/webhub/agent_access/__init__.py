"""External Agent access (Q43 / 2026-10 D2 · D3).

The user's own Agents (Hermes, grokbot, …) reach the library through one MCP
endpoint authenticated by account-scoped bearer tokens.

- ``tokens``      token issue / list / revoke / resolve and the write audit log
- ``schemas``     website REST request and response models
- ``routes``      cookie-session REST for /settings/agents
- ``tools``       the MCP tool implementations (plain async functions)
- ``mcp_server``  FastMCP wiring, the bearer-token ASGI guard and installation
"""
