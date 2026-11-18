"""`agent-bridge setup` and `uninstall` on Windows, against a fake system: the scheduled task, the
firewall rule, stopping the bridge being replaced, and taking over a host set up with
setup.ps1. What every platform does the same way (questions, re-runs, renames, rotation) is
flow's, tested in test_install_macos.py. Never touches Task Scheduler, the firewall, real
processes, ports or a gateway, so it runs on any platform."""

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from dotenv import dotenv_values

from agent_bridge import cli
from agent_bridge.install import common, windows
from agent_bridge.install.common import Refused
from agent_bridge.install.windows import Listener

ENROLLMENT = "crm1@10.0.0.1:8766#" + "a" * 24
SID = "S-1-5-21-1-2-3-1001"
ME = "PC\\me"
RULE = (True, "Inbound", "Allow", "Private", "TCP", "8765")
# The task's own processes: agent-bridgew.exe, and the python.exe below it that listens.
LAUNCHER, SERVER = 4241, 4242


@dataclass
class FakeWindows:
    """What setup asks of Windows. Processes are Listeners with a port (0: not listening);
    registering the task starts it, as its registration trigger does, unless it runs already.
    `enroll_codes` are the exit codes `agent-bridge enroll` gives, in turn (0 once they run
    out)."""

    home: Path
    paths: windows.Paths
    herdr: Path | None
    bin: Path
    lan: str | None = "192.168.1.20"
    addresses: tuple[str, ...] = ("192.168.1.20",)
    desktop: str | None = ME
    lasting: dict[str, list[str]] = field(default_factory=dict)
    excluded: list[tuple[int, int]] = field(default_factory=list)
    processes_: list[tuple[Listener, int]] = field(default_factory=list)
    # PIDs that don't end when stopped.
    unkillable: set[int] = field(default_factory=set)
    task_xml: str | None = None
    task_on: bool = True
    register_fails: bool = False
    delete_fails: bool = False
    rule: tuple | None = None
    category: str = "Private"
    uac: bool = True
    block: bool = False
    enroll_codes: list[int] = field(default_factory=list)
    unenroll_code: int = 0
    gateway_token: str | None = None
    gateway_name: str = ""
    calls: list[tuple[str, ...]] = field(default_factory=list)

    # --- flow.System

    def is_root(self) -> bool:
        return False

    def in_checkout(self) -> bool:
        return False

    def in_uv_cache(self) -> bool:
        return False

    def hostname(self) -> str:
        return "DELL-KG"

    def command(self) -> str:
        return str(self.bin / "agent-bridge.exe")

    def lan_address(self) -> str | None:
        return self.lan

    def has_address(self, address: str) -> bool:
        return address in self.addresses

    def port_holder(self, port: int) -> str | None:
        return windows.holder(self.listeners(port), self.excluded, port)

    def serves(self, port: int) -> bool:
        return windows.serving(self.listeners(port))

    def find_herdr(self) -> str | None:
        return str(self.herdr) if self.herdr else None

    def bridge(self, *args: str, capture: bool = False) -> tuple[int, str]:
        self.calls.append(("agent-bridge", *args))
        if args[0] == "check":
            gateway = self.gateway()
            if gateway is None:
                return 0, "ok    agent-bridge answers\n--    not enrolled with a gateway\n"
            if self.gateway_token != self.env("TOKEN"):
                return 1, f"FAIL  the gateway at {gateway['url']} has this host (That isn't this agent host's token.)\n"
            return 0, f"ok    the gateway at {gateway['url']} has this host as {self.gateway_name}\n"
        if args[0] == "unenroll":
            if self.unenroll_code == 0 and self.state_file.exists():
                self.state_file.write_text(json.dumps({"version": 1, "bridge_id": "b1"}))
                self.gateway_token = None
            return self.unenroll_code, ""
        code = self.enroll_codes.pop(0) if self.enroll_codes else 0
        if code == 0:
            self.gateway_token = self.env("TOKEN")
            self.gateway_name = self.env("NAME") or ""
            self.save_gateway(self.gateway_name)
        return code, ""

    def sleep(self, seconds: float) -> None:
        pass

    # --- windows.System

    def user(self) -> str:
        return SID

    def me(self) -> str:
        return ME

    def desktop_owner(self) -> str | None:
        return self.desktop

    def service_command(self) -> str:
        return str(self.bin / "agent-bridgew.exe")

    def lasting_settings(self) -> dict[str, list[str]]:
        return self.lasting

    def listeners(self, port: int) -> list[Listener]:
        return [process for process, listening in self.processes_ if listening == port]

    def processes(self) -> list[Listener]:
        return [process for process, _ in self.processes_ if process.command_line]

    def stop(self, pids: list[int]) -> None:
        self.calls.append(("stop", *map(str, sorted(pids))))
        self.processes_ = [(process, port) for process, port in self.processes_
                           if process.pid not in pids or process.pid in self.unkillable]

    def network(self, address: str) -> tuple[int, str, str] | None:
        return (16, self.category, "Home") if address in self.addresses else None

    def firewall_rule(self) -> tuple | None:
        return self.rule

    def blocked(self, program: str) -> bool:
        return self.block

    def elevated(self, commands: list[str]) -> int:
        self.calls.append(("elevated", *commands))
        if not self.uac:
            return -1
        for command in commands:
            if "New-NetFirewallRule" in command:
                port = re.search(r"-LocalPort (\d+)", command).group(1)
                self.rule = (True, "Inbound", "Allow", "Private", "TCP", port)
            elif "Remove-NetFirewallRule" in command:
                self.rule = None
            elif "Set-NetConnectionProfile" in command:
                self.category = "Private"
        return 0

    def schtasks(self, *args: str) -> int:
        self.calls.append(("schtasks", *args))
        if self.task_xml is None:
            return 1
        if args[0] == "/Change":
            self.task_on = args[-1] == "/ENABLE"
        elif args[0] == "/End":
            # Ends the task's own processes; what a run.cmd started under cmd.exe runs on.
            self.stop([LAUNCHER, SERVER])
        elif args[0] == "/Delete":
            if self.delete_fails:
                return 1
            self.task_xml = None
        return 0

    def task(self) -> str | None:
        if self.task_xml is None:
            return None
        return self.task_xml if self.task_on else self.task_xml.replace(
            "<Enabled>true</Enabled>\n    <Hidden>", "<Enabled>false</Enabled>\n    <Hidden>")

    def register(self, xml: str, file: Path) -> bool:
        self.calls.append(("register", windows.Task.parse(xml).command))
        if self.register_fails:
            return False
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(xml, encoding="utf-16")
        self.task_xml, self.task_on = xml, True
        # MultipleInstancesPolicy IgnoreNew.
        if any(process.pid == SERVER for process, _ in self.processes_):
            return True
        command = windows.Task.parse(xml).command
        line = f'"{command}" serve --log-file "{self.paths.log}"'
        port = int(self.env("PORT") or 8765)
        self.processes_ += [(Listener(LAUNCHER, "agent-bridgew.exe", line, command), 0),
                            (Listener(SERVER, "python.exe", rf'"C:\Python\python.exe" {line}', r"C:\Python\python.exe"),
                             port)]
        # The service makes its state file when it starts.
        if not self.state_file.exists():
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({"version": 1, "bridge_id": "b1"}))
        return True

    # --- For the tests

    @property
    def env_file(self) -> Path:
        return self.paths.env_file

    @property
    def state_file(self) -> Path:
        return self.paths.state_file

    def env(self, key: str) -> str | None:
        return dotenv_values(self.env_file).get(f"AGENT_BRIDGE_{key}") if self.env_file.exists() else None

    def gateway(self) -> dict | None:
        return json.loads(self.state_file.read_text()).get("gateway") if self.state_file.exists() else None

    def save_gateway(self, name: str) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(
            {"version": 1, "bridge_id": "b1",
             "gateway": {"url": "http://10.0.0.1:8766", "gateway_id": "g1", "name": name}}))

    def ran(self, kind: str) -> list[tuple[str, ...]]:
        return [call[1:] for call in self.calls if call[0] == kind]

    def pids(self) -> set[int]:
        return {process.pid for process, _ in self.processes_}


