"""`agent-bridge setup` and `uninstall` on Linux, against a fake system: the systemd units, herdr's
server, lingering, and taking over a host set up with setup.sh. What every platform does the
same way (questions, re-runs, renames, rotation) is flow's, tested in test_install_macos.py.
Never touches systemd, real ports or a gateway, so it runs on any POSIX platform."""

import json
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from dotenv import dotenv_values

from agent_bridge import cli
from agent_bridge.install import common, flow, linux

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the Linux setup uses POSIX paths")

ENROLLMENT = "crm1@10.0.0.1:8766#" + "a" * 24
COMMAND = "/home/me/.local/bin/agent-bridge"
UNIT = "agent-bridge.service"
HERDR_UNIT = "agent-bridge-herdr.service"


@dataclass
class FakeLinux:
    """What setup asks of the host. `enroll_codes` are the exit codes `agent-bridge enroll`
    gives, in turn (0 once they run out); on 0 it writes the state file as enroll does."""

    home: Path
    herdr: Path | None
    command_path: str = COMMAND
    lan: str | None = "192.168.1.20"
    addresses: tuple[str, ...] = ("192.168.1.20",)
    holders: dict[int, str] = field(default_factory=dict)
    root: bool = False
    checkout: bool = False
    cached: bool = False
    user_manager: bool = True
    linger: bool = False
    linger_allowed: bool = True
    # Units that run, by name, with their main PID.
    running: dict[str, str] = field(default_factory=dict)
    # herdr's server runs outside systemd.
    herdr_by_hand: bool = False
    # The port agent-bridge.service listens on.
    service_port: int | None = None
    # systemctl commands that fail, by their first two words, e.g. ("restart", UNIT).
    failing: set[tuple[str, ...]] = field(default_factory=set)
    enroll_codes: list[int] = field(default_factory=list)
    unenroll_code: int = 0
    gateway_token: str | None = None
    gateway_name: str = ""
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def is_root(self) -> bool:
        return self.root

    def in_checkout(self) -> bool:
        return self.checkout

    def in_uv_cache(self) -> bool:
        return self.cached

    def hostname(self) -> str:
        return "tower-1.lan"

    def command(self) -> str:
        return self.command_path

    def lan_address(self) -> str | None:
        return self.lan

    def has_address(self, address: str) -> bool:
        return address in self.addresses

    def port_holder(self, port: int) -> str | None:
        return self.holders.get(port)

    def serves(self, port: int) -> bool:
        return port == self.service_port

    def find_herdr(self) -> str | None:
        return str(self.herdr) if self.herdr else None

    def has_user_manager(self) -> bool:
        return self.user_manager

    def systemctl(self, *args: str) -> int:
        self.calls.append(("systemctl", *args))
        if (args[0], args[-1]) in self.failing:
            return 1
        if args[0] in ("start", "restart"):
            self.running[args[-1]] = "100"
            # The service makes its state file when it starts.
            if args[-1] == UNIT and not self.state_file.exists():
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                self.state_file.write_text(json.dumps({"version": 1, "bridge_id": "b1"}))
        if args[0] == "disable" and "--now" in args:
            self.running.pop(args[-1], None)
        return 0

    def main_pid(self, unit: str) -> str | None:
        return self.running.get(unit)

    def lingering(self) -> bool:
        return self.linger

    def enable_linger(self) -> bool:
        self.calls.append(("loginctl", "enable-linger"))
        self.linger = self.linger_allowed
        return self.linger_allowed

    def herdr_running(self, herdr: str) -> bool:
        return self.herdr_by_hand

    def user(self) -> str:
        return "me"

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

    def env(self, key: str) -> str | None:
        return dotenv_values(self.env_file).get(f"AGENT_BRIDGE_{key}")

    def gateway(self) -> dict | None:
        return json.loads(self.state_file.read_text()).get("gateway") if self.state_file.exists() else None

    def save_gateway(self, name: str) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(
            {"version": 1, "bridge_id": "b1",
             "gateway": {"url": "http://10.0.0.1:8766", "gateway_id": "g1", "name": name}}))

    @property
    def env_file(self) -> Path:
        return self.home / ".config" / "agent-bridge" / "env"

    @property
    def state_file(self) -> Path:
        return self.home / ".local" / "state" / "agent-bridge" / "state.json"

    @property
    def units(self) -> Path:
        return self.home / ".config" / "systemd" / "user"

    def ran(self, kind: str) -> list[tuple[str, ...]]:
        return [call[1:] for call in self.calls if call[0] == kind]


