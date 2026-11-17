"""`agent-bridge setup` on macOS, against a fake system: what it asks, writes and runs. Never
touches launchd, real ports or a gateway, so it runs on any platform."""

import json
import plistlib
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from dotenv import dotenv_values

from agent_bridge import cli
from agent_bridge.install import macos

# POSIX paths and file modes; the Windows setup is its own.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the macOS setup uses POSIX paths")

ENROLLMENT = "crm1@10.0.0.1:8766#" + "a" * 24
COMMAND = "/Users/me/.local/bin/agent-bridge"
LABEL = "dev.claude-remote.agent-bridge"


@dataclass
class FakeMac:
    """What setup asks of the Mac. `enroll_codes` are the exit codes `agent-bridge enroll`
    gives, in turn (0 once they run out); on 0 it writes the state file as enroll does."""

    home: Path
    herdr: Path | None
    lan: str | None = "192.168.1.20"
    addresses: tuple[str, ...] = ("192.168.1.20",)
    holders: dict[int, str] = field(default_factory=dict)
    root: bool = False
    checkout: bool = False
    cached: bool = False
    name: str = "Studio-Mac.local"
    enroll_codes: list[int] = field(default_factory=list)
    bootstrap_failures: int = 0
    # How many `check`s report agent-bridge not answering yet.
    slow_starts: int = 0
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def is_root(self) -> bool:
        return self.root

    def in_checkout(self) -> bool:
        return self.checkout

    def in_uv_cache(self) -> bool:
        return self.cached

    def hostname(self) -> str:
        return self.name

    def command(self) -> str:
        return COMMAND

    def domain(self) -> str:
        return "gui/501"

    def lan_address(self) -> str | None:
        return self.lan

    def has_address(self, address: str) -> bool:
        return address in self.addresses

    def port_holder(self, port: int) -> str | None:
        return self.holders.get(port)

    def find_herdr(self) -> str | None:
        return str(self.herdr) if self.herdr else None

    def launchctl(self, *args: str) -> int:
        self.calls.append(("launchctl", *args))
        if args[0] == "bootstrap":
            if self.bootstrap_failures:
                self.bootstrap_failures -= 1
                return 5
            # The service makes its state file when it starts.
            if not self.state_file.exists():
                self.state_file.write_text(json.dumps({"version": 1, "bridge_id": "b1"}))
        return 0

    def bridge(self, *args: str, capture: bool = False) -> tuple[int, str]:
        self.calls.append(("agent-bridge", *args))
        if args[0] == "check":
            if self.slow_starts:
                self.slow_starts -= 1
                return 1, "FAIL  agent-bridge answers at http://192.168.1.20:8765 (ConnectError)\n"
            return 0, "ok    agent-bridge answers at http://192.168.1.20:8765\n"
        code = self.enroll_codes.pop(0) if self.enroll_codes else 0
        if code == 0:
            name = dotenv_values(self.env_file)["AGENT_BRIDGE_NAME"]
            self.state_file.write_text(json.dumps(
                {"version": 1, "bridge_id": "b1",
                 "gateway": {"url": "http://10.0.0.1:8766", "gateway_id": "g1", "name": name}}))
        return code, ""

    def sleep(self, seconds: float) -> None:
        pass

    @property
    def support(self) -> Path:
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

    def ran(self, kind: str) -> list[tuple[str, ...]]:
        return [call[1:] for call in self.calls if call[0] == kind]


@pytest.fixture
def mac(tmp_path: Path, set_home: Callable[[Path], None]) -> FakeMac:
    home = tmp_path / "home"
    (home / "Projects").mkdir(parents=True)
    set_home(home)
    herdr = tmp_path / "bin" / "herdr"
    herdr.parent.mkdir()
    herdr.write_text("#!/bin/sh\n")
    herdr.chmod(0o755)
    return FakeMac(home=home, herdr=herdr)


