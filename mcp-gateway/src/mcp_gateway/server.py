"""MCP server exposed to claude.ai as a custom connector.

Tools wrap agent-bridge and answer in short plain text: they are read aloud in the car,
so every result is a few lines, with detail only on request.
"""

import hashlib
import hmac
import logging
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

log = logging.getLogger("mcp_gateway.auth")

# Headers whose values are logged as they are; any other value is replaced by its length,
# so a credential in an unexpected header never reaches the log.
SAFE_HEADERS = frozenset({
    "host", "user-agent", "accept", "accept-encoding", "accept-language", "content-type",
    "content-length", "origin", "referer", "connection", "cf-ray", "cf-visitor",
    "cf-ipcountry", "cf-connecting-ip", "x-forwarded-for", "x-forwarded-proto",
    "mcp-protocol-version", "mcp-session-id",
})
# Schemes short and well known enough to show; anything else may be the token itself.
KNOWN_SCHEMES = frozenset({"bearer", "basic", "digest", "token"})

INSTRUCTIONS = """\
Controls the user's Claude Code agents running on their agent host (via herdr). The user is
often driving and talking by voice: keep replies short and speakable, never read out
long code or logs, and summarize what an agent did instead.

Typical flow: list_agents → send_prompt to an agent (waits for it to finish) →
read_output for more detail. If the agent is still working when the wait ends, call
wait_for_agent to keep waiting instead of re-reading its output. If an agent's status is
"blocked" it is waiting for an approval: read the question with read_output, tell the
user, and answer with answer_agent (with wait_seconds to wait for the result) only after
the user decides. Agent ids look like "w1:p1".
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
ACTION = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)

AgentId = Annotated[str, Field(description='Agent id from list_agents, e.g. "w1:p1".')]
WaitSeconds = Annotated[
    int, Field(ge=5, le=600, description="How long to wait for the agent to finish.")
]
Lines = Annotated[int, Field(ge=1, le=200, description="Output lines to return.")]


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
        wait_seconds: WaitSeconds = 90,
        lines: Lines = 20,
    ) -> str:
        """Send a prompt to an agent and wait until it finishes, then return its status and
        latest output. If it is still working when the wait ends, use wait_for_agent."""
        result = await call(bridge.prompt(agent_id, text, wait_seconds, lines))
        return _report(result, wait_seconds)

    @mcp.tool(annotations=READ_ONLY)
    async def wait_for_agent(
        agent_id: AgentId, wait_seconds: WaitSeconds = 90, lines: Lines = 20
    ) -> str:
        """Wait until an agent finishes its current work or needs an approval, then return
        its status and latest output. Returns at once if it is already idle or done."""
        return _report(await call(bridge.wait(agent_id, wait_seconds, lines)), wait_seconds)

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
            Field(ge=5, le=600, description="Also wait this long for the agent to finish."),
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
        return f"{sent}\n{_report(result, wait_seconds)}"

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


def _report(result: dict, wait_seconds: int) -> str:
    """Status headline, the agent line, then its latest output."""
    agent = result["agent"]
    if result["timed_out"]:
        head = f"Still working after {wait_seconds}s. Keep waiting with wait_for_agent."
    elif agent["status"] == "blocked":
        head = "The agent is waiting for an approval. Ask the user, then use answer_agent."
    else:
        head = f"Agent is {agent['status']}."
    return f"{head}\n{describe(agent)}\n---\n{_with_truncation_note(result['output'])}"


def _with_truncation_note(output: dict) -> str:
    text = output["text"] or "(no output)"
    return f"(earlier output cut)\n{text}" if output["truncated"] else text


def _safe_name(value: str) -> str:
    return re.sub(r"[^\w.-]+", "-", value).strip("-")[:64] or "agent"


class BearerAuth:
    """Rejects every request without the connector's token, except the health check.

    Each rejection is logged with the request's headers, secrets redacted, so a client
    that sends the token differently can be diagnosed without exposing the token.
    """

    def __init__(self, app: ASGIApp, token: str, open_paths: frozenset[str] = frozenset({"/healthz"})):
        self.app = app
        self.expected = token.encode()
        self.open_paths = open_paths

    def _rejection(self, header: bytes | None) -> str | None:
        """Why the Authorization header is refused, or None if it is accepted."""
        if header is None:
            return "no authorization header"
        # The scheme is case-insensitive (RFC 9110), and claude.ai may lowercase it;
        # the token itself must match exactly.
        scheme, _, provided = header.partition(b" ")
        if scheme.lower() != b"bearer":
            return "scheme is not Bearer"
        if not hmac.compare_digest(provided, self.expected):
            return "wrong token"
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] not in self.open_paths:
            reason = self._rejection(dict(scope["headers"]).get(b"authorization"))
            if reason:
                log.warning(
                    "401 %s %s: %s; expected token %s; headers: %s",
                    scope["method"], scope["path"], reason,
                    _describe_token(self.expected), _describe_headers(scope["headers"]),
                )
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _describe_token(token: bytes) -> str:
    """Length and a short hash: enough to tell two tokens apart, useless to an attacker."""
    return f"<{len(token)} chars, sha256 {hashlib.sha256(token).hexdigest()[:8]}>"


def _describe_headers(headers: list[tuple[bytes, bytes]]) -> str:
    parts = []
    for raw_name, raw_value in headers:
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1")
        if name in ("authorization", "proxy-authorization"):
            scheme, separator, token = value.partition(" ")
            if separator and scheme.lower() in KNOWN_SCHEMES:
                value = f"{scheme!r} + {_describe_token(token.encode('latin-1'))}"
            else:
                value = f"no known scheme, {_describe_token(raw_value)}"
        elif name not in SAFE_HEADERS:
            value = f"<redacted, {len(value)} chars>"
        parts.append(f"{name}={value}")
    return ", ".join(parts)


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
