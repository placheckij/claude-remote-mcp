"""`agent-bridge upgrade`: upgrades the installed package to the latest release, then runs the
new version's setup, which restarts the service with it. A uv tool install from a checkout, a
wheel or git is moved to the published package; what it came from is left alone.

On Windows a running program's files can't be replaced, so the installed agent-bridge can't
upgrade itself. It runs from uvx's copy instead, which stops the scheduled task first:

    uvx --from claude-remote-mcp-agent-bridge@latest agent-bridge upgrade

@latest matters: without it uvx runs the installed tool's own environment.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agent_bridge.host import BRIDGE_VERSION
from agent_bridge.install.common import Refused

__all__ = ["PACKAGE", "UVX", "Install", "System", "find_install", "parse_receipt"]

PACKAGE = "claude-remote-mcp-agent-bridge"
UVX = f"uvx --from {PACKAGE}@latest agent-bridge upgrade"
# Where a uv tool requirement comes from when not from the package index, as uv-receipt.toml has it.
SOURCES = ("editable", "path", "directory", "git", "url")


@dataclass(frozen=True)
class Install:
    """How agent-bridge is installed: "uv" (uv tool, from the package index), "source" (uv tool,
    from a checkout, a wheel, git or a URL) or "Homebrew"."""

    kind: str
    # The installed agent-bridge, which runs setup.
    command: str
    # What upgrades it, or moves it to the published package.
    upgrade: tuple[str, ...]
    # uv's tool environment and the programs uv put on the PATH: Windows locks them while they run.
    environment: Path | None = None
    programs: tuple[str, ...] = ()
    # Where a "source" install came from, and what installs it from there again ("" when that
    # can't be told).
    source: str = ""
    back: str = ""


class System(Protocol):
    """What upgrade asks of the host; tests replace it."""

    platform: str
    # sys.prefix: the environment this agent-bridge runs from.
    prefix: Path

    # Runs from an environment in uv's cache, as uvx's.
    def in_uv_cache(self) -> bool: ...
    def uv(self) -> str | None: ...
    # `uv tool dir`; None when uv can't say.
    def tool_dir(self) -> Path | None: ...
    # How agent-bridge is installed on macOS: "Homebrew", "uv" or "pipx", any of them or none.
    def mac_installs(self) -> list[str]: ...
    def homebrew_command(self) -> str | None: ...
    # What `command --version` says, None when it doesn't run.
    def version(self, command: str) -> str | None: ...
    # Runs a command with its output shown; its exit code.
    def run(self, *argv: str) -> int: ...
    # Runs the installed agent-bridge as the service would, its output and input passed through.
    def run_installed(self, command: str, *args: str) -> int: ...
    # Windows: stops the scheduled task and makes sure nothing holds the install's files.
    def stop_bridge(self, install: Install) -> None: ...


def parse_receipt(text: str, environment: Path, uv: str) -> Install:
    """The install uv's receipt (uv-receipt.toml in the tool's environment) describes."""
    try:
        tool = tomllib.loads(text).get("tool", {})
    except tomllib.TOMLDecodeError as error:
        raise Refused(f"can't read {environment / 'uv-receipt.toml'} ({error})") from None
    requirement = next((item for item in tool.get("requirements", []) if item.get("name") == PACKAGE), None)
    programs = {item.get("name"): item.get("install-path", "") for item in tool.get("entrypoints", [])}
    command = programs.get("agent-bridge")
    if requirement is None or not command:
        raise Refused(f"{environment / 'uv-receipt.toml'} doesn't list agent-bridge: reinstall it "
                      f"(uv tool install --reinstall {PACKAGE}), then run agent-bridge setup")
    shown = tuple(path for path in programs.values() if path)
    for key in SOURCES:
        if key in requirement:
            source = str(requirement[key])
            back = {"editable": f"uv tool install --reinstall --editable {_quoted(source)}",
                    "path": f"uv tool install --reinstall {_quoted(source)}",
                    "directory": f"uv tool install --reinstall {_quoted(source)}"}.get(key, "")
            return Install("source", command, (uv, "tool", "install", "--reinstall", PACKAGE), environment, shown,
                           source, back)
    return Install("uv", command, (uv, "tool", "upgrade", PACKAGE), environment, shown)


def find_install(system: System) -> Install:
    """The one install of agent-bridge on this host, or why upgrade can't upgrade it."""
    if system.platform == "darwin":
        installs = system.mac_installs()
        if len(installs) > 1:
            raise Refused(f"agent-bridge is installed with both {' and '.join(installs)}; keep one (agent-bridge setup "
                          "says how), then run the upgrade again")
        if installs == ["pipx"]:
            raise Refused(f"agent-bridge is installed with pipx: upgrade it with pipx upgrade {PACKAGE}, then run "
                          "agent-bridge setup")
        if installs == ["Homebrew"] and (command := system.homebrew_command()):
            # <prefix>/opt/agent-bridge/bin/agent-bridge, and brew is <prefix>/bin/brew.
            brew = str(Path(command).parents[3] / "bin" / "brew")
            return Install("Homebrew", command, (brew, "upgrade", "agent-bridge"))
    uv = system.uv()
    if uv is None:
        raise Refused("can't find uv, which agent-bridge is installed with: put it on the PATH, then run the upgrade "
                      "again")
    tools = system.tool_dir()
    receipt = tools / PACKAGE / "uv-receipt.toml" if tools else None
    if receipt is None or not receipt.is_file():
        raise Refused(f"agent-bridge isn't installed with uv tool here: install it (uv tool install {PACKAGE}), "
                      "then run agent-bridge setup")
    try:
        text = receipt.read_text(encoding="utf-8")
    except OSError as error:
        raise Refused(f"can't read {receipt} ({error.strerror})") from None
    return parse_receipt(text, receipt.parent, uv)


def _quoted(text: str) -> str:
    return f'"{text}"' if " " in text else text
