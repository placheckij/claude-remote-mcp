"""`agent-bridge setup` and `uninstall` on Windows: agent-bridge as a scheduled task of the user
who runs it. It starts at logon, in that user's session (next to their herdr server), with no
window, and again every minute while it isn't running. Another task starts herdr's server at
logon unless one runs, so agents can start after a restart before anyone opens herdr (skipped
with --no-herdr-service). Setup adds a firewall rule for the port on Private networks, the one
step that asks for admin rights. A host set up with setup.ps1 from a checkout is taken over:
the task runs the package from then on. The rest of setup is flow's.
"""

import contextlib
import ctypes
import importlib.resources
import json
import locale
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from base64 import b64encode
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol
from xml.etree import ElementTree  # the user's own task, as schtasks exports it
from xml.sax.saxutils import escape

from dotenv import dotenv_values

from agent_bridge.install import flow
from agent_bridge.install.common import PREFIX, Read, Refused, ask
from agent_bridge.install.flow import Installed, Options, Plan

__all__ = ["HERDR_TASK", "RULE", "TASK", "Options", "WindowsService", "WindowsSystem", "herdr_task_xml", "setup",
           "stop_for_upgrade", "system", "task_xml", "uninstall"]

TASK = "agent-bridge"
# Starts herdr's server at logon; never stops it.
HERDR_TASK = "agent-bridge-herdr"
RULE = "agent-bridge"
# How long setup waits for the herdr task to start herdr's server: 40 looks, 0.5 s apart.
HERDR_LOOKS = 40
# The task's command and its arguments: Task Scheduler expands %...% in both, quoted or not.
NO_EXPANSION = "%"


@dataclass(frozen=True)
class Listener:
    """A process listening on a port. Another user's, or an elevated one, has no command line."""

    pid: int
    name: str
    command_line: str
    path: str


@dataclass(frozen=True)
class Paths:
    """Where the settings and state are, as agent_bridge/state.py has them on Windows."""

    appdata: Path
    local: Path

    @property
    def settings(self) -> Path:
        return self.appdata / "agent-bridge"

    @property
    def env_file(self) -> Path:
        return self.settings / "env"

    @property
    def state(self) -> Path:
        return self.local / "agent-bridge"

    @property
    def state_file(self) -> Path:
        return self.state / "state.json"

    @property
    def log(self) -> Path:
        return self.state / "agent-bridge.log"

    @property
    def task_file(self) -> Path:
        """The task as setup registered it, read back to register it again (which starts it)."""
        return self.state / "task.xml"

    @property
    def herdr_task_file(self) -> Path:
        return self.state / "herdr-task.xml"

    @property
    def no_herdr_task(self) -> Path:
        """There after setup --no-herdr-service, which the next setup keeps. Hosts set up before
        the herdr task existed have neither, and get the task."""
        return self.state / "no-herdr-task"

    @property
    def disabled_marker(self) -> Path:
        """There while setup has the task disabled: a setup that stops early, or the next one
        after one was killed, then knows the task is setup's to bring back, not one you turned
        off. setup.ps1 used the same file."""
        return self.state / "task-disabled-by-setup"

    def leftovers(self) -> tuple[Path, ...]:
        """What setup.ps1 left that the package's setup doesn't use."""
        return (self.state / "run.cmd", self.state / "bridge.txt")

    def check_folder(self) -> Path:
        """Where setup.ps1 checked a copy of the settings; a killed one left it, token and all."""
        return self.state / "setup-check"


def task_xml(user: str, command: str, log: Path, folder: Path) -> str:
    """The scheduled task: at logon, as `user` (a SID), runs `command` (agent-bridgew.exe) to
    serve and append to `log`, with nothing in between, so ending the task ends it."""
    for path in (command, str(log)):
        if NO_EXPANSION in path:
            raise Refused(f"agent-bridge can't run from a path with % in it: {path}")
    return _filled("agent-bridge.task.xml", user, command, f'serve --log-file "{log}"', folder)


def herdr_task_xml(user: str, command: str, herdr: str, log: Path, folder: Path) -> str:
    """The task that starts herdr's server: at logon, as `user`, runs `command`
    (agent-bridgew.exe) start-herdr for `herdr`, which appends to `log`."""
    for path in (command, herdr, str(log)):
        if NO_EXPANSION in path:
            raise Refused(f"the task {HERDR_TASK} can't run a path with % in it: {path}")
    return _filled("agent-bridge-herdr.task.xml", user, command,
                   f'start-herdr --herdr-bin "{herdr}" --log-file "{log}"', folder)


def _filled(template: str, user: str, command: str, arguments: str, folder: Path) -> str:
    fields = {
        "{{USER}}": user,
        "{{COMMAND}}": command,
        "{{ARGUMENTS}}": arguments,
        "{{WORKING_DIRECTORY}}": str(folder),
    }
    xml = importlib.resources.files(__package__).joinpath(template).read_text(encoding="utf-8")
    for placeholder, value in fields.items():
        xml = xml.replace(placeholder, escape(value, {'"': "&quot;"}))
    return xml


def service_command(invoked: str) -> str:
    """The windowless agent-bridgew.exe beside the agent-bridge that was run. uv's shim passes
    its own path without .exe; it is in uv tool's bin folder (~/.local/bin), which upgrades
    keep."""
    if os.sep not in invoked and "/" not in invoked:
        found = shutil.which(invoked)
        if found is None:
            raise Refused(f"can't find {invoked} on the PATH")
        invoked = found
    return str(Path(os.path.abspath(invoked)).with_name("agent-bridgew.exe"))


def bridge_command(invoked: str) -> str:
    """The agent-bridge that was run, as a path with its .exe."""
    if os.sep not in invoked and "/" not in invoked:
        invoked = shutil.which(invoked) or invoked
    path = Path(os.path.abspath(invoked))
    if not path.suffix and path.with_suffix(".exe").is_file():
        path = path.with_suffix(".exe")
    return str(path)


