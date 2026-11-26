"""`agent-bridge upgrade`: upgrades the installed package to the latest release, then runs the
new version's setup, which restarts the service with it. A uv tool install from a checkout, a
wheel or git is moved to the published package; what it came from is left alone.

On Windows a running program's files can't be replaced, so the installed agent-bridge can't
upgrade itself. It runs from uvx's copy instead, which stops the scheduled task first:

    uvx --from claude-remote-mcp-agent-bridge@latest agent-bridge upgrade

@latest matters: without it uvx runs the installed tool's own environment.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agent_bridge.host import BRIDGE_VERSION
from agent_bridge.install.common import PREFIX, Refused
from agent_bridge.install.flow import FAILED, OK

__all__ = ["PACKAGE", "UVX", "Install", "RealSystem", "System", "find_install", "parse_receipt", "run", "system"]

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
    # Windows: an earlier upgrade or setup stopped and left the task disabled (its marker is there).
    def left_stopped(self) -> bool: ...


def parse_receipt(text: str, environment: Path, uv: str) -> Install:
    """The install uv's receipt (uv-receipt.toml in the tool's environment) describes."""
    try:
        tool = tomllib.loads(text).get("tool", {})
    except tomllib.TOMLDecodeError as error:
        raise Refused(f"can't read {environment / 'uv-receipt.toml'} ({error})") from None
    # Items that aren't tables (requirements = ["x"]) are not agent-bridge's.
    requirement = next((item for item in tool.get("requirements", [])
                        if isinstance(item, dict) and item.get("name") == PACKAGE), None)
    programs = {item.get("name"): item.get("install-path", "") for item in tool.get("entrypoints", [])
                if isinstance(item, dict)}
    command = programs.get("agent-bridge")
    if requirement is None or not command:
        raise Refused(f"{environment / 'uv-receipt.toml'} doesn't list agent-bridge: reinstall it "
                      f"(uv tool install --reinstall {PACKAGE}, on Windows with agent-bridge stopped first), "
                      "then run agent-bridge setup")
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


def run(system: System) -> int:
    try:
        return _upgrade(system)
    except Refused as error:
        print(f"error: {error}", file=sys.stderr)
        return FAILED


def _upgrade(system: System) -> int:
    if system.platform not in ("win32", "darwin") and not system.platform.startswith("linux"):
        raise Refused("agent-bridge upgrade is for macOS, Linux and Windows")
    install = find_install(system)
    windows = system.platform == "win32"
    if windows and install.environment is not None and _same(system.prefix, install.environment):
        raise Refused("Windows can't replace the files of a running program, so this agent-bridge can't upgrade "
                      f"itself. Run the upgrade from uvx's copy, outside the install:\n  {UVX}")
    before = system.version(install.command)
    # uvx@latest runs the latest release: nothing to do, and nothing to stop. (That assumes the
    # documented @latest command: with `uvx --from ...@<version>` it would compare against that version.)
    # Not when an upgrade that stopped it was cut short before its setup: this one brings it back.
    if (system.in_uv_cache() and install.kind == "uv" and before == BRIDGE_VERSION
            and not (windows and system.left_stopped())):
        print(f"agent-bridge is up to date ({before}).")
        return OK
    if install.kind == "source":
        print(f"agent-bridge {before or '(version unknown)'} is installed from {install.source}; replacing it with "
              "the published package. What it was installed from is left as it is.")
    if not windows:
        return _replace(system, install, windows, before)
    print("agent-bridge stops until setup starts it again. Agents keep running in herdr, but the gateway can't "
          "reach them meanwhile, also an agent that runs this upgrade.")
    stopped = False
    try:
        system.stop_bridge(install)
        stopped = True
        return _replace(system, install, windows, before)
    except (Refused, KeyboardInterrupt) as error:
        # Ctrl+C while stopping or during a slow uv, or a setup that can't run: stopped, and nothing else
        # says how to start it. A refused stop says itself what it left.
        if stopped or isinstance(error, KeyboardInterrupt):
            _how_to_start(install, gone=False)
        raise


def _replace(system: System, install: Install, windows: bool, before: str | None) -> int:
    """Upgrades the package, then runs the new setup."""
    upgraded = system.run(*install.upgrade) == 0
    if not upgraded and install.kind == "uv":
        # The plain package: a pin or other options in the receipt (--with, --python, an index) aren't
        # carried over.
        print("warning: uv tool upgrade failed; reinstalling the package instead", file=sys.stderr)
        upgraded = system.run(*install.upgrade[:2], "install", "--reinstall", PACKAGE) == 0
    after = system.version(install.command)
    if not upgraded:
        return _failed(system, install, windows, after)
    if after is None:
        # uv says it installed it: slow to start, say, rather than gone. Setup says if it is.
        print("warning: the new agent-bridge doesn't say its version; setting it up all the same", file=sys.stderr)
    elif install.kind != "source" and after == before:
        if system.in_uv_cache() and after != BRIDGE_VERSION:
            # Kept back, e.g. pinned (uv says so above): uvx@latest runs a newer one.
            print(f"agent-bridge stays at {after}; the latest is {BRIDGE_VERSION} (see above).")
        else:
            print(f"agent-bridge is up to date ({after}).")
        # On Windows it was stopped, and setup starts it again.
        if not windows:
            return OK
    else:
        print(f"agent-bridge {before or '(version unknown)'} -> {after}")
    if install.kind == "source" and before and after and _release(after) < _release(before):
        back = install.back or f"reinstall it from {install.source}"
        # Windows can't replace the files agent-bridge runs from, as here.
        stop = " with agent-bridge stopped first (the README's upgrade by hand)" if windows else ""
        print(f"warning: that is older than the {before} you had. To go back: {back}{stop}, then agent-bridge setup",
              file=sys.stderr)
    return system.run_installed(install.command, "setup")


def _failed(system: System, install: Install, windows: bool, after: str | None) -> int:
    print("error: the upgrade failed (see above).", file=sys.stderr)
    if after is None:
        _how_to_start(install, gone=True, stopped=windows)
        return FAILED
    if windows:
        # Stopped above: the version still there is set up again rather than left off.
        print(f"Setting up agent-bridge {after} again, the version still installed.")
        system.run_installed(install.command, "setup")
    return FAILED


def _how_to_start(install: Install, gone: bool, stopped: bool = True) -> None:
    """What to do when agent-bridge is gone or stopped, and the upgrade didn't start it again."""
    reinstall = "brew reinstall agent-bridge" if install.kind == "Homebrew" else f"uv tool install --reinstall {PACKAGE}"
    if gone:
        state = "agent-bridge is gone, and stays stopped" if stopped else "agent-bridge is gone"
        print(f"{state}. Install it again, then set it up:\n  {reinstall}\n  agent-bridge setup", file=sys.stderr)
    else:
        print("agent-bridge is stopped. Start it again with:\n  agent-bridge setup\nIf agent-bridge is gone, "
              f"install it first:\n  {reinstall}", file=sys.stderr)