@pytest.fixture
def host(tmp_path: Path, set_home: Callable[[Path], None]) -> FakeLinux:
    home = tmp_path / "home"
    (home / "Projects").mkdir(parents=True)
    set_home(home)
    herdr = tmp_path / "bin" / "herdr"
    herdr.parent.mkdir()
    herdr.write_text("#!/bin/sh\n")
    herdr.chmod(0o755)
    return FakeLinux(home=home, herdr=herdr)


def every_flag(host: FakeLinux, **changes: str | None) -> list[str]:
    flags = {
        "--roots": str(host.home / "Projects"),
        "--name": "tower",
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


def setup(host: FakeLinux, capsys: pytest.CaptureFixture, *flags: str, answers: tuple[str, ...] = ()) -> Run:
    lines = iter(answers)
    options = linux.Options.from_args(cli._parser().parse_args(["setup", *flags]))
    code = linux.setup(options, host, lambda: next(lines, None))
    out, err = capsys.readouterr()
    asked = [line for line in err.split("]: ") if "[" in line]
    return Run(code, out + err, [line.rsplit("\n", 1)[-1] for line in asked])


def installed_by_setup_sh(host: FakeLinux) -> str:
    """A host as agent-bridge/deploy/linux/setup.sh left it: its env file, its units running
    the checkout's venv, and an enrollment. Returns the token."""
    token = "t" * 64
    host.env_file.parent.mkdir(parents=True)
    host.env_file.write_text(
        f"AGENT_BRIDGE_TOKEN='{token}'\n"
        f"AGENT_BRIDGE_ALLOWED_ROOTS='{host.home / 'Projects'}'\n"
        "AGENT_BRIDGE_HOST='192.168.1.20'\n"
        "AGENT_BRIDGE_PORT='8765'\n"
        "AGENT_BRIDGE_NAME='tower'\n"
        f"AGENT_BRIDGE_HERDR_BIN='{host.herdr}'\n"
        "AGENT_BRIDGE_CLAUDE_CONFIG_DIR=''\n"
        "AGENT_BRIDGE_TRUST_ROOT=''\n"
        "AGENT_BRIDGE_LOG_LEVEL='debug'\n"
    )
    host.units.mkdir(parents=True)
    (host.units / UNIT).write_text('[Service]\nExecStart="/home/me/claude-remote-mcp/.venv/bin/agent-bridge" serve\n')
    (host.units / HERDR_UNIT).write_text(f'[Service]\nExecStart="{host.herdr}" server\n')
    host.running = {UNIT: "50", HERDR_UNIT: "60"}
    host.service_port = 8765
    host.holders = {8765: "PID 50: agent-bridge serve"}
    host.gateway_token, host.gateway_name = token, "tower"
    host.save_gateway("tower")
    host.linger = True
    return token


# --- A fresh host ---------------------------------------------------------------------


def test_installs_both_units_starts_them_and_enrolls(host: FakeLinux, capsys) -> None:
    run = setup(host, capsys, *every_flag(host))

    assert run.code == 0, run.out
    assert run.asked == []
    assert f'ExecStart="{COMMAND}" serve' in (host.units / UNIT).read_text()
    assert f'ExecStart="{host.herdr}" server' in (host.units / HERDR_UNIT).read_text()
    assert ("enable", "--quiet", HERDR_UNIT) in host.ran("systemctl")
    assert ("start", HERDR_UNIT) in host.ran("systemctl")
    assert ("enable", "--quiet", UNIT) in host.ran("systemctl")
    assert ("restart", UNIT) in host.ran("systemctl")
    # The units are read again before they are started.
    systemctl = host.ran("systemctl")
    assert systemctl.index(("daemon-reload",)) < systemctl.index(("restart", UNIT))
    assert host.ran("agent-bridge")[-1] == ("enroll", ENROLLMENT)
    assert host.env("NAME") == "tower"
    assert "enrolled with the gateway at http://10.0.0.1:8766 as tower" in run.out
    assert "starts at boot" in run.out
    assert f"journalctl --user -u {UNIT}" in run.out


def test_the_units_and_settings_have_their_modes(host: FakeLinux, capsys) -> None:
    setup(host, capsys, *every_flag(host))

    assert stat.S_IMODE(host.env_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(host.env_file.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((host.units / UNIT).stat().st_mode) == 0o644


def test_lingering_is_turned_on(host: FakeLinux, capsys) -> None:
    run = setup(host, capsys, *every_flag(host))

    assert host.ran("loginctl") == [("enable-linger",)]
    assert "Lingering is off" not in run.out


def test_lingering_already_on_is_left_alone(host: FakeLinux, capsys) -> None:
    host.linger = True

    setup(host, capsys, *every_flag(host))

    assert host.ran("loginctl") == []


def test_lingering_refused_still_installs_and_says_how_to_turn_it_on(host: FakeLinux, capsys) -> None:
    host.linger_allowed = False

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 0
    assert "sudo loginctl enable-linger me" in run.out


def test_without_a_user_manager_it_stops_before_changing_anything(host: FakeLinux, capsys) -> None:
    host.user_manager = False

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 1
    assert "can't reach your systemd user manager" in run.out
    assert not host.env_file.exists()
    assert host.ran("systemctl") == []


def test_without_a_user_manager_it_stops_before_any_question(host: FakeLinux, capsys) -> None:
    host.user_manager = False

    run = setup(host, capsys)

    assert run.code == 1
    assert run.asked == []
    assert "can't reach your systemd user manager" in run.out


def test_with_sudo_it_says_to_run_as_your_own_user(host: FakeLinux, capsys) -> None:
    host.root, host.user_manager = True, False

    assert setup(host, capsys, *every_flag(host)).code == 1
    assert linux.uninstall(host) == 1

    out = capsys.readouterr().err
    assert "run as your own user, not with sudo" in out
    assert "user manager" not in out


def test_the_herdr_unit_is_written_before_the_bridge_unit(host: FakeLinux, capsys) -> None:
    run = setup(host, capsys, *every_flag(host))

    # So a setup stopped between them doesn't leave a host that re-runs read as having none.
    assert run.out.index(f"Writing {host.units / HERDR_UNIT}") < run.out.index(f"Writing {host.units / UNIT}")


def test_the_default_name_is_the_hostname(host: FakeLinux, capsys) -> None:
    run = setup(host, capsys, *every_flag(host, name=None), answers=("",))

    assert run.code == 0, run.out
    assert run.asked == ["This host's name in agent ids, like tower-1/w1:p1 [tower-1"]
    assert host.env("NAME") == "tower-1"


def test_from_a_checkout_it_says_to_install_the_package(host: FakeLinux, capsys) -> None:
    host.checkout = True

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 1
    assert "the systemd unit must run an installed one" in run.out
    assert not host.env_file.exists()


# --- Unit paths -----------------------------------------------------------------------


def test_paths_with_spaces_dollars_and_percents_are_quoted_for_systemd(host: FakeLinux, tmp_path: Path, capsys) -> None:
    herdr = tmp_path / "my tools $HOME 100%" / "herdr"
    herdr.parent.mkdir()
    herdr.write_text("#!/bin/sh\n")
    herdr.chmod(0o755)
    host.command_path = "/opt/my tools/50%/${X}/agent-bridge"

    run = setup(host, capsys, *every_flag(host, herdr_bin=str(herdr)))

    assert run.code == 0, run.out
    assert 'ExecStart="/opt/my tools/50%%/$${X}/agent-bridge" serve' in (host.units / UNIT).read_text()
    escaped = str(herdr).replace("%", "%%").replace("$", "$$")
    assert f'ExecStart="{escaped}" server' in (host.units / HERDR_UNIT).read_text()


@pytest.mark.parametrize("path", ['/opt/a"b/agent-bridge', "/opt/a\\b/agent-bridge"])
def test_a_path_systemd_cant_run_is_refused_before_anything_is_written(host: FakeLinux, capsys, path: str) -> None:
    host.command_path = path

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 1
    assert "systemd can't run a program whose path has" in run.out
    assert not host.env_file.exists()
    assert not (host.units / UNIT).exists()


def test_the_units_are_the_packaged_ones() -> None:
    text = linux.unit(UNIT, "AGENT_BRIDGE", COMMAND)

    assert "@AGENT_BRIDGE@" not in text
    assert "Restart=always" in text
    assert "WantedBy=default.target" in text
    assert "After=agent-bridge-herdr.service" in text


# --- herdr's server -------------------------------------------------------------------


def test_a_running_herdr_service_is_never_restarted(host: FakeLinux, capsys) -> None:
    host.running[HERDR_UNIT] = "60"

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 0
    assert not any(HERDR_UNIT in call and call[0] in ("start", "restart", "stop") for call in host.ran("systemctl"))
    assert f"herdr's server runs as {HERDR_UNIT}" in run.out


def test_a_herdr_server_started_by_hand_is_left_to_run(host: FakeLinux, capsys) -> None:
    host.herdr_by_hand = True

    run = setup(host, capsys, *every_flag(host))

    assert ("start", HERDR_UNIT) not in host.ran("systemctl")
    assert ("enable", "--quiet", HERDR_UNIT) in host.ran("systemctl")
    assert "starts it from the next boot" in run.out


def test_no_herdr_service_installs_no_herdr_unit_and_removes_one_installed(host: FakeLinux, capsys) -> None:
    host.units.mkdir(parents=True)
    (host.units / HERDR_UNIT).write_text("old")
    host.running[HERDR_UNIT] = "60"

    run = setup(host, capsys, *every_flag(host), "--no-herdr-service")

    assert run.code == 0, run.out
    assert not (host.units / HERDR_UNIT).exists()
    assert ("disable", "--quiet", HERDR_UNIT) in host.ran("systemctl")
    # Its server, and its agents, keep running.
    assert ("stop", HERDR_UNIT) not in host.ran("systemctl")
    assert HERDR_UNIT in host.running


def test_a_host_without_a_herdr_service_keeps_none_on_a_rerun(host: FakeLinux, capsys) -> None:
    setup(host, capsys, *every_flag(host), "--no-herdr-service")
    host.calls.clear()

    run = setup(host, capsys, "--enrollment", "")

    assert run.code == 0, run.out
    assert not (host.units / HERDR_UNIT).exists()
    assert not any(HERDR_UNIT in call for call in host.ran("systemctl"))


def test_herdr_service_brings_the_herdr_unit_back(host: FakeLinux, capsys) -> None:
    setup(host, capsys, *every_flag(host), "--no-herdr-service")

    run = setup(host, capsys, "--enrollment", "", "--herdr-service")

    assert run.code == 0, run.out
    assert (host.units / HERDR_UNIT).exists()


@pytest.mark.parametrize("failing", [("enable", UNIT), ("restart", UNIT)])
def test_a_bridge_unit_that_doesnt_start_stops_the_setup(host: FakeLinux, capsys, failing: tuple[str, str]) -> None:
    host.failing = {failing}

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 1
    assert f"systemctl --user couldn't start {UNIT}; see: journalctl --user -u {UNIT}" in run.out
    assert ("enroll", ENROLLMENT) not in host.ran("agent-bridge")


def test_units_systemd_didnt_reload_stop_the_setup(host: FakeLinux, capsys) -> None:
    installed_by_setup_sh(host)
    host.failing = {("daemon-reload", "daemon-reload")}

    run = setup(host, capsys)

    assert run.code == 1
    assert "daemon-reload failed" in run.out
    # systemd would start the checkout's unit it still has.
    assert ("restart", UNIT) not in host.ran("systemctl")


def test_a_rotation_the_unit_doesnt_start_for_puts_the_token_back(host: FakeLinux, capsys) -> None:
    token = installed_by_setup_sh(host)
    host.failing = {("restart", UNIT)}

    run = setup(host, capsys, "--rotate-token", "--enrollment", ENROLLMENT)

    assert run.code == 1
    assert "the installed one is back" in run.out
    assert host.env("TOKEN") == token


def test_a_herdr_unit_that_cant_be_enabled_stops_the_setup(host: FakeLinux, capsys) -> None:
    host.failing = {("enable", HERDR_UNIT)}

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 1
    assert f"systemctl --user enable {HERDR_UNIT} failed" in run.out
    assert ("restart", UNIT) not in host.ran("systemctl")


def test_a_herdr_unit_that_doesnt_start_is_a_warning(host: FakeLinux, capsys) -> None:
    host.failing = {("start", HERDR_UNIT)}

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 0, run.out
    assert f"warning: {HERDR_UNIT} didn't start; see: journalctl --user -u {HERDR_UNIT}" in run.out


# --- Settings in ~/.env, which serve reads in the unit's working folder ----------------


def test_settings_in_the_home_folders_env_are_checked_too(host: FakeLinux, capsys) -> None:
    (host.home / ".env").write_text("AGENT_BRIDGE_MAX_AGENTS=abc\n")

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 1
    assert f"with the settings in {host.home / '.env'}" in run.out
    assert not host.env_file.exists()


def test_valid_settings_in_the_home_folders_env_are_a_warning(host: FakeLinux, capsys) -> None:
    (host.home / ".env").write_text("AGENT_BRIDGE_MAX_AGENTS=3\nOTHER=x\n")

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 0, run.out
    assert f"also reads the settings in {host.home / '.env'}" in run.out


def test_a_home_folder_env_that_isnt_utf_8_is_left_alone(host: FakeLinux, capsys) -> None:
    (host.home / ".env").write_bytes(b"NAME=caf\xe9\n")

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 0, run.out
    assert ".env" not in run.out


def test_a_home_folder_env_without_settings_is_left_alone(host: FakeLinux, capsys) -> None:
    (host.home / ".env").write_text("OTHER=x\n")

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 0, run.out
    assert ".env" not in run.out


# --- Re-runs and taking over a setup.sh install ---------------------------------------


def test_a_rerun_asks_nothing_keeps_the_token_and_leaves_herdr_running(host: FakeLinux, capsys) -> None:
    setup(host, capsys, *every_flag(host))
    token = host.env("TOKEN")
    host.calls.clear()

    run = setup(host, capsys)

    assert run.code == 0, run.out
    assert run.asked == []
    assert host.env("TOKEN") == token
    assert ("enroll", ENROLLMENT) not in host.ran("agent-bridge")
    assert not any(HERDR_UNIT in call and call[0] in ("start", "restart", "stop") for call in host.ran("systemctl"))
    assert "enrolled with the gateway at http://10.0.0.1:8766 as tower" in run.out


def test_a_setup_sh_install_is_taken_over_without_questions(host: FakeLinux, capsys) -> None:
    token = installed_by_setup_sh(host)

    run = setup(host, capsys)

    assert run.code == 0, run.out
    assert run.asked == []
    settings = dotenv_values(host.env_file)
    assert settings["AGENT_BRIDGE_TOKEN"] == token
    assert settings["AGENT_BRIDGE_NAME"] == "tower"
    assert settings["AGENT_BRIDGE_PORT"] == "8765"
    # Its settings setup doesn't write itself stay.
    assert settings["AGENT_BRIDGE_LOG_LEVEL"] == "debug"
    # The unit runs the package from now on; herdr's server keeps running.
    assert f'ExecStart="{COMMAND}" serve' in (host.units / UNIT).read_text()
    assert ("restart", UNIT) in host.ran("systemctl")
    assert not any(HERDR_UNIT in call and call[0] in ("start", "restart", "stop") for call in host.ran("systemctl"))
    assert host.gateway()["name"] == "tower"
    assert ("enroll", ENROLLMENT) not in host.ran("agent-bridge")
    assert "enrolled with the gateway at http://10.0.0.1:8766 as tower" in run.out


def test_a_setup_sh_install_without_a_herdr_unit_gets_none(host: FakeLinux, capsys) -> None:
    installed_by_setup_sh(host)
    (host.units / HERDR_UNIT).unlink()

    run = setup(host, capsys)

    assert run.code == 0, run.out
    assert not (host.units / HERDR_UNIT).exists()


def test_the_port_another_program_holds_is_refused(host: FakeLinux, capsys) -> None:
    host.holders = {8765: "PID 7: python3 -m http.server 8765"}

    run = setup(host, capsys, *every_flag(host))

    assert run.code == 1
    assert "Port 8765 is in use by PID 7: python3 -m http.server 8765" in run.out


# --- Uninstall ------------------------------------------------------------------------


def test_uninstall_leaves_the_gateway_then_removes_the_units_and_files(host: FakeLinux, capsys) -> None:
    setup(host, capsys, *every_flag(host))
    host.calls.clear()

    code = linux.uninstall(host)
    out = capsys.readouterr()

    assert code == 0, out
    assert host.ran("agent-bridge") == [("unenroll",)]
    assert host.gateway_token is None
    assert ("disable", "--now", "--quiet", UNIT) in host.ran("systemctl")
    assert ("disable", "--quiet", HERDR_UNIT) in host.ran("systemctl")
    # herdr's server isn't stopped: that would stop every agent.
    assert HERDR_UNIT in host.running
    assert "herdr's server keeps running" in out.out
    assert not (host.units / UNIT).exists()
    assert not (host.units / HERDR_UNIT).exists()
    assert not host.env_file.exists()
    assert not host.state_file.exists()
    assert not host.env_file.parent.exists()
    assert not host.state_file.parent.exists()
    assert "loginctl disable-linger me" in out.out


def test_uninstall_removes_what_an_interrupted_write_left(host: FakeLinux, capsys) -> None:
    setup(host, capsys, *every_flag(host))
    left = [host.env_file.with_name("env.tmp"), host.units / f"{UNIT}.tmp", host.units / f"{HERDR_UNIT}.tmp"]
    for path in left:
        path.write_text("partial")

    assert linux.uninstall(host) == 0

    assert not any(path.exists() for path in left)
    assert not host.env_file.parent.exists()


def test_uninstall_keeps_folders_that_hold_other_files(host: FakeLinux, capsys) -> None:
    setup(host, capsys, *every_flag(host))
    (host.env_file.parent / "notes").write_text("mine")

    assert linux.uninstall(host) == 0

    assert (host.env_file.parent / "notes").exists()
    assert "it holds other files" in capsys.readouterr().out


def test_uninstall_takes_a_setup_sh_install_away_too(host: FakeLinux, capsys) -> None:
    installed_by_setup_sh(host)

    assert linux.uninstall(host) == 0

    assert not (host.units / UNIT).exists()
    assert not host.env_file.exists()
    assert host.gateway_token is None


def test_uninstall_goes_on_when_leaving_the_gateway_fails(host: FakeLinux, capsys) -> None:
    setup(host, capsys, *every_flag(host))
    host.unenroll_code = 1

    assert linux.uninstall(host) == 0

    assert "sudo mcp-gateway bridges remove tower" in capsys.readouterr().err
    assert not (host.units / UNIT).exists()


def test_uninstall_without_a_user_manager_changes_nothing(host: FakeLinux, capsys) -> None:
    setup(host, capsys, *every_flag(host))
    host.user_manager = False
    host.calls.clear()

    assert linux.uninstall(host) == 1

    assert host.calls == []
    assert host.env_file.exists()


def test_a_unit_that_cant_be_removed_stops_the_uninstall(host: FakeLinux, capsys,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    setup(host, capsys, *every_flag(host))
    unlink = Path.unlink

    def refuse(path: Path, missing_ok: bool = False) -> None:
        if path.name == UNIT:
            raise PermissionError(13, "Permission denied")
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse)

    assert linux.uninstall(host) == 1

    assert f"can't remove {host.units / UNIT} (Permission denied)" in capsys.readouterr().err
    # The settings stay, so a re-run can still leave the gateway.
    assert host.env_file.exists()


# --- The real host, with its commands' output faked -------------------------------------


@pytest.fixture
def printed(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, ...], str]:
    """What each command prints; any other prints nothing. Every command is installed."""
    outputs: dict[tuple[str, ...], str] = {}
    monkeypatch.setattr(linux, "output", lambda *argv: outputs.get(argv, ""))
    monkeypatch.setattr(linux.shutil, "which", lambda name: f"/usr/bin/{name}")
    return outputs


def test_without_ss_the_port_check_refuses(printed: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(linux.shutil, "which", lambda name: None)

    with pytest.raises(linux.Refused, match="install iproute2"):
        linux.LinuxSystem().port_holder(8765)


def test_finds_the_source_address_of_the_default_route(printed: dict) -> None:
    printed["ip", "-4", "route", "get", "1.1.1.1"] = (
        "1.1.1.1 via 192.168.1.1 dev enp3s0 src 192.168.1.26 uid 1000 \n    cache \n")

    assert linux.LinuxSystem().lan_address() == "192.168.1.26"


def test_no_default_route_has_no_address(printed: dict) -> None:
    assert linux.LinuxSystem().lan_address() is None


@pytest.mark.parametrize(("address", "has"), [("192.168.1.26", True), ("fe80::1", True), ("127.0.0.1", True),
                                              ("192.168.1.2", False)])
def test_knows_the_interfaces_addresses(printed: dict, address: str, has: bool) -> None:
    printed["ip", "-o", "addr", "show"] = (
        "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever\n"
        "2: enp3s0    inet 192.168.1.26/24 brd 192.168.1.255 scope global dynamic enp3s0\\ valid_lft 1d\n"
        "2: enp3s0    inet6 fe80::0001/64 scope link \\       valid_lft forever preferred_lft forever\n")

    assert linux.LinuxSystem().has_address(address) is has


def test_names_your_own_program_on_the_port(printed: dict) -> None:
    printed["ss", "-Hltnp", "sport = :8765"] = (
        'LISTEN 0 2048 192.168.1.26:8765 0.0.0.0:* users:(("python3",pid=123,fd=6))\n')
    printed["ps", "-o", "args=", "-p", "123"] = "python3 -m http.server 8765\n"

    assert linux.LinuxSystem().port_holder(8765) == "PID 123: python3 -m http.server 8765"


def test_sees_another_users_program_on_the_port(printed: dict) -> None:
    printed["ss", "-Hltnp", "sport = :8765"] = "LISTEN 0 2048 0.0.0.0:8765 0.0.0.0:*\n"

    assert linux.LinuxSystem().port_holder(8765) == "another user's program"


def test_a_free_port_has_no_holder(printed: dict) -> None:
    assert linux.LinuxSystem().port_holder(8765) is None


@pytest.mark.parametrize(("main_pid", "serves"), [("123", True), ("456", False), ("0", False)])
def test_the_service_holds_the_port_when_its_main_process_listens(printed: dict, main_pid: str, serves: bool) -> None:
    printed["ss", "-Hltnp", "sport = :8765"] = (
        'LISTEN 0 2048 192.168.1.26:8765 0.0.0.0:* users:(("agent-bridge",pid=123,fd=6))\n')
    printed["systemctl", "--user", "show", "-p", "MainPID", "--value", UNIT] = f"{main_pid}\n"

    assert linux.LinuxSystem().serves(8765) is serves


def test_lingering_is_read_from_loginctl(printed: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(linux.LinuxSystem, "user", lambda self: "me")
    printed["loginctl", "show-user", "me", "-p", "Linger", "--value"] = "yes\n"

    assert linux.LinuxSystem().lingering()


def test_herdr_running_is_read_from_its_status(printed: dict) -> None:
    printed["/bin/herdr", "status", "server"] = "status: running\npid: 5\n"

    assert linux.LinuxSystem().herdr_running("/bin/herdr")
    assert not linux.LinuxSystem().herdr_running("/other/herdr")


def test_finds_herdr_in_the_usual_places(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                         set_home: Callable[[Path], None]) -> None:
    set_home(tmp_path)
    monkeypatch.setattr(flow.shutil, "which", lambda name: None)
    local = tmp_path / ".local" / "bin" / "herdr"
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\n")
    local.chmod(0o755)

    assert linux.LinuxSystem().find_herdr() == str(local)


def test_the_cli_runs_the_linux_setup_on_linux(monkeypatch: pytest.MonkeyPatch, host: FakeLinux, capsys) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(linux, "system", lambda: host)
    monkeypatch.setattr(sys, "stdin", type("Stdin", (), {"readline": lambda self: ""})())

    assert cli.main(["setup", *every_flag(host)]) == 0

    assert (host.units / UNIT).exists()
    assert common.PREFIX + "TOKEN" in dotenv_values(host.env_file)