@pytest.fixture
def host(tmp_path: Path, set_home: Callable[[Path], None]) -> FakeWindows:
    home = tmp_path / "home"
    (home / "Projects").mkdir(parents=True)
    set_home(home)
    herdr = tmp_path / "Herdr" / "herdr.exe"
    tools = tmp_path / "bin"
    for program in (herdr, tools / "agent-bridge.exe", tools / "agent-bridgew.exe"):
        program.parent.mkdir(exist_ok=True)
        program.write_text("")
        program.chmod(0o755)
    return FakeWindows(home=home, paths=windows.Paths(home / "AppData" / "Roaming", home / "AppData" / "Local"),
                       herdr=herdr, bin=tools)


def every_flag(host: FakeWindows, **changes: str | None) -> list[str]:
    flags = {
        "--roots": str(host.home / "Projects"),
        "--name": "dell",
        "--host": "192.168.1.20",
        "--port": "8765",
        "--herdr-bin": str(host.herdr),
        "--claude-config-dir": "",
        "--trust-root": "",
        "--enrollment": ENROLLMENT,
    }
    flags.update({f"--{key.replace('_', '-')}": value for key, value in changes.items()})
    return [part for flag, value in flags.items() if value is not None for part in (flag, value)]


@dataclass
class Run:
    code: int
    out: str
    asked: list[str]


