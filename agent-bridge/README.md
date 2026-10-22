# agent-bridge

A small HTTP service on the machine where your Claude Code agents run (the *agent
host*). [`mcp-gateway`](../mcp-gateway/) calls it over the LAN. It turns each request
into `herdr` CLI calls and returns trimmed results. It keeps no state of its own: herdr
owns the agents, which are addressed by their herdr pane id (e.g. `w1:p1`).

```
mcp-gateway ──LAN, bearer token──> agent-bridge :8765 ──> herdr CLI ──> Claude Code agents
```

It sees and controls every herdr agent of its user, including ones started by hand.

## System requirements

| | |
|---|---|
| OS | macOS or Linux, wherever [herdr](https://github.com/ogulcancelik/herdr) runs. Tested on macOS; Linux should work but is untested, and Windows would need WSL. |
| herdr | 0.8.0 or newer (tested with 0.8.0); its server must be running for the same user (it is whenever herdr is open, and keeps running after you detach) |
| Claude Code | the `claude` CLI, installed and logged in for that user |
| Python | 3.11+, via [uv](https://docs.astral.sh/uv/) (uv installs it if needed) |
| Network | reachable from the gateway host on one TCP port (default 8765). Give the host a fixed LAN address, e.g. a DHCP reservation. Nothing is exposed to the internet. |
| Always on | the agents and agent-bridge only answer while the host is awake and the user is logged in; keep it from sleeping (macOS: `caffeinate` or Energy settings; Linux: disable suspend) |

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

Every error comes back as `{"error": {"code", "message"}}` with a matching status
(`unauthorized` → 401, `path_not_allowed` → 403, `agent_not_found` → 404,
`agent_limit_reached` → 409, `timeout` → 504, …); the message is written to be read
aloud. Only request validation failures keep FastAPI's `{"detail": [...]}` shape (422).

## Starting agents

`POST /agents` refuses, in this order:

1. `403` if `cwd` is outside `AGENT_BRIDGE_ALLOWED_ROOTS` (symlinks and `..` are
   resolved first).
2. `404` if the folder doesn't exist.
3. `409 agent_limit_reached` if `AGENT_BRIDGE_MAX_AGENTS` (10) or more herdr agents
   already exist: *"Refusing to start a new agent: 10 agents are already running and the
   limit is 10. Stop an agent first."* The count and the start are serialized, so
   parallel requests can't slip past the limit.

Agents start with `AGENT_BRIDGE_CLAUDE_ARGS` (default `--permission-mode auto`) and, if
`AGENT_BRIDGE_CLAUDE_CONFIG_DIR` is set, with `CLAUDE_CONFIG_DIR` pointing there. Each
one gets its own herdr workspace, labelled with the name.

### Folder trust

Claude's "Do you trust this folder?" dialog defaults to "No, exit", so a prompt sent
into it would quit Claude. If the dialog shows after a start, the start fails with
`409 folder_not_trusted` and the new workspace is closed.

So either trust your project folders once by hand (open `claude` in them, or in a
parent folder, and accept), or let agent-bridge do it. To turn that on, set both:

- `AGENT_BRIDGE_CLAUDE_CONFIG_DIR`: a separate Claude config for the agents, e.g.
  `~/.claude-agents` (run `CLAUDE_CONFIG_DIR=~/.claude-agents claude` once to log in);
- `AGENT_BRIDGE_TRUST_ROOT`: the folder under which new agents' folders are marked
  trusted in `$AGENT_BRIDGE_CLAUDE_CONFIG_DIR/.claude.json`.

Hard limits:

- only folders under the trust root are ever trusted automatically;
- never in the default config (`~/.claude.json`, `~/.claude/`): agent-bridge refuses
  to start with a trust root but no separate config dir, and refuses each start with
  `409 trust_refused` if the config dir is the default one anyway.

The file is written atomically and left alone if the folder (or a parent) is already
trusted.

## Configuration

Read from the environment, or from a `.env` file in the working directory (the
environment wins). An invalid setting stops startup with one line per problem, naming
the variable.

| Variable | Default | |
|---|---|---|
| `AGENT_BRIDGE_TOKEN` | — | required, at least 32 characters; mcp-gateway's `MCP_GATEWAY_BRIDGE_TOKEN` |
| `AGENT_BRIDGE_ALLOWED_ROOTS` | — | required, `:`-separated folders agents may start in |
| `AGENT_BRIDGE_CLAUDE_CONFIG_DIR` | unset | `CLAUDE_CONFIG_DIR` for new agents; unset uses Claude Code's default config |
| `AGENT_BRIDGE_CLAUDE_ARGS` | `--permission-mode auto` | extra `claude` arguments, shell-quoted; set it empty for none |
| `AGENT_BRIDGE_HERDR_BIN` | `herdr` | use the full path under a service manager, e.g. `~/.local/bin/herdr` |
| `AGENT_BRIDGE_TRUST_ROOT` | unset | auto-trust folders under it; needs `AGENT_BRIDGE_CLAUDE_CONFIG_DIR` |
| `AGENT_BRIDGE_MAX_AGENTS` | `10` | no new agents while this many or more exist (all herdr agents count) |
| `AGENT_BRIDGE_MAX_OUTPUT_CHARS` | `8000` | terminal output beyond this is cut from the front (`truncated: true`) |
| `AGENT_BRIDGE_PROMPT_TIMEOUT_MS` | `600000` | how long prompt and wait block when the request gives no `timeout_s` |

## Run

The herdr server must be running in the same user session. Bind to the host's LAN
address, not `0.0.0.0` (or to `127.0.0.1` when mcp-gateway runs on the same machine).

To try it in the foreground, generate a token and start it by hand. To keep it running, set
it up as a service instead (below); on macOS the setup script generates the token for you.
Either way the same token goes into mcp-gateway's `MCP_GATEWAY_BRIDGE_TOKEN`.

```sh
uv sync --all-packages
AGENT_BRIDGE_TOKEN=$(openssl rand -hex 32)
echo "$AGENT_BRIDGE_TOKEN"     # copy it somewhere safe
AGENT_BRIDGE_TOKEN=$AGENT_BRIDGE_TOKEN \
AGENT_BRIDGE_ALLOWED_ROOTS=$HOME/projects \
  uv run --package agent-bridge uvicorn agent_bridge.app:main --factory --host 192.168.1.20 --port 8765
```

Check from the gateway host, with the same token set there as `AGENT_BRIDGE_TOKEN`:
`curl -H "Authorization: Bearer $AGENT_BRIDGE_TOKEN" http://192.168.1.20:8765/agents`.

The examples below use `/path/to/claude-remote-mcp` for the checkout, `192.168.1.20` for
the LAN address and `~/projects` for the projects folder; replace them with yours.

### As a service on macOS (launchd)

launchd, macOS's service manager, starts agent-bridge at login and restarts it if it
stops. It is configured by a *plist* file in `~/Library/LaunchAgents`. The setup script
writes that file for you.

#### With the setup script

Run it from the checkout as your own user, not with `sudo`:

```sh
agent-bridge/deploy/setup-macos.sh
```

It asks for:

- the LAN address to listen on (it suggests the Mac's current one) and the port (8765);
- the folders agents may start in (`AGENT_BRIDGE_ALLOWED_ROOTS`, default `~/Projects`);
- the path to `herdr` (found on your `PATH`);
- a separate Claude config (default `~/.claude-agents`) and a trust root (default: the
  first folder agents may start in), for [folder trust](#folder-trust). Answer `-` at
  the config question to skip both. If the config is new, log in to it once, as the
  script reminds you at the end;
- the token, hidden as you type or paste it, e.g. one mcp-gateway already uses. Press
  Enter to keep the installed token, or to generate one on the first run.

Then it:

1. runs `uv sync --all-packages` to install the dependencies;
2. writes the plist and makes it readable only by you;
3. (re)starts the service;
4. checks that it answers `401` without the token and `200` with it. A `200` also means
   it reached herdr.

At the end it prints the URL, token and projects folder to give mcp-gateway's
`setup.sh`.

To skip the questions, pass the answers as variables, e.g.
`HOST=192.168.1.20 ALLOWED_ROOTS=/Users/me/projects agent-bridge/deploy/setup-macos.sh`.
The other variables are `PORT`, `HERDR_BIN`, `AGENT_BRIDGE_CLAUDE_CONFIG_DIR` and
`TRUST_ROOT` (pass these two empty for no folder trust), `AGENT_BRIDGE_TOKEN`, and
`ROTATE_TOKEN=1` to replace the installed token with a new generated one.

Re-run it after pulling new code or to change a setting. It offers the installed values,
so Enter keeps them, the token included.

#### By hand

What the script does, step by step:

1. In the checkout, run `uv sync --all-packages`.
2. Create `~/Library/LaunchAgents/dev.claude-remote.agent-bridge.plist` with the content
   below, and replace the example values in it:

   | In the file | Replace with |
   |---|---|
   | `/path/to/claude-remote-mcp` | the checkout's full path (`pwd` in it) |
   | `192.168.1.20` | the Mac's LAN address (`ipconfig getifaddr en0` on Wi-Fi) |
   | `…` | the token (`openssl rand -hex 32`) |
   | `/Users/me/projects` | the folders agents may start in, `:`-separated |
   | `/Users/me/.local/bin/herdr` | the output of `command -v herdr` |
   | `/Users/me` in the log path | your home folder |

   Write every path in full: launchd doesn't expand `~` or `$HOME`. To set any other
   variable from [Configuration](#configuration), add another `<key>NAME</key><string>value</string>`
   line under `EnvironmentVariables`.

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

3. Make it readable only by you (it holds the token), check it, and load it. It starts
   right away and at every login:

   ```sh
   chmod 600 ~/Library/LaunchAgents/dev.claude-remote.agent-bridge.plist
   plutil -lint ~/Library/LaunchAgents/dev.claude-remote.agent-bridge.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.claude-remote.agent-bridge.plist
   ```

4. Check it with the `curl` command from [Run](#run).

If the macOS firewall is on, allow incoming connections for the venv's Python when
asked.

#### Restart, stop, uninstall

These work however you installed it:

```sh
# Status: a PID and "state = running" when it's up; "last exit code" after a crash
launchctl print gui/$(id -u)/dev.claude-remote.agent-bridge | grep -E 'state|pid|last exit'
tail -f ~/Library/Logs/agent-bridge.log

# Restart, e.g. after pulling new code and `uv sync --all-packages` (a plain `uv sync`
# removes agent-bridge from the venv). After editing the plist, stop and start it
# instead (below), so launchd rereads the file. Re-running the setup script does both.
launchctl kickstart -k gui/$(id -u)/dev.claude-remote.agent-bridge

# Stop. `launchctl stop` is not enough: KeepAlive would start it again at once.
# Unloading stops it until you start it again or log in next time.
launchctl bootout gui/$(id -u)/dev.claude-remote.agent-bridge

# Start again
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.claude-remote.agent-bridge.plist

# Keep it from starting at login without deleting the plist
launchctl disable gui/$(id -u)/dev.claude-remote.agent-bridge     # `enable` to undo

# Uninstall
launchctl bootout gui/$(id -u)/dev.claude-remote.agent-bridge
rm ~/Library/LaunchAgents/dev.claude-remote.agent-bridge.plist   # holds the token
rm -f ~/Library/Logs/agent-bridge.log
```

### As a service on Linux (systemd user unit)

Not yet tested. It runs as your user, next to your herdr server. Put the settings in
`~/.config/agent-bridge/env` (`chmod 600`, it holds the token):

```sh
AGENT_BRIDGE_TOKEN=…
AGENT_BRIDGE_ALLOWED_ROOTS=/home/me/projects
AGENT_BRIDGE_HERDR_BIN=/home/me/.local/bin/herdr
```

and the unit in `~/.config/systemd/user/agent-bridge.service`:

```ini
[Unit]
Description=agent-bridge (claude-remote)
After=network-online.target

[Service]
EnvironmentFile=%h/.config/agent-bridge/env
ExecStart=/path/to/claude-remote-mcp/.venv/bin/uvicorn agent_bridge.app:main --factory --host 192.168.1.20 --port 8765
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now agent-bridge
loginctl enable-linger "$USER"     # keep it running while you're logged out

systemctl --user status agent-bridge
journalctl --user -u agent-bridge -f
systemctl --user restart agent-bridge          # after pulling new code or editing env
systemctl --user stop agent-bridge             # until the next start or login
systemctl --user disable --now agent-bridge    # and don't start at login

# Uninstall
systemctl --user disable --now agent-bridge
rm ~/.config/systemd/user/agent-bridge.service ~/.config/agent-bridge/env
systemctl --user daemon-reload
```

### When it's stopped or removed

While agent-bridge is stopped, mcp-gateway answers every tool call with "The agent host
is not reachable", and the agents in herdr keep running untouched. Uninstalling leaves
behind any folder-trust entries agent-bridge added to
`$AGENT_BRIDGE_CLAUDE_CONFIG_DIR/.claude.json`; they're ordinary trust entries and can
stay. To retire the whole setup, also
[uninstall mcp-gateway](../mcp-gateway/README.md#stop-restart-uninstall) and remove the
connector in claude.ai.

## Tests

```sh
uv run --package agent-bridge pytest agent-bridge
```

The tests replace the herdr CLI with a fake runner, so they never touch real agents or
the real Claude config. They include negative cases: starts outside the allowed roots,
in missing folders, at the agent limit, and trust attempts on the default config, each
checked to leave no workspace and no config change behind.
