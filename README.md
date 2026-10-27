# claude-remote-mcp

[![CI](https://github.com/placheckij/claude-remote-mcp/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/placheckij/claude-remote-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

Drive your Claude Code agents by voice from the Claude app (e.g. on CarPlay), through a
custom MCP connector added to your Claude account.

```
Claude app (voice)
  └─ custom MCP connector ── HTTPS, Cloudflare Tunnel, bearer token
       └─ mcp-gateway   (gateway host, always on)   MCP server, auth, frugal tool output
            └─ LAN ──── agent-bridge   (agent host)   stateless HTTP API, project allowlist
                             └─ herdr CLI           owns agents and their state
                                  └─ Claude Code agents
```

## Components

Each component has its own README with system requirements, configuration, deployment
and tests.

| Directory | Runs on | Role |
|---|---|---|
| [`mcp-gateway/`](mcp-gateway/) | an always-on host: a small home server, a VPS, or the agent host itself (Linux, macOS; the setup script needs Debian-based Linux with systemd) | MCP server exposed to claude.ai through a Cloudflare Tunnel; authenticates requests and forwards tool calls to `agent-bridge`. Includes a setup script. |
| [`agent-bridge/`](agent-bridge/) | the machine where herdr and Claude Code run (macOS or Linux) | Narrow LAN-only HTTP API translating requests into `herdr` CLI calls; keeps no state. Runs as a launchd or systemd user service. |

You need:

- [herdr](https://github.com/ogulcancelik/herdr) 0.8.0+ and Claude Code on the agent
  host;
- [uv](https://docs.astral.sh/uv/) on both hosts;
- a Cloudflare account with a domain on it;
- a Claude plan with custom connectors, and the **Request headers** option in the Add
  custom connector dialog. That option is a beta that only some accounts have; without
  it the connector can't send its token, and mcp-gateway has no OAuth sign-in instead.

The gateway and the agent host can be one machine.

## Setup order

1. **Agent host:** start [agent-bridge](agent-bridge/README.md#run) on its LAN address
   and note its token.
2. **Gateway host:** run [`mcp-gateway/deploy/setup.sh`](mcp-gateway/README.md#deploy-with-the-setup-script)
   with the public hostname, agent-bridge's URL and token, and the projects folder. It
   sets up the service and the Cloudflare Tunnel and prints the connector token.
3. **claude.ai:** [add the custom connector](mcp-gateway/README.md#add-the-connector-in-claudeai)
   with that token.

## Development

uv workspace; each component is its own package.

```sh
uv sync --all-packages
uv run --package agent-bridge pytest agent-bridge
uv run --package mcp-gateway pytest mcp-gateway
```

## License

[MIT](LICENSE)
