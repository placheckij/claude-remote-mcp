# agent-bridge

A small HTTP service on the machine where your Claude Code agents run (the *agent
host*). [`mcp-gateway`](https://github.com/placheckij/claude-remote-mcp/tree/main/mcp-gateway) calls it over the LAN. It turns each request
into `herdr` CLI calls and returns trimmed results. herdr owns the agents, which are
addressed by their herdr pane id (e.g. `w1:p1`); agent-bridge's own state is only its id
and the gateway it [enrolled](#enroll-with-the-gateway) with.

```
mcp-gateway ──LAN, bearer token──> agent-bridge :8765 ──> herdr CLI ──> Claude Code agents
```

It sees every herdr agent of its user, including ones started by hand, and controls those
whose folder is under the allowed roots.

## System requirements

| | |
|---|---|
| OS | macOS, Linux or Windows (natively), wherever [herdr](https://github.com/herdrdev/herdr) runs. Tested on macOS 27.2 (Apple silicon), Ubuntu 26.04 LTS (x86_64, systemd 259) and Windows 11 25H2 (Windows PowerShell 5.1). The setup scripts need launchd on macOS, systemd user units on Linux, and Windows 10 version 1809 or later. |
| herdr | 0.8.0 or newer (tested with 0.8.0 and 0.9.5); its server must be running for the same user (it is whenever herdr is open, and keeps running after you detach) |
| Claude Code | the `claude` CLI, installed and logged in for that user |
| Python | 3.11+, via [uv](https://docs.astral.sh/uv/) (uv installs it if needed) |
| Network | reachable from the gateway host on one TCP port (default 8765), and able to reach the gateway on its port (8766) to enroll. Give the host a fixed LAN address, e.g. a DHCP reservation. Nothing is exposed to the internet. |
| Always on | the agents and agent-bridge only answer while the host is awake and, on macOS and Windows, the user is logged in; see [Keeping the agent host available](#keeping-the-agent-host-available) |

## Keeping the agent host available

While you're away from the agent host, it has to stay **awake**, **logged in** and
running **herdr**. If it sleeps, agent-bridge and the agents pause, and every tool call
answers "The agent host is not reachable". The gateway host can't wake it: macOS's
"Wake for network access" doesn't reliably wake for a plain TCP connection.

**macOS**

- Keep it awake on power: `sudo pmset -c sleep 0` (the display can still sleep), or
  System Settings → Energy (on a laptop: Battery → Options) → turn on "Prevent
  automatic sleeping when the display is off". For a single trip, `caffeinate -s` in a
  terminal does the same until you stop it.
- A laptop with the lid closed sleeps anyway, unless it's on power with an external
  display. `sudo pmset -c disablesleep 1` keeps it awake with the lid closed too (an
  undocumented setting; undo with `0`; mind the heat).
- Stay logged in. agent-bridge is a launchd agent, which only runs in a logged-in
  session; a locked screen is fine. After a restart (for example an overnight macOS
  update) FileVault waits for your password, so nothing runs until you log in again.
  Consider turning off automatic installation of macOS updates.
- Keep herdr open, or detached; its server must be running.

**Linux**

- Turn off suspend: `sudo systemctl mask sleep.target suspend.target hibernate.target
  hybrid-sleep.target`. On a laptop, also set `HandleLidSwitch=ignore` and
  `HandleLidSwitchExternalPower=ignore` in `/etc/systemd/logind.conf`.
- The [setup script](#as-a-service-on-linux-systemd-user-unit) turns on lingering, so
  agent-bridge and herdr's server start at boot and keep running after you log out.

**Windows**

- Turn off sleep on power (Settings → System → Power).
- Stay logged on. The [scheduled task](#as-a-service-on-windows-task-scheduler) starts at
  logon and only runs in your session; a locked screen is fine.

**Before you leave**, ask Claude to list your agents. If it answers that the agent host
is not reachable, the host is asleep, logged out, or agent-bridge isn't running.

## API

All routes require `Authorization: Bearer $AGENT_BRIDGE_TOKEN`.

| Route | herdr | Notes |
|---|---|---|
| `GET /info` | `--version` | the host: `api_version` (2; a bridge without `/info` is 1), `bridge_id` (from the state file), `name` (`AGENT_BRIDGE_NAME`, or the hostname as a slug), `platform` (`darwin`, `linux` or `windows`), `bridge_version`, `herdr_version` (`null` if herdr can't be run), `roots`, `projects_root` (the first root), the `accounts`' names and `max_agents` |
| `GET /projects/{name}` | | `{"cwd": …}` of the folder `name` under the first allowed root that has one; `404 project_not_found` otherwise, also for anything but one folder name (`..`, `.`, `~`, `/`, `\`, `:`) |
| `GET /agents` | `agent list` | every herdr agent, in any folder. `project` is the agent's folder under the closest allowed root, joined with `/` on every platform (`grafik/api` for `<root>/grafik/api`), `null` at a root or outside them. An `idle` or `done` agent whose last message ends with a question has it in `question` (read from its screen with `agent read`, so it is a guess; herdr itself can't tell that agent from one that simply finished). `account` is the account its Claude Code runs on (see [Claude accounts](#claude-accounts)) |
| `GET /agents/{id}` | `agent get` | the same, including `question` and `account` |
| `GET /agents/{id}/output?lines=40&source=recent` | `agent read` | last N (1-500) meaningful lines: padding, input box (also when it shows the session's name) and status bar dropped; the visible screen only while the agent is working or blocked (herdr can't scroll its history then). `source=visible` reads only the screen; `recent-unwrapped` reads herdr's history of that name |
| `POST /agents/{id}/prompt` `{text, timeout_s?, lines?}` | `agent prompt --wait` | waits until idle/done/blocked (the agent includes `question`); on timeout returns `timed_out: true` with current status, and `unconfirmed: true` if the text was typed but the agent didn't react within 5 s (don't resend). Refused with `409 agent_blocked` while the agent is `blocked` (the Enter would answer its question), and with `409 agent_busy` while it is `working` or another prompt to it is running |
| `POST /agents/{id}/wait` `{timeout_s?, lines?, expect_change?}` | `agent wait` | same result as `/prompt`, without sending anything; `expect_change` (after `/keys`) first gives the agent 5 s to leave its old `blocked` state |
| `POST /agents/{id}/keys` `{keys}` | `agent send-keys` | answer a `blocked` agent's question |
| `POST /agents` `{project, name?, account?}` | `workspace create` + `agent start --kind claude` | start an agent in a project: a folder name, looked up as `GET /projects/{name}` does, or an absolute path. `{cwd}` (an absolute path) is still taken instead of `project`, for gateways from before it. On the folder's Claude account or the one named (see below); the reply adds the `account` used. `name` (by default the folder's) may be any words: it becomes a name herdr accepts (lower-case ASCII, starting with a letter, at most 32 characters, e.g. `Grafik fix!` → `grafik-fix`), with `-2`, `-3`, … if a live agent already has it |
| `PATCH /agents/{id}` `{name}` | `agent rename` + `workspace rename` | give the agent and its workspace the same new name, made the way `POST /agents` makes one (`-2`, `-3`, … only if *another* live agent has it); returns the agent |
| `DELETE /agents/{id}` | `agent get` + `pane close` | close the agent's pane; `404` if the id isn't an agent (a shell or dev-server pane) |

Every error comes back as `{"error": {"code", "message"}}` with a matching status
(`unauthorized` → 401, `path_not_allowed` → 403, `agent_not_found` → 404,
`agent_limit_reached` → 409, `timeout` → 504, …); the message is written to be read
aloud. Only request validation failures keep FastAPI's `{"detail": [...]}` shape (422).

Reading (list, details, output, wait) works for every agent. Changing one (`/prompt`,
`/keys`, `PATCH`, `DELETE`) only works for agents whose folder is under
`AGENT_BRIDGE_ALLOWED_ROOTS`; any other agent gets `403 agent_not_allowed`, so a
misheard id can't prompt, answer, rename or close an agent you started by hand
elsewhere. To keep agents in some folders off limits, leave those folders out of the
allowed roots.

## Starting agents

`POST /agents` refuses, in this order:

1. `422 bad_project` if `project` is neither one folder name nor an absolute path, and
   `404 project_not_found` if no allowed root has a folder of that name.
2. `403` if an absolute path is outside `AGENT_BRIDGE_ALLOWED_ROOTS` (symlinks and `..`
   are resolved first).
3. `404` if the folder doesn't exist.
4. `422 unknown_account` if the named account isn't in `AGENT_BRIDGE_ACCOUNTS`.
5. `409 agent_limit_reached` if `AGENT_BRIDGE_MAX_AGENTS` (10) or more herdr agents
   already exist: *"Refusing to start a new agent: 10 agents are already running and the
   limit is 10. Stop an agent first."* The count and the start are serialized, so
   parallel requests can't slip past the limit.
6. `409 trust_refused` or `409 folder_not_trusted` (see [Folder trust](#folder-trust)),
   or `502 agent_not_detected` if Claude Code didn't come up, twice.

Agents start with `AGENT_BRIDGE_CLAUDE_ARGS` (default `--permission-mode auto`) and with
`CLAUDE_CONFIG_DIR` set to their account's config (see [Claude accounts](#claude-accounts)),
or else to `AGENT_BRIDGE_CLAUDE_CONFIG_DIR` if set. Each
one gets its own herdr workspace, labelled with the name.

With `"remote_control": true` in the request (the gateway sends it unless the user asks
otherwise), the agent also gets `--remote-control <name>`, so the session shows up in the
Claude app under the agent's name (renaming the agent later doesn't rename the session);
left out, it is off. If `AGENT_BRIDGE_CLAUDE_ARGS` already has `--remote-control`, it
isn't passed again, and every agent has it. The answer says which in `remote_control`:
`on`, `off`, or `blocked` when Claude Code showed *"Remote Control is disabled by your
organization's policy"* within the 5 s the start looks for the trust dialog (e.g. on a
Claude Team account); the agent then runs without it.

### Claude accounts

agent-bridge can start agents on more than one Claude login, e.g. a work subscription for
work projects and a personal one for the rest, picking the login from the project's
folder. Claude Code keeps each login in a config folder: its default one (`~/.claude`,
what a plain `claude` uses), or another named by `CLAUDE_CONFIG_DIR`.

1. **Log in once in each config.** The default one is likely logged in already. For
   another, run `CLAUDE_CONFIG_DIR=~/.claude-personal claude`, log in, and quit. An alias
   such as `alias claude-personal='CLAUDE_CONFIG_DIR=$HOME/.claude-personal claude'`
   keeps using that login by hand easy.
2. **Name the accounts** in `AGENT_BRIDGE_ACCOUNTS`, as JSON: each with its config folder
   and the project folders it's for. Names are lower-case; paths are full paths.
   `"config_dir": null` means Claude Code's default config.

   ```json
   {
     "work": {"config_dir": null, "folders": ["/Users/me/projects/work"]},
     "personal": {"config_dir": "/Users/me/.claude-personal", "folders": ["/Users/me/projects/own"]}
   }
   ```
3. **Apply it.** With the macOS or Linux setup script, pass it in once; re-runs keep it, and an
   empty value removes it:

   ```sh
   AGENT_BRIDGE_ACCOUNTS='{"work": {"config_dir": null, "folders": ["/Users/me/projects/work"]}}' \
     agent-bridge/deploy/macos/setup.sh
   ```

   By hand, add it to the [env file](#configuration) in single quotes, and restart
   agent-bridge. A mistake stops startup with a line naming
   it, e.g. `AGENT_BRIDGE_ACCOUNTS (work.config): Extra inputs are not permitted`.
4. **Trust the default config's folders by hand.** agent-bridge never edits Claude Code's
   default config, so it can't mark folders trusted for an account with
   `"config_dir": null`. Open `claude` once in each of those projects and accept (see
   [folder trust](#folder-trust) for why a folder above them isn't enough). Until then,
   starts there stop with `409 folder_not_trusted`.

A new agent gets the account named in the request (`"account"` in `POST /agents`, any
case; in the Claude app, e.g. "start grafik on my personal account"), else the account
whose folder holds its folder most closely, else `AGENT_BRIDGE_CLAUDE_CONFIG_DIR`, which
counts as the account with the same `config_dir` if there is one. The reply names the
account. An unknown name is refused with `422 unknown_account`, listing the accounts.
A listed agent's `account`, also for one started by hand, is the account whose
`config_dir` is the `CLAUDE_CONFIG_DIR` of its `claude` process (found with `herdr pane
process-info`; none set is the default config). It is `null` when no account or several
have that config, when the process can't be read, or without `AGENT_BRIDGE_ACCOUNTS`.
Folder trust (below) is written to the account's own config, and never to the default
one.

### Folder trust

Claude's "Do you trust this folder?" dialog defaults to "No, exit", so a prompt sent
into it would quit Claude. If the dialog shows after a start, the start fails with
`409 folder_not_trusted` and the new workspace is closed.

So either trust each project folder once by hand (open `claude` in it and accept), or
let agent-bridge do it. In a git repository Claude Code checks the repository's own
folder: trusting a folder above it, such as the one holding all your projects, doesn't
count, so the dialog still shows. To let agent-bridge do it, set both:

- `AGENT_BRIDGE_CLAUDE_CONFIG_DIR`: a separate Claude config for the agents, e.g.
  `~/.claude-agents` (run `CLAUDE_CONFIG_DIR=~/.claude-agents claude` once to log in;
  until then starts fail with `409 trust_refused` saying so);
- `AGENT_BRIDGE_TRUST_ROOT`: the folder under which new agents' folders are marked
  trusted in the agent's Claude config (`<config dir>/.claude.json`).

Hard limits:

- only folders under the trust root are ever trusted automatically;
- never in the default config (`~/.claude.json`, `~/.claude/`): agent-bridge refuses
  to start with a trust root but no separate config dir, or with a config dir that is
  the default one, and refuses each start with `409 trust_refused` if it is anyway. An
  account with `"config_dir": null` (the default config) gets nothing trusted: its
  folders must be trusted by hand, or its starts stop at the dialog.
  Folders are compared on disk, not by spelling, so another letter case (macOS disks
  ignore case), a symlink, or a `.claude.json` linked to `~/.claude.json` is caught too.

The file is written atomically and left alone if the folder itself is already trusted.

## Configuration

Read from the environment, or from the env file (the environment wins), one
`AGENT_BRIDGE_…=value` per line. An invalid setting stops startup with one line per
problem, naming the variable. Enrolling adds a state file. Both must be readable only by
you (`chmod 600`):

| Platform | Settings (env file) | State |
|---|---|---|
| macOS | `~/Library/Application Support/agent-bridge/env` | `~/Library/Application Support/agent-bridge/state.json` |
| Linux | `~/.config/agent-bridge/env` | `~/.local/state/agent-bridge/state.json` |
| Windows | `%APPDATA%\agent-bridge\env` | `%LOCALAPPDATA%\agent-bridge\state.json` |

`AGENT_BRIDGE_ENV_FILE` and `AGENT_BRIDGE_STATE_FILE` point elsewhere. A `.env` file in
the working folder is read too, under the env file, with a warning.

| Variable | Default | |
|---|---|---|
| `AGENT_BRIDGE_TOKEN` | — | required, at least 32 characters: letters, digits, `. _ ~ + / -` and trailing `=` |
| `AGENT_BRIDGE_ALLOWED_ROOTS` | — | required, absolute folders agents may start in, separated by `:` (`;` on Windows); `/`, the home folder or anything above it, and on Windows `\\` paths, are refused |
| `AGENT_BRIDGE_HOST` | — | the LAN address to listen on, given to the gateway too; an IP address, not `0.0.0.0` (`127.0.0.1` when mcp-gateway runs on the same machine). `serve`, `enroll`, `rename` and `check` need it |
| `AGENT_BRIDGE_PORT` | `8765` | |
| `AGENT_BRIDGE_NAME` | — | this host's name in agent ids (`mac/w1:p1`): a lowercase letter, then up to 31 lowercase letters, digits or `-`. `serve`, `enroll`, `rename` and `check` need it |
| `AGENT_BRIDGE_CLAUDE_CONFIG_DIR` | unset | `CLAUDE_CONFIG_DIR` for new agents whose folder has no [account](#claude-accounts); unset uses Claude Code's default config |
| `AGENT_BRIDGE_ACCOUNTS` | none | named Claude logins and the folders they're for, as JSON (see [Claude accounts](#claude-accounts)); names are lower-case, e.g. `work` |
| `AGENT_BRIDGE_CLAUDE_ARGS` | `--permission-mode auto` | extra `claude` arguments, shell-quoted; set it empty for none |
| `AGENT_BRIDGE_HERDR_BIN` | `herdr` | the full path, e.g. `~/.local/bin/herdr`; `serve`, `enroll`, `rename` and `check` need it set |
| `AGENT_BRIDGE_TRUST_ROOT` | unset | auto-trust folders under it; needs `AGENT_BRIDGE_CLAUDE_CONFIG_DIR`; absolute, and like the allowed roots never `/` or the home folder |
| `AGENT_BRIDGE_MAX_AGENTS` | `10` | no new agents while this many or more exist (all herdr agents count) |
| `AGENT_BRIDGE_MAX_OUTPUT_CHARS` | `8000` | terminal output beyond this is cut from the front (`truncated: true`) |
| `AGENT_BRIDGE_PROMPT_TIMEOUT_MS` | `600000` | how long prompt and wait block when the request gives no `timeout_s` |

## Run

The herdr server must be running in the same user session. To try it in the foreground,
write the [env file](#configuration) and start it from the checkout. To keep it running,
set it up as a service instead (below).

```sh
uv sync --all-packages
uv run --package claude-remote-mcp-agent-bridge agent-bridge serve
```

### Commands

| Command | Does | Exit codes |
|---|---|---|
| `agent-bridge serve` | runs the service from the env and state files, and tells the gateway its address | |
| `agent-bridge enroll <string>` | joins the gateway that printed the string (below) | 0 enrolled, 3 name taken, 4 gateway unreachable (prints the command to retry), 5 enrolled but the state file couldn't be saved, 1 otherwise |
| `agent-bridge rename <name>` | renames this host on that gateway | 0 renamed, 2 name won't do, 3 name taken, 4 gateway unreachable, 5 renamed but the state file couldn't be saved, 6 gateway too old to rename hosts, 1 otherwise (e.g. not enrolled); 4, 6 and a refusal from the gateway print the gateway's command, but a gateway without this host says to enroll again |
| `agent-bridge unenroll` | leaves that gateway | 0, also when the gateway can't be reached or refuses (prints the gateway's command); 1 if the state file can't be read or written |
| `agent-bridge check` | checks the settings, the files' modes, herdr, the running service and the gateway, one line each | 0 all passed, 1 one failed |
| `agent-bridge check --token-format <token>`, `--name-format <name>` | checks a value before it goes in the env file | 0 fine, 2 not (why, on stderr) |
| `agent-bridge setup` | macOS: installs the service on a Mac without one and enrolls it ([below](#from-the-package)) | 0 installed (enrolled or not), 1 refused or failed |
| `agent-bridge --version` | prints the installed version | 0 |

### Enroll with the gateway

With agent-bridge running, run `sudo mcp-gateway enroll` on the gateway host, then here:

```sh
uv run --package claude-remote-mcp-agent-bridge agent-bridge enroll 'crm1@192.168.1.10:8766#…'
```

The gateway checks that the address in `AGENT_BRIDGE_HOST` and `AGENT_BRIDGE_PORT`
answers as this bridge, and adds it under `AGENT_BRIDGE_NAME`. From then on, every start
tells the gateway the current address; if it can't be reached, agent-bridge logs how to
enroll again and serves on. A bridge can also be added by hand on the gateway host with
`sudo mcp-gateway bridges add <url>`, which asks for the token.

The examples below use `/path/to/claude-remote-mcp` for the checkout, `192.168.1.20` for
the LAN address and `~/projects` for the projects folder; replace them with yours.

### As a service on macOS (launchd)

launchd, macOS's service manager, starts agent-bridge at login and restarts it if it
stops. It is configured by a *plist* file in `~/Library/LaunchAgents`, which runs
`agent-bridge serve`; the settings are in the [env file](#configuration). The setup
script writes both and enrolls the bridge.

#### From the package

On a Mac without an install, agent-bridge installs from PyPI, no checkout needed. Run
`sudo mcp-gateway enroll` on the gateway host for an enrollment string, then, as your own
user:

```sh
uv tool install claude-remote-mcp-agent-bridge
agent-bridge setup
```

It asks the same questions as the setup script below, finds the LAN address and `herdr`,
checks the port, generates the token, writes the env file and the plist, starts the
service and enrolls it. The plist runs `~/.local/bin/agent-bridge`, which `uv tool upgrade
claude-remote-mcp-agent-bridge` keeps; restart the service after upgrading (below). Pass
values to skip the questions: `--roots`, `--name`, `--host`, `--port`, `--herdr-bin`,
`--claude-config-dir` and `--trust-root` (`''` for no folder trust), `--enrollment`
(`''` to enroll later); see `agent-bridge setup --help`.

It refuses on a Mac that has an install already, and when run from a checkout: to change
an install, re-run the setup script. It refuses under `uvx` too, whose copy uv cleans away.
If it fails after writing the env file, it removes what it wrote, so it can run again.

#### With the setup script

Run `sudo mcp-gateway enroll` on the gateway host for an enrollment string. Then run the
script from the checkout as your own user, not with `sudo`:

```sh
agent-bridge/deploy/macos/setup.sh
```

It asks only for what isn't installed yet:

- the folders agents may start in (`AGENT_BRIDGE_ALLOWED_ROOTS`, default `~/Projects`);
- this host's name in agent ids (`AGENT_BRIDGE_NAME`, default: the hostname);
- once, a separate Claude config (default `~/.claude-agents`) and a trust root
  (default: the first folder agents may start in), for [folder trust](#folder-trust).
  Answer `-` at the config question to skip both. If the config is new, log in to it
  once, as the script reminds you at the end;
- the enrollment string, until the host is enrolled. Enter installs without enrolling.

It finds the LAN address and `herdr` itself, and generates the token on the first run.
Then it:

1. runs `uv sync --all-packages` to install the dependencies;
2. checks the port (8765). If another program holds it, it names the program and offers
   a free port;
3. writes the env file, readable only by you. Other settings in it are kept, but not its
   comments;
4. writes the plist and (re)starts the service;
5. runs `agent-bridge check`;
6. enrolls with the gateway. If the gateway has another host by that name, it asks for
   another one.

The last lines name the host as the gateway knows it. Nothing needs copying to the
gateway host.

To skip the questions or change an installed value, pass it as a variable, e.g.
`ALLOWED_ROOTS=/Users/me/projects NAME=mac ENROLLMENT='crm1@…' agent-bridge/deploy/macos/setup.sh`.
The others are `HOST`, `PORT`, `HERDR_BIN`, `AGENT_BRIDGE_CLAUDE_CONFIG_DIR` and
`TRUST_ROOT` (pass these two empty for no folder trust), `AGENT_BRIDGE_ACCOUNTS` (never
asked for; an empty one removes it), and `ROTATE_TOKEN=1`, which replaces the token and
needs `ENROLLMENT` (if enrolling fails, the old token is put back). A new `NAME` on an
enrolled host renames it on the gateway (`agent-bridge rename`); a gateway too old for that,
or one that can't be reached, is asked for an enrollment string instead, as enrolling again
renames it too. For any other
setting, edit the env file and re-run the script.

Re-run it after pulling new code. If the plist still holds the settings, the script moves
them to the env file, keeps the token, and asks for the name and an enrollment string.
Give the name the host has on the gateway: the gateway replaces its record at the same
address. If the Mac's address has changed since, the old record holds the name; remove it
first with `sudo mcp-gateway bridges remove <name>` on the gateway host.

#### By hand

What the script does, step by step:

1. In the checkout, run `uv sync --all-packages`.
2. Write the [env file](#configuration), with at least `AGENT_BRIDGE_TOKEN` (generate it
   with `openssl rand -hex 32`), `AGENT_BRIDGE_ALLOWED_ROOTS`, `AGENT_BRIDGE_HOST` (the
   LAN address, `ipconfig getifaddr en0` on Wi-Fi), `AGENT_BRIDGE_NAME` and
   `AGENT_BRIDGE_HERDR_BIN` (`command -v herdr`), and `chmod 600` it.
3. Copy [`deploy/macos/dev.claude-remote.agent-bridge.plist`](deploy/macos/dev.claude-remote.agent-bridge.plist)
   to `~/Library/LaunchAgents/`, and in it replace `@AGENT_BRIDGE@` with
   `/path/to/claude-remote-mcp/.venv/bin/agent-bridge` and `@LOG_FILE@` with
   `/Users/me/Library/Logs/agent-bridge.log`. Write paths in full: launchd doesn't
   expand `~` or `$HOME`.
4. Check it and load it. It starts right away and at every login:

   ```sh
   plutil -lint ~/Library/LaunchAgents/dev.claude-remote.agent-bridge.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.claude-remote.agent-bridge.plist
   ```

5. Run `.venv/bin/agent-bridge check`, then [enroll](#enroll-with-the-gateway) it.

If the macOS firewall is on, allow incoming connections for the venv's Python when
asked.

#### Restart, stop, uninstall

These work however you installed it:

```sh
# Status: a PID and "state = running" when it's up; "last exit code" after a crash
launchctl print gui/$(id -u)/dev.claude-remote.agent-bridge | grep -E 'state|pid|last exit'

# Log: errors, plus one line per request. Nothing rotates it; it grows slowly, and
# emptying it is safe while agent-bridge runs (launchd appends to it).
tail -f ~/Library/Logs/agent-bridge.log
: > ~/Library/Logs/agent-bridge.log

# Restart, e.g. after editing the env file, or after pulling new code and
# `uv sync --all-packages`. Re-running the setup script does that too.
launchctl kickstart -k gui/$(id -u)/dev.claude-remote.agent-bridge

# Stop. `launchctl stop` is not enough: KeepAlive would start it again at once.
# Unloading stops it until you start it again or log in next time.
launchctl bootout gui/$(id -u)/dev.claude-remote.agent-bridge

# Start again (after `disable` below, `enable` it first)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.claude-remote.agent-bridge.plist

# Keep it from starting at login without deleting the plist. This doesn't stop it if
# it's running (bootout does), and bootstrap fails with "5: Input/output error" until
# you enable it again. Re-running the setup script enables it too.
launchctl disable gui/$(id -u)/dev.claude-remote.agent-bridge
launchctl enable gui/$(id -u)/dev.claude-remote.agent-bridge      # undo

# Uninstall: leaves the gateway, then removes the service, the env and state files and
# the log
agent-bridge/deploy/macos/uninstall.sh
```

By hand, uninstalling is `.venv/bin/agent-bridge unenroll`, `launchctl bootout` as
above, then removing the plist, `~/Library/Application Support/agent-bridge` and the log.

### As a service on Linux (systemd user unit)

A systemd user service runs agent-bridge as you, next to your herdr server. It starts at
boot without anyone logging in (lingering), and restarts if it stops. A second user
service runs herdr's server headless, so agents can start after a reboot too; you can
still attach to it with `herdr`.

#### With the setup script

Run `sudo mcp-gateway enroll` on the gateway host first. Then run the script from the
checkout as your own user, not with `sudo`, in your own login (SSH or a console; not
through `su` or `sudo -u`, which have no user manager):

```sh
agent-bridge/deploy/linux/setup.sh
```

It asks only for what's missing:

- the folders agents may start in (`AGENT_BRIDGE_ALLOWED_ROOTS`, default `~/Projects`);
- this host's name in agent ids (default: the hostname);
- a separate Claude config (default `~/.claude-agents`) and a trust root (default: the
  first folder agents may start in), for [folder trust](#folder-trust). Answer `-` at
  the config question to skip both. If the config is new, log in to it once, as the
  script reminds you at the end. This is asked once; later runs keep the answer;
- the enrollment string `sudo mcp-gateway enroll` printed. Press Enter to enroll later.

It finds the LAN address and `herdr` itself, and generates the token. Then it:

1. runs `uv sync --all-packages` to install the dependencies;
2. checks the port (8765). If another program holds it, it names that program and
   offers a free port. If an agent-bridge started by hand holds it, stop that one first;
3. writes the settings to `~/.config/agent-bridge/env`, readable only by you, keeping
   any other lines in it;
4. installs `agent-bridge.service` and `agent-bridge-herdr.service` in
   `~/.config/systemd/user/`. It starts herdr's service only when no herdr server is
   running, and never restarts it, so running agents stay;
5. turns on lingering (`loginctl enable-linger`). If that is refused, it prints the
   `sudo` command to run;
6. (re)starts agent-bridge, runs `agent-bridge check`, and enrolls. If the gateway has
   another host by that name, it asks for another one.

The last lines say which name the gateway knows the host by. Nothing is copied to the
gateway by hand.

To skip the questions, pass the answers as variables, e.g.
`ALLOWED_ROOTS=/home/me/projects NAME=linux-box ENROLLMENT='crm1@…' agent-bridge/deploy/linux/setup.sh`.
The others are `HOST`, `PORT`, `HERDR_BIN`, `AGENT_BRIDGE_CLAUDE_CONFIG_DIR` and
`TRUST_ROOT` (pass these two empty for no folder trust), `AGENT_BRIDGE_ACCOUNTS` (never
asked for; the installed value is kept unless you pass one, and an empty one removes it),
`ROTATE_TOKEN=1` (a new token; needs `ENROLLMENT`, so the gateway learns it) and
`HERDR_SERVICE=0` (no service for herdr's server, if you start it yourself).

Re-run it after pulling new code. It asks nothing it already knows; to change a setting,
pass it as a variable or edit the env file. The name is the exception: once the host is
enrolled, it is the gateway's. Run again with `NAME=<new>` to rename it there
(`agent-bridge rename`); a gateway too old for that prints the command to run on the
gateway host instead, `sudo mcp-gateway bridges rename <old> <new>`, after which the next
run takes the new name.

#### Restart, stop, uninstall

```sh
systemctl --user status agent-bridge
journalctl --user -u agent-bridge -f
systemctl --user restart agent-bridge          # after editing the env file
systemctl --user stop agent-bridge             # until the next start or boot
systemctl --user disable --now agent-bridge    # and don't start at boot

agent-bridge/deploy/linux/uninstall.sh
```

The uninstall script leaves the gateway (`agent-bridge unenroll`), then removes both
services, the env file and the state file. herdr's server keeps running with its agents
until you stop it (`herdr server stop`) or reboot. Lingering stays on;
`loginctl disable-linger` turns it off.

### As a service on Windows (Task Scheduler)

A scheduled task starts agent-bridge when you log on. It runs as you, in your session
(where the herdr server runs), with no window. It needs uv 0.12.4 or later: older ones
write the `.venv`'s `pythonw.exe` as a console program, which opens a terminal window at
every start of agent-bridge (astral-sh/uv#19235). When the `.venv`'s `pythonw.exe` is a
console program, setup stops with such a uv; with a newer one, it makes the `.venv` again,
because `uv sync` keeps the old `pythonw.exe` (the old `.venv` is put back if that fails).
Run the setup script from the checkout in an ordinary PowerShell window:

```powershell
powershell -ExecutionPolicy Bypass -File agent-bridge\deploy\windows\setup.ps1
```

It asks for:

- the folders agents may start in (default `%USERPROFILE%\Projects`);
- this host's name, as Claude will say it (default: the computer name, e.g. `win-pc`);
- the enrollment string that `sudo mcp-gateway enroll` prints on the gateway host (Enter
  skips enrolling).

It finds the LAN address and `herdr.exe` itself, including herdr's
`%LOCALAPPDATA%\Programs\Herdr\bin\herdr.exe`, which isn't on `PATH`. Then it:

1. checks the port (8765). If another program holds it, or Windows keeps it for Hyper-V or
   WSL, the script names that and offers a free port. A port held by agent-bridge itself is
   fine; it is restarted;
2. checks the new settings with `agent-bridge check` on a copy, so settings agent-bridge
   would refuse never replace a working install. Then it stops the running bridge, runs
   `uv sync --all-packages --frozen` (checking the settings again with the new code), and
   writes the env file;
3. adds an inbound firewall rule for the port on **Private** networks only. This is the
   only step that asks for admin rights. If Windows treats your network as Public, the
   script asks whether to make it Private (answer `y`): the gateway can't reach
   agent-bridge otherwise;
4. registers the `agent-bridge` task, which starts it. The task runs
   `agent-bridgew.exe serve --log-file …`: `agent-bridgew` is agent-bridge without a
   console window, as `pythonw` is to `python`. Nothing runs in between, so ending the task
   ends agent-bridge. It is tried every minute while you're logged on, which does nothing
   while it runs. That way it starts once Wi-Fi brings up the LAN address, and starts again
   if it stops;
5. enrolls with the gateway (asking for another name if the gateway already has a host by
   that name), and runs `agent-bridge check`.

Its log is `%LOCALAPPDATA%\agent-bridge\agent-bridge.log`, with a line for each start and one
per request. Once it passes 1 MB it is moved to `agent-bridge.log.1`, replacing the one
before, also while agent-bridge runs (while another program has it open, such as
`Get-Content -Wait`, that waits until it lets go). It holds only what agent-bridge logs, not
what it prints; an exception is logged by its type alone, without its message or traceback,
which could include a setting. Re-run setup after pulling new code: it keeps the token and settings,
and asks only for what's missing. Parameters skip the questions or change a setting:
`-Roots`, `-Name`, `-Enrollment` (`''` skips), `-Address`, `-Port`, `-HerdrBin`,
`-ClaudeConfigDir` and `-TrustRoot` (for [folder trust](#folder-trust); `''` removes
them), `-RotateToken`, and `-Bridge` (an `agent-bridge.exe` installed elsewhere, with its `agent-bridgew.exe` beside it). A new `-Name`
on an enrolled host renames it on the gateway (`agent-bridge rename`); a gateway too old
for that, or one that can't be reached, is asked for an enrollment string instead. Settings
it doesn't manage, such as `AGENT_BRIDGE_ACCOUNTS`, can be added to the env file by hand;
re-runs keep them.

```powershell
# Restart it. The log gets a new "starting agent-bridge" line. (After pulling, re-run setup
# instead: it stops agent-bridge before `uv sync`, which can't replace files a running one
# has open. A task an older setup registered runs conhost.exe on run.cmd, and ending it
# leaves agent-bridge running: re-run setup once first.)
Stop-ScheduledTask -TaskName agent-bridge
Start-ScheduledTask -TaskName agent-bridge

# Stop it. Disabled first, or the task starts it again within a minute.
Disable-ScheduledTask -TaskName agent-bridge
Stop-ScheduledTask -TaskName agent-bridge

# Start again (re-running setup does this too). Registering it again, rather than only
# starting it, also brings back the every-minute retry, which disabling ended.
Enable-ScheduledTask -TaskName agent-bridge
Register-ScheduledTask -TaskName agent-bridge -Xml (Export-ScheduledTask -TaskName agent-bridge) -Force

# Uninstall: leaves the gateway, then removes the task, the firewall rule, the env file,
# the state file and the log. The checkout stays.
powershell -ExecutionPolicy Bypass -File agent-bridge\deploy\windows\uninstall.ps1
```

### When it's stopped or removed

While agent-bridge is stopped, mcp-gateway answers every tool call with "The agent host
is not reachable", and the agents in herdr keep running untouched. Uninstalling leaves
behind any folder-trust entries agent-bridge added to the agents' Claude configs
(`<config dir>/.claude.json`); they're ordinary trust entries and can
stay. To retire the whole setup, also
[uninstall mcp-gateway](https://github.com/placheckij/claude-remote-mcp/blob/main/mcp-gateway/README.md#stop-restart-uninstall) and remove the
connector in claude.ai.

## Tests

```sh
uv run --package claude-remote-mcp-agent-bridge pytest agent-bridge
```

The tests replace the herdr CLI with a fake runner, so they never touch real agents or
the real Claude config. They include negative cases: starts outside the allowed roots,
in missing folders, at the agent limit, and trust attempts on the default config, each
checked to leave no workspace and no config change behind.
