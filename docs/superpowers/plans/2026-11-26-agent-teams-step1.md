# Agent teams, step 1: the skill on today's tools - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The gateway serves an `agent-teams` skill over MCP that teaches any connected session to start, task, wait for and stop helper agents with today's tools, and it is proven to load in Claude Code and in voice.

**Architecture:** Pin the Python MCP SDK to the `main` commit that has the `Skills` extension. A new `mcp_gateway/skills.py` loads every skill folder shipped under `mcp_gateway/skills/`, registers each file as a resource and answers `skills/list` and `skills/get`. Step 2 (ownership, limits, push) gets its own plan once this step's results are in.

**Tech Stack:** Python 3.11+, `mcp` (python-sdk `main`), hatchling, pytest with Starlette's `TestClient`.

**Spec:** `docs/superpowers/specs/2026-11-25-agent-teams-design.md`

## Global Constraints

- `mcp` pinned to python-sdk commit `77001c96f9507be0f4d009b83210a384a5e08bda` (has #3485 Skills and #3776 extensions on legacy `initialize`).
- The pin is a direct reference, so this branch must not be released: no `mcp-gateway-v*` tag, PR stays draft until an `mcp` release has Skills and replaces the pin.
- Skill URI scheme `skill://<name>/<path>`; `SKILL.md` frontmatter has `name` (equal to its folder) and `description`.
- Scanned-skill limits: 100 KB per text file, 400 KB per skill.
- Public repo: no real project, folder or host names in the skill or tests.
- Claude Code 2.1.331+ to load MCP skills; interactive sessions only.
- Tests: `uv run --exact pytest` from the worktree root.

## Review Focus

- claude.ai connects with a legacy `initialize`: the skills extension must still be advertised there, or the skill silently never appears (Task 2 test).
- Served bytes must match the advertised digest and size exactly, or clients drop the skill (Task 2 test).
- A skill folder whose frontmatter `name` differs from the folder must fail at startup, not serve a skill clients reject (Task 2 test).
- An unknown skill URI in `skills/get` answers `-32602`, not a server error (Task 2 test).
- The shipped skill stays under 100 KB and its frontmatter is valid (Task 3 test).

---

### Task 1: Pin the SDK to `main`

**Files:**
- Modify: `mcp-gateway/pyproject.toml` (dependency `mcp`, add `[tool.hatch.metadata]`)
- Modify: `uv.lock`

**Interfaces:**
- Produces: `mcp.server.skills.Skills`, `mcp.shared.skills.{Skill, SkillResource, ListSkillsResult, GetSkillResult, ListSkillsParams, GetSkillParams}` importable.

- [ ] **Step 1:** Replace `"mcp>=2.3.0"` with `"mcp @ git+https://github.com/modelcontextprotocol/python-sdk@77001c96f9507be0f4d009b83210a384a5e08bda"`, and add `[tool.hatch.metadata]` with `allow-direct-references = true`, with a comment: unreleased Skills extension; replace with a release before tagging.
- [ ] **Step 2:** Run `uv lock`. Expected: `mcp` resolves to the git commit.
- [ ] **Step 3:** Run `uv run --exact pytest`. Fix any fallout from the 20 SDK commits since 2.3.0 (for example schema titles, invalid-params errors) in the gateway, not by loosening tests. Expected: all pass.
- [ ] **Step 4:** Run `uv run --exact python -c "from mcp.server.skills import Skills"`. Expected: no output, exit 0.
- [ ] **Step 5: Commit** `git commit -am "mcp-gateway: pin the MCP SDK to main for the Skills extension (#166)"`

### Task 2: Serve skills from the package

**Files:**
- Create: `mcp-gateway/src/mcp_gateway/skills.py`
- Modify: `mcp-gateway/src/mcp_gateway/tools.py` (`build_server`)
- Create: `mcp-gateway/tests/test_skills.py`

**Interfaces:**
- Consumes: Task 1's SDK names.
- Produces:
  - `SKILLS_DIR: Traversable` = `files("mcp_gateway") / "skills"`.
  - `load_skills(root: Traversable) -> list[LoadedSkill]`; `LoadedSkill` holds `skill: Skill` and `files: dict[str, bytes]` (URI to bytes). Raises `ValueError` naming the folder when `SKILL.md` is missing, has no `name`/`description`, or `name` differs from the folder.
  - `register_skills(mcp: MCPServer, skills: list[LoadedSkill]) -> None`: adds one `TextResource` per file (`mime_type` `text/markdown` for `.md`, else `text/plain`).
  - `skills_extension(skills: list[LoadedSkill]) -> Skills`: `get_skill` raises `MCPError(INVALID_PARAMS)` for an unknown URI.
  - `build_server(...)` passes `extensions=[skills_extension(loaded)]` and calls `register_skills`, with `loaded = load_skills(SKILLS_DIR)`.

Tests build a skill folder in `tmp_path` for the loader, and use the `client` fixture and `rpc` helper from `test_server.py` (move `rpc`, `client`, `fake`, `app` into `conftest.py` if importing them is awkward) for the wire tests.

- [ ] **Step 1: Write the failing tests** in `mcp-gateway/tests/test_skills.py`:
  - `test_loads_a_skill_with_its_manifest`: a folder `demo/` with `SKILL.md` (`name: demo`, `description: d`) and `references/x.md`; `load_skills` returns one skill with `uri == "skill://demo/SKILL.md"`, two resources, each `digest == "sha256:" + sha256(bytes).hexdigest()` and `size == len(bytes)`.
  - `test_refuses_a_name_that_differs_from_its_folder`: `name: other` in `demo/` raises `ValueError` mentioning `demo`.
  - `test_refuses_a_skill_without_a_description`: raises `ValueError`.
  - `test_initialize_advertises_skills`: `rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})` has `"io.modelcontextprotocol/skills"` in `result.capabilities.extensions`.
  - `test_lists_the_shipped_skill`: `rpc(client, "skills/list")` contains a skill with `uri == "skill://agent-teams/SKILL.md"` and `frontmatter.name == "agent-teams"`.
  - `test_served_bytes_match_the_manifest`: for each resource of that skill, `resources/read` text encoded as UTF-8 has the advertised digest and size.
  - `test_get_skill_answers_by_uri_and_refuses_unknown`: `skills/get` with the listed URI returns the same entry; with `skill://nope/SKILL.md` returns error code `-32602`.
- [ ] **Step 2:** Run `uv run --exact pytest mcp-gateway/tests/test_skills.py -v`. Expected: FAIL (`mcp_gateway.skills` missing).
- [ ] **Step 3:** Implement `skills.py` as in Interfaces. Parse frontmatter as the block between the first two `---` lines, keys `name` and `description` (single-line `key: value`; no YAML dependency). Read files as bytes; serve `bytes.decode("utf-8")`. Create `mcp-gateway/src/mcp_gateway/skills/agent-teams/SKILL.md` with only frontmatter (`name: agent-teams`, a one-line `description`) so the wire tests run; Task 3 writes its body.
- [ ] **Step 4:** Wire `build_server`. Run `uv run --exact pytest`. Expected: all pass, including `test_lists_the_tools` unchanged.
- [ ] **Step 5:** Run `uv build --package claude-remote-mcp-gateway` and `unzip -l dist/claude_remote_mcp_gateway-*.whl | grep SKILL.md`. Expected: `mcp_gateway/skills/agent-teams/SKILL.md` listed. Remove `dist/`.
- [ ] **Step 6: Commit** `git add mcp-gateway && git commit -m "mcp-gateway: serve skills over MCP (#166)"`

### Task 3: Write the `agent-teams` skill

**Files:**
- Modify: `mcp-gateway/src/mcp_gateway/skills/agent-teams/SKILL.md`
- Modify: `mcp-gateway/src/mcp_gateway/tools.py` (`stop_agent` docstring)
- Test: `mcp-gateway/tests/test_skills.py`

**Interfaces:**
- Consumes: Task 2's loader.

The skill teaches the design's tree with today's tools. Ownership and depth travel in the task text (step 2 replaces this with recorded ownership and push). It must say, in plain sentences:

- **Description** (frontmatter, one line): when to load it: starting helper agents on agent hosts, handing them scoped tasks or asking for help, and coordinating or stopping them.
- **Who you are:** find your own id with `list_agents` (the entry whose pane matches `HERDR_PANE_ID` and whose folder is your working folder); a voice chat or a session that is not an agent is the top, depth 0.
- **Starting a helper:** `start_agent` in the project, with a short name for the task. Not from depth 2: an agent at depth 2 does not start helpers.
- **The task text** starts with these lines, then the task:
  ```
  Owner: <your id>
  Depth: <your depth + 1>
  Branch: <a new branch for this task>
  When done, end your turn with at most 10 lines: what you did, the branch, anything open.
  If you need help or a decision, end your turn with the question.
  ```
- **Waiting:** `send_prompt` the task, then `wait_for_agent` until the helper is idle, done, waiting for an answer, or blocked. Read its summary; the branch is the real handoff.
- **Approvals:** never answer a helper's permission prompt. When a helper is blocked, pass up its id and what the prompt asks: a helper ends its turn with that as its question; the top asks the human, who answers the blocked agent directly with `answer_agent`.
- **Help:** a helper waiting for an answer gets it with `send_prompt`.
- **Finishing:** after taking the result, stop your own helpers with `stop_agent`; never stop agents you did not start without the human confirming. Before ending, check `list_agents` for helpers of yours still running.

- [ ] **Step 1: Write the failing test** `test_shipped_skill_is_within_limits`: `load_skills(SKILLS_DIR)` has exactly the skill `agent-teams`; its `SKILL.md` is under 100 KB and contains `Owner:`, `Depth:`, `answer_agent` and `stop_agent`.
- [ ] **Step 2:** Run it. Expected: FAIL (body empty).
- [ ] **Step 3:** Write the body as above, in the repo's plain style, no real host or project names.
- [ ] **Step 4:** Change `stop_agent`'s docstring to: "Stop an agent and close its terminal pane. Ask the user to confirm first, unless it is a helper you started yourself and its job is done." Run `uv run --exact pytest`. Expected: all pass.
- [ ] **Step 5: Commit** `git commit -am "mcp-gateway: agent-teams skill (#166)"`

### Task 4: Prove it in Claude Code and voice

Manual; needs the user's go before deploying to the gateway host (outward-facing). Steps from the local-only `docs/local/gateway-host-access.md`.

- [ ] **Step 1:** Push the branch and open a draft PR for #166 that says it must not be released while `mcp` is pinned to git.
- [ ] **Step 2:** With the user's go, deploy the branch to the gateway host with the "unreleased branch" command from `docs/local/gateway-host-access.md`. Expected: `/healthz` answers `ok`.
- [ ] **Step 3:** Run `npx @modelcontextprotocol/inspector@latest --cli` skill verification against the gateway (needs an OAuth token; skip with a note if impractical). Expected: the manifest verifies.
- [ ] **Step 4:** Ask the user to start a new Claude Code session (2.1.331+) and run `/skills`. Expected: the connector's `agent-teams` skill is listed and loads when asked.
- [ ] **Step 5:** Ask the user to ask voice in the Claude app how to have one agent help another. Expected: it loads `agent-teams`.
- [ ] **Step 6:** Comment the results of steps 3 to 5 on #166. If voice does not load it, stop: the spec says to revisit before step 2.

### Task 5: Check that Claude Code holds text typed mid-turn

Manual; the step 2 push design depends on it.

- [ ] **Step 1:** Start a scratch agent with `start_agent`, `send_prompt` a task that takes over a minute (for example, sleep 60 then reply "first").
- [ ] **Step 2:** While it works, type a second message into its pane with herdr (send text and Enter without waiting), for example "reply: second".
- [ ] **Step 3:** Wait for it. Expected if it holds: it answers "first", then runs "second" as its next turn. Note anything else (dropped, merged, interrupted).
- [ ] **Step 4:** Stop the scratch agent; comment the result on #166.
