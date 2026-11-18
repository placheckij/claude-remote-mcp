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
and `claude-remote-mcp-agent-bridge`. Fixes ship as a new release of the package
affected, built from `main`; there are no maintained release branches. To get one,
upgrade the package and run its setup again (`mcp-gateway setup`, `agent-bridge setup`;
the gateway first, see [Updating](README.md#updating)).

## In scope

- mcp-gateway: the OAuth sign-in (registration, the passphrase page, codes, tokens and
  their storage), the token check, the Host and Origin checks, the rule that everything
  but the enrollment routes answers loopback (the tunnel's cloudflared) only, and
  anything that lets a tool call do more than its description says
- enrollment: the window's secret and its limits, the checks on who may call the
  enrollment routes (LAN only, nothing the tunnel forwarded), and on which addresses the
  gateway will call (LAN IP addresses that answer as the bridge enrolling)
- agent-bridge: its token check, the allowed-roots and agent-count limits, which Claude
  account (login and config) an agent starts under, and folder trust (it must never
  write the default Claude config, or trust a folder outside the trust root)
- `mcp-gateway setup` (its `deploy/setup.sh`): the root-owned copy in `/opt/mcp-gateway`
  the service runs, the `/usr/local/bin/mcp-gateway` wrapper that runs commands as the
  service user, file permissions, where tokens end up, the tunnel's ingress rules
- `agent-bridge setup` (its installers for macOS, Linux and Windows): the service files,
  where the bridge's token ends up, the Windows firewall rule
- the releases: the tag-triggered workflows (`.github/workflows/release.yml`,
  `release-mcp-gateway.yml`) and the packages they publish to PyPI through Trusted
  Publishing and attach to the GitHub Release

## Out of scope

- What an agent does once it runs: agents start with the Claude Code permission mode
  you configure (`--permission-mode auto` by default), and that is a deliberate choice
- Problems in claude.ai, Claude Code, herdr, Cloudflare or uv themselves; report those
  to their maintainers
- Attacks that need the sign-in passphrase, a valid OAuth or static token, an agent
  host's bridge token, or a shell on the gateway host or an agent host
- Attacks from the LAN that need an open enrollment window's secret, or that read the
  gateway's plain-HTTP traffic to the agent-bridges (it carries each bridge's token; TLS
  on the LAN is not provided)
