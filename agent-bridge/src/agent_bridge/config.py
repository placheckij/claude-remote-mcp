"""Settings read from AGENT_BRIDGE_* environment variables."""

import os
import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    token: str
    allowed_roots: tuple[Path, ...]
    # Passed to agents as CLAUDE_CONFIG_DIR; None leaves Claude Code on its default config.
    claude_config_dir: Path | None = None
    claude_args: tuple[str, ...] = ("--permission-mode", "auto")
    herdr_bin: str = "herdr"
    # Agents started under this folder get it marked trusted in the Claude config; None
    # turns that off. Never applied to the default config (see trust.py).
    trust_root: Path | None = None
    max_output_chars: int = 8000
    # Counts every herdr agent, including ones started by hand.
    max_agents: int = 10
    prompt_timeout_ms: int = 10 * 60 * 1000

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("AGENT_BRIDGE_TOKEN", "")
        if len(token) < 32:
            raise RuntimeError("AGENT_BRIDGE_TOKEN must be set to a secret of at least 32 characters")
        roots = tuple(
            Path(root).expanduser().resolve()
            for root in os.environ.get("AGENT_BRIDGE_ALLOWED_ROOTS", "").split(os.pathsep)
            if root
        )
        if not roots:
            raise RuntimeError("AGENT_BRIDGE_ALLOWED_ROOTS must list at least one directory")
        defaults = cls(token=token, allowed_roots=roots)
        config_dir = _optional_path("AGENT_BRIDGE_CLAUDE_CONFIG_DIR")
        trust_root = _optional_path("AGENT_BRIDGE_TRUST_ROOT")
        if trust_root and not config_dir:
            raise RuntimeError(
                "AGENT_BRIDGE_TRUST_ROOT needs AGENT_BRIDGE_CLAUDE_CONFIG_DIR: folders are "
                "only ever trusted in a separate Claude config, never the default one"
            )
        return cls(
            token=token,
            allowed_roots=roots,
            claude_config_dir=config_dir,
            claude_args=tuple(shlex.split(os.environ["AGENT_BRIDGE_CLAUDE_ARGS"]))
            if "AGENT_BRIDGE_CLAUDE_ARGS" in os.environ
            else defaults.claude_args,
            herdr_bin=os.environ.get("AGENT_BRIDGE_HERDR_BIN", defaults.herdr_bin),
            trust_root=trust_root,
            max_agents=int(os.environ.get("AGENT_BRIDGE_MAX_AGENTS", defaults.max_agents)),
        )

    def claude_env(self) -> dict[str, str]:
        if self.claude_config_dir is None:
            return {}
        return {"CLAUDE_CONFIG_DIR": str(self.claude_config_dir)}

    def is_allowed(self, path: Path) -> bool:
        resolved = path.expanduser().resolve()
        return any(resolved.is_relative_to(root) for root in self.allowed_roots)


def _optional_path(name: str) -> Path | None:
    value = os.environ.get(name, "")
    return Path(value).expanduser() if value else None
