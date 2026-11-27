"""`agent-bridge setup` and `uninstall` on Windows, against a fake system: the scheduled task, the
firewall rule, stopping the bridge being replaced, and taking over a host set up with
setup.ps1. What every platform does the same way (questions, re-runs, renames, rotation) is
flow's, tested in test_install_macos.py. Never touches Task Scheduler, the firewall, real
processes, ports or a gateway, so it runs on any platform."""

import json
import re
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from dotenv import dotenv_values

from agent_bridge import cli
from agent_bridge.install import common, herdr_config, windows
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
    out). Registering the herdr task starts herdr's server, unless `herdr_starts` is off."""

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
    disable_fails: bool = False
    delete_fails: bool = False
    console: list[str] = field(default_factory=list)
    rule: tuple | None = None
    category: str = "Private"
    uac: bool = True
    # Bits for the elevated commands that fail, as elevated() returns them.
    elevated_fails: int = 0
    block: bool = False
    enroll_codes: list[int] = field(default_factory=list)
    unenroll_code: int = 0
    gateway_token: str | None = None
    gateway_name: str = ""
    calls: list[tuple[str, ...]] = field(default_factory=list)
    herdr_task: str | None = None
    herdr_up: bool = False
    herdr_starts: bool = True
    herdr_register_fails: bool = False
    herdr_delete_fails: bool = False
    # What `herdr integration install claude` exits with.
    integration_code: int = 0

    # --- flow.System

    def herdr_integration(self, binary: str, config_dir: Path) -> tuple[int, str]:
        self.calls.append(("herdr integration", binary, str(config_dir)))
        return self.integration_code, "can't write settings.json" if self.integration_code else ""

    def is_root(self) -> bool:
        return False

    def in_checkout(self) -> bool:
        return False

    def in_uv_cache(self) -> bool:
        return False

    def hostname(self) -> str:
        return "WINDOWS-1"

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

    def console_programs(self) -> list[str]:
        return self.console

    def elevated(self, commands: list[str]) -> int:
        self.calls.append(("elevated", *commands))
        if not self.uac:
            return -1
        for i, command in enumerate(commands):
            if self.elevated_fails & (1 << i):
                continue
            if "New-NetFirewallRule" in command:
                port = re.search(r"-LocalPort (\d+)", command).group(1)
                self.rule = (True, "Inbound", "Allow", "Private", "TCP", port)
            elif "Remove-NetFirewallRule" in command:
                self.rule = None
            elif "Set-NetConnectionProfile" in command:
                self.category = "Private"
        return self.elevated_fails

    def schtasks(self, *args: str) -> int:
        self.calls.append(("schtasks", *args))
        if args[2] == f"\\{windows.HERDR_TASK}":
            # Deleting it leaves herdr's server running.
            if args[0] != "/Delete" or self.herdr_task is None or self.herdr_delete_fails:
                return 1
            self.herdr_task = None
            return 0
        if self.task_xml is None:
            return 1
        if args[0] == "/Change":
            if args[-1] == "/DISABLE" and self.disable_fails:
                return 1
            self.task_on = args[-1] == "/ENABLE"
        elif args[0] == "/End":
            # Ends the task's own processes; what a run.cmd started under cmd.exe runs on.
            self.stop([LAUNCHER, SERVER])
        elif args[0] == "/Delete":
            if self.delete_fails:
                return 1
            self.task_xml = None
        return 0

    def task(self, name: str = windows.TASK) -> str | None:
        if name == windows.HERDR_TASK:
            return self.herdr_task
        if self.task_xml is None:
            return None
        return self.task_xml if self.task_on else self.task_xml.replace(
            "<Enabled>true</Enabled>\n    <Hidden>", "<Enabled>false</Enabled>\n    <Hidden>")

    def herdr_running(self, herdr: str) -> bool:
        self.calls.append(("herdr status", herdr))
        return self.herdr_up

    def register(self, xml: str, file: Path, name: str = windows.TASK) -> bool:
        if name == windows.HERDR_TASK:
            self.calls.append(("register herdr", xml))
            if self.herdr_register_fails:
                return False
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(xml, encoding="utf-16")
            # Its registration trigger runs start-herdr.
            self.herdr_task, self.herdr_up = xml, self.herdr_up or self.herdr_starts
            return True
        self.calls.append(("register", windows.Task.parse(xml).command))
        if self.register_fails:
            return False
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(xml, encoding="utf-16")
        # Registered as its XML has it; a disabled one starts nothing.
        self.task_xml, self.task_on = xml, windows.Task.parse(xml).enabled
        # MultipleInstancesPolicy IgnoreNew.
        if not self.task_on or any(process.pid == SERVER for process, _ in self.processes_):
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
        "--name": "windows",
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
        "AGENT_BRIDGE_NAME=windows\n"
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
    host.gateway_token, host.gateway_name = token, "windows"
    host.save_gateway("windows")
    return token