def setup(host: FakeWindows, capsys: pytest.CaptureFixture, *flags: str, answers: tuple[str, ...] = ()) -> Run:
    lines = iter(answers)
    options = windows.Options.from_args(cli._parser().parse_args(["setup", *flags]))
    code = windows.setup(options, host, lambda: next(lines, None))
    out, err = capsys.readouterr()
    asked = [line for line in err.split("]: ") if "[" in line]
    return Run(code, out + err, [line.rsplit("\n", 1)[-1] for line in asked])


def installed_by_setup_ps1(host: FakeWindows, checkout: Path) -> str:
    """A host as agent-bridge/deploy/windows/setup.ps1 left it: its env file, its files, the
    firewall rule, its task running the checkout's agent-bridgew.exe, and an enrollment.
    Returns the token."""
    token = "t" * 64
    host.env_file.parent.mkdir(parents=True)
    host.env_file.write_text(
        "# agent-bridge settings, written by agent-bridge\\deploy\\windows\\setup.ps1. Lines it\n"
        f"AGENT_BRIDGE_TOKEN={token}\n"
        f"AGENT_BRIDGE_ALLOWED_ROOTS={host.home / 'Projects'}\n"
        "AGENT_BRIDGE_HOST=192.168.1.20\n"
        "AGENT_BRIDGE_PORT=8765\n"
        "AGENT_BRIDGE_NAME=dell\n"
        f"AGENT_BRIDGE_HERDR_BIN={host.herdr}\n"
        "AGENT_BRIDGE_LOG_LEVEL=debug\n", encoding="utf-8")
    host.paths.state.mkdir(parents=True)
    for leftover in host.paths.leftovers():
        leftover.write_text(f"{checkout}\\.venv\\Scripts\\agent-bridge.exe\n")
    host.paths.check_folder().mkdir()
    old = f"{checkout}\\.venv\\Scripts\\agent-bridgew.exe"
    host.task_xml = windows.task_xml(SID, old, host.paths.log, host.paths.state)
    line = f'"{old}" serve --log-file "{host.paths.log}"'
    host.processes_ = [(Listener(LAUNCHER, "agent-bridgew.exe", line, old), 0),
                       (Listener(SERVER, "pythonw.exe", rf"C:\Python\pythonw.exe {line}", r"C:\Python\pythonw.exe"), 8765)]
    host.rule = RULE
    host.gateway_token, host.gateway_name = token, "dell"
    host.save_gateway("dell")
    return token


