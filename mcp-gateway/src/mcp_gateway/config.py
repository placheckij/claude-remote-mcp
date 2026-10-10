"""Settings read from MCP_GATEWAY_* environment variables."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Sent by claude.ai as `Authorization: Bearer <token>` (connector request header).
    token: str
    bridge_url: str
    bridge_token: str
    # Host header the tunnel forwards, e.g. mcp.example.com; checked against DNS rebinding.
    public_host: str
    # Relative project paths given to start_agent resolve against this (on the agent host).
    projects_root: str = "/Users/me/projects"

    @classmethod
    def from_env(cls) -> "Settings":
        def required(name: str) -> str:
            value = os.environ.get(name, "")
            if not value:
                raise RuntimeError(f"{name} must be set")
            return value

        token = required("MCP_GATEWAY_TOKEN")
        if len(token) < 32:
            raise RuntimeError("MCP_GATEWAY_TOKEN must be at least 32 characters")
        return cls(
            token=token,
            bridge_url=required("MCP_GATEWAY_BRIDGE_URL"),
            bridge_token=required("MCP_GATEWAY_BRIDGE_TOKEN"),
            public_host=required("MCP_GATEWAY_PUBLIC_HOST"),
            projects_root=os.environ.get("MCP_GATEWAY_PROJECTS_ROOT", cls.projects_root),
        )
