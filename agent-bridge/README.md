# agent-bridge

A small HTTP service on the machine where your Claude Code agents run (the *agent
host*). [`mcp-gateway`](https://github.com/placheckij/claude-remote-mcp/tree/main/mcp-gateway) calls it over the LAN. It turns each request
into `herdr` CLI calls and returns trimmed results. herdr owns the agents, which are
addressed by their herdr pane id (e.g. `w1:p1`); agent-bridge's own state is its id, the
gateway it [enrolled](#enroll-with-the-gateway) with, and the agents to
[restore](#after-a-restart) after herdr restarts.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/placheckij/claude-remote-mcp/main/docs/assets/architecture-dark.svg">
  <img src="https://raw.githubusercontent.com/placheckij/claude-remote-mcp/main/docs/assets/architecture-light.svg" width="100%" alt="Three layers and four trust boundaries. The Claude app reaches mcp-gateway on the gateway host over MCP over HTTPS, through a Cloudflare Tunnel, with an OAuth access token; mcp-gateway keeps clients and token hashes in oauth.json, its agent hosts in bridges.json and the enrollment window in enrollment.json. mcp-gateway reaches agent-bridge on each agent host (macOS, Linux, Windows) over HTTP on the LAN with that host's bearer token, and agent hosts enroll with the gateway over the LAN with the window's secret. Each agent-bridge keeps its id and gateway in state.json and the agents to restore after herdr restarts in agents.json, only changes agents under its allowed roots on request, and drives herdr through the herdr CLI; herdr runs Claude Code sessions, one per pane, each on the Claude account set up for its folder, with ids that name the host, such as mac/w1:p1.">
</picture>

It sees every herdr agent of its user, including ones started by hand, and controls those
whose folder is under the allowed roots.

## System requirements

| | |
|---|---|
| OS | macOS, Linux or Windows (natively), wherever [herdr](https://github.com/herdrdev/herdr) runs. Tested on macOS 27.2 (Apple silicon), Ubuntu 26.04 LTS (x86_64, systemd 259) and Windows 11 25H2 (Windows PowerShell 5.1). The service setup needs launchd on macOS, systemd user units on Linux, and Windows 10 version 1809 or later. |
| herdr | 0.8.0 or newer (tested with 0.8.0 and 0.9.5; [restoring agents](#after-a-restart) only with 0.9.5); its server must be running for the same user (it is whenever herdr is open, and keeps running after you detach; `agent-bridge setup` starts it at boot or login) |
| Claude Code | the `claude` CLI, installed and logged in for that user |
| Python | 3.11+, via [uv](https://docs.astral.sh/uv/) (uv installs it if needed); on Windows, uv 0.12.4 or newer, as older ones install a windowless agent-bridge that opens a console window |
| Network | reachable from the gateway host on one TCP port (default 8765), and able to reach the gateway on its port (8766) to enroll and, at every start, to report its address. Give the host a fixed LAN address, e.g. a DHCP reservation. Nothing is exposed to the internet. |
| Always on | the agents and agent-bridge only answer while the host is awake and, on macOS and Windows, the user is logged in; see [Keeping the agent host available](#keeping-the-agent-host-available) |

## Keeping the agent host available

While you're away from the agent host, it has to stay **awake**, **logged in** and
running **herdr**. If it sleeps, agent-bridge and the agents pause, and every tool call
answers "The agent host is not reachable". The gateway host can't wake it: macOS's
"Wake for network access" doesn't reliably wake for a plain TCP connection.

**macOS**

- Keep it awake on power: `sudo pmset -c sleep 0` (the display can still sleep), or
  System Settings → Energy → turn on "Prevent automatic sleeping when the display is
  off" (on a laptop: Battery → Options → "Prevent automatic sleeping on power adapter
  when the display is off"). For a single trip, `caffeinate -s` in a
  terminal does the same until you stop it.
- A laptop with the lid closed sleeps anyway, unless it's on power with an external
  display. `sudo pmset -c disablesleep 1` keeps it awake with the lid closed too (an
  undocumented setting; undo with `0`; mind the heat).
- Stay logged in. The [launch agents](#as-a-service-on-macos-launchd) start agent-bridge
  and herdr's server at login and only run in a logged-in session; a locked screen is
  fine. After a restart (for example an overnight macOS update) FileVault waits for your
  password, so nothing runs until you log in again. Consider turning off automatic
  installation of macOS updates.

**Linux**

- Turn off suspend: `sudo systemctl mask sleep.target suspend.target hibernate.target
  hybrid-sleep.target`. On a laptop, also set `HandleLidSwitch=ignore` and
  `HandleLidSwitchExternalPower=ignore` in `/etc/systemd/logind.conf`.
- [`agent-bridge setup`](#as-a-service-on-linux-systemd-user-unit) turns on lingering, so
  agent-bridge and herdr's server start at boot and keep running after you log out.

**Windows**

- Turn off sleep on power (Settings → System → Power).
- Stay logged on. The [scheduled tasks](#as-a-service-on-windows-task-scheduler) start
  agent-bridge and herdr's server at logon and only run in your session; a locked screen
  is fine.
- Windows Update restarts the PC outside its active hours (Settings → Windows Update →
  Advanced options) to finish an update, which signs you out and ends the agents. Windows
  usually signs you back in by itself afterwards ("Use my sign-in info to automatically
  finish setting up after an update", under Settings → Accounts → Sign-in options), and the
  tasks then bring agent-bridge and herdr's server back without you, and agent-bridge
  [brings the agents back](#after-a-restart). If Windows doesn't sign you in, nothing runs
  until you do. Set active hours to when you need the agents, or pause updates (Settings →
  Windows Update) while you're away.

**Before you leave**, ask Claude to list your agents. If it answers that the agent host
is not reachable, the host is asleep, logged out, or agent-bridge isn't running.

## After a restart

When herdr's server restarts, whether after a host restart or `herdr server stop`, its
agents end. While it runs, `serve` saves every herdr pane running Claude Code to
`agents.json`, beside `state.json`, every 30 seconds: its name, folder, `CLAUDE_CONFIG_DIR`,
Claude arguments (without session flags, `--print`, `--worktree`, `--from-pr`,
`--teleport`, `--cloud`, a prompt typed on the command line, or a flag it doesn't know
followed by a word) and session id. An agent drops out of the file after
two saves without it, and `stop_agent` drops it, and any other entry for its conversation,
at once.

`serve` takes herdr's server as restarted when a saved agent's pane id is back with a new
terminal id. It does too when herdr answers for the first time since `serve` started, or
again after its server wasn't running, and none of the saved agents' terminals is there. Then it
restores each saved agent that isn't running, one after another: a new workspace, its
account (if its environment couldn't be read, the one its folder would get now),
`--resume <session id>` and its arguments; an agent never prompted has no
conversation to resume, so it starts fresh under the same `--session-id`. Without a session id it uses
`--continue`, but only if no other saved agent shares its folder and account, else a fresh
conversation. Agents started by hand come back too. A pane running a saved agent's
conversation with the wrong account or arguments, whether herdr resumed it or you did, is
closed and replaced, but only when herdr reports it idle or done, also when asked again
right before closing it, its process could be read and shows other arguments, or an
account read from it that isn't the saved one, and it is gone after closing. Otherwise
it's left as it is and kept as the agent's: one working, waiting for an approval, that
herdr can't classify, one with its arguments whose account can't be read, or one herdr
doesn't show as an agent yet but whose command line resumes the conversation. An idle one
whose process couldn't be read when the panes were listed is kept as the agent's if Claude
Code is found in it at its turn; if it can't be read then either, it's left and the agent
kept for the next restart: Claude Code may have exited from it. One read earlier in the
restore, unchanged since, but not again is left and kept as the agent's. If herdr's resume
exits during the restore and leaves a shell, an idle one (only the shell in the foreground, no Claude Code
among its jobs) is closed and the agent started; any other is left, and the agent kept for
the next restart, as it is when herdr, asked again, can't say what runs there or still
calls it busy. So is the agent while a Claude agent in its folder with no conversation from
herdr's hook and a process that can't be read, or a Claude Code stopped with ctrl-Z in the
shell herdr left (or that shell's jobs can't be read), may be running its conversation.
None of these holds off an agent whose conversation's pane was just closed to be replaced.
An idle shell herdr left where the agent was, in its folder, is closed, also when the
start is then held off, unless a pane seen before its turn that runs its conversation is
kept as the agent's, replaced, gone or stopped through agent-bridge; a shell running
something is left alone. Right before
starting an agent, the restore looks for its conversation again in panes opened or resumed
meanwhile, and in its folder's panes herdr doesn't show as agents yet. A conversation can
still start twice whenever the restore doesn't see it running: for example outside herdr,
in a pane whose process can't be read and that herdr's hook gives no conversation for, in
a Claude Code stopped with ctrl-Z outside the shell herdr left, under `--continue` or
`/resume` without the hook, or when it's resumed by hand after the restore's last look. An
agent stopped through agent-bridge is never restored, nor is its
conversation from another entry until it runs again, even if it's stopped while a restore
runs; the exception is when `agents.json` can't be written then and agent-bridge restarts
before it can be, and, for its conversation from another entry, when herdr's hook didn't
give it.

- A restored agent is idle: no prompt is sent.
- An agent that a save had already missed comes back only if herdr brought its pane back,
  in its folder; otherwise it's dropped. Each conversation comes back once.
- The restore ignores `AGENT_BRIDGE_MAX_AGENTS` and the allowed roots, so every agent
  that was running comes back. It marks no folder trusted, even under the
  [trust root](#folder-trust): an entry whose folder Claude Code doesn't trust is dropped. An agent that fails to start for another reason stays in
  `agents.json` until it starts, or its conversation runs elsewhere or is stopped through
  the bridge: it is tried again at the next restart, and whenever herdr
  answers for the first time since `serve` started, or again after its server wasn't
  running, while every saved agent is one that failed to start. `start_agent` adds `--session-id <uuid>`, unless
  `AGENT_BRIDGE_CLAUDE_ARGS` picks the conversation itself, so a new agent's conversation
  is known from its first save.
- Not brought back: the turn a busy agent was in the middle of, panes that weren't Claude
  agents, and herdr's layout: each agent gets a workspace of its own.

For a planned herdr restart, `agent-bridge save-agents` saves the list right now.
`AGENT_BRIDGE_RESTORE_AGENTS=false` turns the restore off, and with it the saving: `serve`
then removes `agents.json`.

`agent-bridge setup` prepares herdr for this, and `--no-agent-restore` undoes the first
step below and removes `agents.json`:

- herdr resumes agents on its own too, without their account or arguments. The restore
  replaces those it finds, but one that hasn't come up yet when it runs would make a second
  copy. Setup
  turns that off with `resume_agents_on_restore = false` under `[session]`, on a line it
  marks with a comment, and only if you haven't set the key yourself. It edits
  `HERDR_CONFIG_PATH`, else both `$XDG_CONFIG_HOME/herdr/config.toml` and
  `~/.config/herdr/config.toml` when they differ (on Windows, `%APPDATA%\herdr\config.toml`
  in place of the second). `--no-agent-restore` and `uninstall` remove only that marked line
  (and the `[session]` header right above it, if nothing else is left under it).
- herdr's Claude hook tells herdr which conversation each agent is in, also after `/clear`
  or `/resume`, so a restored agent resumes the one it was in. Without the hook, an agent
  `start_agent` started comes back as the conversation it was started with (its
  `--session-id`), and one started by hand as the conversation its command line named by id, else
  its folder's latest (or fresh, if another saved agent shares its folder and account). Setup runs `herdr integration install
  claude` for each non-default config agent-bridge uses (`AGENT_BRIDGE_CLAUDE_CONFIG_DIR`
  and the accounts'), never the default `~/.claude`: it warns when that lacks the hook, and
  `agent-bridge check` notes it. Install it there yourself with `herdr integration
  install claude`. `--no-agent-restore` and `uninstall` leave the hook in place; it only
  reports to herdr and can stay.
- `--agent-restore` and `--no-agent-restore` are kept across re-runs unless you pass one.

## API

All routes require `Authorization: Bearer $AGENT_BRIDGE_TOKEN`.

| Route | herdr | Notes |
|---|---|---|
| `GET /info` | `--version` | the host: `api_version` (2; a bridge without `/info` is 1), `bridge_id` (from the state file), `name` (`AGENT_BRIDGE_NAME`, or the hostname as a slug), `platform` (`darwin`, `linux` or `windows`), `bridge_version`, `herdr_version` (`null` if herdr can't be run), `roots`, `projects_root` (the first root), the `accounts`' names and `max_agents` |
| `GET /projects/{name}` | | `{"cwd": …}` of the folder `name` under the first allowed root that has one; `404 project_not_found` otherwise, also for anything but one folder name (`..`, `.`, `~`, `/`, `\`, `:`) |
| `GET /agents` | `agent list` | every herdr agent, in any folder. `project` is the agent's folder under the closest allowed root, joined with `/` on every platform (`webapp/api` for `<root>/webapp/api`), `null` at a root or outside them. An `idle` or `done` agent whose last message ends with a question has it in `question` (read from its screen with `agent read`, so it is a guess; herdr itself can't tell that agent from one that simply finished). `account` is the account its Claude Code runs on (see [Claude accounts](#claude-accounts)) |
| `GET /agents/{id}` | `agent get` | the same, including `question` and `account` |
| `GET /agents/{id}/output?lines=40&source=recent` | `agent read` | last N (1-500) meaningful lines: padding, input box (also when it shows the session's name) and status bar dropped; the visible screen only while the agent is working or blocked (herdr can't scroll its history then). `source=visible` reads only the screen; `recent-unwrapped` reads herdr's history of that name |
| `POST /agents/{id}/prompt` `{text, timeout_s?, lines?}` | `agent prompt --wait` | waits until idle/done/blocked (the agent includes `question`); on timeout returns `timed_out: true` with current status, and `unconfirmed: true` if the text was typed but the agent didn't react within 5 s (don't resend). Refused with `409 agent_blocked` while the agent is `blocked` (the Enter would answer its question), and with `409 agent_busy` while it is `working` or another prompt to it is running |
| `POST /agents/{id}/wait` `{timeout_s?, lines?, expect_change?}` | `agent wait` | same result as `/prompt`, without sending anything; `expect_change` (after `/keys`) first gives the agent 5 s to leave its old `blocked` state |
| `POST /agents/{id}/keys` `{keys}` | `agent send-keys` | answer a `blocked` agent's question; `204` |
| `POST /agents` `{project, name?, account?, remote_control?}` | `workspace create` + `agent start --kind claude` | start an agent in a project: a folder name, looked up as `GET /projects/{name}` does, or an absolute path; `201` with the agent. `{cwd}` (an absolute path) is also taken instead of `project`, from gateways older than `project`. On the folder's Claude account or the one named (see below); the reply adds the `account` used. `remote_control` (default off) starts it with Remote Control (see below). `name` (by default the folder's) may be any words: it becomes a name herdr accepts (lower-case ASCII, starting with a letter, at most 32 characters, e.g. `Webapp fix!` → `webapp-fix`), with `-2`, `-3`, … if a live agent already has it |
| `PATCH /agents/{id}` `{name}` | `agent rename` + `workspace rename` | give the agent and its workspace the same new name, made the way `POST /agents` makes one (`-2`, `-3`, … only if *another* live agent has it); returns the agent |
| `DELETE /agents/{id}` | `agent get` + `pane close` | close the agent's pane; `204`, or `404` if the id isn't an agent (a shell or dev-server pane) |

Request limits, refused with `422`: `{id}` must look like a herdr pane id (`w1:p1`);
`text` 1-20,000 characters with no NUL character; `timeout_s` 1-3600 and `lines` 1-500
(default 40) for `/prompt` and `/wait`; `keys` 1-20 entries; `name` 1-200 characters;
`account` 1-64. On Windows a prompt whose quoted command line for herdr is longer than
Windows allows is refused with `422 prompt_too_long`.

Every error comes back as `{"error": {"code", "message"}}` with a matching status
(`unauthorized` → 401, `path_not_allowed` → 403, `agent_not_found` → 404,
`agent_limit_reached` → 409, `timeout` → 504, …); the message is written to be read
aloud. Only request validation failures (422) and unknown routes or methods (404, 405)
keep FastAPI's `{"detail": ...}` shape.

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
2. `403 path_not_allowed` if an absolute path is outside `AGENT_BRIDGE_ALLOWED_ROOTS`
   (symlinks and `..` are resolved first).
3. `404 not_a_directory` if the folder doesn't exist.
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
Team or Enterprise account whose Owner hasn't turned Remote Control on); the agent then
runs without it.

### Claude accounts

agent-bridge can start agents on more than one Claude login, e.g. a work subscription for
work projects and a personal one for the rest, picking the login from the project's
folder. Claude Code keeps each login in a config folder: its default one (`~/.claude`,
what a plain `claude` uses), or another named by `CLAUDE_CONFIG_DIR`.

1. **Log in once in each config.** The default one is likely logged in already. For
   another, run `CLAUDE_CONFIG_DIR=~/.claude-agents claude`, log in, and quit. An alias
   such as `alias claude-agents='CLAUDE_CONFIG_DIR=$HOME/.claude-agents claude'`
   keeps using that login by hand easy.
2. **Name the accounts** in `AGENT_BRIDGE_ACCOUNTS`, as JSON: each with its config folder
   and the project folders it's for. Names are lower-case; paths are full paths.
   `"config_dir": null` means Claude Code's default config.

   ```json
   {
     "work": {"config_dir": null, "folders": ["/Users/me/projects/work"]},
     "personal": {"config_dir": "/Users/me/.claude-agents", "folders": ["/Users/me/projects/own"]}
   }
   ```
3. **Apply it.** With `agent-bridge setup`, pass it in once as `--accounts`; re-runs keep
   it, and an empty value removes it:

   ```sh
   agent-bridge setup --accounts '{"work": {"config_dir": null, "folders": ["/Users/me/projects/work"]}}'
   ```

   In Windows PowerShell 5.1, write each `"` inside it as `\"`: it drops them otherwise
   when it passes the value on. Remove it there with `--accounts=`, as it drops `''` too:

   ```powershell
   agent-bridge setup --accounts '{\"work\": {\"config_dir\": null, \"folders\": [\"C:\\Users\\me\\work\"]}}'
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
case; in the Claude app, e.g. "start webapp on my personal account"), else the account
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

Claude Code's folder-trust dialog ("Is this a project you created or one you trust?")
defaults to "No, exit", so a prompt sent into it would quit Claude. If the dialog shows
after a start, the start fails with `409 folder_not_trusted` and the new workspace is
closed.

So either trust each project folder once by hand (open `claude` in it and accept), or
let agent-bridge do it. In a git repository Claude Code checks the repository's own
folder: trusting a folder above it, such as the one holding all your projects, doesn't
count, so the dialog still shows. To let agent-bridge do it, set both:

- `AGENT_BRIDGE_CLAUDE_CONFIG_DIR`: a separate Claude config for the agents, e.g.
  `~/.claude-agents` (run `CLAUDE_CONFIG_DIR=~/.claude-agents claude` once to log in;
  until then starts under the trust root fail with `409 trust_refused` saying so);
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
problem, naming the variable. The first start creates a state file with this host's id;
enrolling adds the gateway to it. Both must be readable only by
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
| `AGENT_BRIDGE_HERDR_BIN` | `herdr` | the full path, e.g. `/Users/me/.local/bin/herdr` (a `~` isn't expanded); `serve`, `enroll`, `rename` and `check` need it set |
| `AGENT_BRIDGE_TRUST_ROOT` | unset | auto-trust folders under it; needs `AGENT_BRIDGE_CLAUDE_CONFIG_DIR`; absolute, and like the allowed roots never `/` or the home folder |
| `AGENT_BRIDGE_RESTORE_AGENTS` | `true` | bring the saved Claude agents back after herdr's server restarts ([After a restart](#after-a-restart)) |
| `AGENT_BRIDGE_MAX_AGENTS` | `10` | no new agents while this many or more exist (all herdr agents count) |
| `AGENT_BRIDGE_MAX_OUTPUT_CHARS` | `8000` | terminal output beyond this is cut from the front (`truncated: true`) |
| `AGENT_BRIDGE_PROMPT_TIMEOUT_MS` | `600000` | how long prompt and wait block when the request gives no `timeout_s` |

## Run

The herdr server must be running in the same user session. To try it in the foreground,
write the [env file](#configuration) and start it. To keep it running, set it up as a
service instead (below).

```sh
brew install placheckij/tap/agent-bridge        # macOS, or:
uv tool install claude-remote-mcp-agent-bridge   # any OS
agent-bridge serve
```

On macOS, pick one of the two and keep it the only install
([why](#homebrew-or-uv-on-macos)).

From a checkout: `uv sync --all-packages`, then
`uv run --package claude-remote-mcp-agent-bridge agent-bridge serve`.

### Commands

| Command | Does | Exit codes |
|---|---|---|
| `agent-bridge serve [--log-file PATH]` | runs the service from the env and state files, and tells the gateway its address. `--log-file` appends the log to `PATH` instead of stderr, moved to `PATH.1` once past 1 MB | 0 when it stops, 1 if the settings, the state file or the log file are refused (it says why), 3 if it can't start serving (e.g. the port is taken, or the address isn't this host's yet) |
| `agent-bridge enroll <string>` | joins the gateway that printed the string (below) | 0 enrolled, 3 name taken, 4 gateway unreachable (prints the command to retry), 5 enrolled but the state file couldn't be saved, 1 otherwise |
| `agent-bridge rename <name>` | renames this host on that gateway | 0 renamed, 2 name won't do, 3 name taken, 4 gateway unreachable, 5 renamed but the state file couldn't be saved, 6 gateway too old to rename hosts, 1 otherwise (e.g. not enrolled); 4, 6 and a refusal from the gateway print the gateway's command, but a gateway without this host says to enroll again |
| `agent-bridge unenroll` | leaves that gateway | 0, also when the gateway can't be reached or refuses (prints the gateway's command); 1 if the state file can't be read or written |
| `agent-bridge save-agents` | saves the Claude agents running in herdr now, to [restore](#after-a-restart) after a planned restart | 0 saved, or restore is off and there's nothing to save; 1 otherwise |
| `agent-bridge start-herdr [--herdr-bin PATH] [--log-file PATH]` | starts herdr's server in the background unless one runs, and waits until it answers; the macOS launch agent and the Windows task run it at login | 0 running or started, 1 otherwise |
| `agent-bridge check` | checks the settings, the files' modes, herdr, herdr's server (running, and not one herdr says to restart; one still on an older version, e.g. started before `herdr update`, passes with a note), the running service and the gateway, one line each | 0 all passed, 1 one failed |
| `agent-bridge check --token-format <token>`, `--name-format <name>` | checks a value before it goes in the env file | 0 fine, 2 not (why, on stderr) |
| `agent-bridge setup` | installs or updates the service and enrolls it ([macOS](#with-agent-bridge-setup), [Linux](#as-a-service-on-linux-systemd-user-unit), [Windows](#as-a-service-on-windows-task-scheduler)) | 0 installed (enrolled or not), 1 refused or failed |
| `agent-bridge uninstall` | leaves the gateway, then removes the service, its settings, state, `agents.json` and log (on Windows, also the firewall rule). Their folders go too, except one holding other files, which it names | 0 removed, also when leaving the gateway failed (prints the gateway's command); 1 run with sudo, on Linux without a systemd user manager, or on Windows as another account than the desktop's (nothing is removed then); 1 also if the service couldn't be removed, after the host has left the gateway |
| `agent-bridge --version` | prints the installed version | 0 |

### Enroll with the gateway

With agent-bridge running, run `sudo mcp-gateway enroll` on the gateway host, then here:

```sh
agent-bridge enroll 'crm1@192.168.1.10:8766#…'
```

(From a checkout: `uv run --package claude-remote-mcp-agent-bridge agent-bridge enroll …`.)

The gateway checks that the address in `AGENT_BRIDGE_HOST` and `AGENT_BRIDGE_PORT`
answers as this bridge, and adds it under `AGENT_BRIDGE_NAME`. From then on, every start
tells the gateway the current address; if it can't be reached, agent-bridge logs how to
enroll again and serves on. A reinstalled gateway has no hosts: `agent-bridge check`
then fails with "isn't enrolled" and says how to enroll again, the same way. A bridge can also be added by hand on the gateway host with
`sudo mcp-gateway bridges add <url>`, which asks for the token.

The examples below use `/Users/me` for your home folder; replace it with yours.

### As a service on macOS (launchd)

launchd, macOS's service manager, starts agent-bridge at login and restarts it if it
stops. It is configured by a *plist* file in `~/Library/LaunchAgents`, which runs
`agent-bridge serve`; the settings are in the [env file](#configuration).
`agent-bridge setup` writes both and enrolls the bridge. A second launch agent starts
herdr's server at login, headless, unless one runs, so agents can start after a restart
before you open herdr.

#### With agent-bridge setup

Run `sudo mcp-gateway enroll` on the gateway host for an enrollment string. Then, as your
own user, not with `sudo`:

```sh
brew install placheckij/tap/agent-bridge        # with Homebrew, or:
uv tool install claude-remote-mcp-agent-bridge   # with uv
agent-bridge setup
```

Either works; install it one way only ([Homebrew or uv](#homebrew-or-uv-on-macos)).

It asks only for what isn't installed yet:

- the folders agents may start in (`AGENT_BRIDGE_ALLOWED_ROOTS`, default `~/Projects`);
- this host's name in agent ids (`AGENT_BRIDGE_NAME`, default: the hostname);
- once, a separate Claude config (default `~/.claude-agents`) and a trust root
  (default: the first folder agents may start in), for [folder trust](#folder-trust).
  Answer `-` at the config question to skip both. If the config is new, log in to it
  once, as setup reminds you at the end;
- the enrollment string, until the host is enrolled. Enter installs without enrolling.

It finds the LAN address and `herdr` itself, and generates the token on the first run.
Then it:

1. checks the port (8765). If another program holds it, it names the program and offers
   a free port;
2. writes the env file, readable only by you. Other settings in it are kept, but not its
   comments;
3. writes the plist, then writes and loads a second launch agent, `dev.claude-remote.agent-bridge-herdr`, which
   runs `agent-bridge start-herdr` at every login: that starts `herdr server` in the
   background unless a herdr server runs, waits until it answers, and exits. Loading it
   runs it once, so setup starts herdr's server if none runs. It never restarts a running
   one, so running agents stay, and it doesn't start one again after `herdr server stop`
   until the next login. Its log is `~/Library/Logs/agent-bridge-herdr.log`;
4. prepares herdr for [restoring agents](#after-a-restart) (or undoes that with
   `--no-agent-restore`), and (re)starts the service;
5. runs `agent-bridge check`. Before the first enrollment its last line is
   `--    not enrolled with a gateway`, which isn't a failure;
6. enrolls with the gateway. If the gateway has another host by that name, it asks for
   another one.

You can still open herdr, which attaches to the server the launch agent started. The
agents in that server get macOS folder permissions as herdr, not as your terminal app, so
an agent working under Desktop, Documents or Downloads may wait on a permission prompt
until you answer it at the Mac. `--no-herdr-service` removes the herdr launch agent
without stopping herdr's server, if you start it yourself; re-runs keep it off until
`--herdr-service` brings it back. A Mac set up before the launch agent existed gets it at
its next setup.

`--no-agent-restore` turns off agent-bridge's [restore](#after-a-restart) and gives herdr
its own resume back (which brings agents back without their account or arguments);
re-runs keep what is installed.

The last lines name the host as the gateway knows it. Nothing needs copying to the
gateway host. The plist runs agent-bridge at a path upgrades keep:
`/opt/homebrew/opt/agent-bridge/bin/agent-bridge` for Homebrew (`/usr/local/opt/...` on
Intel; `brew upgrade agent-bridge`), `~/.local/bin/agent-bridge` for uv (`uv tool upgrade
claude-remote-mcp-agent-bridge`). `agent-bridge upgrade` does both and runs setup again,
which restarts the service (or run it as `uvx --from claude-remote-mcp-agent-bridge@latest
agent-bridge upgrade`). By hand, re-run `agent-bridge setup` after upgrading. Re-running
asks nothing on an installed Mac, and keeps the token and the enrollment. If setup stops partway, run it again: it picks up from there.

To skip the questions or change an installed value, pass it: `--roots`, `--name`,
`--host`, `--port`, `--herdr-bin`, `--claude-config-dir` and `--trust-root` (`''` for no
folder trust), `--enrollment` (`''` to enroll later), and `--accounts` (never asked for;
`''` removes it). `--rotate-token` replaces the token and needs an enrollment string
(`--enrollment`, or typed when asked; if enrolling fails, the old token is put back). A
new `--name` on an enrolled host renames
it on the gateway (`agent-bridge rename`); a gateway too old for that, or one that can't
be reached, is asked for an enrollment string instead, as enrolling again renames it too.
For any other setting, edit the env file and re-run setup. See `agent-bridge setup --help`.

On a Mac set up from a checkout with the setup script of earlier versions, `agent-bridge
setup` takes the install over: it keeps the env file, the token and the enrollment, and
points the plist at the package. An older install whose settings are still in the plist
has them moved to the env file; setup then asks for the name and an enrollment string.
Give the name the host has on the gateway: the gateway replaces its record at the same
address. If the Mac's address has changed since, the old record holds the name; remove it
first with `sudo mcp-gateway bridges remove <name>` on the gateway host.

Setup refuses to run from a checkout's `.venv` or under `uvx`, as the plist must run an
installed agent-bridge. From a checkout, install it with
`uv tool install --editable ./agent-bridge` and run that one's `agent-bridge setup`.

An agent-bridge installed from a checkout or a wheel stays that version under a plain
`uv tool install claude-remote-mcp-agent-bridge`. `agent-bridge upgrade` moves it to the
published package (the checkout is left as it is), or by hand:
`uv tool install --reinstall claude-remote-mcp-agent-bridge`, then `agent-bridge setup`.

#### Homebrew or uv on macOS

Install agent-bridge with Homebrew or with uv, not both: with two installs, the plist runs
one while you upgrade the other. `brew install` refuses while agent-bridge is in
`~/.local/bin` (uv's, or pipx's), and `agent-bridge setup` refuses while two installs are
found. Each names the command that removes the other.

To move from one to the other, uninstall the package only, not the service: its settings,
token and enrollment stay. Then install the other way and run `agent-bridge setup`, which
points the plist at the new install. The service is down in between.

```sh
# uv to Homebrew
uv tool uninstall claude-remote-mcp-agent-bridge
brew install placheckij/tap/agent-bridge
agent-bridge setup

# Homebrew to uv
brew uninstall agent-bridge
uv tool install claude-remote-mcp-agent-bridge
agent-bridge setup
```

#### By hand

What setup does, step by step:

1. Write the [env file](#configuration), with at least `AGENT_BRIDGE_TOKEN` (generate it
   with `openssl rand -hex 32`), `AGENT_BRIDGE_ALLOWED_ROOTS`, `AGENT_BRIDGE_HOST` (the
   LAN address, `ipconfig getifaddr en0` on Wi-Fi), `AGENT_BRIDGE_NAME` and
   `AGENT_BRIDGE_HERDR_BIN` (`command -v herdr`), and `chmod 600` it.
2. Write `~/Library/LaunchAgents/dev.claude-remote.agent-bridge.plist`, with the full path
   of `agent-bridge` (`command -v agent-bridge`) and of the log. Write paths in full:
   launchd doesn't expand `~` or `$HOME`. Make the log's folder, `~/Library/Logs`, if it
   isn't there.

   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0">
   <dict>
     <key>Label</key><string>dev.claude-remote.agent-bridge</string>
     <key>ProgramArguments</key>
     <array>
       <string>/Users/me/.local/bin/agent-bridge</string>
       <string>serve</string>
     </array>
     <key>RunAtLoad</key><true/>
     <key>KeepAlive</key><true/>
     <key>ThrottleInterval</key><integer>10</integer>
     <key>StandardOutPath</key><string>/Users/me/Library/Logs/agent-bridge.log</string>
     <key>StandardErrorPath</key><string>/Users/me/Library/Logs/agent-bridge.log</string>
   </dict>
   </plist>
   ```

3. Check it and load it. It starts right away and at every login:

   ```sh
   plutil -lint ~/Library/LaunchAgents/dev.claude-remote.agent-bridge.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.claude-remote.agent-bridge.plist
   ```

4. Prepare herdr as in [After a restart](#after-a-restart): add
   `resume_agents_on_restore = false` under `[session]` in herdr's config, and run
   `CLAUDE_CONFIG_DIR=<config dir> herdr integration install claude` for each
   non-default config the agents use. Or set `AGENT_BRIDGE_RESTORE_AGENTS=false`;
   without either, herdr resumes agents too, and one it brings back late can run twice.
5. Run `agent-bridge check`, then [enroll](#enroll-with-the-gateway) it.

This leaves starting herdr's server at login to you (setup's second launch agent does
it). If the macOS firewall is on, allow incoming connections for Python when asked.

#### Restart, stop, uninstall

These work however you installed it:

```sh
# Status: a PID and "state = running" when it's up; "last exit code" after a crash
launchctl print gui/$(id -u)/dev.claude-remote.agent-bridge | grep -E 'state|pid|last exit'

# Log: errors, plus one line per request. Nothing rotates it; it grows slowly, and
# emptying it is safe while agent-bridge runs (launchd appends to it).
tail -f ~/Library/Logs/agent-bridge.log
: > ~/Library/Logs/agent-bridge.log

# Restart, e.g. after editing the env file. Re-running agent-bridge setup does that too.
launchctl kickstart -k gui/$(id -u)/dev.claude-remote.agent-bridge

# Stop. `launchctl stop` is not enough: KeepAlive would start it again at once.
# Unloading stops it until you start it again or log in next time.
launchctl bootout gui/$(id -u)/dev.claude-remote.agent-bridge

# Start again (after `disable` below, `enable` it first)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.claude-remote.agent-bridge.plist

# Keep it from starting at login without deleting the plist. This doesn't stop it if
# it's running (bootout does), and bootstrap fails with "5: Input/output error" until
# you enable it again. Re-running agent-bridge setup enables it too.
launchctl disable gui/$(id -u)/dev.claude-remote.agent-bridge
launchctl enable gui/$(id -u)/dev.claude-remote.agent-bridge      # undo

# herdr's launch agent: whether it ran at login ("last exit code = 0") and its log.
launchctl print gui/$(id -u)/dev.claude-remote.agent-bridge-herdr | grep -E 'state|last exit'
cat ~/Library/Logs/agent-bridge-herdr.log

# Uninstall: leaves the gateway, then removes both launch agents, the env and state files,
# agents.json and the logs. ~/Library/Application Support/agent-bridge goes too, unless it holds other
# files. herdr's server keeps running with its agents until you stop it (herdr server
# stop) or log out. Then `brew uninstall agent-bridge` (or `uv tool uninstall
# claude-remote-mcp-agent-bridge`) removes the package.
agent-bridge uninstall
```

By hand, uninstalling is `agent-bridge unenroll`, `launchctl bootout` as above for each
label you loaded, then removing their plists, `~/Library/Application Support/agent-bridge`
and the logs, and the `resume_agents_on_restore` line if you added it.

### As a service on Linux (systemd user unit)

A systemd user service runs agent-bridge as you, next to your herdr server. It starts at
boot without anyone logging in (lingering), and restarts if it stops. A second user
service runs herdr's server headless, so agents can start after a reboot too; you can
still attach to it with `herdr`.

#### With agent-bridge setup

Run `sudo mcp-gateway enroll` on the gateway host for an enrollment string. Then, as your
own user, not with `sudo`, in your own login (SSH or a console; not through `su` or
`sudo -u`, which have no user manager):

```sh
uv tool install claude-remote-mcp-agent-bridge
agent-bridge setup
```

It asks the same questions as [on macOS](#with-agent-bridge-setup), only for what isn't
installed yet, and takes the same flags. Then it:

1. checks the port (8765). If another program holds it, it names the program and offers
   a free port;
2. writes the settings to `~/.config/agent-bridge/env`, readable only by you. Other
   settings in it are kept, but not its comments;
3. installs `agent-bridge.service` and `agent-bridge-herdr.service` in
   `~/.config/systemd/user/`. It starts herdr's service only when no herdr server is
   running, and never restarts it, so running agents stay;
4. turns on lingering (`loginctl enable-linger`). If that is refused, it prints the
   `sudo` command to run;
5. prepares herdr for [restoring agents](#after-a-restart) (or undoes that with
   `--no-agent-restore`), (re)starts agent-bridge, runs `agent-bridge check`
   (`--    not enrolled with a gateway` before the first enrollment isn't a failure), and
   enrolls. If the gateway has another
   host by that name, it asks for another one.

`--no-herdr-service` installs no service for herdr's server, if you start it yourself,
and removes one installed before without stopping its server; `--herdr-service` brings it
back. Re-runs keep what is installed.

`--no-agent-restore` turns off agent-bridge's [restore](#after-a-restart) and gives herdr
its own resume back (which brings agents back without their account or arguments);
re-runs keep what is installed.

The unit runs `~/.local/bin/agent-bridge`, which `uv tool upgrade
claude-remote-mcp-agent-bridge` keeps; `agent-bridge upgrade` upgrades it and runs setup
again, which restarts agent-bridge but not herdr's server (by hand: `uv tool upgrade`,
then `agent-bridge setup`). Re-running asks nothing on an installed host, and keeps the
token and the enrollment. If setup stops partway, run it again.

A host set up from a checkout with `agent-bridge/deploy/linux/setup.sh` (earlier
versions) is taken over with no questions: setup keeps its env file, token and
enrollment, keeps herdr's service as it was (none, if it was installed with
`HERDR_SERVICE=0`), and points the unit at the package. The checkout isn't used after
that.

Setup refuses to run from a checkout's `.venv` or under `uvx`, as the unit must run an
installed agent-bridge. From a checkout, install it with
`uv tool install --editable ./agent-bridge` and run that one's `agent-bridge setup`.

An agent-bridge installed from a checkout or a wheel stays that version under a plain
`uv tool install claude-remote-mcp-agent-bridge`. `agent-bridge upgrade` moves it to the
published package (the checkout is left as it is), or by hand:
`uv tool install --reinstall claude-remote-mcp-agent-bridge`, then `agent-bridge setup`.

#### Restart, stop, uninstall

```sh
systemctl --user status agent-bridge
journalctl --user -u agent-bridge -f
systemctl --user restart agent-bridge          # after editing the env file
systemctl --user stop agent-bridge             # until the next start or boot
systemctl --user disable --now agent-bridge    # and don't start at boot

agent-bridge uninstall
uv tool uninstall claude-remote-mcp-agent-bridge
```

`agent-bridge uninstall` leaves the gateway (`agent-bridge unenroll`), then removes both
services, the env file, the state file and `agents.json`. Its log is in the journal, which systemd ages
out. herdr's server keeps running with its agents until you stop it (`herdr server stop`)
or reboot; until then `systemctl --user` lists `agent-bridge-herdr.service` as
`not-found active running`. Lingering stays on; `loginctl disable-linger` turns it off.
`~/.config/systemd/user/` stays, as it is systemd's.

### As a service on Windows (Task Scheduler)

A scheduled task starts agent-bridge when you log on. It runs as you, in your session
(where the herdr server runs), with no window. A second task starts herdr's server at
logon, headless, unless one runs, so agents can start after a restart before you open
herdr; you can still open herdr, which attaches to it.

#### With agent-bridge setup

Run `sudo mcp-gateway enroll` on the gateway host for an enrollment string. Then, in an
ordinary PowerShell window (not one run as administrator, and not as another account):

```powershell
uv tool install claude-remote-mcp-agent-bridge
agent-bridge setup
```

It asks the same questions as [on macOS](#with-agent-bridge-setup), only for what isn't
installed yet, and takes the same flags; `--roots` takes folders separated by `;`, in
quotes (`--roots 'C:\Projects;D:\Work'`), as PowerShell ends a command at an unquoted `;`.
Windows PowerShell 5.1 drops an empty `''` argument, so write an empty value with `=`, as
in `--enrollment=` or `--trust-root=`. The default name is the computer name, e.g.
`win-pc`. It finds the LAN address and
`herdr.exe` itself, preferring herdr's `%LOCALAPPDATA%\Programs\Herdr\bin\herdr.exe`,
which isn't on `PATH`: herdr's installer links that folder to the current release, while
`PATH` names one release's folder, so the setting follows `herdr update`. Then it:

1. checks the port (8765). If another program holds it, or Windows keeps it for Hyper-V or
   WSL, it names that and offers a free port. A port held by agent-bridge itself is fine;
   it is restarted;
2. writes the settings to `%APPDATA%\agent-bridge\env`. Other settings in it are kept, but
   not its comments. `AGENT_BRIDGE_*` variables set in your user or machine environment
   would win over it for the task, so setup refuses them and prints how to remove them;
3. adds an inbound firewall rule for the port on **Private** networks only. This is the
   only step that asks for admin rights (one UAC prompt). If Windows treats your network
   as Public, it asks whether to make it Private (answer `y`): the gateway can't reach
   agent-bridge otherwise. If you decline the prompt, it prints the commands to run as
   administrator;
4. registers the `agent-bridge-herdr` task, which runs `agent-bridgew.exe start-herdr` at
   every logon: that starts `herdr server` with no window (herdr.exe itself would open
   one) unless a herdr server runs, waits until it answers, and exits. Registering the task
   runs it once, so setup starts herdr's server if none runs. It never restarts a running
   one, so running agents stay, and it doesn't start one again after `herdr server stop`
   until the next logon;
5. prepares herdr for [restoring agents](#after-a-restart) (or undoes that with
   `--no-agent-restore`), then stops the running agent-bridge, if any, and registers the
   `agent-bridge` task, which starts it. The task runs `agent-bridgew.exe serve --log-file …`: `agent-bridgew` is
   agent-bridge without a console window, as `pythonw` is to `python`. Nothing runs in
   between, so ending the task ends agent-bridge. It is tried every minute while you're
   logged on, which does nothing while it runs. That way it starts once Wi-Fi brings up
   the LAN address, and starts again if it stops;
6. runs `agent-bridge check` (`--    not enrolled with a gateway` before the first
   enrollment isn't a failure), and enrolls with the gateway (asking for another name if
   the gateway already has a host by that name).

`--no-herdr-service` removes the `agent-bridge-herdr` task without stopping herdr's
server, if you start it yourself; re-runs keep it off until `--herdr-service` brings it
back. A host set up before the task existed gets it at its next setup.

`--no-agent-restore` turns off agent-bridge's [restore](#after-a-restart) and gives herdr
its own resume back (which brings agents back without their account or arguments);
re-runs keep what is installed.

Its log is `%LOCALAPPDATA%\agent-bridge\agent-bridge.log`, with a line for each start and one
per request; `start-herdr` adds its lines there too. Once it passes 1 MB it is moved to `agent-bridge.log.1`, replacing the one
before, also while agent-bridge runs (while another program has it open, such as
`Get-Content -Wait`, that waits until it lets go). It holds only what agent-bridge logs, not
what it prints; an exception is logged by its type alone, without its message or traceback,
which could include a setting.

The task runs `%USERPROFILE%\.local\bin\agent-bridgew.exe`, which upgrades keep. Re-running
setup asks nothing on an installed host, and keeps the token and the enrollment. If setup
stops partway, run it again; a setup that stops after stopping agent-bridge puts the task
back as it was.

To upgrade, run agent-bridge's upgrade from uvx's copy of the latest release. Windows can't
replace the files of a running program, so the installed agent-bridge can't upgrade itself:

```powershell
uvx --from claude-remote-mcp-agent-bridge@latest agent-bridge upgrade
```

It stops the task, upgrades the package (`uv tool upgrade`, else a reinstall) and runs the
new `agent-bridge setup`, which starts it again; the agents keep running in herdr, but the
gateway can't reach them until then. It refuses, with agent-bridge started again, while
another program still runs from the install (an `agent-bridge check` in another terminal).
If the upgrade fails and the old version is still there, it sets that one up again. Keep
`@latest`: without it uvx runs the installed copy, which can't replace itself. To let an
agent upgrade the host, allow it exactly that command.

By hand, stop agent-bridge first, or `uv tool upgrade` fails while it runs and leaves the
package half removed:

```powershell
schtasks /Change /TN agent-bridge /DISABLE; schtasks /End /TN agent-bridge
uv tool upgrade claude-remote-mcp-agent-bridge
agent-bridge setup
```

From Git Bash (as Claude Code's `!` runs commands), prefix the schtasks commands with
`MSYS_NO_PATHCONV=1`. If an upgrade failed that way, `agent-bridge` is gone too: run
`uv tool install --reinstall claude-remote-mcp-agent-bridge` (with agent-bridge stopped,
as above), then `agent-bridge setup`.

A host set up from a checkout with `agent-bridge\deploy\windows\setup.ps1` (earlier
versions) is taken over with no questions: setup keeps its env file, token and
enrollment, and no folder trust if none was set up. It stops the checkout's agent-bridge
and points the task at the package. The checkout isn't used after that.

Setup refuses to run from a checkout's `.venv` or under `uvx`, as the task must run an
installed agent-bridge. From a checkout, install it with
`uv tool install --editable ./agent-bridge` and run that one's `agent-bridge setup`.

An agent-bridge installed from a checkout or a wheel stays that version under a plain
`uv tool install claude-remote-mcp-agent-bridge`.
`agent-bridge upgrade` (run with uvx, as above) moves it to the published package: it says
what it replaces and, when the release is older than what ran, how to go back. The checkout
itself is left as it is.

#### Restart, stop, uninstall

```powershell
# Restart it, e.g. after editing the env file. The log gets a new "starting agent-bridge"
# line. Re-running agent-bridge setup does that too.
schtasks /End /TN agent-bridge; schtasks /Run /TN agent-bridge

# Stop it. Disabled first, or the task starts it again within a minute.
schtasks /Change /TN agent-bridge /DISABLE; schtasks /End /TN agent-bridge

# Start again: re-run agent-bridge setup, which registers the task again. Only enabling and
# starting it (schtasks /Change /ENABLE, then /Run) leaves out the every-minute retry,
# which disabling ended, until your next logon.
agent-bridge setup

# Uninstall: leaves the gateway, then removes both tasks, the firewall rule (one UAC
# prompt), the env file, the state file, agents.json and the log. herdr's server keeps running with its
# agents until you stop it (herdr server stop) or log off.
agent-bridge uninstall
uv tool uninstall claude-remote-mcp-agent-bridge
```

### When it's stopped or removed

While agent-bridge is stopped, mcp-gateway answers every tool call with "The agent host
is not reachable", and the agents in herdr keep running untouched. Uninstalling leaves
behind any folder-trust entries agent-bridge added to the agents' Claude configs
(`<config dir>/.claude.json`) and herdr's Claude hook in their `settings.json`; both can
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
