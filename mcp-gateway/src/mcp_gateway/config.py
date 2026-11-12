"""Settings read from MCP_GATEWAY_* environment variables, or a `.env` file in the working
directory (the environment wins)."""

from pathlib import Path

import httpx
from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = ".env"


class StartupError(RuntimeError):
    """A setting or the state the gateway can't start with; the message says what to fix."""


class Settings(BaseSettings):
    # No env_file here: only from_env() reads `.env`, so constructing Settings directly
    # (as tests do) never picks up a stray file. An empty variable counts as unset.
    model_config = SettingsConfigDict(
        env_prefix="MCP_GATEWAY_", frozen=True, extra="ignore", env_ignore_empty=True
    )

    # Typed on the OAuth sign-in page when the connector is added in claude.ai.
    oauth_passphrase: str = Field(min_length=16)
    # Where the OAuth clients and tokens are kept, so a restart doesn't sign the connector out.
    state_dir: Path
    # The static token from before OAuth, sent by claude.ai as `Authorization: Bearer <token>`
    # (connector request header); still accepted while set, for moving connectors over.
    token: str | None = Field(default=None, min_length=32)
    # The one agent-bridge from before the registry (bridges.json): imported as a host at
    # the first start with no hosts, then no longer read.
    bridge_url: str | None = None
    bridge_token: str | None = None
    # Host header the tunnel forwards, e.g. mcp.example.com; checked against DNS rebinding.
    public_host: str
    # Only for an agent-bridge from before /info, which can't look up project names: names
    # given to start_agent resolve against this folder on the agent host, e.g.
    # /home/me/projects. Without it such a host takes absolute paths only.
    projects_root: str | None = None

    @field_validator("bridge_url")
    @classmethod
    def _bridge_url_parses(cls, value: str | None) -> str | None:
        # Checked here, so a typo stops startup with this line rather than at the import.
        try:
            url = httpx.URL(value) if value is not None else None
        except httpx.InvalidURL:
            url = None
        if value is not None and (url is None or url.scheme not in ("http", "https") or not url.host):
            raise ValueError("must be the agent-bridge's address, like http://192.168.1.20:8765")
        return value

    @field_validator("bridge_token")
    @classmethod
    def _bridge_token_fits_a_header(cls, value: str | None) -> str | None:
        if value is not None and not (value.isascii() and value.isprintable()):
            raise ValueError("must be printable ASCII, as it goes in an HTTP header")
        return value

    @model_validator(mode="after")
    def _bridge_url_and_token_together(self) -> "Settings":
        if (self.bridge_url is None) != (self.bridge_token is None):
            raise ValueError("MCP_GATEWAY_BRIDGE_URL and MCP_GATEWAY_BRIDGE_TOKEN go together: set both or neither")
        return self

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            return cls(_env_file=ENV_FILE)
        except ValidationError as error:
            raise StartupError(describe(error, cls.model_config["env_prefix"])) from None


def describe(error: ValidationError, prefix: str) -> str:
    """One line per problem, naming the environment variable rather than the field."""
    lines = []
    for problem in error.errors():
        message = "must be set" if problem["type"] == "missing" else problem["msg"]
        message = message.removeprefix("Value error, ")
        if problem["loc"]:
            message = f"{prefix}{str(problem['loc'][0]).upper()}: {message}"
        lines.append(message)
    return "Invalid settings:\n" + "\n".join(f"  {line}" for line in lines)
