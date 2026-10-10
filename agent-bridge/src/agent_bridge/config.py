"""Settings read from AGENT_BRIDGE_* environment variables."""

import os
import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    token: str
    allowed_roots: tuple[Path, ...]
    # A separate Claude account (CLAUDE_CONFIG_DIR=$HOME/.claude-agents claude).
    claude_config_dir: Path = Path.home() / ".claude-agents"
    claude_args: tuple[str, ...] = ("--permission-mode", "auto")
    herdr_bin: str = "herdr"
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
        return cls(
            token=token,
            allowed_roots=roots,
            claude_config_dir=Path(
                os.environ.get("AGENT_BRIDGE_CLAUDE_CONFIG_DIR", defaults.claude_config_dir)
            ).expanduser(),
            claude_args=tuple(shlex.split(os.environ["AGENT_BRIDGE_CLAUDE_ARGS"]))
            if "AGENT_BRIDGE_CLAUDE_ARGS" in os.environ
            else defaults.claude_args,
            herdr_bin=os.environ.get("AGENT_BRIDGE_HERDR_BIN", defaults.herdr_bin),
            max_agents=int(os.environ.get("AGENT_BRIDGE_MAX_AGENTS", defaults.max_agents)),
        )

    def claude_env(self) -> dict[str, str]:
        return {"CLAUDE_CONFIG_DIR": str(self.claude_config_dir)}

    def is_allowed(self, path: Path) -> bool:
        resolved = path.expanduser().resolve()
        return any(resolved.is_relative_to(root) for root in self.allowed_roots)