def every_flag(mac: FakeMac, **changes: str | None) -> list[str]:
    """Every flag setup takes; a change of None leaves that one out."""
    flags = {
        "--roots": str(mac.home / "Projects"),
        "--name": "studio",
        "--host": "192.168.1.20",
        "--port": "8765",
        "--herdr-bin": str(mac.herdr),
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
    # What was asked, one line per question.
    asked: list[str]


def setup(mac: FakeMac, capsys: pytest.CaptureFixture, *flags: str, answers: tuple[str, ...] = ()) -> Run:
    lines = iter(answers)
    options = macos.Options.from_args(cli._parser().parse_args(["setup", *flags]))
    code = macos.setup(options, mac, lambda: next(lines, None))
    out, err = capsys.readouterr()
    asked = [line for line in err.split("]: ") if "[" in line]
    return Run(code, out + err, [line.rsplit("\n", 1)[-1] for line in asked])


def env(mac: FakeMac) -> dict[str, str | None]:
    return dotenv_values(mac.env_file)


# --- With every value passed in -------------------------------------------------------


def test_installs_and_enrolls_without_asking(mac: FakeMac, capsys) -> None:
    run = setup(mac, capsys, *every_flag(mac))

    assert run.code == 0, run.out
    assert run.asked == []
    settings = env(mac)
    assert len(settings.pop("AGENT_BRIDGE_TOKEN")) == 64
    assert settings == {
        "AGENT_BRIDGE_ALLOWED_ROOTS": str(mac.home / "Projects"),
        "AGENT_BRIDGE_HOST": "192.168.1.20",
        "AGENT_BRIDGE_PORT": "8765",
        "AGENT_BRIDGE_NAME": "studio",
        "AGENT_BRIDGE_HERDR_BIN": str(mac.herdr),
        "AGENT_BRIDGE_CLAUDE_CONFIG_DIR": "",
        "AGENT_BRIDGE_TRUST_ROOT": "",
    }
    assert mac.ran("agent-bridge") == [("check",), ("enroll", ENROLLMENT)]
    assert "enrolled with the gateway at http://10.0.0.1:8766 as studio" in run.out


def test_only_the_user_can_read_the_settings(mac: FakeMac, capsys) -> None:
    setup(mac, capsys, *every_flag(mac))

    assert stat.S_IMODE(mac.env_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(mac.support.stat().st_mode) == 0o700


def test_the_launch_agent_runs_the_installed_command(mac: FakeMac, capsys) -> None:
    setup(mac, capsys, *every_flag(mac))

    log = str(mac.home / "Library" / "Logs" / "agent-bridge.log")
    assert plistlib.loads(mac.plist.read_bytes()) == {
        "Label": LABEL,
        "ProgramArguments": [COMMAND, "serve"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    assert stat.S_IMODE(mac.plist.stat().st_mode) == 0o644


def test_starts_the_launch_agent(mac: FakeMac, capsys) -> None:
    setup(mac, capsys, *every_flag(mac))

    assert mac.ran("launchctl") == [
        ("bootout", f"gui/501/{LABEL}"),
        ("enable", f"gui/501/{LABEL}"),
        ("bootstrap", "gui/501", str(mac.plist)),
    ]


def test_retries_the_bootstrap_while_the_old_job_goes(mac: FakeMac, capsys) -> None:
    mac.bootstrap_failures = 2

    run = setup(mac, capsys, *every_flag(mac))

    assert run.code == 0, run.out
    assert [call[0] for call in mac.ran("launchctl")].count("bootstrap") == 3


def test_waits_for_the_bridge_to_answer_before_enrolling(mac: FakeMac, capsys) -> None:
    mac.slow_starts = 3

    run = setup(mac, capsys, *every_flag(mac))

    assert mac.ran("agent-bridge") == [("check",)] * 4 + [("enroll", ENROLLMENT)]
    assert "ok    agent-bridge answers" in run.out
    assert "FAIL" not in run.out


def test_folder_trust_in_a_separate_config(mac: FakeMac, capsys) -> None:
    agents = mac.home / ".claude-agents"

    run = setup(mac, capsys, *every_flag(mac, claude_config_dir="~/.claude-agents", trust_root="~/Projects"))

    assert run.code == 0, run.out
    assert env(mac)["AGENT_BRIDGE_CLAUDE_CONFIG_DIR"] == str(agents)
    assert env(mac)["AGENT_BRIDGE_TRUST_ROOT"] == str(mac.home / "Projects")
    assert f"CLAUDE_CONFIG_DIR={agents} claude" in run.out


# --- Questions ------------------------------------------------------------------------


def test_asks_for_what_is_missing_and_takes_the_defaults(mac: FakeMac, capsys) -> None:
    run = setup(mac, capsys, "--herdr-bin", str(mac.herdr), answers=("",) * 5)

    assert run.code == 0, run.out
    assert run.asked == [
        "Folders agents may start in, :-separated [" + str(mac.home / "Projects"),
        "This host's name in agent ids, like studio-mac/w1:p1 [studio-mac",
        "Separate Claude config folder for agents [" + str(mac.home / ".claude-agents") + "; Enter for that, - for none",
        "Trust agents' folders under [" + str(mac.home / "Projects") + "; Enter for that, - for none",
        "Enrollment string it printed, crm1@… [Enter to enroll later",
    ]
    assert env(mac)["AGENT_BRIDGE_NAME"] == "studio-mac"
    assert env(mac)["AGENT_BRIDGE_CLAUDE_CONFIG_DIR"] == str(mac.home / ".claude-agents")
    assert mac.ran("agent-bridge") == [("check",)]
    assert "It is not enrolled with a gateway yet" in run.out


def test_no_input_left_takes_the_defaults(mac: FakeMac, capsys) -> None:
    run = setup(mac, capsys, "--herdr-bin", str(mac.herdr))

    assert run.code == 0, run.out
    assert env(mac)["AGENT_BRIDGE_ALLOWED_ROOTS"] == str(mac.home / "Projects")


def test_dash_answers_no_folder_trust(mac: FakeMac, capsys) -> None:
    run = setup(mac, capsys, *every_flag(mac, claude_config_dir=None), answers=("-",))

    assert run.code == 0, run.out
    assert len(run.asked) == 1
    assert env(mac)["AGENT_BRIDGE_CLAUDE_CONFIG_DIR"] == ""
    assert env(mac)["AGENT_BRIDGE_TRUST_ROOT"] == ""


def test_asks_again_for_a_name_that_wont_do(mac: FakeMac, capsys) -> None:
    run = setup(mac, capsys, *every_flag(mac, name=None), answers=("Studio Mac", "studio"))

    assert run.code == 0, run.out
    assert len(run.asked) == 2
    assert "lowercase letter" in run.out
    assert env(mac)["AGENT_BRIDGE_NAME"] == "studio"


def test_asks_again_for_a_string_that_isnt_one(mac: FakeMac, capsys) -> None:
    run = setup(mac, capsys, *every_flag(mac, enrollment=None), answers=("hello", ENROLLMENT))

    assert run.code == 0, run.out
    assert "isn't an enrollment string" in run.out
    assert mac.ran("agent-bridge")[-1] == ("enroll", ENROLLMENT)


def test_a_hostname_starting_with_a_digit_gets_a_mac_name(mac: FakeMac, capsys) -> None:
    mac.name = "2019-MBP.local"

    setup(mac, capsys, *every_flag(mac, name=None), answers=("",))

    assert env(mac)["AGENT_BRIDGE_NAME"] == "mac-2019-mbp"


def test_offers_a_free_port_when_the_default_is_taken(mac: FakeMac, capsys) -> None:
    mac.holders = {8765: "PID 42: python3 -m http.server 8765", 8766: "PID 43: other"}
    flags = every_flag(mac)
    del flags[flags.index("--port"):flags.index("--port") + 2]

    run = setup(mac, capsys, *flags, answers=("",))

    assert run.code == 0, run.out
    assert run.asked == ["Port [8767"]
    assert "PID 42: python3 -m http.server 8765" in run.out
    assert env(mac)["AGENT_BRIDGE_PORT"] == "8767"


def test_asks_again_for_a_port_that_isnt_one(mac: FakeMac, capsys) -> None:
    mac.holders = {8765: "PID 42: other"}
    flags = every_flag(mac)
    del flags[flags.index("--port"):flags.index("--port") + 2]

    run = setup(mac, capsys, *flags, answers=("abc", "9000"))

    assert run.code == 0, run.out
    assert "The port must be a number from 1 to 65535, not abc." in run.out
    assert env(mac)["AGENT_BRIDGE_PORT"] == "9000"


def test_finds_the_lan_address_and_herdr(mac: FakeMac, capsys) -> None:
    flags = every_flag(mac)
    for flag in ("--host", "--herdr-bin"):
        del flags[flags.index(flag):flags.index(flag) + 2]

    setup(mac, capsys, *flags)

    assert env(mac)["AGENT_BRIDGE_HOST"] == "192.168.1.20"
    assert env(mac)["AGENT_BRIDGE_HERDR_BIN"] == str(mac.herdr)


def test_warns_about_an_address_no_interface_has(mac: FakeMac, capsys) -> None:
    run = setup(mac, capsys, *every_flag(mac, host="192.168.1.99"))

    assert run.code == 0, run.out
    assert "no interface has 192.168.1.99 right now" in run.out


# --- Enrolling ------------------------------------------------------------------------


def test_asks_for_another_name_when_the_gateway_has_it(mac: FakeMac, capsys) -> None:
    mac.enroll_codes = [3]

    run = setup(mac, capsys, *every_flag(mac), answers=("studio-2",))

    assert run.code == 0, run.out
    assert run.asked == ["Another name for this host [required"]
    assert env(mac)["AGENT_BRIDGE_NAME"] == "studio-2"
    assert mac.ran("agent-bridge")[-1] == ("enroll", ENROLLMENT)
    # Restarted, so the bridge serves under the new name.
    assert [call[0] for call in mac.ran("launchctl")].count("bootstrap") == 2
    assert "as studio-2" in run.out


def test_fails_when_no_other_name_is_given(mac: FakeMac, capsys) -> None:
    mac.enroll_codes = [3]

    run = setup(mac, capsys, *every_flag(mac))

    assert run.code == 1
    assert "the gateway has another host named studio" in run.out
    assert nothing_left(mac)
    # So the run it advises is not refused as an install.
    assert setup(mac, capsys, *every_flag(mac, name="studio-2")).code == 0


def test_leaves_enrolling_for_later_when_the_gateway_is_down(mac: FakeMac, capsys) -> None:
    mac.enroll_codes = [4]

    run = setup(mac, capsys, *every_flag(mac))

    assert run.code == 0, run.out
    assert run.asked == []
    assert mac.ran("agent-bridge") == [("check",), ("enroll", ENROLLMENT)]


def test_asks_for_another_string_when_one_is_refused(mac: FakeMac, capsys) -> None:
    mac.enroll_codes = [1]

    run = setup(mac, capsys, *every_flag(mac), answers=(ENROLLMENT.replace("a", "b"),))

    assert run.code == 0, run.out
    assert run.asked == ["Enrollment string, crm1@… [Enter to enroll later"]
    assert mac.ran("agent-bridge")[-1] == ("enroll", ENROLLMENT.replace("a", "b"))


def test_stops_when_the_gateway_took_it_but_the_state_isnt_saved(mac: FakeMac, capsys) -> None:
    mac.enroll_codes = [5]

    run = setup(mac, capsys, *every_flag(mac), answers=(ENROLLMENT,))

    assert run.asked == []
    assert mac.ran("agent-bridge") == [("check",), ("enroll", ENROLLMENT)]
    assert "state file couldn't be written" in run.out


# --- Failing after writing: undone, so setup can run again ----------------------------


def nothing_left(mac: FakeMac) -> bool:
    return not any(path.exists() for path in (mac.env_file, mac.state_file, mac.plist)) and (
        mac.ran("launchctl")[-1] == ("bootout", f"gui/501/{LABEL}"))


def test_undoes_the_install_when_the_plist_cant_be_written(mac: FakeMac, capsys) -> None:
    (mac.home / "Library").mkdir()
    (mac.home / "Library" / "LaunchAgents").write_text("not a folder")

    run = setup(mac, capsys, *every_flag(mac))

    assert run.code == 1
    assert f"can't write {mac.plist}" in run.out
    assert nothing_left(mac)


def test_keeps_the_state_of_an_enrollment_when_undoing(mac: FakeMac, capsys) -> None:
    def interrupted() -> str:
        raise KeyboardInterrupt

    mac.enroll_codes = [3]
    original = mac.bridge

    def enrolled_then_taken(*args: str, capture: bool = False) -> tuple[int, str]:
        if args[0] == "enroll":
            mac.state_file.write_text(json.dumps(
                {"version": 1, "bridge_id": "b1",
                 "gateway": {"url": "http://10.0.0.1:8766", "gateway_id": "g1", "name": "studio"}}))
        return original(*args, capture=capture)

    mac.bridge = enrolled_then_taken
    options = macos.Options.from_args(cli._parser().parse_args(["setup", *every_flag(mac)]))
    with pytest.raises(KeyboardInterrupt):
        macos.setup(options, mac, interrupted)

    assert not mac.env_file.exists() and not mac.plist.exists()
    assert "gateway" in json.loads(mac.state_file.read_text())


def test_undoes_the_install_when_launchd_wont_start_it(mac: FakeMac, capsys) -> None:
    mac.bootstrap_failures = macos.BOOTSTRAPS

    run = setup(mac, capsys, *every_flag(mac))

    assert run.code == 1
    assert "launchctl bootstrap failed" in run.out
    assert "Setup removed what it wrote" in run.out
    assert nothing_left(mac)
    assert setup(mac, capsys, *every_flag(mac)).code == 0


def test_undoes_the_install_on_ctrl_c(mac: FakeMac, capsys) -> None:
    mac.enroll_codes = [3]

    def interrupted() -> str:
        raise KeyboardInterrupt

    options = macos.Options.from_args(cli._parser().parse_args(["setup", *every_flag(mac)]))
    with pytest.raises(KeyboardInterrupt):
        macos.setup(options, mac, interrupted)

    assert nothing_left(mac)


# --- Refusals, before anything changes ------------------------------------------------


def nothing_changed(mac: FakeMac) -> bool:
    return not mac.support.exists() and not mac.plist.exists() and mac.calls == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"roots": "/"}, "too broad"),
        ({"roots": "Projects"}, "must be a full path"),
        ({"name": "Studio Mac"}, "lowercase letter"),
        ({"host": "0.0.0.0"}, "not one that listens on every interface"),
        ({"host": "studio.local"}, "IP address"),
        ({"port": "70000"}, "1 to 65535"),
        ({"herdr_bin": "/nowhere/herdr"}, "not an executable"),
        ({"claude_config_dir": "~/.claude"}, "default Claude config"),
        ({"trust_root": "~/Projects"}, "TRUST_ROOT needs"),
        ({"enrollment": "hello"}, "isn't an enrollment string"),
        ({"claude_config_dir": "/tmp/${HOME}"}, "can't contain ${"),
    ],
)
def test_refuses_a_value_the_bridge_would_refuse(mac: FakeMac, capsys, change: dict, reason: str) -> None:
    run = setup(mac, capsys, *every_flag(mac, **change))

    assert run.code == 1
    assert reason in run.out
    assert nothing_changed(mac)


