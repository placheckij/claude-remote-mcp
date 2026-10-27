"""The MCP tools claude.ai sees; each one wraps an agent-bridge call."""

import posixpath
import re
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from mcp_gateway.bridge import AgentBridge, BridgeError
from mcp_gateway.config import Settings
from mcp_gateway.replies import describe, report, with_truncation_note

INSTRUCTIONS = """\
Controls the user's Claude Code agents running on their agent host (via herdr). The user is
often driving and talking by voice: keep replies short and speakable, never read out
long code or logs, and summarize what an agent did instead.

Typical flow: list_agents → send_prompt to an agent (waits for it to finish) →
read_output for more detail. If the agent is still working when the wait ends, call
wait_for_agent to keep waiting instead of re-reading its output. If an agent's status is
"blocked" it is waiting for an approval: read the question with read_output, tell the
user, and answer with answer_agent (with wait_seconds to wait for the result) only after
the user decides. Each wait lasts at most 100 seconds; for longer work call
wait_for_agent again. Agent ids look like "w1:p1".
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
ACTION = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)

AgentId = Annotated[str, Field(description='Agent id from list_agents, e.g. "w1:p1".')]
# Nothing is sent until a tool returns, and Cloudflare ends a request after 125 s; the cap
# leaves room for the calls around the wait.
MAX_WAIT_SECONDS = 100
WaitSeconds = Annotated[
    int,
    Field(
        ge=5,
        le=MAX_WAIT_SECONDS,
        description=f"How long to wait for the agent to finish, at most {MAX_WAIT_SECONDS} s.",
    ),
]
Lines = Annotated[int, Field(ge=1, le=200, description="Output lines to return.")]


def build_server(settings: Settings, bridge: AgentBridge) -> MCPServer:
    mcp = MCPServer(name="claude-remote", instructions=INSTRUCTIONS)

    async def call(coro):
        try:
            return await coro
        except BridgeError as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=READ_ONLY)
    async def list_agents() -> str:
        """List the Claude Code agents running on the agent host with id, name, status and folder."""
        agents = await call(bridge.list_agents())
        if not agents:
            return "No agents are running."
        return f"{len(agents)} agents:\n" + "\n".join(describe(agent) for agent in agents)

    @mcp.tool(annotations=READ_ONLY)
    async def agent_details(agent_id: AgentId) -> str:
        """Show one agent's name, status and folder."""
        return describe(await call(bridge.get_agent(agent_id)))

    @mcp.tool(annotations=READ_ONLY)
    async def read_output(
        agent_id: AgentId,
        lines: Annotated[int, Field(ge=1, le=200, description="How many recent lines.")] = 20,
    ) -> str:
        """Read the latest lines of an agent's terminal (its recent answer or question)."""
        output = await call(bridge.read_output(agent_id, lines))
        return with_truncation_note(output)

    @mcp.tool(annotations=ACTION)
    async def send_prompt(
        agent_id: AgentId,
        text: Annotated[str, Field(min_length=1, description="The prompt, as the user said it.")],
        wait_seconds: WaitSeconds = 90,
        lines: Lines = 20,
    ) -> str:
        """Send a prompt to an agent and wait until it finishes, then return its status and
        latest output. If it is still working when the wait ends, use wait_for_agent. A
        blocked agent refuses prompts: answer its question with answer_agent first."""
        result = await call(bridge.prompt(agent_id, text, wait_seconds, lines))
        return report(result, wait_seconds)

    @mcp.tool(annotations=READ_ONLY)
    async def wait_for_agent(
        agent_id: AgentId, wait_seconds: WaitSeconds = 90, lines: Lines = 20
    ) -> str:
        """Wait until an agent finishes its current work or needs an approval, then return
        its status and latest output. Returns at once if it is already idle or done."""
        return report(await call(bridge.wait(agent_id, wait_seconds, lines)), wait_seconds)

    @mcp.tool(annotations=ACTION)
    async def answer_agent(
        agent_id: AgentId,
        keys: Annotated[
            list[str],
            Field(
                min_length=1,
                max_length=20,
                description='Key presses, e.g. ["1"], ["Enter"], ["Down", "Enter"] or ["esc"].',
            ),
        ],
        wait_seconds: Annotated[
            int | None,
            Field(
                ge=5,
                le=MAX_WAIT_SECONDS,
                description=f"Also wait this long for the agent to finish, at most {MAX_WAIT_SECONDS} s.",
            ),
        ] = None,
        lines: Lines = 20,
    ) -> str:
        """Answer a blocked agent's question (e.g. a permission prompt) with key presses.
        Only use this after the user decided; read_output shows the options first. With
        wait_seconds, also waits for the agent to carry on and reports how it ended."""
        await call(bridge.send_keys(agent_id, keys))
        sent = f"Sent {' '.join(keys)} to {agent_id}."
        if wait_seconds is None:
            return sent
        result = await call(bridge.wait(agent_id, wait_seconds, lines, expect_change=True))
        return f"{sent}\n{report(result, wait_seconds)}"

    @mcp.tool(annotations=ACTION)
    async def start_agent(
        project: Annotated[
            str,
            Field(
                min_length=1,
                description=f"Project folder: a name under {settings.projects_root} "
                "(e.g. \"grafik\") or an absolute path on the agent host.",
            ),
        ],
        name: Annotated[
            str | None,
            Field(description="Short agent name, any words; turned into a slug like \"my-agent\". Defaults to the folder name."),
        ] = None,
    ) -> str:
        """Start a new Claude Code agent in a project folder on the agent host. Refused when too
        many agents are already running or the folder is not allowed."""
        cwd = posixpath.normpath(posixpath.join(settings.projects_root, project))
        # Names come from speech ("my agent"); agent-bridge only takes [\w.-].
        agent_name = _safe_name(name or posixpath.basename(cwd))
        agent = await call(bridge.start_agent(cwd, agent_name))
        return f"Started {describe(agent)}"

    @mcp.tool(annotations=DESTRUCTIVE)
    async def stop_agent(agent_id: AgentId) -> str:
        """Stop an agent and close its terminal pane. Ask the user to confirm first."""
        await call(bridge.stop_agent(agent_id))
        return f"Stopped {agent_id}."

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return mcp


def _safe_name(value: str) -> str:
    return re.sub(r"[^\w.-]+", "-", value.strip()).strip("-.")[:64].strip("-.") or "agent"