# --- A fresh host ---------------------------------------------------------------------


def test_installs_the_task_adds_the_firewall_rule_and_enrolls(host: FakeWindows, capsys) -> None:
    run = setup(host, capsys, *every_flag(host))

    assert run.code == 0, run.out
    assert run.asked == []
    assert host.env("NAME") == "dell"
    assert host.env("ALLOWED_ROOTS") == str(host.home / "Projects")
    assert len(host.env("TOKEN") or "") == 64
    # The task runs the windowless agent-bridge beside the one that was run, as you.
    assert host.ran("register") == [(host.service_command(),)]
    task = host.paths.task_file.read_text(encoding="utf-16")
    assert f"<UserId>{SID}</UserId>" in task
    assert f"serve --log-file &quot;{host.paths.log}&quot;" in task
    # One UAC prompt, for the rule only.
    assert host.ran("elevated") == [(windows.firewall_rule_commands(8765),)]
    assert host.rule == RULE
    assert host.gateway()["name"] == "dell"
    assert not host.paths.disabled_marker.exists()
    assert "Done. agent-bridge listens on http://192.168.1.20:8765 and starts at every logon." in run.out
    assert "enrolled with the gateway at http://10.0.0.1:8766 as dell" in run.out


def test_asks_for_what_is_missing_with_the_computer_name_as_the_default(host: FakeWindows, capsys) -> None:
    run = setup(host, capsys, answers=("", "", "-", ENROLLMENT))

    assert run.code == 0, run.out
    assert any("Folders agents may start in, " in question for question in run.asked)
    assert host.env("NAME") == "dell-kg"
    assert host.gateway()["name"] == "dell-kg"


def test_the_default_name_starts_with_pc_when_the_computer_name_doesnt_start_with_a_letter(
        host: FakeWindows, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "hostname", lambda: "7-DESKTOP")

    run = setup(host, capsys, *every_flag(host, name=None))

    assert run.code == 0, run.out
    assert host.env("NAME") == "pc-7-desktop"


def test_another_account_than_the_desktops_is_refused_before_anything(host: FakeWindows, capsys) -> None:
    host.desktop = "PC\\admin"

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 1
    assert "run this as PC\\admin, whose desktop this is, not as PC\\me" in run.out
    assert host.calls == []
    assert not host.env_file.exists()


def test_lasting_settings_in_your_environment_are_refused_with_how_to_remove_them(host: FakeWindows, capsys) -> None:
    host.lasting = {"user": ["AGENT_BRIDGE_PORT"]}

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 1
    assert "AGENT_BRIDGE_PORT set in your user environment would win over" in run.out
    assert "[Environment]::SetEnvironmentVariable('AGENT_BRIDGE_PORT', $null, 'User')" in run.out
    assert not host.env_file.exists()
    assert host.ran("register") == []


def test_without_the_windowless_agent_bridge_nothing_changes(host: FakeWindows, capsys) -> None:
    (host.bin / "agent-bridgew.exe").unlink()

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 1
    assert "agent-bridgew.exe, the windowless agent-bridge the task runs, isn't there" in run.out
    assert not host.env_file.exists()


def test_a_path_task_scheduler_would_expand_is_refused_before_anything_is_written(host: FakeWindows, capsys,
                                                                                   tmp_path: Path) -> None:
    host.bin = tmp_path / "%TEMP%"
    host.bin.mkdir()
    (host.bin / "agent-bridgew.exe").write_text("")

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 1
    assert "path with % in it" in run.out
    assert not host.env_file.exists()


