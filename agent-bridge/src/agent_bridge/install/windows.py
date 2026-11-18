"""`agent-bridge setup` and `uninstall` on Windows: installs agent-bridge as a scheduled task of
the user who runs it, from the installed package, and enrolls it with the gateway.

The task starts at logon, in that user's session (next to their herdr server), with no window,
and again every minute while it isn't running. Setup adds a firewall rule for the port on
Private networks, the one step that asks for admin rights. A host set up with setup.ps1 from a
checkout is taken over: its settings, token and enrollment stay, and the task runs the package
from then on.
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
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree  # the user's own task, as schtasks exports it
from xml.sax.saxutils import escape

from agent_bridge.install.common import PREFIX, Refused

TASK = "agent-bridge"
RULE = "agent-bridge"
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
    fields = {
        "{{USER}}": user,
        "{{COMMAND}}": command,
        "{{ARGUMENTS}}": f'serve --log-file "{log}"',
        "{{WORKING_DIRECTORY}}": str(folder),
    }
    xml = importlib.resources.files(__package__).joinpath("agent-bridge.task.xml").read_text(encoding="utf-8")
    for field, value in fields.items():
        xml = xml.replace(field, escape(value, {'"': "&quot;"}))
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


def firewall_rule_commands(port: int) -> str:
    """Replaces the rule with one that allows TCP `port` in, on Private networks only."""
    return (f"Remove-NetFirewallRule -Name '{RULE}' -ErrorAction SilentlyContinue; "
            f"New-NetFirewallRule -Name '{RULE}' -DisplayName 'agent-bridge' -Description 'Lets mcp-gateway on "
            "the LAN reach agent-bridge. Added by agent-bridge setup.' -Direction Inbound -Action Allow "
            f"-Protocol TCP -LocalPort {port} -Profile Private | Out-Null")


def remove_rule_command() -> str:
    return f"Remove-NetFirewallRule -Name '{RULE}'"


def quoted(text: str) -> str:
    """`text` in single quotes as PowerShell reads them, for commands printed to copy."""
    return "'" + text.replace("'", "''") + "'"


# --- The real Windows ----------------------------------------------------------------


class WindowsSystem:
    """What setup asks of Windows; tests replace it."""

    def __init__(self) -> None:
        self.home = Path.home()
        self.paths = Paths(Path(os.environ.get("APPDATA") or self.home / "AppData" / "Roaming"),
                           Path(os.environ.get("LOCALAPPDATA") or self.home / "AppData" / "Local"))

    # --- Who and what runs setup

    def user(self) -> str:
        """This account's SID."""
        output = _output("whoami", "/user", "/fo", "csv", "/nh")
        return output.strip().rsplit(",", 1)[-1].strip().strip('"')

    def desktop_owner(self) -> str | None:
        """Whose desktop this session is (DOMAIN\\user), when it can be told; None otherwise.
        Another account than the one running setup is one typed in at a UAC prompt."""
        session = _session(os.getpid())
        owners = set()
        for process in _processes("explorer.exe"):
            if _session(process.pid) != session:
                continue
            with contextlib.suppress(Exception):
                owners.add(process.username())
        return owners.pop() if len(owners) == 1 else None

    def me(self) -> str:
        return f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}"

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

    def excluded_ranges(self) -> list[tuple[int, int]]:
        """The TCP ports Windows keeps for itself (Hyper-V, WSL and Docker reserve them), IPv4's
        and IPv6's: nothing can listen there although nothing does."""
        return [*excluded_ranges(_output("netsh", "int", "ipv4", "show", "excludedportrange", "protocol=tcp")),
                *excluded_ranges(_output("netsh", "int", "ipv6", "show", "excludedportrange", "protocol=tcp"))]

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

    def firewall_rule(self) -> tuple[bool, str, str, str, str, str] | None:
        """Setup's rule as (enabled, direction, action, profile, protocol, port), or None."""
        code, output = self.powershell(
            f"$r = Get-NetFirewallRule -Name '{RULE}' -ErrorAction SilentlyContinue; if ($r) {{ "
            "$f = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $r; @{ Enabled = [string]$r.Enabled; "
            "Direction = [string]$r.Direction; Action = [string]$r.Action; Profile = [string]$r.Profile; "
            "Protocol = [string]$f.Protocol; Port = [string]$f.LocalPort } | ConvertTo-Json -Compress }")
        try:
            rule = json.loads(output) if code == 0 and output.strip() else None
        except ValueError:
            return None
        if not rule:
            return None
        return (rule["Enabled"] == "True", rule["Direction"], rule["Action"], rule["Profile"], rule["Protocol"],
                rule["Port"])

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
            "try { $p = Start-Process -FilePath powershell.exe -Verb RunAs -Wait -PassThru -WindowStyle Hidden "
            f"-ArgumentList '-NoProfile', '-NonInteractive', '-EncodedCommand', '{encoded}'; $p.ExitCode }} "
            "catch { -1 }")
        try:
            return int(output.strip().splitlines()[-1]) if code == 0 else -1
        except (ValueError, IndexError):
            return -1

    def schtasks(self, *args: str) -> int:
        try:
            return subprocess.run(["schtasks", *args], capture_output=True,
                                  creationflags=subprocess.CREATE_NO_WINDOW).returncode
        except OSError:
            return -1

    def task(self) -> str | None:
        """The installed task's XML, or None when there is no task."""
        try:
            result = subprocess.run(["schtasks", "/Query", "/TN", f"\\{TASK}", "/XML"], capture_output=True,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError:
            return None
        if result.returncode != 0:
            return None
        # In the console's code page.
        return result.stdout.decode(_oem(), "replace")

    def register(self, xml: str, file: Path) -> bool:
        """Registers the task from `xml`, kept in `file` (UTF-16, as its declaration says).
        Registering fires its registration trigger, which starts it."""
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(xml, encoding="utf-16")
        except OSError as error:
            raise Refused(f"can't write {file} ({error.strerror})") from None
        return self.schtasks("/Create", "/TN", f"\\{TASK}", "/XML", str(file), "/F") == 0

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
        """Every process whose command line can be read and might be agent-bridge serving."""
        import psutil

        found = []
        for process in psutil.process_iter(["pid", "name", "cmdline", "exe"]):
            command = process.info.get("cmdline")
            if not command:
                continue
            line = subprocess.list2cmdline(command)
            if "serve" in line or "agent_bridge" in line:
                found.append(Listener(process.pid, process.info.get("name") or "", line, process.info.get("exe") or ""))
        return found

    def find_herdr(self) -> str | None:
        """Where herdr's installer puts the CLI, else herdr.exe on PATH."""
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


def _powershell() -> str:
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0"
               / "powershell.exe")


def _session(pid: int) -> int | None:
    session = ctypes.c_ulong()
    if ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(session)):
        return session.value
    return None


def _processes(name: str) -> list:
    import psutil

    return [process for process in psutil.process_iter(["name"]) if (process.info.get("name") or "").lower() == name]
