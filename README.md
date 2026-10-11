# claude-remote-mcp

Drive Claude Code sessions on the agent host by voice from the Claude app (e.g. on CarPlay),
through a custom MCP connector added to the Claude account.

```
Claude app (voice)
  └─ custom MCP connector ── HTTPS, Cloudflare Tunnel, bearer token
       └─ mcp-gateway   (gateway host, always on)   MCP server, auth, frugal tool output
            └─ LAN ──── agent-bridge   (agent host)           stateless HTTP API, project allowlist
                             └─ herdr CLI           owns sessions and their state
                                  └─ Claude Code sessions
```

## Components

Each component has its own README with configuration, deployment and tests.

| Directory | Runs on | Role |
|---|---|---|
| [`mcp-gateway/`](mcp-gateway/) | gateway host | MCP server exposed to claude.ai through a Cloudflare Tunnel; authenticates requests and forwards tool calls to `agent-bridge`. Includes the gateway host setup script. |
| [`agent-bridge/`](agent-bridge/) | agent host | Narrow LAN-only HTTP API translating requests into `herdr` CLI calls; keeps no state. Runs as a launchd agent. |

## Setup order

1. **Agent host:** start [agent-bridge](agent-bridge/README.md#run) on the LAN address and note its
   token.
2. **Gateway host:** run [`mcp-gateway/deploy/setup.sh`](mcp-gateway/README.md#deploy-with-the-setup-script)
   with the public hostname and agent-bridge's URL and token. It sets up the service and
   the Cloudflare Tunnel and prints the connector token.
3. **claude.ai:** [add the custom connector](mcp-gateway/README.md#add-the-connector-in-claudeai)
   with that token.

## Development

uv workspace; each component is its own package.

```sh
uv sync --all-packages
uv run --package agent-bridge pytest agent-bridge
uv run --package mcp-gateway pytest mcp-gateway
```
