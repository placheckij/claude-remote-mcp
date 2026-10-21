"""Settings read from AGENT_BRIDGE_* environment variables, or a `.env` file in the working
directory (the environment wins)."""

import os
import shlex
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ENV_FILE = ".env"


class Settings(BaseSettings):
    # No env_file here: only from_env() reads `.env`, so constructing Settings directly
    # (as tests do) never picks up a stray file.
    model_config = SettingsConfigDict(env_prefix="AGENT_BRIDGE_", frozen=True, extra="ignore")

    token: str = Field(min_length=32)
    # `:`-separated (os.pathsep) in the environment.
    allowed_roots: Annotated[tuple[Path, ...], NoDecode]
    # Passed to agents as CLAUDE_CONFIG_DIR; None leaves Claude Code on its default config.
    claude_config_dir: Path | None = None
    # Shell-quoted in the environment; set it empty to pass no extra arguments.
    claude_args: Annotated[tuple[str, ...], NoDecode] = ("--permission-mode", "auto")
    herdr_bin: str = "herdr"
    # Agents started under this folder get it marked trusted in the Claude config; None
    # turns that off. Never applied to the default config (see trust.py).
    trust_root: Path | None = None
    max_output_chars: int = Field(default=8000, ge=1)
    # Counts every herdr agent, including ones started by hand.
    max_agents: int = Field(default=10, ge=1)
    prompt_timeout_ms: int = Field(default=10 * 60 * 1000, ge=1000)

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            settings = cls(_env_file=ENV_FILE)
        except ValidationError as error:
            raise RuntimeError(describe(error, cls.model_config["env_prefix"])) from None
        # Checked here rather than in a model validator so tests can still build this
        # combination and show trust.py refusing the default config on its own.
        if settings.trust_root and not settings.claude_config_dir:
            raise RuntimeError(
                "AGENT_BRIDGE_TRUST_ROOT needs AGENT_BRIDGE_CLAUDE_CONFIG_DIR: folders are "
                "only ever trusted in a separate Claude config, never the default one"
            )
        return settings

    @field_validator("allowed_roots", mode="before")
    @classmethod
    def _split_roots(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = [root for root in value.split(os.pathsep) if root]
        return value

    @field_validator("allowed_roots")
    @classmethod
    def _resolve_roots(cls, roots: tuple[Path, ...]) -> tuple[Path, ...]:
        if not roots:
            raise ValueError("must list at least one directory")
        return tuple(root.expanduser().resolve() for root in roots)

    @field_validator("claude_args", mode="before")
    @classmethod
    def _split_args(cls, value: Any) -> Any:
        return shlex.split(value) if isinstance(value, str) else value

    @field_validator("claude_config_dir", "trust_root", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: Any) -> Any:
        return None if value == "" else value

    @field_validator("claude_config_dir", "trust_root")
    @classmethod
    def _expand(cls, path: Path | None) -> Path | None:
        return path.expanduser() if path else None

    def claude_env(self) -> dict[str, str]:
        if self.claude_config_dir is None:
            return {}
        return {"CLAUDE_CONFIG_DIR": str(self.claude_config_dir)}

    def is_allowed(self, path: Path) -> bool:
        resolved = path.expanduser().resolve()
        return any(resolved.is_relative_to(root) for root in self.allowed_roots)


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
