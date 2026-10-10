# mcp-gateway

MCP server on the gateway host, added to the Claude account as a custom connector.
Checks the connector's bearer token on every request and forwards tool calls over the
LAN to `agent-bridge`. Built on the MCP Python SDK 2.x (`MCPServer`, Streamable HTTP,
stateless, JSON responses).

Tool results are short plain text meant to be read aloud: one line per agent, the last
few output lines, refusals from the agent host passed through verbatim.

## Tools

| Tool | Does |
|---|---|
| `list_agents` | one line per agent: id · name · status · folder |
| `agent_details` | the same line for one agent |
| `read_output` | last N meaningful terminal lines (default 20) |
| `send_prompt` | prompt, wait up to `wait_seconds` (default 90), return status + output; flags `blocked` and still-working |
| `answer_agent` | key presses for a blocked agent's question, e.g. `["1"]`, `["Enter"]` |
| `start_agent` | new agent in a project: a name under `MCP_GATEWAY_PROJECTS_ROOT` or an absolute path |
| `stop_agent` | close an agent's pane (marked destructive) |

## Configuration

| Variable | |
|---|---|
| `MCP_GATEWAY_TOKEN` | required, 32+ chars; claude.ai sends it as `Authorization: Bearer …` |
| `MCP_GATEWAY_PUBLIC_HOST` | required; Host header via the tunnel, e.g. `mcp.example.com` |
| `MCP_GATEWAY_BRIDGE_URL` | required, e.g. `http://192.168.1.20:8765` |
| `MCP_GATEWAY_BRIDGE_TOKEN` | required; agent-bridge's `AGENT_BRIDGE_TOKEN` |
| `MCP_GATEWAY_PROJECTS_ROOT` | default `/Users/me/projects` |

## Connector authentication

In claude.ai → Customize → Connectors → Add custom connector: URL
`https://<public host>/mcp`, Authentication **No sign-in**, Request headers
`authorization` = `Bearer <MCP_GATEWAY_TOKEN>`. Request headers are a beta feature not
every account has; without it the server needs OAuth instead.

## Run

```sh
uv run --package mcp-gateway uvicorn mcp_gateway.server:main --factory --host 127.0.0.1 --port 8766
uv run --package mcp-gateway pytest mcp-gateway
```