def _same(one: Path, other: Path) -> bool:
    return os.path.normcase(os.path.realpath(one)) == os.path.normcase(os.path.realpath(other))


def _release(version: str) -> tuple[int, ...]:
    """A version's release numbers, e.g. (0, 4, 0) for 0.4.0.dev0."""
    match = re.match(r"\d+(?:\.\d+)*", version)
    return tuple(int(part) for part in match.group().split(".")) if match else ()


def _quoted(text: str) -> str:
    return f'"{text}"' if " " in text else text


class RealSystem:
    """What upgrade asks of the real host."""

    def __init__(self) -> None:
        self.platform = sys.platform
        self.prefix = Path(sys.prefix)

    def in_uv_cache(self) -> bool:
        # uvx's environments are <uv cache>/archive-v0/<id>.
        return self.prefix.parent.name.startswith("archive-v")

    def uv(self) -> str | None:
        # uv tells the programs it runs where it is.
        return os.environ.get("UV") or shutil.which("uv")

    def tool_dir(self) -> Path | None:
        uv = self.uv()
        if uv is None:
            return None
        try:
            result = subprocess.run([uv, "tool", "dir"], capture_output=True, text=True, timeout=60,
                                    stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return None
        found = result.stdout.strip()
        return Path(found) if result.returncode == 0 and found else None

    def mac_installs(self) -> list[str]:
        from agent_bridge.install import macos

        return macos.MacSystem().installs()

    def homebrew_command(self) -> str | None:
        from agent_bridge.install import macos

        return next((command for command in macos.MacSystem.homebrew_commands if os.path.exists(command)), None)

    def version(self, command: str) -> str | None:
        try:
            result = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=60,
                                    stdin=subprocess.DEVNULL, env=_environment())
        except (OSError, subprocess.TimeoutExpired):
            return None
        found = re.search(r"agent-bridge (\S+)", result.stdout or "")
        return found.group(1) if result.returncode == 0 and found else None

    def run(self, *argv: str) -> int:
        # What was printed so far goes first: piped, Python holds it while the child writes.
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            return subprocess.run(argv).returncode
        except OSError as error:
            print(f"error: can't run {argv[0]} ({error.strerror})", file=sys.stderr)
            return FAILED

    def run_installed(self, command: str, *args: str) -> int:
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            process = subprocess.Popen([command, *args], env=_environment(), cwd=self._cwd())
        except OSError as error:
            raise Refused(f"can't run {command} ({error.strerror})") from None
        while True:
            try:
                return process.wait()
            except KeyboardInterrupt:
                # Ctrl+C reaches setup too, which puts things back and says so itself; subprocess.run
                # would kill it a moment later, halfway through that.
                continue

    def _cwd(self) -> str:
        # Not the shared /tmp, where another user could plant the legacy .env agent-bridge still reads
        # from its working directory: / on macOS and Linux, as their setup runs it, and the per-user
        # temp folder on Windows.
        return tempfile.gettempdir() if self.platform == "win32" else "/"

    def stop_bridge(self, install: Install) -> None:
        from agent_bridge.install import windows

        windows.stop_for_upgrade(windows.system(), install.environment, install.programs)

    def left_stopped(self) -> bool:
        if self.platform != "win32":
            return False
        from agent_bridge.install import windows

        return windows.system().paths.disabled_marker.exists()


def system() -> RealSystem:
    return RealSystem()


def _environment() -> dict[str, str]:
    """This environment without AGENT_BRIDGE_*, which would win over the env file, as setup runs
    agent-bridge; in UTF-8 mode, as Windows' code page may not hold what it prints."""
    environment = {name: value for name, value in os.environ.items() if not name.upper().startswith(PREFIX)}
    environment["PYTHONUTF8"] = "1"
    return environment
