"""`agent-bridge setup` on macOS: installs agent-bridge as a launchd agent of the user who runs
it, from the installed package, and enrolls it with the gateway.

It starts at login and restarts when it stops. Only a Mac without an install is set up here;
agent-bridge/deploy/macos/setup.sh still handles re-runs from a checkout.
"""

import argparse
import ipaddress
import os
import plistlib
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Protocol

from agent_bridge.cli import FAILED, NAME_TAKEN, OK, SAVE_FAILED, UNREACHABLE
from agent_bridge.config import check_host, check_name_format
from agent_bridge.gateway import parse_enrollment
from agent_bridge.install.common import (
    PREFIX,
    Read,
    Refused,
    absolute,
    ask,
    ask_optional,
    check_settings,
    default_name,
    env_text,
    warn_about_settings_in_env,
    write_file,
)
from agent_bridge.state import StateError, load

LABEL = "dev.claude-remote.agent-bridge"
DEFAULT_PORT = 8765
# launchctl bootout returns before the old job is gone, so a bootstrap right after can fail.
BOOTSTRAPS = 10
# Half-second waits for a started bridge to answer.
CHECKS = 30

TRUST_EXPLAINED = """
Folder trust (optional). The first time Claude opens in a folder it asks "Do you trust
this folder?", and an agent started remotely would quit there. agent-bridge can mark
folders under a trust root as trusted beforehand, but only in a separate Claude config
for the agents, never in your own. Answer - to skip this if you have already trusted your
project folders by opening claude in each one.
"""


@dataclass(frozen=True)
class Options:
    """The values passed in; None asks for it (or finds it), "" for the optional ones is none."""

    roots: str | None = None
    name: str | None = None
    host: str | None = None
    port: str | None = None
    herdr_bin: str | None = None
    claude_config_dir: str | None = None
    trust_root: str | None = None
    enrollment: str | None = None

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "Options":
        return cls(**{field.name: getattr(args, field.name) for field in fields(cls)})


class System(Protocol):
    """What setup asks of the Mac; tests replace it."""

    home: Path

    def is_root(self) -> bool: ...
    def in_checkout(self) -> bool: ...
    def hostname(self) -> str: ...
    # The agent-bridge the launch agent runs.
    def command(self) -> str: ...
    # launchd's domain for this user's agents, gui/<uid>.
    def domain(self) -> str: ...
    def lan_address(self) -> str | None: ...
    def has_address(self, address: str) -> bool: ...
    # Who listens on `port`, or None when nothing does.
    def port_holder(self, port: int) -> str | None: ...
    def find_herdr(self) -> str | None: ...
    def launchctl(self, *args: str) -> int: ...
    # Runs the installed agent-bridge with no AGENT_BRIDGE_* in its environment, as launchd
    # would; its output is returned when captured, else shown.
    def bridge(self, *args: str, capture: bool = False) -> tuple[int, str]: ...
    def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def support(self) -> Path:
        # Where `agent-bridge serve` reads its settings and state, as launchd starts it with
        # no environment.
        return self.home / "Library" / "Application Support" / "agent-bridge"

    @property
    def env_file(self) -> Path:
        return self.support / "env"

    @property
    def state_file(self) -> Path:
        return self.support / "state.json"

    @property
    def plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    @property
    def log(self) -> Path:
        return self.home / "Library" / "Logs" / "agent-bridge.log"


@dataclass
class Plan:
    """What setup decided before changing anything."""

    values: dict[str, str]
    enrollment: str
    login_hint: str

    @property
    def name(self) -> str:
        return self.values[f"{PREFIX}NAME"]


def setup(options: Options, system: System, read: Read) -> int:
    paths = Paths(system.home)
    try:
        plan = _decide(options, system, read, paths)
        write_file(paths.env_file, env_text(plan.values).encode(), 0o600, folder_mode=0o700)
        write_file(paths.plist, plistlib.dumps(launch_agent(system.command(), paths.log)), 0o644)
        _start(system, paths)
        gateway_took, name_taken = _enroll(plan, system, read, paths)
    except Refused as error:
        print(f"error: {error}", file=sys.stderr)
        return FAILED
    _summary(plan, system, paths, gateway_took)
    if name_taken:
        print(f"error: the gateway has another host named {name_taken}; run again with another --name",
              file=sys.stderr)
        return FAILED
    return OK


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


