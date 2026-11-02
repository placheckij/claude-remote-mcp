# Architecture

claude-remote-mcp has three layers. Each runs in a different place, holds its own
state, and reaches the layer below only with that layer's own credential. This page is
the map; the component READMEs have the details.

```mermaid
flowchart TB
  subgraph interaction["Interaction layer"]
    direction LR
    app(["<b>Claude app</b><br/>voice · mobile · CarPlay · web · desktop"])
  end

  subgraph orchestration["Orchestration#nbsp;layer#nbsp;·#nbsp;gateway#nbsp;host"]
    direction LR
    gateway("<b>mcp-gateway</b><br/>OAuth · authentication<br/>tool routing<br/>agent operations")
    tokens[("oauth.json<br/>clients · token hashes")]
    gateway -.- tokens
  end

  subgraph execution["Execution#nbsp;layer#nbsp;·#nbsp;agent#nbsp;host"]
    direction LR
    bridge("<b>agent-bridge</b><br/>HTTP API · no state")
    herdr("<b>herdr</b><br/>workspaces · panes")
    sessions(["<b>Claude Code</b><br/>sessions"])
    bridge -- "herdr CLI" --> herdr --> sessions
  end

  %% Linking the layers, not the boxes in them, keeps each layer laid out left to right.
  interaction == "MCP over HTTPS<br/>Cloudflare Tunnel<br/>OAuth access token" ==> orchestration
  orchestration == "HTTP over the LAN<br/>agent-bridge's bearer token" ==> execution

  classDef interactionNode fill:#ffffff,stroke:#0284c7,stroke-width:2px,color:#0c4a6e
  classDef orchestrationNode fill:#ffffff,stroke:#7c3aed,stroke-width:2px,color:#3b0764
  classDef executionNode fill:#ffffff,stroke:#059669,stroke-width:2px,color:#064e3b
  classDef store fill:#f5f3ff,stroke:#7c3aed,stroke-width:1px,stroke-dasharray:4 3,color:#3b0764
  class app interactionNode
  class gateway orchestrationNode
  class tokens store
  class bridge,herdr,sessions executionNode
  style interaction fill:#e0f2fe,stroke:#0284c7,stroke-width:1px,color:#0c4a6e
  style orchestration fill:#ede9fe,stroke:#7c3aed,stroke-width:1px,color:#3b0764
  style execution fill:#d1fae5,stroke:#059669,stroke-width:1px,color:#064e3b
  linkStyle default stroke:#64748b,stroke-width:2px
```

## Interaction layer: the Claude app

The user talks to Claude, often by voice while driving. The connector is added once on
claude.ai by its URL and is then available in every Claude app on the account, including
mobile and CarPlay. Claude decides which tool to call; nothing in this repo runs here.

What this layer imposes on the others:

- **Time.** Voice mode gives up on a tool call after about 30 s, other clients after
  about 60 s. Tools therefore wait at most 20 s, and longer work is followed with
  `wait_for_agent`.
- **Speech.** Replies are read aloud, so tool results are a few short lines of plain
  text, never code or logs.
- **Approval.** Voice mode asks before each tool call unless the tools are set to
  *Always allow* in the connector's settings.

## Orchestration layer: mcp-gateway

An MCP server on an always-on host, reachable from the internet only through a
Cloudflare Tunnel. See [mcp-gateway/README.md](mcp-gateway/README.md).

- **OAuth.** It is its own authorization server for one user: claude.ai registers
  itself, and the user signs in once with a passphrase. Access tokens last an hour and
  refresh tokens 90 days, replaced on every refresh.
- **Authentication.** Every `/mcp` request needs a valid access token. The tunnel only
  publishes `/mcp` and the OAuth paths, and the server only accepts its own hostname and
  requests from claude.ai or claude.com (or with no origin).
- **Tool routing.** Each MCP tool becomes one agent-bridge call. Arguments (agent ids,
  project names, prompt length, waits) are checked before anything is sent.
- **Agent operations.** `list_agents`, `agent_details`, `read_output`, `send_prompt`,
  `wait_for_agent`, `answer_agent`, `start_agent`, `rename_agent` and `stop_agent`.
  agent-bridge's JSON comes back as one short line per agent plus the last output lines.

It runs as a single process: sign-ins in progress and the wrong-passphrase counts live in
its memory, and it reads `oauth.json` only at startup.

## Execution layer: agent-bridge and herdr

The machine where Claude Code runs. See [agent-bridge/README.md](agent-bridge/README.md).

- **agent-bridge** is a small HTTP API on the LAN with its own bearer token. It turns
  each request into `herdr` CLI calls and keeps no state of its own.
- **Workspace management.** herdr owns the agents. Each agent started here gets its own
  herdr workspace, named after the agent; renaming renames both. Agents are addressed by
  their herdr pane id, e.g. `w1:p1`.
- **Agent lifecycle.** Start in a project folder (only under the allowed roots, at most
  10 agents, on the Claude account set up for that folder), prompt, wait, answer a
  question, rename, stop. Every agent can be read; only those in allowed folders can be
  changed, including ones started by hand.
- **Claude Code sessions** run in herdr panes, each with its account's Claude config.
  With a trust root and a separate Claude config set, a new agent's folder under the
  trust root is marked trusted in that config, so it starts without Claude's trust
  dialog; the default config is never changed.

## Where state lives

| State | Where |
|---|---|
| The connector's access and refresh tokens | claude.ai |
| Signed-in clients and hashes of their tokens | `oauth.json` on the gateway host |
| Sign-ins in progress, authorization codes, wrong-passphrase counts | mcp-gateway's memory |
| Agents, their workspaces and output | herdr on the agent host |
| Claude logins and trusted folders | each account's Claude config on the agent host |

agent-bridge holds nothing: after a restart it sees the same agents through herdr.

## Trust boundaries

1. **Internet to gateway:** Cloudflare terminates TLS. The tunnel forwards only `/mcp`
   and the OAuth paths, and `/mcp` needs an access token that only the passphrase
   sign-in produces.
2. **Gateway to agent host:** a separate bearer token, over the LAN only. agent-bridge
   isn't reachable from the internet.
3. **agent-bridge to agents:** allowed roots, the agent limit and the trust root bound
   what a request can start or change.

[SECURITY.md](SECURITY.md) says how to report a vulnerability and what is in scope.
