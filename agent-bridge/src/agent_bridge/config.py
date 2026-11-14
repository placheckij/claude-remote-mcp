"""Settings read from AGENT_BRIDGE_* environment variables, or the env file (state.env_file;
the environment wins)."""

import ipaddress
import logging
import os
import re
import shlex
from pathlib import Path
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from agent_bridge.state import env_file
from agent_bridge.trust import is_default_config

log = logging.getLogger(__name__)

# Read before the env file, which wins, until the platform setups write the env file.
LEGACY_ENV_FILE = ".env"
# What the service needs on top of what has a default; the env file always sets them.
SERVICE_SETTINGS = ("host", "name", "herdr_bin")

ACCOUNT_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# The host part of the gateway's agent ids ("mac/w1:p1"), as the gateway checks it.
NAME_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,31}")
# The characters a bearer token may have (RFC 6750); they also keep an env file plain.
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/-]+=*")


def check_name_format(value: str) -> str:
    if not NAME_PATTERN.fullmatch(value):
        raise ValueError(
            "must be a lowercase letter, then up to 31 lowercase letters, digits or -, like mac or linux-box"
        )
    return value


def check_token_format(value: str) -> str:
    if len(value) < 32:
        raise ValueError("must be at least 32 characters")
    if not TOKEN_PATTERN.fullmatch(value):
        raise ValueError("may only have letters, digits, . _ ~ + / - and trailing =")
    return value


def check_host(value: str) -> str:
    # The gateway is given http://HOST:PORT and calls only IP addresses.
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ValueError("must be this host's IP address on the LAN, like 192.168.1.20") from None
    if address.is_unspecified:
        raise ValueError("must be this host's LAN address, not one that listens on every interface")
    return value


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

    token: Annotated[str, AfterValidator(check_token_format)]
    # os.pathsep-separated in the environment: ":", or ";" on Windows.
    allowed_roots: Annotated[tuple[Path, ...], NoDecode]
    # The LAN address to listen on, which the gateway is given too.
    host: Annotated[str, AfterValidator(check_host)] | None = None
    port: int = Field(default=8765, ge=1, le=65535)
    # This host's name in the gateway's agent ids, e.g. "mac".
    name: Annotated[str, AfterValidator(check_name_format)] | None = None
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
        path = env_file()
        files: tuple[Path, ...] = (path,)
        if _has_settings(Path(LEGACY_ENV_FILE)):
            log.warning(
                "Reading settings from %s in the working folder too; move them to %s, which wins. "
                "The next release reads only that.",
                Path(LEGACY_ENV_FILE).absolute(), path,
            )
            files = (Path(LEGACY_ENV_FILE), path)
        try:
            settings = cls(_env_file=files)
        except ValidationError as error:
            raise RuntimeError(describe(error, cls.model_config["env_prefix"], path)) from None
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

    def missing_for_service(self) -> list[str]:
        """The environment variables the service needs that weren't set."""
        prefix = self.model_config["env_prefix"]
        return [f"{prefix}{name.upper()}" for name in SERVICE_SETTINGS if name not in self.model_fields_set]

    @property
    def url(self) -> str | None:
        """The address the gateway reaches this bridge at."""
        if self.host is None:
            return None
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

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


def _has_settings(path: Path) -> bool:
    """Whether a file has AGENT_BRIDGE_* lines; a checkout's .env may be mcp-gateway's."""
    try:
        # As dotenv reads them: `export ` in front, and names in any case.
        return any(
            line.strip().removeprefix("export ").lstrip().upper().startswith("AGENT_BRIDGE_")
            for line in path.read_text().splitlines()
        )
    except (OSError, UnicodeDecodeError):
        return False


def narrow_folder(folder: Path) -> Path:
    """`folder` resolved, if it is absolute and narrower than the home folder.

    "/" or the home folder (or anything above it) as a root would let agents start, or be
    trusted, anywhere. A relative path would resolve against the service's working folder,
    which is "/" under launchd.
    """
    if not folder.expanduser().is_absolute():
        raise ValueError(f"{folder} must be an absolute path")
    # resolve() leaves \\?\C:\Users\me and \\localhost\C$\Users\me as they are, so the home
    # check below wouldn't see them as home.
    if os.name == "nt" and str(folder).startswith("\\\\"):
        raise ValueError(f"{folder} must be on a drive, like C:\\projects, not a \\\\ path")
    resolved = folder.expanduser().resolve()
    home = Path.home().resolve()
    if home.is_relative_to(resolved):
        raise ValueError(
            f"{resolved} is too broad: use a projects folder, not / or the home folder itself"
        )
    return resolved


def describe(error: ValidationError, prefix: str, path: Path | None = None) -> str:
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
    source = f" (env file: {path})" if path else ""
    return f"Invalid settings{source}:\n" + "\n".join(f"  {line}" for line in lines)