def stable_command(invoked: str, which: Callable[[str], str | None] = shutil.which) -> str:
    """The path of the agent-bridge that was run, as one that survives upgrades: links are kept
    (uv tool's ~/.local/bin shim, Homebrew's bin), and a Homebrew version's own folder becomes
    its opt link."""
    if os.sep not in invoked:
        found = which(invoked)
        if found is None:
            raise Refused(f"can't find {invoked} on the PATH")
        invoked = found
    parts = Path(os.path.abspath(invoked)).parts
    if "Cellar" in parts:
        cellar = parts.index("Cellar")
        if len(parts) > cellar + 3:
            # <prefix>/Cellar/<formula>/<version>/... -> <prefix>/opt/<formula>/...
            return str(Path(*parts[:cellar], "opt", parts[cellar + 1], *parts[cellar + 3:]))
    return str(Path(*parts))


# --- Deciding: questions and checks, before anything changes ------------------------


def _decide(options: Options, system: System, read: Read, paths: Paths) -> Plan:
    if system.is_root():
        raise Refused("run as your own user, not with sudo: agent-bridge must run next to your herdr server")
    if system.in_checkout():
        raise Refused(
            "this agent-bridge runs from a checkout of claude-remote-mcp: from a checkout, run "
            "agent-bridge/deploy/macos/setup.sh; or install the package "
            "(uv tool install claude-remote-mcp-agent-bridge) and run its agent-bridge setup"
        )
    installed = next((path for path in (paths.env_file, paths.state_file, paths.plist) if path.exists()), None)
    if installed is not None:
        raise Refused(
            f"agent-bridge is already installed here ({installed}). This setup installs on a Mac "
            "without one; to change an install, run agent-bridge/deploy/macos/setup.sh from a checkout"
        )
    warn_about_settings_in_env()

    roots = _roots(options.roots if options.roots is not None else
                   ask(read, "Folders agents may start in, :-separated", str(Path.home() / "Projects")))
    name = _name(options.name, system, read)
    host = _host(options.host, system)
    port = _port(options.port, system, read)
    herdr = _herdr(options.herdr_bin, system)
    config_dir, trust_root = _folder_trust(options, roots[0], read)
    login_hint = ""
    if config_dir and not (Path(config_dir) / ".claude.json").is_file():
        login_hint = ("Log in to Claude once in the agents' config, or new agents can't start:\n"
                      f"  CLAUDE_CONFIG_DIR={config_dir} claude")
        print(f"warning: {config_dir} has no Claude login yet (see the end)", file=sys.stderr)
    enrollment = _enrollment(options.enrollment, read)

    values = {
        f"{PREFIX}TOKEN": secrets.token_hex(32),
        f"{PREFIX}ALLOWED_ROOTS": ":".join(roots),
        f"{PREFIX}HOST": host,
        f"{PREFIX}PORT": str(port),
        f"{PREFIX}NAME": name,
        f"{PREFIX}HERDR_BIN": herdr,
        # Written even when empty, so a later setup knows folder trust was answered.
        f"{PREFIX}CLAUDE_CONFIG_DIR": config_dir,
        f"{PREFIX}TRUST_ROOT": trust_root,
    }
    check_settings(values)
    return Plan(values, enrollment, login_hint)


def _roots(answer: str) -> list[str]:
    roots = []
    for root in answer.split(":"):
        if root:
            root = absolute(root, "--roots")
            if not Path(root).is_dir():
                print(f"warning: {root} does not exist yet", file=sys.stderr)
            roots.append(root)
    if not roots:
        raise Refused("--roots needs at least one folder")
    return roots


def _name(passed: str | None, system: System, read: Read) -> str:
    if passed is not None:
        try:
            return check_name_format(passed)
        except ValueError as error:
            raise Refused(f"--name {error}") from None
    default = default_name(system.hostname(), "mac")
    while True:
        name = ask(read, f"This host's name in agent ids, like {default}/w1:p1", default)
        try:
            return check_name_format(name)
        except ValueError as error:
            print(f"The name {error}.", file=sys.stderr)