def is_bridge(command_line: str, known: Iterable[str] = ()) -> bool:
    """Whether a command line is agent-bridge serving, wherever it is installed: one of `known`
    (the agent-bridge(w).exe paths this run knows), a package's or checkout's agent-bridge(w),
    `python -m agent_bridge.cli serve`, or uvicorn with agent_bridge.app:main as installed
    before. Other mentions of agent_bridge don't count."""
    if not command_line:
        return False
    for path in known:
        # serve right after the path, not anywhere on the line.
        if path and re.search(re.escape(path) + r'"?\s+serve\b', command_line, re.IGNORECASE):
            return True
    return bool(
        # The program's own name: after a folder, a quote or the start, not inside another name.
        re.search(r'(?:^|[\\/"\s])agent-bridgew?(\.(exe|cmd|bat))?"?\s+serve\b', command_line, re.IGNORECASE)
        or re.search(r"-m\s+agent_bridge(\.cli)?\s+serve\b", command_line, re.IGNORECASE)
        or re.search(r"\bagent_bridge\.app:main\b", command_line, re.IGNORECASE)
    )


def excluded_ranges(netsh: str) -> list[tuple[int, int]]:
    """The port ranges in `netsh int ipv4 show excludedportrange protocol=tcp`'s output."""
    return [(int(first), int(last)) for first, last in re.findall(r"^\s*(\d+)\s+(\d+)", netsh, re.MULTILINE)]


@dataclass(frozen=True)
class Task:
    """What setup reads from an installed task's XML."""

    command: str
    enabled: bool

    @classmethod
    def parse(cls, xml: str) -> "Task":
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError:
            return cls("", True)
        command = root.find("t:Actions/t:Exec/t:Command", _NS)
        # The task's own, not a trigger's; left out, it is enabled.
        enabled = root.find("t:Settings/t:Enabled", _NS)
        return cls((command.text or "").strip().strip('"') if command is not None else "",
                   enabled is None or (enabled.text or "").strip().lower() != "false")


_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def enabled(xml: str) -> str:
    """The task with its Settings' Enabled true, so registering it starts it: a task setup puts
    back after an earlier setup was killed with it disabled."""
    if Task.parse(xml).enabled:
        return xml
    ElementTree.register_namespace("", _NS["t"])
    root = ElementTree.fromstring(xml)
    root.find("t:Settings/t:Enabled", _NS).text = "true"
    return '<?xml version="1.0" encoding="UTF-16"?>\n' + ElementTree.tostring(root, encoding="unicode")


def pe_subsystem(path: Path) -> int:
    """The subsystem in the PE header of the program at `path`: 2 for a GUI program, 3 for a
    console one, which Windows opens a console window for when it has none to share. 0 when it
    isn't a program or can't be read."""
    try:
        with path.open("rb") as file:
            header = file.read(4096)
    except OSError:
        return 0
    if len(header) < 64 or header[:2] != b"MZ":
        return 0
    pe = int.from_bytes(header[0x3C:0x40], "little")
    # PE\0\0, the 20-byte file header, then the optional header, with Subsystem at 68 in both
    # PE32 and PE32+.
    if pe < 64 or pe + 94 > len(header) or header[pe:pe + 4] != b"PE\0\0":
        return 0
    return int.from_bytes(header[pe + 92:pe + 94], "little")


def firewall_rule_commands(port: int) -> str:
    """Replaces the rule with one that allows TCP `port` in, on Private networks only."""
    return (f"Remove-NetFirewallRule -Name '{RULE}' -ErrorAction SilentlyContinue; "
            f"New-NetFirewallRule -Name '{RULE}' -DisplayName 'agent-bridge' -Description 'Lets mcp-gateway on "
            "the LAN reach agent-bridge. Added by agent-bridge setup.' -Direction Inbound -Action Allow "
            f"-Protocol TCP -LocalPort {port} -Profile Private | Out-Null")


def remove_rule_command() -> str:
    # Also when it isn't there, for when setup couldn't tell.
    return f"Get-NetFirewallRule -Name '{RULE}' -ErrorAction SilentlyContinue | Remove-NetFirewallRule"


def quoted(text: str) -> str:
    """`text` in single quotes as PowerShell reads them, for commands printed to copy."""
    return "'" + text.replace("'", "''") + "'"


def holder(listeners: list[Listener], excluded: list[tuple[int, int]], port: int) -> str | None:
    """Who has `port`, as setup says it: another program before an agent-bridge, which setup
    can stop; Windows when it keeps the port for itself; None when it is free."""
    others = [listener for listener in listeners if not is_bridge(listener.command_line)]
    for listener in others or listeners:
        if not listener.command_line:
            return f"PID {listener.pid} ({listener.name}, another user's or an elevated program)"
        return f"PID {listener.pid}: {listener.command_line}"
    if any(first <= port <= last for first, last in excluded):
        return ("Windows, which keeps it for itself (see: netsh int ipv4 show excludedportrange "
                "protocol=tcp)")
    return None


def serving(listeners: list[Listener]) -> bool:
    """Whether agent-bridge, and nothing else, listens there: setup stops it and takes the port."""
    return bool(listeners) and all(is_bridge(listener.command_line) for listener in listeners)


