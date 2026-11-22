"""`agent-bridge setup` and `uninstall` on macOS: agent-bridge as a launchd agent of the user
who runs it. It starts at login and restarts when it stops. Another launch agent starts herdr's
server at login unless one runs, so agents can start before anyone opens herdr (skipped with
--no-herdr-service). The rest of setup is flow's.
"""

import contextlib
import json
import os
import plistlib
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from agent_bridge.install import flow
from agent_bridge.install.common import PREFIX, Read, Refused, write_file
from agent_bridge.install.flow import Installed, Options, Plan, PosixSystem, stable_command
from agent_bridge.install.flow import output as _output

__all__ = ["HERDR_LABEL", "LABEL", "MacService", "MacSystem", "Options", "herdr_launch_agent", "launch_agent", "setup",
           "stable_command", "system", "uninstall"]

LABEL = "dev.claude-remote.agent-bridge"
# Starts herdr's server at login; never stops it.
HERDR_LABEL = "dev.claude-remote.agent-bridge-herdr"
# launchctl bootout returns before the old job is gone, so a bootstrap right after can fail.
BOOTSTRAPS = 10
# How long setup waits for the herdr launch agent to start herdr's server: 40 looks, 0.5 s apart.
HERDR_LOOKS = 40
# Each install's uninstall command.
UNINSTALLS = {
    "Homebrew": "brew uninstall agent-bridge",
    "uv": "uv tool uninstall claude-remote-mcp-agent-bridge",
    "pipx": "pipx uninstall claude-remote-mcp-agent-bridge",
}


class System(flow.System, Protocol):
    # launchd's domain for this user's agents, gui/<uid>.
    def domain(self) -> str: ...
    # Whether `launchctl disable` keeps the job from starting.
    def disabled(self) -> bool: ...
    def launchctl(self, *args: str) -> int: ...
    # How agent-bridge is installed: "Homebrew", "uv" or "pipx", any of them or none.
    def installs(self) -> list[str]: ...
    def herdr_running(self, herdr: str) -> bool: ...


def setup(options: Options, system: System, read: Read) -> int:
    # One install, so the launch agent can't run one while you upgrade the other.
    installs = system.installs()
    if len(installs) > 1:
        print(f"error: agent-bridge is installed with both {' and '.join(installs)}; keep one. Remove the other "
              f"with {', or '.join(UNINSTALLS[install] for install in installs)}, then run agent-bridge setup "
              "again: it keeps the settings, the token and the enrollment",
              file=sys.stderr)
        return flow.FAILED
    return flow.setup(options, system, MacService(system), read)


def uninstall(system: System) -> int:
    return flow.uninstall(system, MacService(system))


def launch_agent(command: str, log: Path) -> dict:
    """The launch agent. Its settings are in the env file `agent-bridge serve` reads, never here."""
    return {
        "Label": LABEL,
        "ProgramArguments": [command, "serve"],
        "RunAtLoad": True,
        # Retries until the LAN address exists, e.g. right after login or wake.
        "KeepAlive": True,
        "ThrottleInterval": 10,
        # Errors (stderr) and one line per request (stdout), in one file.
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }


def herdr_launch_agent(command: str, herdr: str, log: Path) -> dict:
    """The launch agent that starts herdr's server at login: `agent-bridge start-herdr` starts
    it in its own session unless one runs, waits until it answers, and exits. The server is in
    no process group of the job's, so neither the job's exit nor a bootout ends it."""
    return {
        "Label": HERDR_LABEL,
        "ProgramArguments": [command, "start-herdr", "--herdr-bin", herdr, "--log-file", str(log)],
        "RunAtLoad": True,
    }


