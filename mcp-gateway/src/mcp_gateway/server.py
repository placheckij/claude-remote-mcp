"""MCP server exposed to claude.ai as a custom connector.

Tools wrap agent-bridge and answer in short plain text: they are read aloud in the car,
so every result is a few lines, with detail only on request.
"""

import hmac
import posixpath
import re
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_gateway.bridge import BridgeError, AgentBridge
from mcp_gateway.config import Settings

INSTRUCTIONS = """\
Controls the user's Claude Code agents running on their agent host (via herdr). The user is
often driving and talking by voice: keep replies short and speakable, never read out
long code or logs, and summarize what an agent did instead.

Typical flow: list_agents → send_prompt to an agent (waits for it to finish) →
read_output for more detail. If an agent's status is "blocked" it is waiting for an
approval: read the question with read_output, tell the user, and answer with
answer_agent only after the user decides. Agent ids look like "w1:p1".
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
ACTION = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)

AgentId = Annotated[str, Field(description='Agent id from list_agents, e.g. "w1:p1".')]


def describe(agent: dict) -> str:
    label = agent.get("name") or agent.get("title") or "unnamed"
    return f"{agent['id']} · {label} · {agent['status']} · {agent.get('cwd') or '?'}"


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
        return _with_truncation_note(output)

    @mcp.tool(annotations=ACTION)
    async def send_prompt(
        agent_id: AgentId,
        text: Annotated[str, Field(min_length=1, description="The prompt, as the user said it.")],
        wait_seconds: Annotated[
            int, Field(ge=5, le=600, description="How long to wait for the agent to finish.")
        ] = 90,
        lines: Annotated[int, Field(ge=1, le=200, description="Output lines to return.")] = 20,
    ) -> str:
        """Send a prompt to an agent and wait until it finishes, then return its status and
        latest output. If it is still working when the wait ends, check later with
        read_output or agent_details."""
        result = await call(bridge.prompt(agent_id, text, wait_seconds, lines))
        agent = result["agent"]
        if result["timed_out"]:
            head = f"Still working after {wait_seconds}s. Check back with read_output."
        elif agent["status"] == "blocked":
            head = "The agent is waiting for an approval. Ask the user, then use answer_agent."
        else:
            head = f"Agent is {agent['status']}."
        return f"{head}\n{describe(agent)}\n---\n{_with_truncation_note(result['output'])}"

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
    ) -> str:
        """Answer a blocked agent's question (e.g. a permission prompt) with key presses.
        Only use this after the user decided; read_output shows the options first."""
        await call(bridge.send_keys(agent_id, keys))
        return f"Sent {' '.join(keys)} to {agent_id}."

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
            Field(description="Short agent name; defaults to the folder name."),
        ] = None,
    ) -> str:
        """Start a new Claude Code agent in a project folder on the agent host. Refused when too
        many agents are already running or the folder is not allowed."""
        cwd = posixpath.normpath(posixpath.join(settings.projects_root, project))
        agent_name = name or _safe_name(posixpath.basename(cwd))
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


def _with_truncation_note(output: dict) -> str:
    text = output["text"] or "(no output)"
    return f"(earlier output cut)\n{text}" if output["truncated"] else text


def _safe_name(value: str) -> str:
    return re.sub(r"[^\w.-]+", "-", value).strip("-")[:64] or "agent"


class BearerAuth:
    """Rejects every request without the connector's token, except the health check."""

    def __init__(self, app: ASGIApp, token: str, open_paths: frozenset[str] = frozenset({"/healthz"})):
        self.app = app
        self.expected = f"Bearer {token}".encode()
        self.open_paths = open_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] not in self.open_paths:
            provided = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(provided, self.expected):
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(settings: Settings, bridge: AgentBridge | None = None) -> ASGIApp:
    bridge = bridge or AgentBridge.connect(settings.bridge_url, settings.bridge_token)
    mcp = build_server(settings, bridge)
    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[settings.public_host, "127.0.0.1:*", "localhost:*"],
            allowed_origins=["https://claude.ai"],
        ),
    )
    return BearerAuth(app, settings.token)


def main() -> ASGIApp:
    """uvicorn factory: `uvicorn mcp_gateway.server:main --factory`."""
    return create_app(Settings.from_env())