def test_refuses_a_port_in_use_that_was_passed(mac: FakeMac, capsys) -> None:
    mac.holders = {8765: "PID 42: other"}

    run = setup(mac, capsys, *every_flag(mac))

    assert run.code == 1
    assert "Port 8765 is in use by PID 42: other" in run.out
    assert nothing_changed(mac)


def test_refuses_with_sudo(mac: FakeMac, capsys) -> None:
    mac.root = True

    run = setup(mac, capsys, *every_flag(mac))

    assert run.code == 1
    assert "not with sudo" in run.out
    assert nothing_changed(mac)


def test_refuses_from_a_checkout(mac: FakeMac, capsys) -> None:
    mac.checkout = True

    run = setup(mac, capsys, *every_flag(mac))

    assert run.code == 1
    assert "deploy/macos/setup.sh" in run.out
    assert nothing_changed(mac)


def test_refuses_under_uvx(mac: FakeMac, capsys) -> None:
    mac.cached = True

    run = setup(mac, capsys, *every_flag(mac))

    assert run.code == 1
    assert "uv tool install claude-remote-mcp-agent-bridge" in run.out
    assert nothing_changed(mac)


@pytest.mark.parametrize("existing", ["env_file", "state_file", "plist"])
def test_refuses_on_a_mac_that_has_an_install(mac: FakeMac, capsys, existing: str) -> None:
    path = getattr(mac, existing)
    path.parent.mkdir(parents=True)
    path.write_text("kept")

    run = setup(mac, capsys, *every_flag(mac))

    assert run.code == 1
    assert "already installed" in run.out
    assert path.read_text() == "kept"
    assert mac.calls == []


