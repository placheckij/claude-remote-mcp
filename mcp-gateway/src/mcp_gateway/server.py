"""MCP server exposed to claude.ai as a custom connector.

Layout: tools.py (the MCP tools) → bridge.py (agent-bridge client); replies.py turns
agent-bridge's JSON into short speakable text. oauth.py signs the connector in and keeps
its tokens; auth.py checks them in front of the MCP endpoint.
"""

import logging

from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcp_gateway.auth import BearerAuth
from mcp_gateway.bridge import AgentBridge
from mcp_gateway.config import Settings
from mcp_gateway.oauth import GatewayOAuth
from mcp_gateway.tools import build_server

log = logging.getLogger(__name__)

# claude.ai calls the connector from Anthropic's servers and may send no Origin at all
# (accepted); Anthropic also serves Claude from claude.com.
ALLOWED_ORIGINS = ("https://claude.ai", "https://claude.com")


def create_app(
    settings: Settings, bridge: AgentBridge | None = None, oauth: GatewayOAuth | None = None
) -> ASGIApp:
    bridge = bridge or AgentBridge.connect(settings.bridge_url, settings.bridge_token)
    oauth = oauth or GatewayOAuth(settings)
    mcp = build_server(settings, bridge, oauth)
    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[settings.public_host, "127.0.0.1:*", "localhost:*"],
            allowed_origins=list(ALLOWED_ORIGINS),
        ),
    )
    return BearerAuth(StatelessEdges(app, bridge), oauth.load_access_token, oauth.resource_metadata_url)


class StatelessEdges:
    """The parts of the transport the SDK leaves to the server, behind the token check:

    - a refused Origin is logged as an error: the request had a valid token, so it most
      likely came from the Claude app, and every call will fail the same way;
    - GET /mcp answers 405, as the spec allows: stateless mode never sends anything on a GET
      stream, which would only hold a task open until the client hangs up;
    - on shutdown the agent-bridge client is closed.
    """

    def __init__(self, app: ASGIApp, bridge: AgentBridge):
        self.app = app
        self.bridge = bridge

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, self._closing_bridge_on_shutdown(send))
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
        await self.app(scope, receive, send)

    def _closing_bridge_on_shutdown(self, send: Send) -> Send:
        async def wrapped(message: Message) -> None:
            if message["type"] == "lifespan.shutdown.complete":
                await self.bridge.aclose()
            await send(message)

        return wrapped


def main() -> ASGIApp:
    """uvicorn factory: `uvicorn mcp_gateway.server:main --factory`."""
    return create_app(Settings.from_env())