def _host(passed: str | None, system: System) -> str:
    host = passed if passed is not None else system.lan_address()
    if not host:
        raise Refused("couldn't find this Mac's LAN address; pass it, e.g. --host 192.168.1.20")
    try:
        # As ifconfig writes it: lower case and compressed for IPv6.
        host = str(ipaddress.ip_address(check_host(host)))
    except ValueError as error:
        raise Refused(f"--host {error}, not {host}") from None
    if not system.has_address(host):
        print(f"warning: no interface has {host} right now; agent-bridge retries every 10 s until one does",
              file=sys.stderr)
    return host


def _port_number(value: str) -> int:
    if not re.fullmatch(r"[0-9]+", value) or not 1 <= int(value) <= 65535:
        raise Refused(f"--port must be a number from 1 to 65535, not {value}")
    return int(value)


def _port(passed: str | None, system: System, read: Read) -> int:
    if passed is not None:
        port = _port_number(passed)
        if holder := system.port_holder(port):
            raise Refused(f"Port {port} is in use by {holder}; pass another --port")
        return port
    port = DEFAULT_PORT
    while holder := system.port_holder(port):
        print(f"Port {port} is in use by {holder}.", file=sys.stderr)
        free = port + 1
        while system.port_holder(free):
            free += 1
        port = _port_number(ask(read, "Port", str(free)))
    return port


def _herdr(passed: str | None, system: System) -> str:
    herdr = passed if passed is not None else system.find_herdr()
    if not herdr:
        raise Refused("herdr not found; install it, or pass --herdr-bin /full/path/to/herdr")
    herdr = absolute(herdr, "--herdr-bin")
    if not (os.path.isfile(herdr) and os.access(herdr, os.X_OK)):
        raise Refused(f"{herdr} is not an executable")
    return herdr


def _folder_trust(options: Options, first_root: str, read: Read) -> tuple[str, str]:
    config_dir, trust_root = options.claude_config_dir, options.trust_root
    if config_dir is None or trust_root is None:
        print(TRUST_EXPLAINED, file=sys.stderr)
    if config_dir is None:
        config_dir = ask_optional(read, "Separate Claude config folder for agents", str(Path.home() / ".claude-agents"))
    config_dir = absolute(config_dir, "--claude-config-dir")
    if trust_root is None:
        trust_root = ask_optional(read, "Trust agents' folders under", first_root) if config_dir else ""
    return config_dir, absolute(trust_root, "--trust-root")


def _enrollment(passed: str | None, read: Read) -> str:
    if passed is not None:
        if passed:
            try:
                parse_enrollment(passed)
            except ValueError as error:
                raise Refused(str(error)) from None
        return passed.strip()
    print("\nTo enroll with the gateway, run on the gateway host: sudo mcp-gateway enroll", file=sys.stderr)
    return _ask_enrollment(read, "Enrollment string it printed, crm1@…")


def _ask_enrollment(read: Read, question: str) -> str:
    """A valid string, or "" to enroll later."""
    while answer := ask(read, question, "", "Enter to enroll later"):
        try:
            parse_enrollment(answer)
            return answer
        except ValueError as error:
            print(error, file=sys.stderr)
    return ""


# --- Installing and enrolling ------------------------------------------------------


def _start(system: System, paths: Paths) -> None:
    print(f"\n==> Starting {LABEL}")
    service = f"{system.domain()}/{LABEL}"
    system.launchctl("bootout", service)
    system.launchctl("enable", service)
    for attempt in range(BOOTSTRAPS):
        if system.launchctl("bootstrap", system.domain(), str(paths.plist)) == 0:
            break
        if attempt == BOOTSTRAPS - 1:
            raise Refused(f"launchctl bootstrap failed; see: launchctl print {service}")
        system.sleep(1)
    print("\n==> Checks")
    for _ in range(CHECKS):
        _, result = system.bridge("check", capture=True)
        if "FAIL  agent-bridge answers" not in result:
            break
        system.sleep(0.5)
    print(result, end="")


def _enroll(plan: Plan, system: System, read: Read, paths: Paths) -> tuple[bool, str]:
    """Whether the gateway took it without the state file being saved, and a name it has for
    another host that no other name was given for."""
    while plan.enrollment:
        print("\n==> Enrolling with the gateway")
        code, _ = system.bridge("enroll", plan.enrollment)
        if code == OK or code == UNREACHABLE:  # on 4 it printed the command to run later
            return False, ""
        if code == SAVE_FAILED:
            return True, ""
        if code == NAME_TAKEN:
            taken = plan.name
            # It drops an old record only at the same address, e.g. one added with bridges add.
            print(f"If {taken} is this Mac's old record (its address has changed), remove it on the gateway "
                  f"host with: sudo mcp-gateway bridges remove {taken}, then run again. Otherwise, choose "
                  "another name.", file=sys.stderr)
            name = _another_name(read)
            if not name:
                return False, taken
            plan.values[f"{PREFIX}NAME"] = name
            write_file(paths.env_file, env_text(plan.values).encode(), 0o600, folder_mode=0o700)
            _start(system, paths)
            continue
        plan.enrollment = _ask_enrollment(read, "Enrollment string, crm1@…")
    return False, ""