# --- A fresh host ---------------------------------------------------------------------


def test_installs_the_task_adds_the_firewall_rule_and_enrolls(host: FakeWindows, capsys) -> None:
    run = setup(host, capsys, *every_flag(host))

    assert run.code == 0, run.out
    assert run.asked == []
    assert host.env("NAME") == "windows"
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
    assert host.gateway()["name"] == "windows"
    assert not host.paths.disabled_marker.exists()
    assert "Done. agent-bridge listens on http://192.168.1.20:8765 and starts at every logon." in run.out
    assert "enrolled with the gateway at http://10.0.0.1:8766 as windows" in run.out


def test_asks_for_what_is_missing_with_the_computer_name_as_the_default(host: FakeWindows, capsys) -> None:
    run = setup(host, capsys, answers=("", "", "-", ENROLLMENT))

    assert run.code == 0, run.out
    assert any("Folders agents may start in, " in question for question in run.asked)
    assert host.env("NAME") == "windows-1"
    assert host.gateway()["name"] == "windows-1"


def test_the_default_name_starts_with_pc_when_the_computer_name_doesnt_start_with_a_letter(
        host: FakeWindows, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "hostname", lambda: "7-DESKTOP")

    run = setup(host, capsys, *every_flag(host, name=None))

    assert run.code == 0, run.out
    assert host.env("NAME") == "pc-7-desktop"


# --- herdr's server --------------------------------------------------------------------


def test_the_herdr_task_starts_herdrs_server_at_logon_and_now(host: FakeWindows, capsys) -> None:
    run = setup(host, capsys, *every_flag(host))

    assert run.code == 0, run.out
    task = host.paths.herdr_task_file.read_text(encoding="utf-16")
    assert task == host.herdr_task
    # The windowless agent-bridge starts it: herdr.exe, a console program, would open a window.
    assert windows.Task.parse(task).command == host.service_command()
    assert (f"start-herdr --herdr-bin &quot;{host.herdr}&quot; --log-file &quot;{host.paths.log}&quot;"
            in task)
    assert f"<UserId>{SID}</UserId>" in task
    assert "<LogonTrigger>" in task and "<Repetition>" not in task
    # Registered before the bridge's task, so the bridge's checks find the server.
    kinds = [call[0] for call in host.calls if call[0].startswith("register")]
    assert kinds == ["register herdr", "register"]
    assert host.herdr_up
    assert "Starting herdr's server\nok" in run.out
    assert "herdr's server starts at every logon too (the task agent-bridge-herdr)." in run.out


def test_a_running_herdr_server_is_left_as_it_is(host: FakeWindows, capsys) -> None:
    host.herdr_up = True

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 0, run.out
    assert host.herdr_task is not None
    assert "herdr's server already runs; it is left as it is." in run.out
    assert "Starting herdr's server" not in run.out


def test_a_herdr_server_that_doesnt_start_is_said_at_the_end(host: FakeWindows, capsys) -> None:
    host.herdr_starts = False

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 0, run.out
    assert f"warning: herdr's server didn't start; see {host.paths.log}" in run.out
    assert "agents can't start until you open herdr" in run.out.rsplit("Done.", 1)[1]


def test_no_herdr_service_removes_the_task_and_later_runs_keep_it_off(host: FakeWindows, capsys) -> None:
    setup(host, capsys, *every_flag(host))

    run = setup(host, capsys, "--no-herdr-service")

    assert run.code == 0, run.out
    assert host.herdr_task is None
    assert not host.paths.herdr_task_file.exists()
    # Its server keeps running, with its agents.
    assert host.herdr_up
    assert "Removing the scheduled task agent-bridge-herdr (herdr's server keeps running)" in run.out
    assert "Open herdr after each logon" in run.out
    host.calls.clear()

    assert setup(host, capsys).code == 0

    assert host.herdr_task is None
    assert host.ran("register herdr") == []

    assert setup(host, capsys, "--herdr-service").code == 0

    assert host.herdr_task is not None
    assert not host.paths.no_herdr_task.exists()


def herdr_resume() -> bool | None:
    path = herdr_config.herdr_config_paths()[0]
    if not path.exists():
        return None
    return tomllib.loads(path.read_text(encoding="utf-8")).get("session", {}).get("resume_agents_on_restore")