class System(flow.System, Protocol):
    paths: Paths

    # This account's SID, and DOMAIN\user.
    def user(self) -> str: ...
    def me(self) -> str: ...
    # Whose desktop this session is, when it can be told.
    def desktop_owner(self) -> str | None: ...
    # The agent-bridgew.exe the task runs.
    def service_command(self) -> str: ...
    def lasting_settings(self) -> dict[str, list[str]]: ...
    def listeners(self, port: int) -> list[Listener]: ...
    # Every process whose command line can be read.
    def processes(self) -> list[Listener]: ...
    def stop(self, pids: list[int]) -> None: ...
    def network(self, address: str) -> tuple[int, str, str] | None: ...
    # Setup's rule; None when it isn't there, () when that can't be told.
    def firewall_rule(self) -> tuple | None: ...
    # The programs the task runs that are console programs, which open a window at each start.
    def console_programs(self) -> list[str]: ...
    def blocked(self, program: str) -> bool: ...
    def elevated(self, commands: list[str]) -> int: ...
    def schtasks(self, *args: str) -> int: ...
    # The task's XML, None when there is no such task.
    def task(self, name: str = TASK) -> str | None: ...
    def register(self, xml: str, file: Path, name: str = TASK) -> bool: ...
    def herdr_running(self, herdr: str) -> bool: ...


def setup(options: Options, system: System, read: Read) -> int:
    # Before any question.
    if (refusal := _other_account(system)) is not None:
        print(f"error: {refusal}", file=sys.stderr)
        return flow.FAILED
    service = WindowsService(system, read)
    code = flow.FAILED
    try:
        code = flow.setup(_setup_ps1_answers(options, system.paths), system, service, read)
    finally:
        # However it stopped (a refusal, Ctrl+C), and also before it stopped anything, after
        # an earlier setup was killed with the task disabled.
        if code != flow.OK:
            service.abandon()
    return code


def uninstall(system: System) -> int:
    if (refusal := _other_account(system)) is not None:
        print(f"error: {refusal}", file=sys.stderr)
        return flow.FAILED
    return flow.uninstall(system, WindowsService(system, lambda: None))


def stop_for_upgrade(system: System, environment: Path | None, programs: Iterable[str]) -> None:
    """For `agent-bridge upgrade`: stops the task and the bridge it runs, as setup does, leaving
    the task disabled for the setup that follows to bring back. Refused, with the task put back,
    while anything else still runs from the install (e.g. an agent-bridge check in another
    terminal): uv couldn't replace its files, and would leave the package half removed."""
    if (refusal := _other_account(system)) is not None:
        raise Refused(refusal)
    service = WindowsService(system, lambda: None)
    service.prepare()
    if service.previous is not None:
        print(f"\n==> Stopping the scheduled task {TASK}")
    service._stop((service.installed_port,))
    held = [process for process in system.processes() if _runs_from(process, environment, programs)]
    if held:
        service.abandon()
        running = ", ".join(f"{process.name} (PID {process.pid})" for process in held)
        raise Refused(f"{running} still {'run' if len(held) > 1 else 'runs'} from the installed agent-bridge, so uv "
                      "couldn't replace its files. Close them, then run the upgrade again; agent-bridge was started "
                      "again meanwhile")


def _runs_from(process: Listener, environment: Path | None, programs: Iterable[str]) -> bool:
    """Whether `process` is one of `programs` or runs from `environment`."""
    path = os.path.normcase(process.path)
    if not path:
        return False
    if environment is not None and path.startswith(os.path.normcase(str(environment)) + os.sep):
        return True
    return any(path == os.path.normcase(program) for program in programs)


def _setup_ps1_answers(options: Options, paths: Paths) -> Options:
    """setup.ps1 set up folder trust only when it was passed, and asked nothing about it: on a
    host it set up, a folder-trust setting its env file doesn't have was answered none. Not the
    trust root when a config folder is passed now: that is folder trust set up on this run."""
    if not paths.env_file.exists() or not _from_setup_ps1(paths):
        return options
    try:
        keys = {key.upper() for key in dotenv_values(paths.env_file)}
    except OSError:
        return options
    answers = {}
    if options.claude_config_dir is None and f"{PREFIX}CLAUDE_CONFIG_DIR" not in keys:
        answers["claude_config_dir"] = ""
    if options.trust_root is None and options.claude_config_dir is None and f"{PREFIX}TRUST_ROOT" not in keys:
        answers["trust_root"] = ""
    return replace(options, **answers)


def _from_setup_ps1(paths: Paths) -> bool:
    """Whether setup.ps1 wrote the env file: its first line says so, or its files are there."""
    try:
        with paths.env_file.open(encoding="utf-8", errors="replace") as file:
            first = file.readline()
    except OSError:
        first = ""
    return r"deploy\windows\setup.ps1" in first or any(path.exists() for path in paths.leftovers())


def _other_account(system: System) -> str | None:
    """Why setup refuses to run as another account than the one whose desktop this is, e.g. an
    administrator's typed in at a UAC prompt: the task and the settings would be that
    account's, away from herdr."""
    owner, me = system.desktop_owner(), system.me()
    if owner is None or owner.lower() == me.lower():
        return None
    return (f"run this as {owner}, whose desktop this is, not as {me}: agent-bridge must run next to your herdr "
            "server. Open PowerShell normally; setup asks for admin rights only for the firewall rule.")


