# Agent teams over the gateway

Issue #166.

## Intent

Agents on any host help each other toward a common goal. Any agent can start a helper for a
scoped task or for assistance, coordinate with it, and stop it when the job is done. The gateway
publishes the playbook as a skill (Skills over MCP), so every session with the connector, in
Claude Code or in voice, knows the connector is there and how to work as a team through it.

## Goals

- Any agent with the connector can start, task, track and stop helpers on any host.
- One shared playbook, served by the gateway as a skill.
- The gateway records ownership and enforces limits; the skill does not just ask for them.
- Every approval goes up to the human.
- Orphaned helpers can be found and stopped.

## Non-goals

- Messaging outside the tree (peer to peer), and LAN transport (#142).

## Anti-goals

- An agent answers a permission prompt, its own helper's or any other.
- Helpers keep running with no owner and no way to find them.
- Fan-out runs away.
- An agent stops an agent it does not own without the human confirming.

## Constraints

- Waits are capped at 20 s per call; claude.ai drops a tool call after about 60 s (30 s in voice).
- Skills over MCP: Claude Code 2.1.331+, interactive sessions only (not `claude -p`). A skill is
  named `<server>:<skill>`. Scanned skills: 100 KB per text file, 400 KB per skill.
- The Python MCP SDK 2.3.0 has no skills helpers; the extension is merged on `main`, unreleased.
- herdr is the source of truth for agents. agent-bridge keeps `agents.json` to restore agents.
- New bridge fields need an `api_version` bump; the gateway keeps working with older bridges.
- MCP calls carry no caller identity: the gateway can't tell which agent made a call.
- A blocked agent can't act, so it can't report its own prompt. `send_prompt` refuses it.
- The bridge refuses a prompt to a working agent (`agent_busy`): herdr's `--wait` can't tell
  turns apart.
- A restore brings an agent back in a new pane, with a new id.
- Public repo: no real project, folder or host names in the skill.

## Approaches

- **A. Skill only, on today's tools.** Ownership and depth as naming conventions. Not the end
  state: conventions get forgotten and orphans are hard to find. Built first, as step 1.
- **B. Skill plus gateway and bridge support (chosen).** Ownership recorded with the agent,
  limits enforced, tree listing, stop checked against ownership.
- Dropped: LAN messaging (#142) is transport, not coordination. Claude Code's own SendMessage
  doesn't reach agents on other hosts unless they are Remote Control sessions.

## Decided

- **Tree.** Whoever starts a helper owns it: gives the task, takes the result, stops it.
  Helpers may start their own helpers.
- **Results.** The helper works on its own branch; the branch or PR is the handoff. It ends
  with a short summary.
- **Sync is push; owners stay free.** While helpers run, an owner keeps doing its own work or
  ends its turn; it does not sit polling.
  - A helper reports, or asks for help, by sending to its owner's id. A new bridge mode
    delivers a message without waiting, also to a working agent: Claude Code holds text typed
    during a turn until the turn ends.
  - The bridge notices when an agent with an owner becomes blocked and pushes that to the
    owner (the blocked agent's id and what the prompt asks), through the gateway when the owner
    is on another host.
- **Approvals.** The owner never answers a helper's prompt. It passes the blocked agent's id and
  what the prompt asks to its own owner; each owner above does the same. The top asks the human,
  who reads the real prompt with `read_output` on the blocked agent and answers it there with
  `answer_agent`. Nothing passes back down. `answer_agent`'s rule is unchanged.
- **The top** is a Claude Code session (its question to the user) or voice ("waiting for an
  answer").
- **Steps.** Step 1: the skill on today's tools, which also proves Skills over MCP in Claude
  Code and voice. Step 2: gateway and bridge support.
- **Voice loads skills** (the user has used it). If step 1 shows the gateway's skill doesn't
  load there, stop and revisit before step 2.

## My calls

- **Ownership record.** `start_agent` takes the caller's own agent id as owner. agent-bridge
  stores it with the agent (and in `agents.json`) and returns it in agent summaries.
- **Top.** A start with no owner is allowed (voice, a human's session, agents from before this
  feature) and makes a top agent, at depth 0.
- **Claimed identity.** Since calls carry no caller identity, the owner id is what the caller
  claims. The checks guard against mistakes, not malice; the human stays the last line. An
  agent that leaves out its owner starts a new tree: accepted, bounded by agent-bridge's
  per-host agent limit.
- **Stop.** `stop_agent` stops without asking the human when the caller names itself as the
  target's owner. Otherwise it keeps today's behaviour: the tool asks the caller to confirm
  with the user first (not enforced; identity is claimed anyway). Stopping an owner leaves its
  helpers running; they show as orphans.
- **Orphans.** An agent whose recorded owner no longer exists. Agents with no owner are tops,
  not orphans.
- **Restore.** A restore gives agents new ids, so helpers of a restored owner become orphans,
  found and cleaned up as any other. Remapping owner ids on the same host is left to the builder.
- **Listing.** `list_agents` shows each agent's owner and can list one agent's helpers, or
  orphans only.
- **Limits.** The gateway refuses a start deeper than a configurable depth (default 2: a top's
  helpers are depth 1, theirs depth 2) or beyond a configurable total of helpers per tree. A
  limit check that can't reach a host in the tree fails closed. agent-bridge's per-host agent
  limit stays.
- **Delivery to a blocked owner.** Typing into a pending prompt would answer it, so a message
  for a blocked owner is held and delivered once it is no longer blocked.
- **Skill contents.** When to start a helper; writing its task (goal, scope, branch, how to
  report, owner id); reporting to an owner and handling reports; passing approvals up; collecting results; stopping
  helpers and cleaning up orphans; limits.

## Left to the builder

- SDK route: pin `mcp` to `main` (recommended in #166) or register `skills/list` and
  `skills/get` on 2.3.0's extension API.
- How an agent learns its own full id (host and pane). herdr gives the pane as
  `HERDR_PANE_ID`, not the host name.
- Check first that Claude Code holds text typed mid-turn and runs it after the turn; the push
  design depends on it.
- How a bridge reaches an owner on another host (through the gateway), and where held
  messages live.
- Field names, `api_version` handling, skill wording, tests.
