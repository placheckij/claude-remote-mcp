# Security

claude-remote-mcp lets a caller on the internet drive Claude Code agents on someone's
computer, so a hole in it can mean code running on that machine. Please report one
privately.

## Reporting a vulnerability

Use GitHub's private reporting: the repository's **Security** tab → **Report a
vulnerability**. Please don't open a public issue or pull request for it.

Include what an attacker needs (network position, which token, which settings), the
steps to reproduce, and what they gain. You'll get a reply within a week. Fixes go to
`main`; there are no maintained release branches.

## In scope

- mcp-gateway: the OAuth sign-in (registration, the passphrase page, codes, tokens and
  their storage), the token check, the Host and Origin checks, and anything that lets a
  tool call do more than its description says
- agent-bridge: its token check, the allowed-roots and agent-count limits, and folder
  trust (it must never write the default Claude config, or trust a folder outside the
  trust root)
- the setup scripts: file permissions, where tokens end up, the tunnel's ingress rules

## Out of scope

- What an agent does once it runs: agents start with the Claude Code permission mode
  you configure (`--permission-mode auto` by default), and that is a deliberate choice
- Problems in claude.ai, Claude Code, herdr, Cloudflare or uv themselves; report those
  to their maintainers
- Attacks that need the sign-in passphrase, a valid OAuth or static token, agent-bridge's
  token, or a shell on either host