def test_a_port_windows_keeps_for_itself_is_asked_again(host: FakeWindows, capsys) -> None:
    host.excluded = [(8765, 8765)]

    run = setup(host, capsys, *every_flag(host, port=None), answers=("",))

    assert run.code == 0, run.out
    assert "Port 8765 is in use by Windows, which keeps it for itself" in run.out
    assert host.env("PORT") == "8766"
    assert host.rule == (*RULE[:5], "8766")


def test_the_port_another_program_holds_is_refused(host: FakeWindows, capsys) -> None:
    host.processes_ = [(Listener(7, "python.exe", "python.exe -m http.server 8765", r"C:\Python\python.exe"), 8765)]

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 1
    assert "Port 8765 is in use by PID 7: python.exe -m http.server 8765" in run.out


# --- The firewall ---------------------------------------------------------------------


def test_a_rule_already_there_asks_for_no_admin_rights(host: FakeWindows, capsys) -> None:
    host.rule = RULE

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 0, run.out
    assert host.ran("elevated") == []
    assert "The rule is already there." in run.out


def test_a_rule_for_another_port_is_replaced(host: FakeWindows, capsys) -> None:
    host.rule = (*RULE[:5], "9000")

    assert setup(host, capsys, *every_flag(host)).code == 0

    assert host.rule == RULE


def test_a_public_network_is_made_private_when_you_say_so(host: FakeWindows, capsys) -> None:
    host.category = "Public"

    run = setup(host, capsys, *every_flag(host), answers=("y",))

    assert run.code == 0, run.out
    assert "Windows treats the network 192.168.1.20 is on (Home) as Public" in run.out
    # Both in the one UAC prompt.
    assert host.ran("elevated") == [(windows.firewall_rule_commands(8765),
                                     "Set-NetConnectionProfile -InterfaceIndex 16 -NetworkCategory Private")]
    assert host.category == "Private"


def test_a_public_network_left_public_is_said_at_the_end(host: FakeWindows, capsys) -> None:
    host.category = "Public"

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 0, run.out
    assert host.category == "Public"
    assert "The network Home is Public, so the gateway can't reach agent-bridge" in run.out.rsplit("Done.", 1)[1]


def test_a_declined_uac_prompt_still_installs_and_says_what_to_run(host: FakeWindows, capsys) -> None:
    host.uac = False

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 0, run.out
    assert host.rule is None
    assert f"  {windows.firewall_rule_commands(8765)}" in run.out
    assert "The firewall step wasn't done" in run.out
    assert host.ran("register")


def test_a_rule_that_blocks_python_is_said_at_the_end(host: FakeWindows, capsys) -> None:
    host.block = True

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 0, run.out
    assert r"a firewall rule blocks C:\Python\python.exe" in run.out
    assert "Get-NetFirewallApplicationFilter -Program 'C:\\Python\\python.exe'" in run.out


# --- Re-runs and taking over setup.ps1's install ----------------------------------------


def test_a_rerun_asks_nothing_keeps_the_token_and_restarts_the_task(host: FakeWindows, capsys) -> None:
    setup(host, capsys, *every_flag(host))
    token = host.env("TOKEN")
    host.calls.clear()

    run = setup(host, capsys)

    assert run.code == 0, run.out
    assert run.asked == []
    assert host.env("TOKEN") == token
    assert ("enroll", ENROLLMENT) not in host.ran("agent-bridge")
    # The running one is stopped first: the task would ignore a start while it runs.
    assert host.ran("schtasks")[:2] == [("/Change", "/TN", "\\agent-bridge", "/DISABLE"),
                                        ("/End", "/TN", "\\agent-bridge")]
    assert host.ran("register") == [(host.service_command(),)]
    assert host.ran("elevated") == []
    assert host.task() is not None and windows.Task.parse(host.task()).enabled
    assert "enrolled with the gateway at http://10.0.0.1:8766 as dell" in run.out


