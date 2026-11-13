"""MCP server exposed to claude.ai as a custom connector.

Layout: tools.py (the MCP tools) → hosts.py (which agent host an id names) → bridge.py
(agent-bridge client); registry.py keeps the hosts in bridges.json, cli.py changes it, and
enrollment.py lets bridges on the LAN add themselves.
replies.py turns agent-bridge's JSON into short speakable text. oauth.py signs the
connector in and keeps its tokens; auth.py checks them in front of the MCP endpoint.
"""

import logging
import os
import sys
from collections.abc import Callable

from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from uvicorn.config import STARTUP_FAILURE

from mcp_gateway.auth import BearerAuth
from mcp_gateway.bridge import AgentBridge
from mcp_gateway.config import Settings, StartupError
from mcp_gateway.enrollment import Enrollment, enrollment_routes
from mcp_gateway.hosts import Hosts
from mcp_gateway.oauth import GatewayOAuth
from mcp_gateway.registry import Registry, RegistryError
from mcp_gateway.tools import build_server, instructions

log = logging.getLogger(__name__)

# claude.ai calls the connector from Anthropic's servers and may send no Origin at all
# (accepted); Anthropic also serves Claude from claude.com.
ALLOWED_ORIGINS = ("https://claude.ai", "https://claude.com")


def create_app(
    settings: Settings,
    connect: Callable[[str, str], AgentBridge] = AgentBridge.connect,
    oauth: GatewayOAuth | None = None,
    enrollment: Enrollment | None = None,
) -> ASGIApp:
    """`connect` makes the client for a bridge's URL and token."""
    oauth = oauth or GatewayOAuth(settings)
    try:
        registry = Registry(settings.state_dir / "bridges.json")
    except RegistryError as error:
        oauth.close()
        raise StartupError(str(error)) from None
    legacy = (settings.bridge_url, settings.bridge_token) if settings.bridge_url else None
    hosts = Hosts(registry, connect, legacy)
    mcp = build_server(settings, hosts, oauth)
    # The instructions name the hosts, so they follow the registry.
    hosts.on_change = lambda names: setattr(mcp._lowlevel_server, "instructions", instructions(names))
    hosts.on_change(registry.names())
    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[settings.public_host, "127.0.0.1:*", "localhost:*"],
            allowed_origins=list(ALLOWED_ORIGINS),
        ),
    )
    app = BearerAuth(StatelessEdges(app, hosts, oauth), oauth.load_access_token, oauth.resource_metadata_url)
    # In front of the token check: bridges have no OAuth token, and the routes check their own.
    enrollment = enrollment or Enrollment(settings.state_dir / "enrollment.json")
    return enrollment_routes(app, registry, enrollment, connect)


class StatelessEdges:
    """The parts of the transport the SDK leaves to the server, behind the token check:

    - a refused Origin is logged as an error: the request had a valid token, so it most
      likely came from the Claude app, and every call will fail the same way;
    - GET /mcp answers 405, as the spec allows: stateless mode never sends anything on a GET
      stream, which would only hold a task open until the client hangs up;
    - at startup and before every MCP request the agent hosts are brought up to date with
      bridges.json, which `mcp-gateway bridges` may have changed;
    - on shutdown the agent-bridge clients are closed and the state folder unlocked.
    """

    def __init__(self, app: ASGIApp, hosts: Hosts, oauth: GatewayOAuth):
        self.app = app
        self.hosts = hosts
        self.oauth = oauth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, self._refreshing_hosts_on_startup(receive), self._closing_hosts_on_shutdown(send))
            return
        if scope["type"] == "http" and scope["path"] == "/mcp":
            origin = Headers(scope=scope).get("origin")
            if origin and origin not in ALLOWED_ORIGINS:
                log.error(
                    "403 %s %s: Origin %r is not allowed (allowed: %s); the token was valid, "
                    "so if this is the Claude app, every call fails until the Origin is allowed",
                    scope["method"], scope["path"], origin, ", ".join(ALLOWED_ORIGINS),
                )
                await JSONResponse({"error": "origin not allowed"}, status_code=403)(scope, receive, send)
                return
            if scope["method"] == "GET":
                response = JSONResponse({"error": "method not allowed"}, status_code=405, headers={"Allow": "POST"})
                await response(scope, receive, send)
                return
            await self.hosts.refresh()
        await self.app(scope, receive, send)

    def _refreshing_hosts_on_startup(self, receive: Receive) -> Receive:
        # Imports the legacy bridge before the first request, so `bridges list` shows it.
        async def wrapped() -> Message:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await self.hosts.refresh()
            return message

        return wrapped

    def _closing_hosts_on_shutdown(self, send: Send) -> Send:
        async def wrapped(message: Message) -> None:
            if message["type"] == "lifespan.shutdown.complete":
                await self.hosts.aclose()
                self.oauth.close()
            await send(message)

        return wrapped


class LevelFormatter(logging.Formatter):
    """Log lines with their level in front, like uvicorn's own (`WARNING:  …`).

    Under systemd each line also starts with its syslog priority (`<4>`), which journald
    takes off and files the line by, so `journalctl -p err` shows the errors. Every line of
    a traceback gets it, since journald reads each line on its own.
    """

    PRIORITIES = {logging.DEBUG: 7, logging.INFO: 6, logging.WARNING: 4, logging.ERROR: 3, logging.CRITICAL: 2}

    def __init__(self, journald: bool):
        super().__init__("%(message)s")
        self.journald = journald

    def format(self, record: logging.LogRecord) -> str:
        text = f"{record.levelname + ':':<10}{super().format(record)}"
        if not self.journald:
            return text
        priority = self.PRIORITIES.get(record.levelno, 6)
        return "\n".join(f"<{priority}>{line}" for line in text.splitlines())


def configure_logging() -> None:
    # Before the app: the MCP SDK sets up logging when its server is built, with bare
    # messages, but only if nothing has yet. systemd sets JOURNAL_STREAM for the journal.
    handler = logging.StreamHandler()
    handler.setFormatter(LevelFormatter(journald="JOURNAL_STREAM" in os.environ))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def main() -> ASGIApp:
    """uvicorn factory: `uvicorn mcp_gateway.server:main --factory`."""
    configure_logging()
    try:
        return create_app(Settings.from_env())
    except StartupError as error:
        # Bad settings or state, or another process on the state folder: the message is enough.
        log.error("%s", error)
    except Exception:
        log.exception("mcp-gateway could not start")
    # With --workers, uvicorn stops on this exit code instead of starting the worker again and again.
    sys.exit(STARTUP_FAILURE)
