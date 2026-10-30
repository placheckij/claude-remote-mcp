"""Settings read from AGENT_BRIDGE_* environment variables, or a `.env` file in the working
directory (the environment wins)."""

import os
import re
import shlex
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from agent_bridge.trust import is_default_config

ENV_FILE = ".env"

ACCOUNT_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class Account(BaseModel):
    """A Claude login agents can be started with, e.g. a work and a personal one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Passed to agents as CLAUDE_CONFIG_DIR; None leaves Claude Code on its default config.
    config_dir: Path | None = None
    # Agents started in one of these folders, or below, get this account unless the
    # request names another.
    folders: tuple[Path, ...] = ()

    @field_validator("config_dir")
    @classmethod
    def _absolute_config_dir(cls, path: Path | None) -> Path | None:
        if path and not path.expanduser().is_absolute():
            raise ValueError(f"{path} must be an absolute path")
        return path.expanduser() if path else None

    @field_validator("folders")
    @classmethod
    def _narrow_folders(cls, folders: tuple[Path, ...]) -> tuple[Path, ...]:
        return tuple(narrow_folder(folder) for folder in folders)


class Settings(BaseSettings):
    # No env_file here: only from_env() reads `.env`, so constructing Settings directly
    # (as tests do) never picks up a stray file.
    model_config = SettingsConfigDict(env_prefix="AGENT_BRIDGE_", frozen=True, extra="ignore")

    token: str = Field(min_length=32)
    # `:`-separated (os.pathsep) in the environment.
    allowed_roots: Annotated[tuple[Path, ...], NoDecode]
    # Passed to agents as CLAUDE_CONFIG_DIR; None leaves Claude Code on its default config.
    # Agents that no account below applies to use it.
    claude_config_dir: Path | None = None
    # Named Claude logins, as JSON in the environment, e.g.
    # {"work": {"config_dir": null, "folders": ["/Users/me/projects/work"]}}.
    accounts: dict[str, Account] = {}
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
        if settings.claude_config_dir and is_default_config(settings.claude_config_dir):
            raise RuntimeError(
                f"AGENT_BRIDGE_CLAUDE_CONFIG_DIR is the default Claude config "
                f"({settings.claude_config_dir}); set it to a separate folder, e.g. ~/.claude-agents"
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
        return tuple(narrow_folder(root) for root in roots)

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

    @field_validator("trust_root")
    @classmethod
    def _narrow_trust_root(cls, path: Path | None) -> Path | None:
        if path:
            narrow_folder(path)
        return path

    @field_validator("accounts")
    @classmethod
    def _check_accounts(cls, accounts: dict[str, Account]) -> dict[str, Account]:
        for name in accounts:
            if not ACCOUNT_NAME.match(name):
                raise ValueError(
                    f"account name {name!r} must be lower-case letters, digits, - or _, "
                    "starting with a letter, at most 32 long"
                )
        # A folder in two accounts would leave the choice to dictionary order.
        owners: dict[Path, str] = {}
        for name, account in accounts.items():
            for folder in account.folders:
                if folder in owners:
                    raise ValueError(f"{folder} is in both the {owners[folder]} and {name} accounts")
                owners[folder] = name
        return accounts

    def account_for(self, folder: Path) -> str | None:
        """The account whose folders hold `folder` most closely, if any."""
        matches = [
            (len(root.parts), name)
            for name, account in self.accounts.items()
            for root in account.folders
            if folder.is_relative_to(root)
        ]
        return max(matches)[1] if matches else None

    def is_allowed(self, path: Path) -> bool:
        resolved = path.expanduser().resolve()
        return any(resolved.is_relative_to(root) for root in self.allowed_roots)


def claude_env(config_dir: Path | None) -> dict[str, str]:
    return {} if config_dir is None else {"CLAUDE_CONFIG_DIR": str(config_dir)}



def narrow_folder(folder: Path) -> Path:
    """`folder` resolved, if it is absolute and narrower than the home folder.

    "/" or the home folder (or anything above it) as a root would let agents start, or be
    trusted, anywhere. A relative path would resolve against the service's working folder,
    which is "/" under launchd.
    """
    if not folder.expanduser().is_absolute():
        raise ValueError(f"{folder} must be an absolute path")
    resolved = folder.expanduser().resolve()
    home = Path.home().resolve()
    if home.is_relative_to(resolved):
        raise ValueError(
            f"{resolved} is too broad: use a projects folder, not / or the home folder itself"
        )
    return resolved


def describe(error: ValidationError, prefix: str) -> str:
    """One line per problem, naming the environment variable rather than the field."""
    lines = []
    for problem in error.errors():
        message = "must be set" if problem["type"] == "missing" else problem["msg"]
        message = message.removeprefix("Value error, ")
        if problem["loc"]:
            variable, *inside = (str(part) for part in problem["loc"])
            # Inside AGENT_BRIDGE_ACCOUNTS' JSON, e.g. "work.folders.0".
            where = f" ({'.'.join(inside)})" if inside else ""
            message = f"{prefix}{variable.upper()}{where}: {message}"
        lines.append(message)
    return "Invalid settings:\n" + "\n".join(f"  {line}" for line in lines)