def test_refuses_without_a_lan_address(mac: FakeMac, capsys) -> None:
    mac.lan = None
    flags = every_flag(mac)
    del flags[flags.index("--host"):flags.index("--host") + 2]

    run = setup(mac, capsys, *flags)

    assert run.code == 1
    assert "--host" in run.out
    assert nothing_changed(mac)


def test_refuses_without_herdr(mac: FakeMac, capsys) -> None:
    mac.herdr = None
    flags = every_flag(mac)
    del flags[flags.index("--herdr-bin"):flags.index("--herdr-bin") + 2]

    run = setup(mac, capsys, *flags)

    assert run.code == 1
    assert "herdr not found" in run.out
    assert nothing_changed(mac)


def test_ignores_settings_in_the_environment(mac: FakeMac, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_BRIDGE_PORT", "not a port")

    run = setup(mac, capsys, *every_flag(mac))

    assert run.code == 0, run.out
    assert "AGENT_BRIDGE_PORT is ignored" in run.out


# --- The command the launch agent runs ------------------------------------------------


@pytest.mark.parametrize(
    ("invoked", "runs"),
    [
        # uv tool's shim, which `uv tool upgrade` keeps.
        ("/Users/me/.local/bin/agent-bridge", "/Users/me/.local/bin/agent-bridge"),
        ("/opt/homebrew/bin/agent-bridge", "/opt/homebrew/bin/agent-bridge"),
        # A Homebrew version's own folder goes at the next upgrade; its opt link stays.
        ("/opt/homebrew/Cellar/agent-bridge/0.1.0/bin/agent-bridge", "/opt/homebrew/opt/agent-bridge/bin/agent-bridge"),
        ("/opt/homebrew/Cellar/agent-bridge/0.1.0/libexec/bin/agent-bridge",
         "/opt/homebrew/opt/agent-bridge/libexec/bin/agent-bridge"),
    ],
)
def test_runs_a_path_that_survives_upgrades(invoked: str, runs: str) -> None:
    assert macos.stable_command(invoked) == runs


def test_finds_a_command_run_by_name_on_the_path() -> None:
    assert macos.stable_command("agent-bridge", which=lambda name: f"/opt/homebrew/bin/{name}") == (
        "/opt/homebrew/bin/agent-bridge"
    )


# --- The real Mac, over canned command output -----------------------------------------


@pytest.fixture
def outputs(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, ...], str]:
    """What each command prints; one not listed prints nothing."""
    printed: dict[tuple[str, ...], str] = {}
    monkeypatch.setattr(macos, "_output", lambda *argv: printed.get(argv, ""))
    return printed


