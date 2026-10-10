# claude-remote-mcp

Drive Claude Code sessions on the agent host by voice from the Claude app (e.g. on CarPlay),
through a custom MCP connector added to the Claude account.

```
Claude app (voice)
  └─ custom MCP connector ── HTTPS, Cloudflare Tunnel + Access, bearer token
       └─ mcp-gateway   (gateway host, always on)   MCP server, auth, frugal tool output
            └─ LAN ──── agent-bridge   (agent host)           stateless HTTP API, project allowlist
                             └─ herdr CLI           owns sessions and their state
                                  └─ Claude Code sessions
```

## Components

| Directory | Runs on | Role |
|---|---|---|
| [`mcp-gateway/`](mcp-gateway/) | gateway host | MCP server exposed to claude.ai; authenticates requests and forwards tool calls to `agent-bridge` |
| [`agent-bridge/`](agent-bridge/) | agent host | Narrow LAN-only HTTP API translating requests into `herdr` CLI calls; keeps no state |
| [`infra/`](infra/) | both | Tunnel, service units (launchd / systemd) and deployment config |

## Development

uv workspace; each component is its own package.

```sh
uv sync --all-packages
```