@dataclass
class MacService:
    system: System
    machine: str = "Mac"
    runner: str = "launch agent"
    name_fallback: str = "mac"
    retries: str = "every 10 s"
    # The launch agent that starts herdr's server at login, and the herdr it starts.
    herdr_service: bool = True
    herdr: str = ""
    # Lines for the end: what setup couldn't do.
    notes: list[str] = field(default_factory=list)

    @property
    def support(self) -> Path:
        # Where `agent-bridge serve` reads its settings and state, as launchd starts it with
        # no environment.
        return self.system.home / "Library" / "Application Support" / "agent-bridge"

    @property
    def env_file(self) -> Path:
        return self.support / "env"

    @property
    def state_file(self) -> Path:
        return self.support / "state.json"

    @property
    def plist(self) -> Path:
        return self.system.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    @property
    def log(self) -> Path:
        return self.system.home / "Library" / "Logs" / "agent-bridge.log"

    @property
    def herdr_plist(self) -> Path:
        return self.system.home / "Library" / "LaunchAgents" / f"{HERDR_LABEL}.plist"

    @property
    def herdr_log(self) -> Path:
        return self.system.home / "Library" / "Logs" / "agent-bridge-herdr.log"

    @property
    def no_herdr_agent(self) -> Path:
        """There after setup --no-herdr-service, which the next setup keeps. Macs set up before
        the herdr launch agent existed have neither, and get it."""
        return self.support / "no-herdr-agent"

    def check(self, plan: Plan, options: Options) -> None:
        # Kept as the last setup left it unless passed: off only after --no-herdr-service.
        if options.herdr_service is not None:
            self.herdr_service = options.herdr_service
        else:
            self.herdr_service = not self.no_herdr_agent.exists()
        self.herdr = plan.values[f"{PREFIX}HERDR_BIN"]

    def prepare(self) -> None:
        pass

    def abandon(self) -> None:
        pass

    def legacy(self) -> Installed | None:
        """Versions before the env file had the settings in the plist."""
        try:
            with self.plist.open("rb") as file:
                plist = plistlib.load(file)
        except Exception:  # noqa: BLE001 - a plist that can't be read has no settings
            plist = {}
        environment = plist.get("EnvironmentVariables") if isinstance(plist, dict) else None
        if not isinstance(environment, dict) or not environment.get(f"{PREFIX}TOKEN"):
            return None
        values = {key: str(value) for key, value in environment.items()}
        arguments = plist.get("ProgramArguments", [])
        # It ran uvicorn with --host HOST --port PORT, and had the rest in its environment.
        if len(arguments) > 6:
            values[f"{PREFIX}HOST"], values[f"{PREFIX}PORT"] = str(arguments[4]), str(arguments[6])
        print(f"\n==> Moving the settings from {self.plist} to {self.env_file}")
        return Installed("plist", values)

    def install(self, plan: Plan) -> None:
        write_file(self.plist, plistlib.dumps(launch_agent(self.system.command(), self.log)), 0o644)
        # launchd opens the log but doesn't make its folder.
        try:
            self.log.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise Refused(f"can't make {self.log.parent} ({error.strerror})") from None
        # Here, not in start(): setup restarts agent-bridge, and herdr's server needs no restart.
        if self.herdr_service:
            self._herdr_agent()
        else:
            self._no_herdr_agent()

    def _herdr_agent(self) -> None:
        """Loads the launch agent that starts herdr's server at login. Loading it runs it once,
        which starts the server unless one runs; a running one is never restarted."""
        print(f"\n==> Starting {HERDR_LABEL}, which starts herdr's server at every login")
        running = self.system.herdr_running(self.herdr)
        write_file(self.herdr_plist, plistlib.dumps(herdr_launch_agent(self.system.command(), self.herdr,
                                                                       self.herdr_log)), 0o644)
        with contextlib.suppress(OSError):
            self.no_herdr_agent.unlink(missing_ok=True)
        service = f"{self.system.domain()}/{HERDR_LABEL}"
        # Ends only start-herdr, if it still runs: the server it started has its own session.
        self.system.launchctl("bootout", service)
        for attempt in range(BOOTSTRAPS):
            if self.system.launchctl("bootstrap", self.system.domain(), str(self.herdr_plist)) == 0:
                break
            if attempt == BOOTSTRAPS - 1:
                self._herdr_failed(f"launchctl bootstrap failed; see: launchctl print {service}")
                return
            self.system.sleep(1)
        if running:
            print("herdr's server already runs; it is left as it is.")
            return
        print("Starting herdr's server")
        for _ in range(HERDR_LOOKS):
            self.system.sleep(0.5)
            if self.system.herdr_running(self.herdr):
                print("ok")
                return
        self._herdr_failed(f"herdr's server didn't start; see {self.herdr_log}")

    def _herdr_failed(self, warning: str) -> None:
        print(f"warning: {warning}", file=sys.stderr)
        self.notes.append("herdr's server didn't start (see above), so agents can't start until you open herdr.")

    def _no_herdr_agent(self) -> None:
        """Removes the herdr launch agent, and remembers that, so the next setup doesn't add it
        back. herdr's server keeps running."""
        write_file(self.no_herdr_agent, b"setup --no-herdr-service\n", 0o600)
        if self.herdr_plist.exists():
            print(f"\n==> Removing {HERDR_LABEL} (herdr's server keeps running)")
            self._remove_herdr_agent()

    def _remove_herdr_agent(self) -> None:
        self.system.launchctl("bootout", f"{self.system.domain()}/{HERDR_LABEL}")
        for path in (self.herdr_plist, self.herdr_log, self.herdr_log.with_name(f"{self.herdr_log.name}.1")):
            flow.remove_with_temporary(path)

    def start(self) -> None:
        print(f"\n==> Starting {LABEL}")
        service = f"{self.system.domain()}/{LABEL}"
        self.system.launchctl("bootout", service)
        # Only when disabled: enabling records an override that launchd keeps after uninstall.
        if self.system.disabled():
            self.system.launchctl("enable", service)
        for attempt in range(BOOTSTRAPS):
            if self.system.launchctl("bootstrap", self.system.domain(), str(self.plist)) == 0:
                break
            if attempt == BOOTSTRAPS - 1:
                raise Refused(f"launchctl bootstrap failed; see: launchctl print {service}")
            self.system.sleep(1)

    def done(self, url: str) -> list[str]:
        return [f"Done. agent-bridge listens on {url} and starts at every login.",
                f"herdr's server starts at every login too ({HERDR_LABEL})." if self.herdr_service else
                "Open herdr after each login: its server must run for agents to start.",
                f"If a check failed: tail {self.log} (herdr failing means its server isn't running).",
                "If the macOS firewall asks, allow incoming connections for Python.",
                *self.notes]

    def remove(self) -> None:
        print(f"\n==> Removing {LABEL}")
        self.system.launchctl("bootout", f"{self.system.domain()}/{LABEL}")
        for path in (self.plist, self.log):
            flow.remove_with_temporary(path)
        if self.herdr_plist.exists():
            print(f"\n==> Removing {HERDR_LABEL}")
            self._remove_herdr_agent()
            print("herdr's server, if it runs, keeps running with its agents until you stop it (herdr server stop) "
                  "or log out.")
        flow.remove_with_temporary(self.no_herdr_agent)

    def folders(self) -> Iterable[Path]:
        return (self.support,)


