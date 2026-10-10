# agent-bridge

Stateless HTTP service on the agent host. Translates requests from `mcp-gateway` into `herdr`
CLI calls and returns trimmed results. Herdr is the source of truth for agents, which
are addressed by their herdr pane id (e.g. `w1:p1`).

## API

All routes require `Authorization: Bearer $AGENT_BRIDGE_TOKEN`.

| Route | herdr | Notes |
|---|---|---|
| `GET /agents` | `agent list` | every herdr agent, including ones started by hand |
| `GET /agents/{id}` | `agent get` | |
| `GET /agents/{id}/output?lines=40` | `agent read` | last N meaningful lines: padding, input box and status bar dropped |
| `POST /agents/{id}/prompt` `{text, timeout_s?, lines?}` | `agent prompt --wait` | waits until idle/done/blocked; on timeout returns `timed_out: true` with current status |
| `POST /agents/{id}/keys` `{keys}` | `agent send-keys` | answer a `blocked` agent's question |
| `POST /agents` `{cwd, name}` | `workspace create` + `agent start --kind claude` | `cwd` must be under an allowed root; refused with `409 agent_limit_reached` when the agent count is at the limit |
| `DELETE /agents/{id}` | `pane close` | |

Agents are started with a separate Claude config (`CLAUDE_CONFIG_DIR=~/.claude-agents`) with
`--permission-mode auto`. If Claude shows its folder-trust dialog, the start is refused
with `folder_not_trusted` and the workspace closed: the dialog defaults to "No, exit",
so a prompt sent into it would quit Claude.

## Configuration

| Variable | Default | |
|---|---|---|
| `AGENT_BRIDGE_TOKEN` | — | required, at least 32 characters |
| `AGENT_BRIDGE_ALLOWED_ROOTS` | — | required, `:`-separated directories agents may start in |
| `AGENT_BRIDGE_CLAUDE_CONFIG_DIR` | `~/.claude-agents` | |
| `AGENT_BRIDGE_CLAUDE_ARGS` | `--permission-mode auto` | |
| `AGENT_BRIDGE_HERDR_BIN` | `herdr` | |
| `AGENT_BRIDGE_MAX_AGENTS` | `10` | no new agents while this many or more exist (all herdr agents count) |

## Run

```sh
uv run --package agent-bridge uvicorn agent_bridge.app:main --factory --host <lan-ip> --port 8765
uv run --package agent-bridge pytest agent-bridge
```
