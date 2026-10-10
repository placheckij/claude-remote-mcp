# mcp-gateway

MCP server on the gateway host, added to the Claude account as a custom connector.
Authenticates every request and forwards tool calls over the LAN to `agent-bridge`.

Tool output is frugal by default: summaries first, detail on request, hard size caps
and cursors for paging.

Planned tools: list sessions, session details, send prompt, read response, step detail,
start session, stop session.