LSOF = ("lsof", "-nP", "-iTCP:8765", "-sTCP:LISTEN", "-t")
NETSTAT = ("netstat", "-anv", "-p", "tcp")
NETSTAT_HEADER = "Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)\n"


def test_names_your_own_program_on_the_port(outputs: dict) -> None:
    outputs[LSOF] = "123\n"
    outputs[("ps", "-o", "command=", "-p", "123")] = "python3 -m http.server 8765\n"

    assert macos.MacSystem().port_holder(8765) == "PID 123: python3 -m http.server 8765"


def test_sees_another_users_program_on_the_port(outputs: dict) -> None:
    outputs[NETSTAT] = NETSTAT_HEADER + "tcp4       0      0  *.8765                 *.*                    LISTEN\n"

    assert macos.MacSystem().port_holder(8765) == "another user's program"


def test_a_free_port_has_no_holder(outputs: dict) -> None:
    outputs[NETSTAT] = NETSTAT_HEADER + (
        "tcp4       0      0  *.18765                *.*                    LISTEN\n"
        "tcp4       0      0  10.0.0.2.8765          10.0.0.1.50000         ESTABLISHED\n"
    )

    assert macos.MacSystem().port_holder(8765) is None


def test_finds_the_address_of_the_default_routes_interface(outputs: dict) -> None:
    outputs[("route", "-n", "get", "default")] = "   route to: default\n   interface: en0\n"
    outputs[("ipconfig", "getifaddr", "en0")] = "192.168.1.20\n"

    assert macos.MacSystem().lan_address() == "192.168.1.20"