def _another_name(read: Read) -> str:
    """A valid name, or "" when none is given."""
    while name := ask(read, "Another name for this host", "", "required"):
        try:
            return check_name_format(name)
        except ValueError as error:
            print(f"The name {error}.", file=sys.stderr)
    return ""


def _summary(plan: Plan, system: System, paths: Paths, gateway_took: bool) -> None:
    host, port = plan.values[f"{PREFIX}HOST"], plan.values[f"{PREFIX}PORT"]
    url_host = f"[{host}]" if ":" in host else host
    print(f"\nDone. agent-bridge listens on http://{url_host}:{port} and starts at every login.")
    print(f"If a check failed: tail {paths.log} (herdr failing means its server isn't running).")
    print("If the macOS firewall asks, allow incoming connections for Python.")
    if plan.login_hint:
        print(f"\n{plan.login_hint}")
    try:
        state = load(paths.state_file)
    except StateError:
        state = None
    later = f"  {system.command()} enroll '<what it printed>'"
    if gateway_took:
        print("\nThe gateway enrolled it, but its state file couldn't be written (see above). Fix that, then "
              f"enroll again with a new string from sudo mcp-gateway enroll:\n{later}")
    elif state is not None and state.gateway is not None:
        print(f"\nIt is enrolled with the gateway at {state.gateway.url} as {state.gateway.name}: Claude reaches "
              f"its agents as {state.gateway.name}/…")
    else:
        print(f"\nIt is not enrolled with a gateway yet. Run sudo mcp-gateway enroll on the gateway host, then "
              f"here:\n{later}")


# --- The real Mac ------------------------------------------------------------------


class MacSystem:
    def __init__(self) -> None:
        self.home = Path.home()

    def is_root(self) -> bool:
        return os.geteuid() == 0

    def in_checkout(self) -> bool:
        # A checkout's agent-bridge runs from <checkout>/.venv.
        return (Path(sys.prefix).parent / "agent-bridge" / "pyproject.toml").is_file()

    def hostname(self) -> str:
        return socket.gethostname()

    def command(self) -> str:
        return stable_command(sys.argv[0])

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

    def port_holder(self, port: int) -> str | None:
        pids = _output("lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t").split()
        if pids:
            command = _output("ps", "-o", "command=", "-p", pids[0]).strip()
            return f"PID {pids[0]}: {command}" if command else f"PID {pids[0]}"
        # lsof sees only your own processes; netstat sees every listener, without its PID.
        for line in _output("netstat", "-anv", "-p", "tcp").splitlines():
            words = line.split()
            if len(words) > 5 and words[5] == "LISTEN" and words[3].endswith(f".{port}"):
                return "another user's program"
        return None

    def find_herdr(self) -> str | None:
        """The herdr on PATH, else one in the usual places."""
        for path in (shutil.which("herdr"), self.home / ".local/bin/herdr", "/opt/homebrew/bin/herdr",
                     "/usr/local/bin/herdr"):
            if path and os.path.isfile(path) and os.access(path, os.X_OK):
                return str(path)
        return None

    def launchctl(self, *args: str) -> int:
        return subprocess.run(["launchctl", *args], capture_output=True).returncode

    def bridge(self, *args: str, capture: bool = False) -> tuple[int, str]:
        environment = {name: value for name, value in os.environ.items() if not name.startswith(PREFIX)}
        # What was printed so far goes first: piped, Python holds it while the child writes.
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            result = subprocess.run([self.command(), *args], env=environment, cwd="/", text=True,
                                    capture_output=capture)
        except OSError as error:
            raise Refused(f"can't run {self.command()} ({error.strerror})") from None
        return result.returncode, result.stdout or ""

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def _output(*argv: str) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True).stdout
    except OSError:
        return ""