@dataclass
class WindowsService:
    system: System
    read: Read
    machine: str = "PC"
    runner: str = "scheduled task"
    name_fallback: str = "pc"
    # How often the task tries again, e.g. until the LAN address exists.
    retries: str = "every minute"
    host: str = ""
    port: int = 0
    # The port the bridge being replaced had, to stop it there.
    installed_port: int = 0
    # The task as it was, to put back when setup stops after stopping it.
    previous: str | None = None
    # abandon() ran: flow's refusal handler and setup()'s finally both call it.
    abandoned: bool = False
    # The task that starts herdr's server at logon, and the herdr it starts.
    herdr_service: bool = True
    herdr: str = ""
    # Lines for the end: what setup couldn't do.
    notes: list[str] = field(default_factory=list)

    @property
    def paths(self) -> Paths:
        return self.system.paths

    @property
    def env_file(self) -> Path:
        return self.paths.env_file

    @property
    def state_file(self) -> Path:
        return self.paths.state_file

    def _xml(self) -> str:
        return task_xml(self.system.user(), self.system.service_command(), self.paths.log, self.paths.state)

    def _herdr_xml(self) -> str:
        return herdr_task_xml(self.system.user(), self.system.service_command(), self.herdr, self.paths.log,
                              self.paths.state)

    def check(self, plan: Plan, options: Options) -> None:
        # The task gets your lasting environment, where these win over the env file.
        for scope, names in self.system.lasting_settings().items():
            target = "Machine" if scope == "machine" else "User"
            remove = "; ".join(f"[Environment]::SetEnvironmentVariable('{name}', $null, '{target}')" for name in names)
            raise Refused(f"{', '.join(names)} set in your {scope} environment would win over {self.env_file} for "
                          f"the task. Remove {'them' if len(names) > 1 else 'it'}"
                          f"{' as administrator' if scope == 'machine' else ''}, then run setup again:\n  {remove}")
        command = self.system.service_command()
        if not Path(command).is_file():
            raise Refused(f"{command}, the windowless agent-bridge the task runs, isn't there: reinstall the "
                          "package (uv tool install --reinstall claude-remote-mcp-agent-bridge)")
        if console := self.system.console_programs():
            are = "are console programs" if len(console) > 1 else "is a console program"
            raise Refused(f"{' and '.join(console)} {are}, so agent-bridge would open a terminal window at every "
                          "start. uv before 0.12.4 makes them so (astral-sh/uv#19235): update uv (uv self update), "
                          "reinstall the package with agent-bridge stopped (uv tool install --reinstall "
                          "claude-remote-mcp-agent-bridge), then run agent-bridge setup again")
        # Kept as the last setup left it unless passed: off only after --no-herdr-service.
        if options.herdr_service is not None:
            self.herdr_service = options.herdr_service
        else:
            self.herdr_service = not self.paths.no_herdr_task.exists()
        self.herdr = plan.values[f"{PREFIX}HERDR_BIN"]
        # Refused here, before anything changes, rather than when the tasks are registered.
        self._xml()
        if self.herdr_service:
            self._herdr_xml()
        if plan.login_hint:
            config = plan.values[f"{PREFIX}CLAUDE_CONFIG_DIR"]
            plan.login_hint = ("Log in to Claude once in the agents' config, or new agents can't start. In "
                               f"PowerShell:\n  $env:CLAUDE_CONFIG_DIR = {quoted(config)}; claude")
        self.host = plan.values[f"{PREFIX}HOST"]
        self.port = int(plan.values[f"{PREFIX}PORT"])

    def prepare(self) -> None:
        self.previous = self.system.task()
        self.installed_port = _installed_port(self.env_file)

    def abandon(self) -> None:
        """Puts back the task setup stopped (this one, or one killed before), so a setup that
        stops halfway doesn't leave the bridge off. Registering it, enabled, starts it."""
        if self.abandoned or not self.paths.disabled_marker.exists():
            return
        self.abandoned = True
        xml = self.previous if self.previous is not None else self.system.task()
        if xml is None:
            # Unregistered since: nothing to bring back.
            with contextlib.suppress(OSError):
                self.paths.disabled_marker.unlink()
            return
        try:
            registered = self.system.register(enabled(xml), self.paths.state / "task-before-setup.xml")
        except Refused as error:
            print(f"warning: {error}", file=sys.stderr)
            registered = False
        if registered:
            with contextlib.suppress(OSError):
                self.paths.disabled_marker.unlink()
        else:
            # The marker stays, so the next setup brings it back.
            print("warning: couldn't start the agent-bridge task again; run setup again", file=sys.stderr)

    def legacy(self) -> Installed | None:
        # setup.ps1 wrote the same env file.
        return None

    def install(self, plan: Plan) -> None:
        self._firewall()
        if self.herdr_service:
            self._herdr_task()
        else:
            self._no_herdr_task()

    def _herdr_task(self) -> None:
        """Registers the task that starts herdr's server at logon. Registering it runs it once,
        which starts the server unless one runs; a running one is never restarted."""
        print(f"\n==> The scheduled task {HERDR_TASK}, which starts herdr's server at every logon")
        running = self.system.herdr_running(self.herdr)
        if not self.system.register(self._herdr_xml(), self.paths.herdr_task_file, HERDR_TASK):
            raise Refused(f"schtasks couldn't register the task {HERDR_TASK}")
        with contextlib.suppress(OSError):
            self.paths.no_herdr_task.unlink(missing_ok=True)
        if running:
            print("herdr's server already runs; it is left as it is.")
            return
        print("Starting herdr's server")
        for _ in range(HERDR_LOOKS):
            self.system.sleep(0.5)
            if self.system.herdr_running(self.herdr):
                print("ok")
                return
        print(f"warning: herdr's server didn't start; see {self.paths.log}", file=sys.stderr)
        self.notes.append("herdr's server didn't start (see above), so agents can't start until you open herdr.")

    def _no_herdr_task(self) -> None:
        """Removes the herdr task, and remembers that, so the next setup doesn't add it back.
        herdr's server keeps running."""
        try:
            self.paths.state.mkdir(parents=True, exist_ok=True)
            self.paths.no_herdr_task.write_text("setup --no-herdr-service\n", encoding="utf-8")
        except OSError as error:
            raise Refused(f"can't write {self.paths.no_herdr_task} ({error.strerror})") from None
        if self.system.task(HERDR_TASK) is not None:
            print(f"\n==> Removing the scheduled task {HERDR_TASK} (herdr's server keeps running)")
            self._delete_herdr_task()
        with contextlib.suppress(OSError):
            self.paths.herdr_task_file.unlink(missing_ok=True)

    def _delete_herdr_task(self) -> None:
        if self.system.schtasks("/Delete", "/TN", f"\\{HERDR_TASK}", "/F") != 0:
            print(f"warning: couldn't remove the scheduled task {HERDR_TASK}; remove it in Task Scheduler",
                  file=sys.stderr)
            self.notes.append(f"The scheduled task {HERDR_TASK} is still there (see above).")

    def _firewall(self) -> None:
        print(f"\n==> Firewall rule for TCP {self.port}, on Private networks only")
        commands = []
        if self.system.firewall_rule() != (True, "Inbound", "Allow", "Private", "TCP", str(self.port)):
            commands.append(firewall_rule_commands(self.port))
        network = self.system.network(self.host)
        if network is not None and network[1] == "Public":
            index, _, name = network
            print(f"Windows treats the network {self.host} is on ({name}) as Public, where the rule doesn't apply, "
                  "so the gateway couldn't reach agent-bridge. On your own network, make it Private.")
            if ask(self.read, f"Make {name} a Private network?", "n", "y/N").lower().startswith("y"):
                commands.append(f"Set-NetConnectionProfile -InterfaceIndex {index} -NetworkCategory Private")
            else:
                self.notes.append(f"The network {name} is Public, so the gateway can't reach agent-bridge until you "
                                  "make it Private.")
        elif network is not None and network[1] != "Private":
            print(f"warning: the network {self.host} is on is {network[1]}; the rule applies only on Private "
                  "networks", file=sys.stderr)
        if not commands:
            print("The rule is already there.")
            return
        print("Windows asks for admin rights for this.")
        failed = self.system.elevated(commands)
        by_hand = [command for i, command in enumerate(commands) if failed == -1 or failed & (1 << i)]
        if not by_hand:
            print("ok")
            return
        print("warning: not done, so the gateway can't reach agent-bridge. In a PowerShell run as administrator:",
              file=sys.stderr)
        for command in by_hand:
            print(f"  {command}", file=sys.stderr)
        self.notes.append("The firewall step wasn't done (see above), so the gateway can't reach agent-bridge yet.")

    def start(self) -> None:
        print(f"\n==> Starting the scheduled task {TASK}")
        self._stop((self.port, self.installed_port))
        print("If Windows asks whether to let Python through the firewall, allow it on Private networks: Cancel "
              "adds a rule that blocks Python, which wins over setup's rule.")
        xml = self._xml()
        if not self.system.register(xml, self.paths.task_file):
            raise Refused(f"schtasks couldn't register the task {TASK}")
        # What a later stop (another name, a token put back) puts back, not the old one.
        self.previous = xml
        with contextlib.suppress(OSError):
            self.paths.disabled_marker.unlink()
        # What setup.ps1 used and the package's setup doesn't; only now, as the task it had,
        # which abandon() would put back, may run run.cmd.
        for path in self.paths.leftovers():
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
        shutil.rmtree(self.paths.check_folder(), ignore_errors=True)

    def _stop(self, ports: Iterable[int]) -> None:
        """Stops the task and the bridge it runs, and any agent-bridge still holding one of
        `ports`. The task is left disabled, or its every-minute repeat could start the old
        bridge again meanwhile; registering it enables it again."""
        known = {self.system.service_command(), self.system.command()}
        xml = self.system.task()
        if xml is not None:
            # Brought back by a setup that stops early only if it was on, or setup had turned it
            # off before (the marker is there): one you disabled stays off then. Marked first, so
            # a setup killed in between leaves the mark too.
            marked = False
            if Task.parse(xml).enabled and not self.paths.disabled_marker.exists():
                try:
                    self.paths.state.mkdir(parents=True, exist_ok=True)
                    self.paths.disabled_marker.write_text("setup\n", encoding="utf-8")
                    marked = True
                except OSError as error:
                    raise Refused(f"can't write {self.paths.disabled_marker} ({error.strerror})") from None
            if self.system.schtasks("/Change", "/TN", f"\\{TASK}", "/DISABLE") != 0:
                # Still enabled and running: nothing to put back.
                if marked:
                    with contextlib.suppress(OSError):
                        self.paths.disabled_marker.unlink()
                raise Refused(f"schtasks couldn't disable the task {TASK}, which would start the old agent-bridge "
                              "again; nothing was stopped")
            self.system.schtasks("/End", "/TN", f"\\{TASK}")
            # The one it ran, e.g. a checkout's, and the agent-bridge.exe beside it.
            if command := Task.parse(xml).command:
                known |= {command, str(Path(command).with_name("agent-bridge.exe"))}
        # Ending the task ends the agent-bridgew.exe it runs, which takes its python.exe along.
        # Not so for a bridge started by hand, or by setup.ps1's older task, which ran run.cmd
        # under cmd.exe, leaving everything below it running.
        run_file = str(self.paths.state / "run.cmd").lower()
        known_lower = [path.lower() for path in known]
        self.system.stop([process.pid for process in self.system.processes() if (
            (process.name.lower() == "cmd.exe" and run_file in process.command_line.lower())
            or (is_bridge(process.command_line, known)
                and any(path in process.command_line.lower() for path in known_lower)))])
        for port in {port for port in ports if port}:
            # Even beside another program on the port, on another address.
            bridges = [listener.pid for listener in self.system.listeners(port) if is_bridge(listener.command_line)]
            if bridges:
                print(f"Stopping the agent-bridge on port {port} (PID {', '.join(map(str, bridges))})")
                self.system.stop(bridges)
            for _ in range(20):
                if not any(is_bridge(listener.command_line) for listener in self.system.listeners(port)):
                    break
                self.system.sleep(0.5)

    def done(self, url: str) -> list[str]:
        lines = [f"Done. agent-bridge listens on {url} and starts at every logon.",
                 f"herdr's server starts at every logon too (the task {HERDR_TASK})." if self.herdr_service else
                 "Open herdr after each logon: its server must run for agents to start.",
                 f"If a check failed: see its log, {self.paths.log} (herdr failing means its server isn't running)."]
        program = next((listener.path for listener in self.system.listeners(self.port)
                        if is_bridge(listener.command_line) and listener.path), "")
        if program and self.system.blocked(program):
            lines += [f"warning: a firewall rule blocks {program}, so the gateway can't reach agent-bridge. In a "
                      "PowerShell run as administrator:",
                      f"  Get-NetFirewallApplicationFilter -Program {quoted(program)} | Get-NetFirewallRule | "
                      "Where-Object Action -eq Block | Remove-NetFirewallRule"]
        if " " in (command := self.system.command()):
            lines.append(f"In PowerShell, run agent-bridge as & {quoted(command)} (its path has a space).")
        return lines + self.notes

    def remove(self) -> None:
        print(f"\n==> Removing the scheduled task {TASK}")
        self.previous = self.system.task()
        self._stop((_installed_port(self.env_file),))
        if self.previous is not None and self.system.schtasks("/Delete", "/TN", f"\\{TASK}", "/F") != 0:
            self.abandon()
            raise Refused(f"couldn't remove the scheduled task {TASK}; it is left as it was. Run uninstall again.")
        if self.system.task(HERDR_TASK) is not None:
            print(f"\n==> Removing the scheduled task {HERDR_TASK}")
            self._delete_herdr_task()
            print("herdr's server, if it runs, keeps running with its agents until you stop it (herdr server stop) "
                  "or log off.")
        print("\n==> Removing the firewall rule")
        rule = self.system.firewall_rule()
        if rule is None:
            print("not there")
        else:
            if rule == ():
                print("Couldn't tell whether it is there; removing it if it is.")
            print("Windows asks for admin rights for this.")
            if self.system.elevated([remove_rule_command()]) == 0:
                print("removed")
            else:
                print(f"warning: not removed. In a PowerShell run as administrator:\n  {remove_rule_command()}",
                      file=sys.stderr)
        for path in (self.paths.log, self.paths.log.with_name(f"{self.paths.log.name}.1"), self.paths.task_file,
                     self.paths.herdr_task_file, self.paths.no_herdr_task, self.paths.state / "task-before-setup.xml",
                     self.paths.disabled_marker, *self.paths.leftovers()):
            self._remove(path)
        shutil.rmtree(self.paths.check_folder(), ignore_errors=True)

    def _remove(self, path: Path) -> None:
        # A process that has only just exited may still hold the log for a moment.
        for attempt in range(5):
            try:
                path.unlink(missing_ok=True)
                return
            except OSError as error:
                if attempt == 4:
                    print(f"warning: couldn't remove {path} ({error.strerror}); delete it by hand", file=sys.stderr)
                    return
                self.system.sleep(1)

    def folders(self) -> Iterable[Path]:
        return (self.paths.settings, self.paths.state)