def test_a_setup_ps1_install_is_taken_over_without_questions(host: FakeWindows, capsys, tmp_path: Path) -> None:
    checkout = tmp_path / "claude-remote-mcp"
    token = installed_by_setup_ps1(host, checkout)
    # setup.ps1's older task ran run.cmd under cmd.exe, which ending the task left running.
    run_cmd = Listener(31, "cmd.exe", f'cmd.exe /c "{host.paths.state / "run.cmd"}"', r"C:\Windows\cmd.exe")
    host.processes_.append((run_cmd, 0))

    run = setup(host, capsys)

    assert run.code == 0, run.out
    assert run.asked == []
    settings = dotenv_values(host.env_file)
    assert settings["AGENT_BRIDGE_TOKEN"] == token
    assert settings["AGENT_BRIDGE_NAME"] == "dell"
    assert settings["AGENT_BRIDGE_LOG_LEVEL"] == "debug"
    # The task runs the package from now on; the checkout's bridge is gone.
    assert windows.Task.parse(host.task()).command == host.service_command()
    assert all(str(checkout) not in process.command_line for process, _ in host.processes_)
    assert 31 not in host.pids()
    for leftover in (*host.paths.leftovers(), host.paths.check_folder()):
        assert not leftover.exists()
    assert host.ran("elevated") == []
    assert ("enroll", ENROLLMENT) not in host.ran("agent-bridge")
    assert "enrolled with the gateway at http://10.0.0.1:8766 as dell" in run.out


def test_a_bridge_started_by_hand_on_the_port_is_stopped(host: FakeWindows, capsys) -> None:
    by_hand = Listener(77, "python.exe", r"C:\Python\python.exe -m agent_bridge.cli serve", r"C:\Python\python.exe")
    host.processes_ = [(by_hand, 8765)]

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 0, run.out
    assert "Stopping the agent-bridge on port 8765 (PID 77)" in run.out
    assert 77 not in host.pids()
    assert SERVER in host.pids()


def test_a_task_that_cant_be_registered_puts_the_old_one_back(host: FakeWindows, capsys, tmp_path: Path) -> None:
    installed_by_setup_ps1(host, tmp_path / "claude-remote-mcp")
    before = host.task_xml
    host.register_fails = True

    run = setup(host, capsys)

    assert run.code == 1
    assert "schtasks couldn't register the task agent-bridge" in run.out
    assert host.ran("register")[-1] == (windows.Task.parse(before).command,)
    host.register_fails = False


def test_a_setup_stopped_after_the_bridge_puts_the_task_back_on(host: FakeWindows, capsys, tmp_path: Path) -> None:
    installed_by_setup_ps1(host, tmp_path / "claude-remote-mcp")
    before = host.task_xml
    tries = iter([False, True])
    real = host.register

    def register(xml: str, file: Path) -> bool:
        if next(tries):
            return real(xml, file)
        host.calls.append(("register", windows.Task.parse(xml).command))
        return False

    host.register = register  # type: ignore[method-assign]

    run = setup(host, capsys)

    assert run.code == 1
    # Back as it was, enabled and started, and setup's mark is gone.
    assert host.task_xml == before
    assert host.task_on
    assert SERVER in host.pids()
    assert not host.paths.disabled_marker.exists()


def test_a_task_you_turned_off_stays_off_when_setup_stops(host: FakeWindows, capsys, tmp_path: Path) -> None:
    installed_by_setup_ps1(host, tmp_path / "claude-remote-mcp")
    host.task_on = False
    host.register_fails = True

    assert setup(host, capsys).code == 1

    assert not host.task_on
    assert len(host.ran("register")) == 1


# --- Uninstall ------------------------------------------------------------------------


