"""Settings read from MCP_GATEWAY_* environment variables, or a `.env` file in the working
directory (the environment wins)."""

from pathlib import Path

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = ".env"


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
    bridge_url: str
    bridge_token: str
    # Host header the tunnel forwards, e.g. mcp.example.com; checked against DNS rebinding.
    public_host: str
    # Relative project paths given to start_agent resolve against this folder on the
    # agent host, e.g. /home/me/projects.
    projects_root: str

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            return cls(_env_file=ENV_FILE)
        except ValidationError as error:
            raise RuntimeError(describe(error, cls.model_config["env_prefix"])) from None


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