def _installed_port(env_file: Path) -> int:
    """The port the env file sets, else agent-bridge's default; 0 without an env file, when no
    port is ours."""
    try:
        values = dotenv_values(env_file) if env_file.exists() else None
    except OSError:
        values = None
    if values is None:
        return 0
    port = {key.upper(): value for key, value in values.items()}.get(f"{PREFIX}PORT") or ""
    return int(port) if port.isdigit() and 1 <= int(port) <= 65535 else flow.DEFAULT_PORT


# --- The real Windows ----------------------------------------------------------------


class WindowsSystem:
    """What setup asks of the real Windows; tests replace it."""

    def __init__(self) -> None:
        self.home = Path.home()
        self.paths = Paths(Path(os.environ.get("APPDATA") or self.home / "AppData" / "Roaming"),
                           Path(os.environ.get("LOCALAPPDATA") or self.home / "AppData" / "Local"))

    # --- Who and what runs setup

    def user(self) -> str:
        """This account's SID."""
        output = _output(_system32("whoami.exe"), "/user", "/fo", "csv", "/nh")
        return output.strip().rsplit(",", 1)[-1].strip().strip('"')

    def desktop_owner(self) -> str | None:
        """Who is logged on to this session (DOMAIN\\user), as Windows' session list has it, also
        over Remote Desktop; None when it can't be told. Another account than the one running
        setup is one typed in at a UAC prompt, or with runas."""
        session = _session(os.getpid())
        if session is None:
            return None
        domain, user = _session_info(session, WTS_DOMAIN_NAME), _session_info(session, WTS_USER_NAME)
        return f"{domain}\\{user}" if user else None

    def me(self) -> str:
        return f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}"

    def is_root(self) -> bool:
        # Another account than the desktop's is refused before flow asks (_other_account).
        return False

    def in_checkout(self) -> bool:
        # A checkout's agent-bridge runs from <checkout>/.venv.
        return (Path(sys.prefix).parent / "agent-bridge" / "pyproject.toml").is_file()

    def in_uv_cache(self) -> bool:
        # uvx's environments are <uv cache>/archive-v0/<id>.
        return Path(sys.prefix).parent.name.startswith("archive-v")

    def hostname(self) -> str:
        return os.environ.get("COMPUTERNAME") or socket.gethostname()

    def command(self) -> str:
        """The agent-bridge.exe that was run."""
        return bridge_command(sys.argv[0])

    def service_command(self) -> str:
        """The agent-bridgew.exe the task runs."""
        return service_command(sys.argv[0])

    def lasting_settings(self) -> dict[str, list[str]]:
        """AGENT_BRIDGE_* in your lasting environment and the machine's, by scope: the task
        gets them, and they win over the env file."""
        import winreg

        keys = {
            "user": (winreg.HKEY_CURRENT_USER, r"Environment"),
            "machine": (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        }
        found: dict[str, list[str]] = {}
        for scope, (root, path) in keys.items():
            names = []
            with contextlib.suppress(OSError), winreg.OpenKey(root, path) as key:
                for index in range(winreg.QueryInfoKey(key)[1]):
                    name = winreg.EnumValue(key, index)[0]
                    if name.upper().startswith(PREFIX):
                        names.append(name)
            if names:
                found[scope] = sorted(names)
        return found

    # --- The network

    def lan_address(self) -> str | None:
        """The IPv4 address of the interface the default route goes through."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                # No packet is sent: connecting a UDP socket only picks the route.
                probe.connect(("192.0.2.1", 9))
                address = probe.getsockname()[0]
        except OSError:
            return None
        return address if address != "0.0.0.0" else None

    def has_address(self, address: str) -> bool:
        if address == "127.0.0.1":
            return True
        import psutil

        return any(entry.address.split("%")[0] == address
                   for entries in psutil.net_if_addrs().values() for entry in entries)

    def listeners(self, port: int) -> list[Listener]:
        """Every process listening on TCP `port`, on any address."""
        import psutil

        pids = {connection.pid for connection in psutil.net_connections("tcp")
                if connection.status == psutil.CONN_LISTEN and connection.laddr and connection.laddr.port == port
                and connection.pid}
        found = []
        for pid in sorted(pids):
            try:
                process = psutil.Process(pid)
                name = process.name()
            except psutil.Error:
                found.append(Listener(pid, "?", "", ""))
                continue
            try:
                command_line = subprocess.list2cmdline(process.cmdline())
                path = process.exe()
            except psutil.Error:
                command_line, path = "", ""
            found.append(Listener(pid, name, command_line, path))
        return found

    def port_holder(self, port: int) -> str | None:
        return holder(self.listeners(port), self.excluded_ranges(), port)

    def serves(self, port: int) -> bool:
        return serving(self.listeners(port))

    def excluded_ranges(self) -> list[tuple[int, int]]:
        """The TCP ports Windows keeps for itself (Hyper-V, WSL and Docker reserve them), IPv4's
        and IPv6's: nothing can listen there although nothing does."""
        return [*excluded_ranges(_output(_system32("netsh.exe"), "int", "ipv4", "show", "excludedportrange", "protocol=tcp")),
                *excluded_ranges(_output(_system32("netsh.exe"), "int", "ipv6", "show", "excludedportrange", "protocol=tcp"))]

    def network(self, address: str) -> tuple[int, str, str] | None:
        """The interface index, connection category (Public, Private, DomainAuthenticated) and
        network name of the interface with `address`."""
        code, output = self.powershell(
            f"$ip = Get-NetIPAddress -IPAddress {quoted(address)} -ErrorAction SilentlyContinue | Select-Object -First 1; "
            "if ($ip) { $c = Get-NetConnectionProfile -InterfaceIndex $ip.InterfaceIndex -ErrorAction SilentlyContinue "
            "| Select-Object -First 1; if ($c) { @{ Index = $ip.InterfaceIndex; Category = [string]$c.NetworkCategory; "
            "Name = [string]$c.Name } | ConvertTo-Json -Compress } }")
        try:
            found = json.loads(output) if code == 0 and output.strip() else None
        except ValueError:
            return None
        return (int(found["Index"]), str(found["Category"]), str(found["Name"])) if found else None

    def firewall_rule(self) -> tuple | None:
        """Setup's rule as (enabled, direction, action, profile, protocol, port); None when it
        isn't there, () when PowerShell couldn't tell."""
        # A rule that isn't there leaves PowerShell's exit code 1 although it is no error, so
        # the exit code is set here: 0 unless something failed.
        code, output = self.powershell(
            f"$ErrorActionPreference = 'Stop'; try {{ $r = Get-NetFirewallRule -Name '{RULE}' "
            "-ErrorAction SilentlyContinue; if ($r) { $f = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $r; "
            "@{ Enabled = [string]$r.Enabled; Direction = [string]$r.Direction; Action = [string]$r.Action; "
            "Profile = [string]$r.Profile; Protocol = [string]$f.Protocol; Port = [string]$f.LocalPort } | "
            "ConvertTo-Json -Compress }; exit 0 } catch { exit 1 }")
        if code != 0:
            return ()
        try:
            rule = json.loads(output) if output.strip() else None
        except ValueError:
            return ()
        if not rule:
            return None
        return (rule["Enabled"] == "True", rule["Direction"], rule["Action"], rule["Profile"], rule["Protocol"],
                rule["Port"])

    def console_programs(self) -> list[str]:
        """agent-bridgew.exe and the environment's pythonw.exe it runs, if either is a console
        program."""
        programs = (Path(self.service_command()), Path(sys.prefix) / "Scripts" / "pythonw.exe")
        return [str(program) for program in programs if pe_subsystem(program) == 3]

    def blocked(self, program: str) -> bool:
        """Whether an inbound rule blocks `program`, e.g. python.exe after a Cancel at Windows'
        firewall prompt; it wins over the rule setup adds."""
        code, output = self.powershell(
            f"@(Get-NetFirewallApplicationFilter -Program {quoted(program)} -ErrorAction SilentlyContinue | "
            "Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object { [string]$_.Direction -eq 'Inbound' "
            "-and [string]$_.Action -eq 'Block' -and [string]$_.Enabled -eq 'True' }).Count")
        return code == 0 and output.strip() not in ("", "0")

    # --- Running things

    def powershell(self, script: str) -> tuple[int, str]:
        """Runs `script` in Windows PowerShell; its exit code and what it printed, as UTF-8."""
        wrapped = f"[Console]::OutputEncoding = New-Object Text.UTF8Encoding $false; {script}"
        try:
            result = subprocess.run(
                [_powershell(), "-NoProfile", "-NonInteractive", "-EncodedCommand",
                 b64encode(wrapped.encode("utf-16-le")).decode()],
                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError:
            return -1, ""
        return result.returncode, result.stdout.decode("utf-8", "replace")

    def elevated(self, commands: list[str]) -> int:
        """Runs each command in an elevated, hidden Windows PowerShell (one UAC prompt for all).
        Which failed, as bit i for commands[i] (0: all worked), or -1 when the prompt was
        declined."""
        steps = "; ".join(f"try {{ {command} }} catch {{ $failed += {1 << i} }}" for i, command in enumerate(commands))
        inner = f"$ErrorActionPreference = 'Stop'; $failed = 0; {steps}; exit $failed"
        encoded = b64encode(inner.encode("utf-16-le")).decode()
        code, output = self.powershell(
            f"try {{ $p = Start-Process -FilePath {quoted(_powershell())} -Verb RunAs -Wait -PassThru -WindowStyle "
            "Hidden -ArgumentList '-NoProfile', '-NonInteractive', '-EncodedCommand', "
            f"'{encoded}'; $p.ExitCode }} catch {{ -1 }}")
        try:
            return int(output.strip().splitlines()[-1]) if code == 0 else -1
        except (ValueError, IndexError):
            return -1

    def schtasks(self, *args: str) -> int:
        try:
            return subprocess.run([_system32("schtasks.exe"), *args], capture_output=True,
                                  creationflags=subprocess.CREATE_NO_WINDOW).returncode
        except OSError:
            return -1

    def task(self, name: str = TASK) -> str | None:
        """The installed task's XML, or None when there is no such task."""
        try:
            result = subprocess.run([_system32("schtasks.exe"), "/Query", "/TN", f"\\{name}", "/XML"], capture_output=True,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError:
            return None
        if result.returncode != 0:
            return None
        # In the console's code page.
        return result.stdout.decode(_oem(), "replace")

    def register(self, xml: str, file: Path, name: str = TASK) -> bool:
        """Registers the task `name` from `xml`, kept in `file` (UTF-16, as its declaration
        says). Registering fires its registration trigger, which starts it."""
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(xml, encoding="utf-16")
        except OSError as error:
            raise Refused(f"can't write {file} ({error.strerror})") from None
        return self.schtasks("/Create", "/TN", f"\\{name}", "/XML", str(file), "/F") == 0

    def herdr_running(self, herdr: str) -> bool:
        """Whether `herdr status server` says its server runs."""
        try:
            result = subprocess.run([herdr, "status", "server", "--json"], capture_output=True, timeout=30,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            return bool(json.loads(result.stdout).get("running"))
        except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
            return False

    def stop(self, pids: list[int]) -> None:
        """Ends the processes and waits (up to 10 s) until they are gone and have let go of
        their files."""
        import psutil

        processes = []
        for pid in pids:
            with contextlib.suppress(psutil.Error):
                process = psutil.Process(pid)
                process.kill()
                processes.append(process)
        psutil.wait_procs(processes, timeout=10)

    def processes(self) -> list[Listener]:
        """Every process whose command line can be read."""
        import psutil

        found = []
        for process in psutil.process_iter(["pid", "name", "cmdline", "exe"]):
            command = process.info.get("cmdline")
            if not command:
                continue
            found.append(Listener(process.pid, process.info.get("name") or "", subprocess.list2cmdline(command),
                                  process.info.get("exe") or ""))
        return found

    def find_herdr(self) -> str | None:
        """Where herdr's installer puts the CLI, else herdr.exe on PATH. The installer links
        Programs\\Herdr\\bin to the current release, while PATH names one release's folder, so
        this one follows `herdr update`."""
        installed = self.paths.local / "Programs" / "Herdr" / "bin" / "herdr.exe"
        if installed.is_file():
            return str(installed)
        return shutil.which("herdr.exe")

    def bridge(self, *args: str, capture: bool = False) -> tuple[int, str]:
        """Runs the installed agent-bridge with no AGENT_BRIDGE_* in its environment, as the task
        would, in UTF-8 mode and away from a .env in the current folder."""
        environment = {name: value for name, value in os.environ.items() if not name.upper().startswith(PREFIX)}
        environment["PYTHONUTF8"] = "1"
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            captured = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT} if capture else {}
            result = subprocess.run([self.command(), *args], env=environment, cwd=tempfile.gettempdir(),
                                    encoding="utf-8", errors="replace", **captured)
        except OSError as error:
            raise Refused(f"can't run {self.command()} ({error.strerror})") from None
        return result.returncode, result.stdout or ""

    def herdr_integration(self, binary: str, config_dir: Path) -> tuple[int, str]:
        environment = {**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)}
        try:
            result = subprocess.run([binary, "integration", "install", "claude"], env=environment,
                                    stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace",
                                    timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as error:
            return flow.FAILED, str(error)
        return result.returncode, result.stdout + result.stderr

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def _output(*argv: str) -> str:
    try:
        return subprocess.run(argv, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout.decode(
            _oem(), "replace")
    except OSError:
        return ""


def _oem() -> str:
    """The console's code page, which Windows' own commands write in when piped."""
    try:
        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except AttributeError:
        return locale.getpreferredencoding(False)


def _system32(*parts: str) -> str:
    """Windows' own program, by its full path: by name alone, Windows would look in the current
    folder first."""
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows"), "System32", *parts))


def _powershell() -> str:
    return _system32("WindowsPowerShell", "v1.0", "powershell.exe")


def _session(pid: int) -> int | None:
    session = ctypes.c_ulong()
    if ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(session)):
        return session.value
    return None


# WTS_INFO_CLASS values.
WTS_USER_NAME, WTS_DOMAIN_NAME = 5, 7


def _session_info(session: int, info: int) -> str:
    from ctypes import wintypes

    buffer, size = wintypes.LPWSTR(), wintypes.DWORD()
    wts = ctypes.windll.wtsapi32
    # None: this server (WTS_CURRENT_SERVER_HANDLE).
    if not wts.WTSQuerySessionInformationW(None, session, info, ctypes.byref(buffer), ctypes.byref(size)):
        return ""
    try:
        return buffer.value or ""
    finally:
        wts.WTSFreeMemory(buffer)


def system() -> WindowsSystem:
    return WindowsSystem()
