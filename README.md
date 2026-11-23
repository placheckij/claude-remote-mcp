# claude-remote-mcp

[![CI](https://github.com/placheckij/claude-remote-mcp/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/placheckij/claude-remote-mcp/actions/workflows/ci.yml)
[![CodeQL](https://github.com/placheckij/claude-remote-mcp/actions/workflows/github-code-scanning/codeql/badge.svg?branch=main)](https://github.com/placheckij/claude-remote-mcp/security/code-scanning)
[![mcp-gateway on PyPI](https://img.shields.io/pypi/v/claude-remote-mcp-gateway?label=mcp-gateway)](https://pypi.org/project/claude-remote-mcp-gateway/)
[![agent-bridge on PyPI](https://img.shields.io/pypi/v/claude-remote-mcp-agent-bridge?label=agent-bridge)](https://pypi.org/project/claude-remote-mcp-agent-bridge/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

Voice-driven remote orchestration for Claude Code agents. Manage and supervise AI coding agents from anywhere using natural language.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/architecture-dark.svg">
  <img src="docs/assets/architecture-light.svg" width="100%" alt="Three layers and four trust boundaries. The Claude app reaches mcp-gateway on the gateway host over MCP over HTTPS, through a Cloudflare Tunnel, with an OAuth access token; mcp-gateway keeps clients and token hashes in oauth.json, its agent hosts in bridges.json and the enrollment window in enrollment.json. mcp-gateway reaches agent-bridge on each agent host (macOS, Linux, Windows) over HTTP on the LAN with that host's bearer token, and agent hosts enroll with the gateway over the LAN with the window's secret. Each agent-bridge keeps its id and gateway in state.json and the agents to restore after herdr restarts in agents.json, only changes agents under its allowed roots on request, and drives herdr through the herdr CLI; herdr runs Claude Code sessions, one per pane, each on the Claude account set up for its folder, with ids that name the host, such as mac/w1:p1.">
</picture>

## Components

How the layers fit together, where state lives and the trust boundaries:
[ARCHITECTURE.md](ARCHITECTURE.md). Each component has its own README with system
requirements, configuration, deployment and tests. Both are on PyPI, released separately:
mcp-gateway as `claude-remote-mcp-gateway`, agent-bridge as
`claude-remote-mcp-agent-bridge` ([releases](https://github.com/placheckij/claude-remote-mcp/releases)).

| Directory | Runs on | Role |
|---|---|---|
| [`mcp-gateway/`](mcp-gateway/) | an always-on host: a small home server, a VPS, or the agent host itself (Linux, macOS; `mcp-gateway setup` needs Debian-based Linux with systemd) | MCP server exposed to claude.ai through a Cloudflare Tunnel; signs the connector in with OAuth (a passphrase) and forwards tool calls to `agent-bridge`. `mcp-gateway setup` installs it as a service. |
| [`agent-bridge/`](agent-bridge/) | the machine where herdr and Claude Code run (macOS, Linux or Windows) | Narrow LAN-only HTTP API translating requests into `herdr` CLI calls; keeps its id, its gateway and the agents to restore after herdr restarts. Enrolls itself with the gateway. Runs as a launchd or systemd user service, or a Windows scheduled task. |

You need:

- [herdr](https://github.com/herdrdev/herdr) 0.8.0+ (restoring agents after a restart is
  tested with 0.9.5) and Claude Code on each agent host;
- [uv](https://docs.astral.sh/uv/) on each host;
- an agent host that stays awake while you use it, and on macOS and Windows logged in (see
  [Keeping the agent host available](agent-bridge/README.md#keeping-the-agent-host-available));
- a Cloudflare account (the Free plan is fine) with a domain whose DNS is on Cloudflare;
- a Claude account: every plan has custom connectors (Free allows one; on Team and
  Enterprise an Owner adds it).

The gateway and the agent host can be one machine.

## Setup order

1. **Cloudflare:** add your domain to Cloudflare if it isn't there yet (its DNS must be
   managed by Cloudflare), and pick a subdomain for the connector, e.g.
   `mcp.example.com`. Nothing else to create by hand: `mcp-gateway setup` in step 2 makes
   the tunnel and the DNS record.
2. **Gateway host:** run `uv tool install claude-remote-mcp-gateway`, then
   [`mcp-gateway setup`](mcp-gateway/README.md#install-with-mcp-gateway-setup) with that
   subdomain. On the first run it prints a Cloudflare login URL: open it in
   any browser and pick your domain. It then sets up the service and the
   [Cloudflare Tunnel](mcp-gateway/README.md#cloudflare-tunnel), prints the sign-in
   passphrase, and, once its checks pass, prints an enrollment string for the agent hosts,
   good for 30 minutes (`sudo mcp-gateway enroll` prints a new one). If only the tunnel
   checks fail, DNS for a new hostname may need a few more minutes: run it again.
3. **Agent hosts:** run `uv tool install claude-remote-mcp-agent-bridge`, or on macOS
   either that or `brew install placheckij/tap/agent-bridge` (one of the two, not both),
   then
   `agent-bridge setup` ([macOS](agent-bridge/README.md#as-a-service-on-macos-launchd),
   [Linux](agent-bridge/README.md#as-a-service-on-linux-systemd-user-unit),
   [Windows](agent-bridge/README.md#as-a-service-on-windows-task-scheduler)), and paste
   that string when asked; it installs the service (a launch agent, a systemd user
   service, or a logon task with a firewall rule for the port on Private networks), a
   second one that starts herdr's server, prepares herdr for
   [bringing agents back after a restart](agent-bridge/README.md#after-a-restart), and
   enrolls it. Or, without setup, start [agent-bridge](agent-bridge/README.md#run), then
   [enroll](agent-bridge/README.md#enroll-with-the-gateway) it with that string.
4. **claude.ai:** [add the custom connector](mcp-gateway/README.md#add-the-connector-in-claudeai)
   by its URL and sign in with that passphrase.

If you have more than one Claude subscription, e.g. a work and a personal one, agents can
start on the right one for each project folder: see
[Claude accounts](agent-bridge/README.md#claude-accounts).

Agents only start in folders Claude Code trusts. agent-bridge can mark them trusted in a
separate Claude config: `agent-bridge setup` asks for it. Log in to that config once when
setup says so.
See [Folder trust](agent-bridge/README.md#folder-trust).

## Updating

Upgrade the package, then run its setup again; it keeps the state and the enrollment, and
restarts the service on the new version.

- **Gateway host:** `uv tool upgrade claude-remote-mcp-gateway`, then `mcp-gateway setup`.
  It keeps `PUBLIC_HOST`, the passphrase, the tokens, the agent hosts and the tunnel; pass
  `PORT` and `TUNNEL_NAME` again if you changed them, as they aren't kept (see
  [Update and rotate](mcp-gateway/README.md#update-and-rotate)).
- **Agent hosts:** `uv tool upgrade claude-remote-mcp-agent-bridge` (on a Mac with the
  Homebrew install, `brew upgrade agent-bridge`), then `agent-bridge setup`. On Windows, stop agent-bridge first: the upgrade fails while it
  runs (see [Windows](agent-bridge/README.md#as-a-service-on-windows-task-scheduler)).
  From 0.3.0, agent-bridge restores agents after herdr restarts; setup turns off herdr's own
  resume unless you set it yourself, so agents don't come back twice (see
  [After a restart](agent-bridge/README.md#after-a-restart)). `--no-agent-restore` turns
  the restore off and keeps herdr's.

Update the gateway first: it calls agent-bridges at its own API version and one behind, but
not newer ones. The agent hosts can then follow one at a time.

## Development

uv workspace; each component is its own package.

```sh
uv sync --all-packages --frozen
uv run --frozen --package claude-remote-mcp-agent-bridge pytest agent-bridge
uv run --frozen --package claude-remote-mcp-gateway pytest mcp-gateway
uv run --frozen --exact pytest   # both packages plus tests/ (enrollment end to end)
```

### Releasing

Each package has its own version, tag and release workflow:

| Package | Tag | Workflow |
|---|---|---|
| `claude-remote-mcp-gateway` | `mcp-gateway-vX.Y.Z` | [release-mcp-gateway.yml](.github/workflows/release-mcp-gateway.yml) |
| `claude-remote-mcp-agent-bridge` | `agent-bridge-vX.Y.Z` | [release.yml](.github/workflows/release.yml) |

1. Set the version, e.g. `uv version --package claude-remote-mcp-agent-bridge 0.3.0`, and
   merge that to main.
2. Tag that commit on main and push the tag:
   `git tag agent-bridge-v0.3.0 && git push origin agent-bridge-v0.3.0`.
3. The workflow runs CI, checks that the tag is the package's version, builds it, and waits
   for approval in the `pypi` environment. Approve it in the run; it then publishes to PyPI
   through Trusted Publishing and creates the GitHub Release. For agent-bridge, it then
   updates the formula in [placheckij/homebrew-tap](https://github.com/placheckij/homebrew-tap)
   from [.github/homebrew/agent-bridge.rb](.github/homebrew/agent-bridge.rb), after
   installing, testing and auditing it on macOS.

## License

[MIT](LICENSE)
