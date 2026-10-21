"""MCP server exposed to claude.ai as a custom connector.

Layout: tools.py (the MCP tools) → bridge.py (agent-bridge client); replies.py turns
agent-bridge's JSON into short speakable text, auth.py checks the connector's token.
"""

from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp

from mcp_gateway.auth import BearerAuth
from mcp_gateway.bridge import AgentBridge
from mcp_gateway.config import Settings
from mcp_gateway.tools import build_server


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