def test_uninstall_leaves_the_gateway_then_removes_the_task_rule_and_files(host: FakeWindows, capsys) -> None:
    setup(host, capsys, *every_flag(host))
    host.paths.log.write_text("log")
    host.paths.log.with_name("agent-bridge.log.1").write_text("old log")
    host.calls.clear()

    code = windows.uninstall(host)
    out = capsys.readouterr()

    assert code == 0, out
    assert host.ran("agent-bridge") == [("unenroll",)]
    assert host.gateway_token is None
    assert host.task_xml is None
    assert host.pids() == set()
    assert host.ran("elevated") == [(windows.remove_rule_command(),)]
    assert host.rule is None
    for path in (host.env_file, host.state_file, host.paths.log, host.paths.task_file, host.paths.settings,
                 host.paths.state):
        assert not path.exists(), path


def test_uninstall_takes_a_setup_ps1_install_away_too(host: FakeWindows, capsys, tmp_path: Path) -> None:
    installed_by_setup_ps1(host, tmp_path / "claude-remote-mcp")

    assert windows.uninstall(host) == 0

    assert host.task_xml is None
    assert host.pids() == set()
    assert host.rule is None
    assert not host.paths.state.exists()
    assert not host.env_file.exists()


def test_uninstall_says_how_to_remove_the_rule_when_the_uac_prompt_is_declined(host: FakeWindows, capsys) -> None:
    setup(host, capsys, *every_flag(host))
    host.uac = False

    assert windows.uninstall(host) == 0

    assert f"In a PowerShell run as administrator:\n  {windows.remove_rule_command()}" in capsys.readouterr().err


def test_uninstall_puts_the_task_back_when_it_cant_remove_it(host: FakeWindows, capsys) -> None:
    setup(host, capsys, *every_flag(host))
    host.delete_fails = True

    assert windows.uninstall(host) == 1

    assert "couldn't remove the scheduled task agent-bridge; it is left as it was" in capsys.readouterr().err
    assert host.task_on
    assert SERVER in host.pids()
    assert host.env_file.exists()


def test_uninstall_as_another_account_changes_nothing(host: FakeWindows, capsys) -> None:
    setup(host, capsys, *every_flag(host))
    host.desktop = "PC\\admin"
    host.calls.clear()

    assert windows.uninstall(host) == 1

    assert host.calls == []
    assert host.env_file.exists()


# --- The pieces -----------------------------------------------------------------------


def test_task_runs_the_windowless_bridge_with_its_log() -> None:
    xml = windows.task_xml(SID, r"C:\Users\Me & You\.local\bin\agent-bridgew.exe",
                           Path(r"C:\Users\Me & You\AppData\Local\agent-bridge\agent-bridge.log"),
                           Path(r"C:\Users\Me & You\AppData\Local\agent-bridge"))
    assert "{{" not in xml
    assert f"<UserId>{SID}</UserId>" in xml
    assert r"<Command>C:\Users\Me &amp; You\.local\bin\agent-bridgew.exe</Command>" in xml
    assert (r"<Arguments>serve --log-file &quot;C:\Users\Me &amp; You\AppData\Local\agent-bridge\agent-bridge.log"
            "&quot;</Arguments>") in xml
    task = windows.Task.parse(xml)
    assert task == windows.Task(r"C:\Users\Me & You\.local\bin\agent-bridgew.exe", True)


def test_task_refuses_a_path_task_scheduler_would_expand() -> None:
    with pytest.raises(Refused, match="%"):
        windows.task_xml(SID, r"C:\%USERNAME%\agent-bridgew.exe", Path(r"C:\log"), Path("C:\\"))


def test_a_disabled_task_reads_as_disabled_whatever_its_triggers_say() -> None:
    xml = windows.task_xml(SID, r"C:\b\agent-bridgew.exe", Path(r"C:\log"), Path("C:\\"))
    disabled = xml.replace("<Enabled>true</Enabled>\n    <Hidden>", "<Enabled>false</Enabled>\n    <Hidden>")
    assert disabled != xml
    assert windows.Task.parse(disabled).enabled is False
    # The triggers' Enabled stay true.
    assert disabled.count("<Enabled>true</Enabled>") == 2