def test_no_default_route_has_no_address(outputs: dict) -> None:
    assert macos.MacSystem().lan_address() is None


@pytest.mark.parametrize(
    ("address", "has"),
    [("192.168.1.20", True), ("fe80::1", True), ("127.0.0.1", True), ("192.168.1.2", False)],
)
def test_knows_the_interfaces_addresses(outputs: dict, address: str, has: bool) -> None:
    outputs[("ifconfig",)] = (
        "en0: flags=8863<UP,BROADCAST> mtu 1500\n"
        "\tinet6 fe80::1%en0 prefixlen 64 scopeid 0x4\n"
        "\tinet 192.168.1.20 netmask 0xffffff00 broadcast 192.168.1.255\n"
    )

    assert macos.MacSystem().has_address(address) is has


def test_knows_a_checkout_and_uvx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "agent-bridge").mkdir()
    (tmp_path / "agent-bridge" / "pyproject.toml").write_text("")
    monkeypatch.setattr(sys, "prefix", str(tmp_path / ".venv"))
    assert macos.MacSystem().in_checkout()
    assert not macos.MacSystem().in_uv_cache()

    monkeypatch.setattr(sys, "prefix", "/Users/me/.cache/uv/archive-v0/m4XVlWm3syEIKOSH")
    assert not macos.MacSystem().in_checkout()
    assert macos.MacSystem().in_uv_cache()


def test_runs_the_bridge_without_its_settings_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = {}

    def run(argv: list[str], **options) -> object:
        ran.update(argv=argv, **options)
        return type("Result", (), {"returncode": 0, "stdout": "ok\n"})()

    monkeypatch.setattr(macos.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", [COMMAND, "setup"])
    monkeypatch.setenv("AGENT_BRIDGE_PORT", "1")
    monkeypatch.setenv("HOME", "/Users/me")

    assert macos.MacSystem().bridge("check", capture=True) == (0, "ok\n")
    assert ran["argv"] == [COMMAND, "check"]
    assert "AGENT_BRIDGE_PORT" not in ran["env"]
    assert ran["env"]["HOME"] == "/Users/me"


def test_setup_is_for_macos_only(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    assert cli.main(["setup"]) == 1
    assert "for macOS for now" in capsys.readouterr().err