def test_agent_restore_turns_off_herdrs_resume_and_installs_its_hook_in_an_accounts_config(
        host: FakeWindows, capsys) -> None:
    work = host.home / ".claude-work"
    accounts = json.dumps({"personal": {"config_dir": None}, "work": {"config_dir": str(work)}})

    run = setup(host, capsys, *every_flag(host, accounts=accounts))

    assert run.code == 0, run.out
    assert host.ran("herdr integration") == [(str(host.herdr), str(work))]
    assert host.env("RESTORE_AGENTS") == "true"
    assert herdr_resume() is False

    assert setup(host, capsys, "--no-agent-restore").code == 0
    assert host.env("RESTORE_AGENTS") == "false"
    assert herdr_resume() is None
    assert setup(host, capsys).code == 0
    assert host.env("RESTORE_AGENTS") == "false"

    assert setup(host, capsys, "--agent-restore").code == 0
    assert host.env("RESTORE_AGENTS") == "true"
    assert herdr_resume() is False


def test_a_hook_that_wont_install_says_how_to_rerun_it_in_powershell(
        host: FakeWindows, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    host.integration_code = 1
    work = host.home / "it's work"

    run = setup(host, capsys, *every_flag(host, accounts=json.dumps({"work": {"config_dir": str(work)}})))

    assert run.code == 0, run.out
    escaped = str(work).replace("'", "''")
    assert f"Run it again with: $env:CLAUDE_CONFIG_DIR='{escaped}'; herdr integration install claude" in run.out


def test_a_host_set_up_before_the_herdr_task_gets_it(host: FakeWindows, capsys, tmp_path: Path) -> None:
    installed_by_setup_ps1(host, tmp_path / "claude-remote-mcp")

    run = setup(host, capsys)

    assert run.code == 0, run.out
    assert run.asked == []
    assert host.herdr_task is not None
    assert host.herdr_up


def test_a_herdr_task_that_cant_be_removed_is_said_at_the_end(host: FakeWindows, capsys) -> None:
    setup(host, capsys, *every_flag(host))
    host.herdr_delete_fails = True

    run = setup(host, capsys, "--no-herdr-service")

    assert run.code == 0, run.out
    assert "The scheduled task agent-bridge-herdr is still there" in run.out.rsplit("Done.", 1)[1]


def test_a_herdr_path_task_scheduler_would_expand_is_refused_before_anything(host: FakeWindows, capsys,
                                                                              tmp_path: Path) -> None:
    herdr = tmp_path / "%HERDR%" / "herdr.exe"
    herdr.parent.mkdir()
    herdr.write_text("")
    herdr.chmod(0o755)

    run = setup(host, capsys, *every_flag(host, herdr_bin=str(herdr)))

    assert run.code == 1
    assert "the task agent-bridge-herdr can't run a path with % in it" in run.out
    assert not host.env_file.exists()

    # Not when it isn't installed.
    assert setup(host, capsys, *every_flag(host, herdr_bin=str(herdr)), "--no-herdr-service").code == 0


def test_a_herdr_task_that_cant_be_registered_puts_the_bridge_task_back(host: FakeWindows, capsys,
                                                                         tmp_path: Path) -> None:
    installed_by_setup_ps1(host, tmp_path / "claude-remote-mcp")
    host.herdr_register_fails = True

    run = setup(host, capsys)

    assert run.code == 1
    assert "schtasks couldn't register the task agent-bridge-herdr" in run.out
    assert host.task_on
    assert SERVER in host.pids()


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


def test_lasting_settings_in_the_machines_environment_need_an_administrator(host: FakeWindows, capsys) -> None:
    host.lasting = {"machine": ["AGENT_BRIDGE_HOST", "AGENT_BRIDGE_PORT"]}

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 1
    assert "AGENT_BRIDGE_HOST, AGENT_BRIDGE_PORT set in your machine environment" in run.out
    assert "Remove them as administrator" in run.out
    assert "[Environment]::SetEnvironmentVariable('AGENT_BRIDGE_PORT', $null, 'Machine')" in run.out


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


def test_only_the_step_that_failed_is_left_to_run_by_hand(host: FakeWindows, capsys) -> None:
    host.category = "Public"
    host.elevated_fails = 2

    run = setup(host, capsys, *every_flag(host), answers=("y",))

    assert run.code == 0, run.out
    assert host.rule == RULE
    by_hand = run.out.split("In a PowerShell run as administrator:", 1)[1]
    assert "  Set-NetConnectionProfile -InterfaceIndex 16 -NetworkCategory Private" in by_hand
    assert "New-NetFirewallRule" not in by_hand


def test_a_domain_network_is_warned_about(host: FakeWindows, capsys) -> None:
    host.category = "DomainAuthenticated"

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 0, run.out
    assert "the network 192.168.1.20 is on is DomainAuthenticated; the rule applies only on Private networks" in run.out
    assert host.ran("elevated") == [(windows.firewall_rule_commands(8765),)]


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
    assert "enrolled with the gateway at http://10.0.0.1:8766 as windows" in run.out


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
    assert settings["AGENT_BRIDGE_NAME"] == "windows"
    assert settings["AGENT_BRIDGE_LOG_LEVEL"] == "debug"
    # The task runs the package from now on; the checkout's bridge is gone.
    assert windows.Task.parse(host.task()).command == host.service_command()
    assert all(str(checkout) not in process.command_line for process, _ in host.processes_)
    assert 31 not in host.pids()
    for leftover in (*host.paths.leftovers(), host.paths.check_folder()):
        assert not leftover.exists()
    assert host.ran("elevated") == []
    assert ("enroll", ENROLLMENT) not in host.ran("agent-bridge")
    assert "enrolled with the gateway at http://10.0.0.1:8766 as windows" in run.out


def test_a_setup_ps1_host_given_a_config_folder_is_asked_for_the_trust_root(host: FakeWindows, capsys,
                                                                            tmp_path: Path) -> None:
    installed_by_setup_ps1(host, tmp_path / "claude-remote-mcp")
    config = host.home / ".claude-agents"

    run = setup(host, capsys, "--claude-config-dir", str(config), answers=("",))

    assert run.code == 0, run.out
    assert any("Trust agents' folders under" in question for question in run.asked)
    assert host.env("CLAUDE_CONFIG_DIR") == str(config)
    assert host.env("TRUST_ROOT") == str(host.home / "Projects")


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

    def register(xml: str, file: Path, name: str = windows.TASK) -> bool:
        if name != windows.TASK:
            return real(xml, file, name)
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


def killed_with_the_task_disabled(host: FakeWindows, tmp_path: Path) -> None:
    """A setup.ps1 host after a setup was killed once it had disabled the task and stopped the
    bridge: the marker says the task is setup's to bring back."""
    installed_by_setup_ps1(host, tmp_path / "claude-remote-mcp")
    host.task_on = False
    host.processes_ = []
    host.paths.disabled_marker.write_text("setup\n")


def test_after_a_killed_setup_a_failed_one_puts_the_task_back_on(host: FakeWindows, capsys, tmp_path: Path) -> None:
    killed_with_the_task_disabled(host, tmp_path)
    tries = iter([False, True])
    real = host.register

    def register(xml: str, file: Path, name: str = windows.TASK) -> bool:
        if name != windows.TASK:
            return real(xml, file, name)
        if next(tries):
            return real(xml, file)
        host.calls.append(("register", windows.Task.parse(xml).command))
        return False

    host.register = register  # type: ignore[method-assign]

    assert setup(host, capsys).code == 1

    assert host.task_on
    assert SERVER in host.pids()
    assert not host.paths.disabled_marker.exists()


def test_after_a_killed_setup_one_refused_early_puts_the_task_back_on(host: FakeWindows, capsys,
                                                                       tmp_path: Path) -> None:
    killed_with_the_task_disabled(host, tmp_path)
    host.lasting = {"user": ["AGENT_BRIDGE_PORT"]}

    assert setup(host, capsys).code == 1

    assert host.task_on
    assert SERVER in host.pids()
    assert not host.paths.disabled_marker.exists()


def test_a_setup_ps1_task_put_back_still_has_its_files(host: FakeWindows, capsys, tmp_path: Path) -> None:
    installed_by_setup_ps1(host, tmp_path / "claude-remote-mcp")
    host.register_fails = True

    assert setup(host, capsys).code == 1

    # Its task may run run.cmd: removed only once the new task is registered.
    for leftover in host.paths.leftovers():
        assert leftover.exists()


def test_a_task_that_cant_be_disabled_stops_setup_with_nothing_stopped(host: FakeWindows, capsys,
                                                                        tmp_path: Path) -> None:
    installed_by_setup_ps1(host, tmp_path / "claude-remote-mcp")
    host.disable_fails = True

    run = setup(host, capsys)

    assert run.code == 1
    assert "schtasks couldn't disable the task agent-bridge" in run.out
    assert host.task_on
    assert SERVER in host.pids()
    assert not host.paths.disabled_marker.exists()
    assert host.ran("register") == []


def test_a_console_pythonw_is_refused_before_anything(host: FakeWindows, capsys) -> None:
    host.console = [r"C:\Users\me\AppData\Roaming\uv\tools\x\Scripts\pythonw.exe"]

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 1
    assert r"Scripts\pythonw.exe is a console program, so agent-bridge would open a terminal window" in run.out
    assert "uv tool install --reinstall claude-remote-mcp-agent-bridge" in run.out
    assert not host.env_file.exists()


def test_setup_stopped_by_ctrl_c_puts_the_task_back_on(host: FakeWindows, capsys, tmp_path: Path) -> None:
    installed_by_setup_ps1(host, tmp_path / "claude-remote-mcp")
    before = host.task_xml
    real = host.register

    def interrupted(xml: str, file: Path, name: str = windows.TASK) -> bool:
        if name != windows.TASK:
            return real(xml, file, name)
        host.register = real  # type: ignore[method-assign]
        raise KeyboardInterrupt

    host.register = interrupted  # type: ignore[method-assign]

    with pytest.raises(KeyboardInterrupt):
        setup(host, capsys)

    assert host.task_xml == before
    assert host.task_on
    assert SERVER in host.pids()
    assert not host.paths.disabled_marker.exists()


def test_a_failed_restart_after_the_takeover_puts_back_the_new_task(host: FakeWindows, capsys,
                                                                     tmp_path: Path) -> None:
    installed_by_setup_ps1(host, tmp_path / "claude-remote-mcp")
    # The gateway has another host by that name; the restart with the new one fails.
    host.enroll_codes = [3]
    registered = iter([True, False, True])
    real = host.register

    def register(xml: str, file: Path, name: str = windows.TASK) -> bool:
        if name != windows.TASK:
            return real(xml, file, name)
        if next(registered):
            return real(xml, file)
        host.calls.append(("register", windows.Task.parse(xml).command))
        return False

    host.register = register  # type: ignore[method-assign]

    run = setup(host, capsys, "--enrollment", ENROLLMENT, answers=("windows2",))

    assert run.code == 1
    # The package's task, not setup.ps1's, whose files are gone.
    assert windows.Task.parse(host.task_xml).command == host.service_command()
    assert host.task_on
    assert SERVER in host.pids()


def test_the_login_hint_is_for_powershell(host: FakeWindows, capsys) -> None:
    config = host.home / ".claude-agents"

    run = setup(host, capsys, *every_flag(host, claude_config_dir=str(config)))

    assert run.code == 0, run.out
    assert f"$env:CLAUDE_CONFIG_DIR = '{config}'; claude" in run.out
    assert f"CLAUDE_CONFIG_DIR={config} claude" not in run.out


def test_a_path_with_a_space_says_how_to_run_it_in_powershell(host: FakeWindows, capsys, tmp_path: Path) -> None:
    host.bin = tmp_path / "Jan Kowalski" / "bin"
    host.bin.mkdir(parents=True)
    for program in ("agent-bridge.exe", "agent-bridgew.exe"):
        (host.bin / program).write_text("")

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 0, run.out
    assert f"run agent-bridge as & '{host.bin / 'agent-bridge.exe'}'" in run.out


def test_a_restore_that_cant_write_its_file_warns_once_and_keeps_the_marker(host: FakeWindows, capsys,
                                                                           tmp_path: Path) -> None:
    installed_by_setup_ps1(host, tmp_path / "claude-remote-mcp")

    real = host.register

    def register(xml: str, file: Path, name: str = windows.TASK) -> bool:
        if name != windows.TASK:
            return real(xml, file, name)
        raise Refused(f"can't write {file} (No space left on device)")

    host.register = register  # type: ignore[method-assign]

    run = setup(host, capsys)

    assert run.code == 1
    assert run.out.count("couldn't start the agent-bridge task again") == 1
    assert "task-before-setup.xml (No space left on device)" in run.out
    # The next setup brings it back.
    assert host.paths.disabled_marker.exists()


def test_a_rotation_the_gateway_didnt_take_restarts_the_task_with_the_installed_token(host: FakeWindows, capsys) -> None:
    setup(host, capsys, *every_flag(host))
    token = host.env("TOKEN")
    host.enroll_codes = [1]

    run = setup(host, capsys, "--rotate-token", "--enrollment", ENROLLMENT, answers=("",))

    assert run.code == 1
    assert "the installed one is back" in run.out
    assert host.env("TOKEN") == token
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
    # The herdr task goes; herdr's server and its agents stay.
    assert host.herdr_task is None
    assert host.herdr_up
    assert "herdr's server, if it runs, keeps running with its agents" in out.out
    for path in (host.env_file, host.state_file, host.paths.log, host.paths.task_file, host.paths.herdr_task_file,
                 host.paths.settings, host.paths.state):
        assert not path.exists(), path


def test_uninstall_after_no_herdr_service_removes_its_marker(host: FakeWindows, capsys) -> None:
    setup(host, capsys, *every_flag(host), "--no-herdr-service")
    assert host.paths.no_herdr_task.exists()

    assert windows.uninstall(host) == 0

    assert not host.paths.no_herdr_task.exists()
    assert not host.paths.state.exists()


def test_uninstall_says_when_the_herdr_task_cant_be_removed(host: FakeWindows, capsys) -> None:
    setup(host, capsys, *every_flag(host))
    host.herdr_delete_fails = True

    assert windows.uninstall(host) == 0

    assert "couldn't remove the scheduled task agent-bridge-herdr; remove it in Task Scheduler" in \
        capsys.readouterr().err
    assert host.herdr_task is not None
    assert host.task_xml is None
    assert not host.env_file.exists()


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


def test_uninstall_removes_a_rule_it_couldnt_read(host: FakeWindows, capsys) -> None:
    setup(host, capsys, *every_flag(host))
    real = host.firewall_rule
    host.firewall_rule = lambda: () if host.rule else real()  # type: ignore[method-assign]
    host.calls.clear()

    assert windows.uninstall(host) == 0

    assert "Couldn't tell whether it is there; removing it if it is." in capsys.readouterr().out
    assert host.ran("elevated") == [(windows.remove_rule_command(),)]
    assert host.rule is None


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


def test_enabled_turns_the_task_on_but_not_its_triggers() -> None:
    xml = windows.task_xml(SID, r"C:\b\agent-bridgew.exe", Path(r"C:\log"), Path("C:\\"))
    disabled = xml.replace("<Enabled>true</Enabled>\n    <Hidden>", "<Enabled>false</Enabled>\n    <Hidden>")

    again = windows.enabled(disabled)

    assert windows.Task.parse(again) == windows.Task(r"C:\b\agent-bridgew.exe", True)
    assert "ns0:" not in again and "<Hidden>true</Hidden>" in again
    assert windows.enabled(xml) == xml


def pe(subsystem: int) -> bytes:
    header = bytearray(512)
    header[:2] = b"MZ"
    header[0x3C:0x40] = (128).to_bytes(4, "little")
    header[128:132] = b"PE\0\0"
    header[128 + 92:128 + 94] = subsystem.to_bytes(2, "little")
    return bytes(header)


@pytest.mark.parametrize(("data", "subsystem"), [(pe(2), 2), (pe(3), 3), (b"not a program", 0), (b"MZ" + bytes(62), 0)])
def test_pe_subsystem(tmp_path: Path, data: bytes, subsystem: int) -> None:
    (tmp_path / "program.exe").write_bytes(data)
    assert windows.pe_subsystem(tmp_path / "program.exe") == subsystem
    assert windows.pe_subsystem(tmp_path / "missing.exe") == 0


def test_windows_own_programs_run_by_their_full_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    ran: list[list[str]] = []

    def run(argv: list[str], **_: object) -> object:
        ran.append(argv)
        return type("Result", (), {"returncode": 0, "stdout": b""})()

    monkeypatch.setattr(windows.subprocess, "run", run)
    monkeypatch.setattr(windows.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(windows, "_oem", lambda: "ascii")
    system = windows.WindowsSystem()

    system.user()
    system.excluded_ranges()
    system.schtasks("/End", "/TN", "\\agent-bridge")
    system.task()

    assert [argv[0] for argv in ran] == [str(tmp_path / "System32" / name) for name in (
        "whoami.exe", "netsh.exe", "netsh.exe", "schtasks.exe", "schtasks.exe")]


@pytest.mark.parametrize(("code", "printed", "rule"), [
    (0, "", None),
    (0, '{"Enabled":"True","Direction":"Inbound","Action":"Allow","Profile":"Private","Protocol":"TCP","Port":"8765"}',
     RULE),
    (1, "", ()),
    (0, "not json", ()),
])
def test_the_firewall_rule_as_powershell_reports_it(monkeypatch: pytest.MonkeyPatch, code: int, printed: str,
                                                    rule: tuple | None) -> None:
    monkeypatch.setattr(windows.WindowsSystem, "powershell", lambda self, script: (code, printed))
    assert windows.WindowsSystem().firewall_rule() == rule


@pytest.mark.parametrize(("code", "printed", "failed"), [(0, "0", 0), (0, "2", 2), (0, "-1", -1), (0, "", -1),
                                                         (1, "0", -1)])
def test_the_elevated_steps_that_failed_as_powershell_reports_them(monkeypatch: pytest.MonkeyPatch, code: int,
                                                                   printed: str, failed: int) -> None:
    scripts: list[str] = []

    def powershell(self: windows.WindowsSystem, script: str) -> tuple[int, str]:
        scripts.append(script)
        return code, printed

    monkeypatch.setattr(windows.WindowsSystem, "powershell", powershell)
    assert windows.WindowsSystem().elevated(["a", "b"]) == failed
    assert "-Verb RunAs" in scripts[0]
    assert "WindowsPowerShell" in scripts[0]


@pytest.mark.parametrize(("outcome", "running"), [
    (b'{"status":"running","running":true,"version":"0.9.5"}', True),
    (b'{"status":"not_running","running":false,"version":null}', False),
    (b"[]", False),
    (b"not json", False),
    (b"", False),
    (FileNotFoundError(2, "No such file"), False),
    (windows.subprocess.TimeoutExpired("herdr", 30), False),
])
def test_herdr_running_is_read_from_its_status(monkeypatch: pytest.MonkeyPatch, outcome: bytes | Exception,
                                               running: bool) -> None:
    ran: list[list[str]] = []

    def run(argv: list[str], **_: object) -> object:
        ran.append(argv)
        if isinstance(outcome, Exception):
            raise outcome
        return type("Result", (), {"returncode": 0, "stdout": outcome})()

    monkeypatch.setattr(windows.subprocess, "run", run)
    monkeypatch.setattr(windows.subprocess, "CREATE_NO_WINDOW", 0, raising=False)

    assert windows.WindowsSystem().herdr_running(r"C:\Herdr\herdr.exe") is running
    assert ran == [[r"C:\Herdr\herdr.exe", "status", "server", "--json"]]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own firewall, read only")
def test_a_firewall_rule_that_isnt_there_reads_as_not_there(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows, "RULE", "agent-bridge-test-no-such-rule")
    assert windows.WindowsSystem().firewall_rule() is None


def test_putting_back_a_task_unregistered_since_only_drops_the_marker(host: FakeWindows) -> None:
    host.paths.state.mkdir(parents=True)
    host.paths.disabled_marker.write_text("setup\n")
    service = windows.WindowsService(host, lambda: None)

    service.abandon()

    assert not host.paths.disabled_marker.exists()
    assert host.ran("register") == []


def test_a_marker_that_cant_be_written_stops_before_the_task_is_disabled(host: FakeWindows, tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    installed_by_setup_ps1(host, tmp_path / "claude-remote-mcp")
    write_text = Path.write_text

    def full_disk(path: Path, *args: object, **kwargs: object) -> int:
        if path == host.paths.disabled_marker:
            raise OSError(28, "No space left on device")
        return write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", full_disk)
    service = windows.WindowsService(host, lambda: None)

    with pytest.raises(Refused, match="can't write"):
        service.start()

    assert host.ran("schtasks") == []
    assert host.task_on
    assert SERVER in host.pids()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own session")
def test_the_desktop_owner_is_whoever_runs_the_tests() -> None:
    # None where no one is logged on to the session, as on a CI runner's service session.
    owner = windows.WindowsSystem().desktop_owner()
    assert owner is None or owner.lower() == windows.WindowsSystem().me().lower()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own programs")
def test_windows_own_programs_have_the_subsystems_they_should() -> None:
    system = Path(r"C:\Windows\System32")
    assert windows.pe_subsystem(system / "cmd.exe") == 3
    assert windows.pe_subsystem(system / "notepad.exe") == 2


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


# --- Stopping for agent-bridge upgrade -------------------------------------------------


def upgrade_paths(host: FakeWindows) -> tuple[Path, list[str]]:
    """uv's tool environment, and the programs uv put on the PATH (the task runs agent-bridgew)."""
    return host.home / "uv" / "tools" / "claude-remote-mcp-agent-bridge", [
        str(host.bin / "agent-bridge.exe"), str(host.bin / "agent-bridgew.exe")]


def test_upgrade_stops_the_task_and_its_bridge_and_leaves_it_disabled(host: FakeWindows, capsys) -> None:
    assert setup(host, capsys, *every_flag(host)).code == 0

    windows.stop_for_upgrade(host, *upgrade_paths(host))

    assert not host.task_on
    assert not {LAUNCHER, SERVER} & host.pids()
    # Setup, run after the upgrade, brings it back.
    assert host.paths.disabled_marker.exists()


def test_upgrade_with_a_program_still_running_from_the_install_puts_the_task_back(host: FakeWindows, capsys) -> None:
    assert setup(host, capsys, *every_flag(host)).code == 0
    environment, programs = upgrade_paths(host)
    host.processes_ += [
        (Listener(5000, "agent-bridge.exe", f'"{programs[0]}" check', programs[0]), 0),
        (Listener(5001, "python.exe", "python.exe -c pass", str(environment / "Scripts" / "python.exe")), 0),
    ]

    with pytest.raises(Refused, match=r"agent-bridge.exe \(PID 5000\), python.exe \(PID 5001\)"):
        windows.stop_for_upgrade(host, environment, programs)

    assert host.task_on
    assert SERVER in host.pids()
    assert not host.paths.disabled_marker.exists()


def test_upgrade_refusal_says_the_task_was_started_again_only_when_it_was(host: FakeWindows, capsys) -> None:
    assert setup(host, capsys, *every_flag(host)).code == 0
    environment, programs = upgrade_paths(host)
    host.processes_.append((Listener(5000, "agent-bridge.exe", "agent-bridge.exe check", programs[0]), 0))

    with pytest.raises(Refused) as refused:
        windows.stop_for_upgrade(host, environment, programs)

    assert "wouldn't be able to replace its files" in str(refused.value)
    assert "agent-bridge was started again meanwhile" in str(refused.value)


def test_upgrade_refusal_on_a_host_never_set_up_says_how_to_start_agent_bridge(host: FakeWindows) -> None:
    environment, programs = upgrade_paths(host)
    host.processes_.append((Listener(5000, "agent-bridge.exe", "agent-bridge.exe check", programs[0]), 0))

    with pytest.raises(Refused) as refused:
        windows.stop_for_upgrade(host, environment, programs)

    assert "started again meanwhile" not in str(refused.value)
    assert "agent-bridge is stopped meanwhile: start it again with agent-bridge setup" in str(refused.value)


def test_upgrade_refusal_when_the_task_cant_be_put_back_says_agent_bridge_is_stopped(host: FakeWindows, capsys) -> None:
    assert setup(host, capsys, *every_flag(host)).code == 0
    environment, programs = upgrade_paths(host)
    host.processes_.append((Listener(5000, "agent-bridge.exe", "agent-bridge.exe check", programs[0]), 0))
    host.register_fails = True

    with pytest.raises(Refused) as refused:
        windows.stop_for_upgrade(host, environment, programs)

    assert not host.task_on
    assert "started again meanwhile" not in str(refused.value)
    assert "agent-bridge is stopped meanwhile" in str(refused.value)


def test_upgrade_refusal_with_a_stale_marker_and_no_task_says_agent_bridge_is_stopped(host: FakeWindows) -> None:
    # Left by a setup killed long ago, the task since unregistered: abandon() only removes the marker.
    environment, programs = upgrade_paths(host)
    host.paths.state.mkdir(parents=True, exist_ok=True)
    host.paths.disabled_marker.write_text("setup\n", encoding="utf-8")
    host.processes_.append((Listener(5000, "agent-bridge.exe", "agent-bridge.exe check", programs[0]), 0))

    with pytest.raises(Refused) as refused:
        windows.stop_for_upgrade(host, environment, programs)

    assert "started again meanwhile" not in str(refused.value)
    assert "agent-bridge is stopped meanwhile" in str(refused.value)


def test_upgrade_refusal_after_stopping_a_bridge_started_by_hand_says_how_to_start_it(host: FakeWindows, capsys) -> None:
    # The task disabled by the user, and agent-bridge run by hand: stopped, and left off.
    assert setup(host, capsys, *every_flag(host)).code == 0
    host.task_on = False
    environment, programs = upgrade_paths(host)
    host.processes_.append((Listener(5000, "agent-bridge.exe", "agent-bridge.exe check", programs[0]), 0))

    with pytest.raises(Refused) as refused:
        windows.stop_for_upgrade(host, environment, programs)

    assert SERVER not in host.pids()
    assert "agent-bridge is stopped meanwhile: start it again with agent-bridge setup" in str(refused.value)


def test_upgrade_does_not_count_a_sibling_folder_as_the_install(host: FakeWindows, capsys) -> None:
    assert setup(host, capsys, *every_flag(host)).code == 0
    environment, programs = upgrade_paths(host)
    other = Path(str(environment) + "2") / "Scripts" / "python.exe"
    host.processes_.append((Listener(5002, "python.exe", "python.exe", str(other)), 0))

    windows.stop_for_upgrade(host, environment, programs)

    assert not host.task_on


def test_upgrade_ignores_programs_elsewhere(host: FakeWindows, capsys) -> None:
    assert setup(host, capsys, *every_flag(host)).code == 0
    # uvx's copy, which runs the upgrade, and another environment's python.
    host.processes_ += [
        (Listener(6000, "agent-bridge.exe", "agent-bridge.exe upgrade", r"C:\cache\archive-v0\x\Scripts\agent-bridge.exe"), 0),
        (Listener(6001, "python.exe", "python.exe", r"C:\Python\python.exe"), 0),
    ]

    windows.stop_for_upgrade(host, *upgrade_paths(host))

    assert not host.task_on


def test_upgrade_on_a_host_never_set_up_stops_nothing(host: FakeWindows) -> None:
    windows.stop_for_upgrade(host, *upgrade_paths(host))

    assert host.ran("schtasks") == []


def test_upgrade_as_another_account_than_the_desktops_is_refused(host: FakeWindows) -> None:
    host.desktop = "PC\\someone"

    with pytest.raises(Refused, match="run this as PC\\\\someone"):
        windows.stop_for_upgrade(host, *upgrade_paths(host))

    assert host.ran("schtasks") == []