def test_a_task_that_cannot_be_read_has_no_command() -> None:
    assert windows.Task.parse("not xml") == windows.Task("", True)


@pytest.mark.parametrize(("line", "known", "expected"), [
    (r'"C:\Users\me\.local\bin\agent-bridgew.exe" serve --log-file x', (), True),
    (r"C:\Python\pythonw.exe C:\Users\me\.local\bin\agent-bridgew.exe serve --log-file x", (), True),
    (r"C:\repo\.venv\Scripts\agent-bridge.exe serve", (), True),
    (r"C:\Python\python.exe -m agent_bridge.cli serve", (), True),
    (r"C:\repo\.venv\Scripts\uvicorn.exe agent_bridge.app:main --host 1.2.3.4", (), True),
    (r"C:\tools\bridge.exe serve", (r"C:\tools\bridge.exe",), True),
    (r"C:\tools\bridge.exe check", (r"C:\tools\bridge.exe",), False),
    (r"C:\Users\me\.local\bin\agent-bridge.exe check", (), False),
    (r"code.exe C:\repo\agent_bridge\serve.py", (), False),
    (r"C:\tools\my-agent-bridge.exe serve", (), False),
    ("agent-bridge serve", (), True),
    ("", (), False),
])
def test_is_bridge(line: str, known: tuple[str, ...], expected: bool) -> None:
    assert windows.is_bridge(line, known) is expected


def test_the_holder_is_another_program_before_a_bridge_beside_it() -> None:
    bridge = Listener(1, "python.exe", "agent-bridgew.exe serve", "")
    other = Listener(2, "nginx.exe", "nginx.exe", "")
    assert windows.holder([bridge, other], [], 8765) == "PID 2: nginx.exe"
    assert windows.serving([bridge]) and not windows.serving([bridge, other]) and not windows.serving([])
    assert windows.holder([Listener(4, "System", "", "")], [], 445) == (
        "PID 4 (System, another user's or an elevated program)")
    assert windows.holder([], [(50000, 50059)], 50001).startswith("Windows, which keeps it")
    assert windows.holder([], [(50000, 50059)], 8765) is None


def test_excluded_ranges_from_netsh() -> None:
    netsh = """
Protocol tcp Port Exclusion Ranges

Start Port    End Port
----------    --------
      5357        5357
     50000       50059     *

* - Administered port exclusions.
"""
    assert windows.excluded_ranges(netsh) == [(5357, 5357), (50000, 50059)]


def test_service_command_is_beside_the_shim_uv_ran(tmp_path: Path) -> None:
    # uv's shim passes its own path without .exe.
    assert windows.service_command(str(tmp_path / "bin" / "agent-bridge")) == str(
        tmp_path / "bin" / "agent-bridgew.exe")


def test_bridge_command_gets_the_exe_uv_left_off(tmp_path: Path) -> None:
    (tmp_path / "agent-bridge.exe").write_text("")
    assert windows.bridge_command(str(tmp_path / "agent-bridge")) == str(tmp_path / "agent-bridge.exe")
    assert windows.bridge_command(str(tmp_path / "other")) == str(tmp_path / "other")


def test_quoted_for_powershell() -> None:
    assert windows.quoted("C:\\it's") == "'C:\\it''s'"


def test_the_cli_runs_the_windows_setup_on_windows(monkeypatch: pytest.MonkeyPatch, host: FakeWindows, capsys) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(windows, "system", lambda: host)
    monkeypatch.setattr(sys, "stdin", type("Stdin", (), {"readline": lambda self: ""})())

    assert cli.main(["setup", *every_flag(host)]) == 0

    assert host.task_xml is not None
    assert common.PREFIX + "TOKEN" in dotenv_values(host.env_file)