class MacSystem(PosixSystem):
    herdr_places = ("/opt/homebrew/bin/herdr", "/usr/local/bin/herdr")
    # The formula's opt links, on Apple silicon and on Intel.
    homebrew_commands = ("/opt/homebrew/opt/agent-bridge/bin/agent-bridge",
                         "/usr/local/opt/agent-bridge/bin/agent-bridge")

    def installs(self) -> list[str]:
        found = ["Homebrew"] if any(os.path.exists(command) for command in self.homebrew_commands) else []
        # uv tool's link, or pipx's: pipx keeps its virtualenvs in a pipx folder.
        link = self.home / ".local" / "bin" / "agent-bridge"
        if os.path.exists(link):
            found.append("pipx" if "pipx" in Path(os.path.realpath(link)).parts else "uv")
        return found

    def domain(self) -> str:
        return f"gui/{os.getuid()}"

    def lan_address(self) -> str | None:
        # The address of the interface the default route goes through.
        interface = re.search(r"interface:\s*(\S+)", _output("route", "-n", "get", "default"))
        if interface is None:
            return None
        return _output("ipconfig", "getifaddr", interface.group(1)).strip() or None

    def has_address(self, address: str) -> bool:
        if address == "127.0.0.1":
            return True
        for line in _output("ifconfig").splitlines():
            words = line.split()
            if len(words) > 1 and words[0] in ("inet", "inet6") and (
                words[1] == address or words[1].startswith(f"{address}%")
            ):
                return True
        return False

    def _listener(self, port: int) -> str | None:
        pids = _output("lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t").split()
        return pids[0] if pids else None

    def port_holder(self, port: int) -> str | None:
        if pid := self._listener(port):
            command = _output("ps", "-o", "command=", "-p", pid).strip()
            return f"PID {pid}: {command}" if command else f"PID {pid}"
        # lsof sees only your own processes; netstat sees every listener, without its PID.
        for line in _output("netstat", "-anv", "-p", "tcp").splitlines():
            words = line.split()
            if len(words) > 5 and words[5] == "LISTEN" and words[3].endswith(f".{port}"):
                return "another user's program"
        return None

    def serves(self, port: int) -> bool:
        # The job launchd runs under the label (the version being replaced has the same one).
        # Not by command line: a bridge started by hand can run the same command, and keeps
        # the port.
        job = re.search(r"^\s*pid = (\d+)", _output("launchctl", "print", f"{self.domain()}/{LABEL}"), re.MULTILINE)
        return job is not None and self._listener(port) == job.group(1)

    def disabled(self) -> bool:
        # One line per override, `"<label>" => disabled` (`=> true` before macOS 11).
        listed = re.search(rf'^\s*"{re.escape(LABEL)}" => (\w+)', _output("launchctl", "print-disabled", self.domain()),
                           re.MULTILINE)
        return listed is not None and listed.group(1) in ("disabled", "true")

    def launchctl(self, *args: str) -> int:
        return subprocess.run(["launchctl", *args], capture_output=True).returncode

    def herdr_running(self, herdr: str) -> bool:
        """Whether `herdr status server` says its server runs."""
        try:
            status = json.loads(_output(herdr, "status", "server", "--json"))
        except ValueError:
            return False
        return isinstance(status, dict) and bool(status.get("running"))


def system() -> MacSystem:
    return MacSystem()
