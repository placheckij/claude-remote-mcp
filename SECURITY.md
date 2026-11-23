# Security

claude-remote-mcp lets a caller on the internet drive Claude Code agents on someone's
computer, so a hole in it can mean code running on that machine. Please report one
privately.

## Reporting a vulnerability

Use GitHub's private reporting: the repository's **Security** tab → **Report a
vulnerability**. Please don't open a public issue or pull request for it.

Include what an attacker needs (network position, which token, which settings), the
steps to reproduce, and what they gain. You'll get a reply within a week.

## Supported versions

Only the latest PyPI release of each package is supported: `claude-remote-mcp-gateway`
and `claude-remote-mcp-agent-bridge` (also the Homebrew formula in `placheckij/tap`,
built from the same release). Fixes ship as a new release of the package
affected, built from `main`; there are no maintained release branches. To get one,
upgrade the package and run its setup again (`mcp-gateway setup`, `agent-bridge setup`;
the gateway first, see [Updating](README.md#updating)).

## In scope

- mcp-gateway: the OAuth sign-in (registration, the passphrase page, codes, tokens and
  their storage), the token check, the Host and Origin checks, the rule that everything
  but the enrollment routes answers loopback (the tunnel's cloudflared) only, and
  anything that lets a tool call do more than its description says
- enrollment: the window's secret and its limits, and the checks that only private,
  loopback or link-local IP addresses may call the enrollment routes (nothing the tunnel
  forwarded) or be called by the gateway (only ones that answer as the bridge enrolling)
- agent-bridge: its token check, the allowed-roots and agent-count limits (the roots
  bound starting and changing agents; listing and reading them covers every agent in
  herdr, by design), which Claude
  account (login and config) an agent starts under, reading each agent's process
  environment to name its account (only `CLAUDE_CONFIG_DIR` is used), and folder trust (it must never
  write the default Claude config, or trust a folder outside the trust root)
- agent restore: `agents.json` (mode 0600 on macOS and Linux) and the restore after a
  herdr restart (and, for agents a restore couldn't start, whenever agent-bridge starts),
  which starts only agents that were running in herdr, ones started by
  hand included, on their saved folder, account (if theirs couldn't be read, the one the
  folder would get if started now) and arguments; the panes it closes (a pane herdr
  reports idle, whose process it could read, running a saved agent's conversation under other arguments or another account, whether
  herdr resumed it or someone did by hand, and an idle leftover shell); and reading each
  Claude agent's process environment every 30 s, of which only `CLAUDE_CONFIG_DIR` is kept;
  it skips the allowed-roots and agent-count limits and the trust root by design, and
  doesn't restore a folder Claude Code doesn't already trust
- `mcp-gateway setup` (its `deploy/setup.sh`): the root-owned copy in `/opt/mcp-gateway`
  the service runs, the `/usr/local/bin/mcp-gateway` wrapper that runs commands as the
  service user, file permissions, where tokens end up, the tunnel's ingress rules
- `agent-bridge setup` (its installers for macOS, Linux and Windows): the service files,
  where the bridge's token ends up, the Windows firewall rule, the line it adds to herdr's
  config, and herdr's Claude hook it installs in the agents' non-default Claude configs
- the releases: the tag-triggered workflows (`.github/workflows/release.yml`,
  `release-mcp-gateway.yml`) and the packages they publish to PyPI through Trusted
  Publishing and attach to the GitHub Release, and the agent-bridge formula they push to
  `placheckij/homebrew-tap`

## Out of scope

- What an agent does once it runs: agents start with the Claude Code permission mode
  you configure (`--permission-mode auto` by default; a restored agent with the arguments
  it was started with), and that is a deliberate choice
- Problems in claude.ai, Claude Code, herdr, Cloudflare or uv themselves; report those
  to their maintainers
- Attacks that need the sign-in passphrase, a valid OAuth or static token, an agent
  host's bridge token, or a shell on the gateway host or an agent host
- Attacks from the LAN that need an open enrollment window's secret, or that read the
  plain-HTTP traffic between the gateway and the agent-bridges (it carries each bridge's
  token, and the window's secret when a bridge enrolls; TLS on the LAN is not provided)
