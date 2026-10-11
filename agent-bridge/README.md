# agent-bridge

A small HTTP service on the agent host that [`mcp-gateway`](../mcp-gateway/) calls over the LAN.
It turns each request into `herdr` CLI calls and returns trimmed results. It keeps no
state of its own: herdr owns the agents, which are addressed by their herdr pane id
(e.g. `w1:p1`).

```
mcp-gateway (gateway host) ──LAN, bearer token──> agent-bridge :8765 ──> herdr CLI ──> Claude Code agents
```

It sees and controls every herdr agent, including ones started by hand.

## API

All routes require `Authorization: Bearer $AGENT_BRIDGE_TOKEN`.

| Route | herdr | Notes |
|---|---|---|
| `GET /agents` | `agent list` | every herdr agent |
| `GET /agents/{id}` | `agent get` | |
| `GET /agents/{id}/output?lines=40` | `agent read` | last N meaningful lines: padding, input box and status bar dropped; the visible screen only while the agent is working or blocked (herdr can't scroll its history then) |
| `POST /agents/{id}/prompt` `{text, timeout_s?, lines?}` | `agent prompt --wait` | waits until idle/done/blocked; on timeout returns `timed_out: true` with current status |
| `POST /agents/{id}/wait` `{timeout_s?, lines?, expect_change?}` | `agent wait` | same result as `/prompt`, without sending anything; `expect_change` (after `/keys`) first gives the agent 5 s to leave its old `blocked` state |
| `POST /agents/{id}/keys` `{keys}` | `agent send-keys` | answer a `blocked` agent's question |
| `POST /agents` `{cwd, name}` | `workspace create` + `agent start --kind claude` | start an agent in a project folder (see below) |
| `DELETE /agents/{id}` | `pane close` | |

Errors from herdr come back as `{"error": {"code", "message"}}` with a matching status
(`agent_not_found` → 404, `timeout` → 504, …); the message is written to be read aloud.

## Starting agents

`POST /agents` refuses, in this order:

1. `403` if `cwd` is outside `AGENT_BRIDGE_ALLOWED_ROOTS` (symlinks and `..` are resolved
   first).
2. `404` if the folder doesn't exist.
3. `409 agent_limit_reached` if `AGENT_BRIDGE_MAX_AGENTS` (10) or more herdr agents
   already exist: *"Refusing to start a new agent: 10 agents are already running and the
   limit is 10. Stop an agent first."* The count and the start are serialized, so
   parallel requests can't slip past the limit.

Agents start with a separate Claude config: `CLAUDE_CONFIG_DIR=~/.claude-agents`, with
`--permission-mode auto`. Each one gets its own herdr workspace, labelled with the name.

### Folder trust

Claude's "Do you trust this folder?" dialog defaults to "No, exit", so a prompt sent
into it would quit Claude. Before starting, agent-bridge marks the folder as trusted in
`$AGENT_BRIDGE_CLAUDE_CONFIG_DIR/.claude.json`, with two hard limits:

- only for folders under `AGENT_BRIDGE_TRUST_ROOT` (`~/projects`); folders
  elsewhere are never trusted automatically;
- never in the default config (`~/.claude.json`, `~/.claude/`); it refuses with
  `409 trust_refused` instead.

The file is written atomically and left alone if the folder (or a parent) is already
trusted. If the dialog shows anyway, the start fails with `folder_not_trusted` and the
new workspace is closed.

## Configuration

| Variable | Default | |
|---|---|---|
| `AGENT_BRIDGE_TOKEN` | — | required, at least 32 characters; mcp-gateway's `MCP_GATEWAY_BRIDGE_TOKEN` |
| `AGENT_BRIDGE_ALLOWED_ROOTS` | — | required, `:`-separated folders agents may start in |
| `AGENT_BRIDGE_CLAUDE_CONFIG_DIR` | `~/.claude-agents` | |
| `AGENT_BRIDGE_CLAUDE_ARGS` | `--permission-mode auto` | |
| `AGENT_BRIDGE_HERDR_BIN` | `herdr` | use the full path under launchd (`~/.local/bin/herdr`) |
| `AGENT_BRIDGE_TRUST_ROOT` | `~/projects` | only folders under it are auto-trusted |
| `AGENT_BRIDGE_MAX_AGENTS` | `10` | no new agents while this many or more exist (all herdr agents count) |

## Run

The herdr server must be running in the same user session (it is whenever herdr is
open). Bind to the Mac's LAN address, not `0.0.0.0`, and give the agent host a fixed address
(a DHCP reservation on the router) so mcp-gateway's URL stays valid.

```sh
uv sync --all-packages
AGENT_BRIDGE_TOKEN=$(openssl rand -hex 32) \
AGENT_BRIDGE_ALLOWED_ROOTS=$HOME/projects \
  uv run --package agent-bridge uvicorn agent_bridge.app:main --factory --host 192.168.1.20 --port 8765
```

### As a launchd agent

To start it at login and keep it running, save as
`~/Library/LaunchAgents/dev.claude-remote.agent-bridge.plist` (it holds the token, so
`chmod 600` it):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.claude-remote.agent-bridge</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/claude-remote-mcp/.venv/bin/uvicorn</string>
    <string>agent_bridge.app:main</string>
    <string>--factory</string>
    <string>--host</string><string>192.168.1.20</string>
    <string>--port</string><string>8765</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>AGENT_BRIDGE_TOKEN</key><string>…</string>
    <key>AGENT_BRIDGE_ALLOWED_ROOTS</key><string>/Users/me/projects</string>
    <key>AGENT_BRIDGE_HERDR_BIN</key><string>/Users/me/.local/bin/herdr</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <!-- Retries until the LAN address exists, e.g. right after login or wake. -->
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardErrorPath</key><string>/Users/me/Library/Logs/agent-bridge.log</string>
</dict>
</plist>
```

Load it (it starts right away and at every login):

```sh
chmod 600 ~/Library/LaunchAgents/dev.claude-remote.agent-bridge.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.claude-remote.agent-bridge.plist
```

If the macOS firewall is on, allow incoming connections for the venv's Python when
asked. Check from the gateway host:
`curl -H "Authorization: Bearer $TOKEN" http://192.168.1.20:8765/agents`.

### Restart, stop, uninstall

```sh
# Status: a PID and "state = running" when it's up; "last exit code" after a crash
launchctl print gui/$(id -u)/dev.claude-remote.agent-bridge | grep -E 'state|pid|last exit'
tail -f ~/Library/Logs/agent-bridge.log

# Restart, e.g. after pulling new code or `uv sync`. After editing the plist,
# stop and start it instead (below), so launchd rereads the file.
launchctl kickstart -k gui/$(id -u)/dev.claude-remote.agent-bridge

# Stop. `launchctl stop` is not enough: KeepAlive would start it again at once.
# Unloading stops it until you start it again or log in next time.
launchctl bootout gui/$(id -u)/dev.claude-remote.agent-bridge

# Start again
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.claude-remote.agent-bridge.plist

# Keep it from starting at login without deleting the plist
launchctl disable gui/$(id -u)/dev.claude-remote.agent-bridge     # `enable` to undo
```

**Uninstall:**

```sh
launchctl bootout gui/$(id -u)/dev.claude-remote.agent-bridge
rm ~/Library/LaunchAgents/dev.claude-remote.agent-bridge.plist   # holds the token
rm -f ~/Library/Logs/agent-bridge.log
```

While agent-bridge is stopped, mcp-gateway answers every tool call with "The agent host is not
reachable", and the agents in herdr keep running untouched. Uninstalling leaves behind
the folder-trust entries agent-bridge added to `~/.claude-agents/.claude.json`; they're
ordinary trust entries and can stay. To retire the whole setup, also uninstall
[mcp-gateway on the gateway host](../mcp-gateway/README.md#stop-restart-uninstall) and remove the
connector in claude.ai.

## Tests

```sh
uv run --package agent-bridge pytest agent-bridge
```

The tests replace the herdr CLI with a fake runner, so they never touch real agents or
the real Claude config. They include negative cases: starts outside the allowed roots,
in missing folders, at the agent limit, and trust attempts on the default config, each
checked to leave no workspace and no config change behind.
