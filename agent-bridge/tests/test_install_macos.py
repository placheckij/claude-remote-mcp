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
from agent_bridge.config import check_name_format
from agent_bridge.install import common, flow, macos

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
    rename_codes: list[int] = field(default_factory=list)
    unenroll_code: int = 0
    # The port the launch agent listens on.
    service_port: int | None = None
    # What the gateway has for this host: the token (None for no record) and the name.
    gateway_token: str | None = None
    gateway_name: str = ""
    gateway_down: bool = False
    bootstrap_failures: int = 0
    # `launchctl disable` was run on the job.
    job_disabled: bool = False
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

    def serves(self, port: int) -> bool:
        return port == self.service_port

    def find_herdr(self) -> str | None:
        return str(self.herdr) if self.herdr else None

    def disabled(self) -> bool:
        return self.job_disabled

    def launchctl(self, *args: str) -> int:
        self.calls.append(("launchctl", *args))
        if args[0] == "enable":
            self.job_disabled = False
        if args[0] == "bootstrap":
            if self.job_disabled:
                return 5
            if self.bootstrap_failures:
                self.bootstrap_failures -= 1
                return 5
            # The service makes its state file when it starts.
            if not self.state_file.exists():
                self.state_file.write_text(json.dumps({"version": 1, "bridge_id": "b1"}))
        return 0

    def bridge(self, *args: str, capture: bool = False) -> tuple[int, str]:
        """`check`, `enroll`, `rename` and `unenroll`, against the gateway's record above."""
        self.calls.append(("agent-bridge", *args))
        if args[0] == "check":
            if self.slow_starts:
                self.slow_starts -= 1
                return 1, "FAIL  agent-bridge answers at http://192.168.1.20:8765 (ConnectError)\n"
            return 0, "ok    agent-bridge answers at http://192.168.1.20:8765\n" + self._gateway_check()
        if args[0] == "rename":
            code = self.rename_codes.pop(0) if self.rename_codes else 0
            if code in (0, 5):
                self.gateway_name = args[1]
            if code == 0:
                self.save_gateway(args[1])
            return code, ""
        if args[0] == "unenroll":
            if self.unenroll_code == 0 and self.state_file.exists():
                self.state_file.write_text(json.dumps({"version": 1, "bridge_id": "b1"}))
                self.gateway_token = None
            return self.unenroll_code, ""
        code = self.enroll_codes.pop(0) if self.enroll_codes else 0
        if code in (0, 5):
            self.gateway_token = self.env("TOKEN")
            self.gateway_name = self.env("NAME")
        if code == 0:
            self.save_gateway(self.gateway_name)
        return code, ""

    def _gateway_check(self) -> str:
        gateway = self.gateway()
        if gateway is None:
            return "--    not enrolled with a gateway\n"
        failed = f"FAIL  the gateway at {gateway['url']} has this host"
        if self.gateway_down:
            return f"{failed} (ConnectError)\n"
        if self.gateway_token is None:
            return f"{failed} (This agent host isn't enrolled with the gateway.)\n"
        if self.gateway_token != self.env("TOKEN"):
            return f"{failed} (That isn't this agent host's token.)\n"
        # check saves the gateway's name.
        self.save_gateway(self.gateway_name)
        return f"ok    the gateway at {gateway['url']} has this host as {self.gateway_name}\n"

    def env(self, key: str) -> str | None:
        return dotenv_values(self.env_file).get(f"AGENT_BRIDGE_{key}")

    def gateway(self) -> dict | None:
        return json.loads(self.state_file.read_text()).get("gateway") if self.state_file.exists() else None

    def save_gateway(self, name: str) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(
            {"version": 1, "bridge_id": "b1",
             "gateway": {"url": "http://10.0.0.1:8766", "gateway_id": "g1", "name": name}}))

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

    @property
    def log(self) -> Path:
        return self.home / "Library" / "Logs" / "agent-bridge.log"

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


@pytest.mark.parametrize("flag", ["--herdr-service", "--no-herdr-service"])
def test_the_herdr_service_flags_are_refused(mac: FakeMac, capsys, flag: str) -> None:
    run = setup(mac, capsys, flag)

    assert run.code == 1
    assert run.asked == []
    assert "are for Linux" in run.out
    assert not mac.env_file.exists()


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

    # Not enabled: that would leave an override in launchd after uninstall.
    assert mac.ran("launchctl") == [
        ("bootout", f"gui/501/{LABEL}"),
        ("bootstrap", "gui/501", str(mac.plist)),
    ]
    # launchd doesn't make the log's folder.
    assert (mac.home / "Library" / "Logs").is_dir()


def test_enables_a_disabled_launch_agent(mac: FakeMac, capsys) -> None:
    mac.job_disabled = True

    run = setup(mac, capsys, *every_flag(mac))

    assert run.code == 0, run.out
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


@pytest.mark.parametrize(
    ("hostname", "name"),
    [
        ("Studio-Mac.local", "studio-mac"),
        ("-", "mac"),
        ("", "mac"),
        ("a" * 40, "a" * 32),
        # Cut to 32 characters, then the - it ends with.
        ("a" * 31 + "-b", "a" * 31),
    ],
)
def test_the_default_name_is_always_valid(hostname: str, name: str) -> None:
    assert common.default_name(hostname, "mac") == name
    assert check_name_format(name) == name


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
    assert "the gateway has another host named studio; run again with another --name" in run.out
    assert env(mac)["AGENT_BRIDGE_NAME"] == "studio"
    # The run it advises.
    run = setup(mac, capsys, "--name", "studio-2", answers=(ENROLLMENT,))
    assert run.code == 0, run.out
    assert "as studio-2" in run.out


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


# --- Failing partway: running again picks it up --------------------------------------


def test_a_bridge_that_cant_run_stops_setup(mac: FakeMac, capsys) -> None:
    def missing(*args: str, capture: bool = False) -> tuple[int, str]:
        raise macos.Refused(f"can't run {COMMAND} (No such file or directory)")

    mac.bridge = missing

    run = setup(mac, capsys, *every_flag(mac))

    assert run.code == 1
    assert f"can't run {COMMAND}" in run.out


def test_stops_when_the_log_folder_cant_be_made(mac: FakeMac, capsys) -> None:
    (mac.home / "Library").mkdir()
    (mac.home / "Library" / "Logs").write_text("not a folder")

    run = setup(mac, capsys, *every_flag(mac))

    assert run.code == 1
    assert f"can't make {mac.home / 'Library' / 'Logs'}" in run.out


def test_stops_when_the_plist_cant_be_written(mac: FakeMac, capsys) -> None:
    (mac.home / "Library").mkdir()
    (mac.home / "Library" / "LaunchAgents").write_text("not a folder")

    run = setup(mac, capsys, *every_flag(mac))

    assert run.code == 1
    assert f"can't write {mac.plist}" in run.out


def test_runs_again_after_launchd_wouldnt_start_it(mac: FakeMac, capsys) -> None:
    mac.bootstrap_failures = macos.BOOTSTRAPS

    run = setup(mac, capsys, *every_flag(mac))

    assert run.code == 1
    assert "launchctl bootstrap failed" in run.out
    token = env(mac)["AGENT_BRIDGE_TOKEN"]
    run = setup(mac, capsys, answers=(ENROLLMENT,))
    assert run.code == 0, run.out
    assert run.asked == ["Enrollment string it printed, crm1@… [Enter to enroll later"]
    assert env(mac)["AGENT_BRIDGE_TOKEN"] == token
    assert "as studio" in run.out


# --- Running again on an installed Mac ------------------------------------------------

TOKEN = "t" * 64
# What the checkout's setup script had the launch agent run.
CHECKOUT_COMMAND = "/Users/me/claude-remote-mcp/.venv/bin/agent-bridge"


def installed(mac: FakeMac, *, enrolled: bool = True, **changes: str | None) -> dict[str, str]:
    """An install the checkout's setup script made: its env file, plist and enrollment, with
    the service on its port. A change of None leaves that setting out."""
    values = {"TOKEN": TOKEN, "ALLOWED_ROOTS": str(mac.home / "Projects"), "HOST": "192.168.1.20", "PORT": "8765",
              "NAME": "studio", "HERDR_BIN": str(mac.herdr), "CLAUDE_CONFIG_DIR": "", "TRUST_ROOT": ""}
    values.update(changes)
    settings = {f"AGENT_BRIDGE_{key}": value for key, value in values.items() if value is not None}
    mac.support.mkdir(parents=True)
    mac.env_file.write_text(common.env_text(settings))
    mac.plist.parent.mkdir(parents=True)
    mac.plist.write_bytes(plistlib.dumps(macos.launch_agent(CHECKOUT_COMMAND, mac.home / "Library/Logs/agent-bridge.log")))
    port = int(values["PORT"] or macos.DEFAULT_PORT)
    mac.holders[port] = f"PID 7: {CHECKOUT_COMMAND} serve"
    mac.service_port = port
    if enrolled:
        mac.gateway_token, mac.gateway_name = values["TOKEN"], values["NAME"] or "studio"
        mac.save_gateway(mac.gateway_name)
    return settings


def old_plist(mac: FakeMac, **environment: str) -> None:
    """A plist from before the env file, with the settings in it, running uvicorn."""
    mac.plist.parent.mkdir(parents=True)
    mac.plist.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": ["/Users/me/claude-remote-mcp/.venv/bin/uvicorn", "agent_bridge.app:main", "--factory",
                             "--host", "127.0.0.1", "--port", "8770"],
        "EnvironmentVariables": {"AGENT_BRIDGE_TOKEN": TOKEN, "AGENT_BRIDGE_ALLOWED_ROOTS": str(mac.home / "Projects"),
                                 "AGENT_BRIDGE_HERDR_BIN": str(mac.herdr), **environment},
    }))


def test_a_rerun_asks_nothing_and_keeps_the_settings_and_the_enrollment(mac: FakeMac, capsys) -> None:
    settings = installed(mac)

    run = setup(mac, capsys)

    assert run.code == 0, run.out
    assert run.asked == []
    assert env(mac) == settings
    # It asks the gateway first, then checks the restarted service; nothing enrolls again.
    assert mac.ran("agent-bridge") == [("check",), ("check",)]
    assert "enrolled with the gateway at http://10.0.0.1:8766 as studio" in run.out


def test_takes_over_a_checkout_install(mac: FakeMac, capsys) -> None:
    installed(mac)

    run = setup(mac, capsys)

    assert run.code == 0, run.out
    assert plistlib.loads(mac.plist.read_bytes())["ProgramArguments"] == [COMMAND, "serve"]
    assert mac.ran("launchctl")[-1] == ("bootstrap", "gui/501", str(mac.plist))
    # The gateway still reaches it under the same name, with the same token.
    assert mac.gateway_token == env(mac)["AGENT_BRIDGE_TOKEN"] == TOKEN
    assert "as studio: Claude reaches its agents as studio/" in run.out


def test_a_rerun_keeps_settings_setup_doesnt_write(mac: FakeMac, capsys) -> None:
    installed(mac)
    with mac.env_file.open("a") as file:
        file.write("# a comment\nAGENT_BRIDGE_MAX_AGENTS=3\nOTHER='x y'\n")

    run = setup(mac, capsys)

    assert run.code == 0, run.out
    assert env(mac)["AGENT_BRIDGE_MAX_AGENTS"] == "3"
    assert env(mac)["OTHER"] == "x y"
    assert "# a comment" not in mac.env_file.read_text()


def test_a_hand_edited_env_file_is_read_as_the_bridge_reads_it(mac: FakeMac, capsys) -> None:
    installed(mac)
    # Keys in any case, the last one winning.
    with mac.env_file.open("a") as file:
        file.write("agent_bridge_max_agents=2\nAGENT_BRIDGE_MAX_AGENTS=3\n")

    setup(mac, capsys)

    assert env(mac)["AGENT_BRIDGE_MAX_AGENTS"] == "3"
    assert "agent_bridge_max_agents" not in mac.env_file.read_text()


def test_a_value_passed_in_changes_an_installed_setting(mac: FakeMac, capsys) -> None:
    installed(mac)
    (mac.home / "work").mkdir()

    run = setup(mac, capsys, "--roots", str(mac.home / "work"), "--port", "9000")

    assert run.code == 0, run.out
    assert env(mac)["AGENT_BRIDGE_ALLOWED_ROOTS"] == str(mac.home / "work")
    assert env(mac)["AGENT_BRIDGE_PORT"] == "9000"
    assert run.asked == []


def test_folder_trust_is_asked_once(mac: FakeMac, capsys) -> None:
    installed(mac, CLAUDE_CONFIG_DIR=None, TRUST_ROOT=None)

    run = setup(mac, capsys, answers=("-",))

    assert run.asked == ["Separate Claude config folder for agents [" + str(mac.home / ".claude-agents")
                         + "; Enter for that, - for none"]
    assert setup(mac, capsys).asked == []


def test_accounts_are_kept_changed_or_removed(mac: FakeMac, capsys) -> None:
    accounts = '{"work": {"config_dir": null, "folders": ["/Users/me/work"]}}'
    installed(mac, ACCOUNTS=accounts)

    setup(mac, capsys)
    assert env(mac)["AGENT_BRIDGE_ACCOUNTS"] == accounts

    setup(mac, capsys, "--accounts", "")
    assert "AGENT_BRIDGE_ACCOUNTS" not in env(mac)

    setup(mac, capsys, "--accounts", accounts)
    assert env(mac)["AGENT_BRIDGE_ACCOUNTS"] == accounts


def test_the_installed_herdr_wins_over_the_one_found(mac: FakeMac, capsys, tmp_path: Path) -> None:
    installed(mac)
    other = tmp_path / "other-herdr"
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)
    mac.herdr, installed_herdr = other, mac.herdr

    setup(mac, capsys)

    assert env(mac)["AGENT_BRIDGE_HERDR_BIN"] == str(installed_herdr)


def test_the_port_the_service_holds_is_kept(mac: FakeMac, capsys) -> None:
    installed(mac)

    run = setup(mac, capsys)

    assert "in use" not in run.out
    assert env(mac)["AGENT_BRIDGE_PORT"] == "8765"


def test_an_installed_port_another_program_holds_is_named_and_a_free_one_offered(mac: FakeMac, capsys) -> None:
    installed(mac)
    mac.service_port = None
    mac.holders = {8765: "PID 42: python3 -m http.server 8765"}

    run = setup(mac, capsys, answers=("",))

    assert run.asked == ["Port [8766"]
    assert "Port 8765 is in use by PID 42: python3 -m http.server 8765" in run.out
    assert env(mac)["AGENT_BRIDGE_PORT"] == "8766"


def test_an_installed_address_the_mac_no_longer_has_is_replaced(mac: FakeMac, capsys) -> None:
    installed(mac, HOST="192.168.1.5")

    run = setup(mac, capsys)

    assert env(mac)["AGENT_BRIDGE_HOST"] == "192.168.1.20"
    assert "The LAN address is now 192.168.1.20 (was 192.168.1.5)." in run.out


def test_an_offline_rerun_keeps_the_installed_address(mac: FakeMac, capsys) -> None:
    installed(mac, HOST="192.168.1.5")
    mac.lan = None

    run = setup(mac, capsys)

    assert run.code == 0, run.out
    assert env(mac)["AGENT_BRIDGE_HOST"] == "192.168.1.5"
    assert "no interface has 192.168.1.5 right now" in run.out


def test_an_installed_ipv6_address_is_kept_in_ifconfigs_form(mac: FakeMac, capsys) -> None:
    installed(mac, HOST="FE80:0:0:0:0:0:0:1")
    mac.addresses = ("fe80::1",)

    setup(mac, capsys)

    assert env(mac)["AGENT_BRIDGE_HOST"] == "fe80::1"


def test_an_installed_name_that_wont_do_is_asked_for(mac: FakeMac, capsys) -> None:
    installed(mac, enrolled=False, NAME="Studio Mac")

    run = setup(mac, capsys, answers=("studio", ""))

    assert run.code == 0, run.out
    assert "The installed name" in run.out
    assert run.asked[0] == "This host's name in agent ids, like studio-mac/w1:p1 [studio-mac"
    assert env(mac)["AGENT_BRIDGE_NAME"] == "studio"


def test_an_installed_port_that_wont_do_is_refused(mac: FakeMac, capsys) -> None:
    settings = installed(mac)
    mac.env_file.write_text(common.env_text({**settings, "AGENT_BRIDGE_PORT": "http"}))

    run = setup(mac, capsys)

    assert run.code == 1
    assert "the installed port must be a number from 1 to 65535, not http; pass another --port" in run.out
    assert mac.ran("launchctl") == []


def test_a_state_file_that_cant_be_read_is_refused(mac: FakeMac, capsys) -> None:
    installed(mac)
    mac.state_file.write_text("not json")

    run = setup(mac, capsys)

    assert run.code == 1
    assert str(mac.state_file) in run.out
    assert mac.ran("launchctl") == []


def test_an_installed_token_that_wont_do_is_refused(mac: FakeMac, capsys) -> None:
    installed(mac, TOKEN="short")

    run = setup(mac, capsys)

    assert run.code == 1
    assert "run again with --rotate-token" in run.out


# --- Upgrading an install from before the env file ------------------------------------


def test_an_upgrade_moves_the_plist_settings_into_the_env_file(mac: FakeMac, capsys) -> None:
    accounts = '{"work": {"config_dir": null, "folders": ["/Users/me/work"]}}'
    old_plist(mac, AGENT_BRIDGE_CLAUDE_CONFIG_DIR=str(mac.home / ".claude-agents"),
              AGENT_BRIDGE_TRUST_ROOT=str(mac.home / "Projects"), AGENT_BRIDGE_ACCOUNTS=accounts,
              AGENT_BRIDGE_MAX_AGENTS="3", PATH="/usr/bin")
    mac.addresses = ("127.0.0.1",)

    run = setup(mac, capsys, "--name", "studio", "--enrollment", ENROLLMENT)

    assert run.code == 0, run.out
    assert run.asked == []
    assert f"Moving the settings from {mac.plist} to {mac.env_file}" in run.out
    settings = env(mac)
    assert settings["AGENT_BRIDGE_TOKEN"] == TOKEN
    assert settings["AGENT_BRIDGE_HOST"] == "127.0.0.1"
    assert settings["AGENT_BRIDGE_PORT"] == "8770"
    assert settings["AGENT_BRIDGE_TRUST_ROOT"] == str(mac.home / "Projects")
    assert settings["AGENT_BRIDGE_ACCOUNTS"] == accounts
    assert settings["AGENT_BRIDGE_MAX_AGENTS"] == "3"
    assert "PATH" not in settings
    assert plistlib.loads(mac.plist.read_bytes()) == macos.launch_agent(COMMAND, mac.home / "Library/Logs/agent-bridge.log")
    assert "as studio" in run.out


def test_an_upgrade_without_folder_trust_doesnt_ask_for_it(mac: FakeMac, capsys) -> None:
    old_plist(mac)
    mac.addresses = ("127.0.0.1",)

    run = setup(mac, capsys, "--name", "studio", "--enrollment", ENROLLMENT)

    assert run.asked == []
    assert env(mac)["AGENT_BRIDGE_CLAUDE_CONFIG_DIR"] == ""


# --- The gateway's record on a re-run --------------------------------------------------


def test_a_rename_on_the_gateway_is_kept(mac: FakeMac, capsys) -> None:
    installed(mac)
    mac.gateway_name = "studio-2"  # sudo mcp-gateway bridges rename studio studio-2

    run = setup(mac, capsys)

    assert run.asked == []
    assert env(mac)["AGENT_BRIDGE_NAME"] == "studio-2"
    assert "as studio-2" in run.out


def test_an_enrolled_host_with_no_name_installed_takes_the_gateways(mac: FakeMac, capsys) -> None:
    installed(mac, NAME=None)

    run = setup(mac, capsys)

    assert run.asked == []
    assert env(mac)["AGENT_BRIDGE_NAME"] == "studio"


@pytest.mark.parametrize("lost", ["dropped", "other token"])
def test_a_host_the_gateway_lost_is_asked_to_enroll_again(mac: FakeMac, capsys, lost: str) -> None:
    installed(mac)
    mac.gateway_token = None if lost == "dropped" else "o" * 64

    run = setup(mac, capsys, answers=(ENROLLMENT,))

    assert run.code == 0, run.out
    assert "didn't confirm it has this host as studio" in run.out
    assert run.asked == ["Enrollment string it printed, crm1@… [Enter to enroll later"]
    assert mac.ran("agent-bridge")[-1] == ("enroll", ENROLLMENT)
    assert "It is enrolled with the gateway" in run.out


def test_a_host_the_gateway_lost_and_left_unenrolled_says_so(mac: FakeMac, capsys) -> None:
    installed(mac)
    mac.gateway_token = None

    run = setup(mac, capsys)

    assert run.code == 0, run.out
    assert "didn't confirm it has it as studio (see the warning above)" in run.out


def test_a_gateway_that_cant_be_asked_keeps_the_enrollment(mac: FakeMac, capsys) -> None:
    installed(mac)
    mac.gateway_down = True

    run = setup(mac, capsys)

    assert run.code == 0, run.out
    assert run.asked == []
    assert "couldn't check with the gateway at http://10.0.0.1:8766 right now (ConnectError)" in run.out


def test_a_new_name_on_an_enrolled_host_renames_it_on_the_gateway(mac: FakeMac, capsys) -> None:
    installed(mac)

    run = setup(mac, capsys, "--name", "studio-2")

    assert run.code == 0, run.out
    assert run.asked == []
    assert ("rename", "studio-2") in mac.ran("agent-bridge")
    assert env(mac)["AGENT_BRIDGE_NAME"] == "studio-2"
    assert "as studio-2" in run.out


def test_a_new_name_with_a_bad_enrollment_string_isnt_renamed(mac: FakeMac, capsys) -> None:
    settings = installed(mac)

    run = setup(mac, capsys, "--name", "studio-2", "--enrollment", "crm1@typo")

    assert run.code == 1
    assert "isn't an enrollment string" in run.out
    assert "note:" not in run.out
    assert mac.ran("agent-bridge") == [("check",)]
    assert env(mac) == settings


def test_a_new_name_taken_on_the_gateway_changes_nothing(mac: FakeMac, capsys) -> None:
    settings = installed(mac)
    mac.rename_codes = [3]

    run = setup(mac, capsys, "--name", "studio-2")

    assert run.code == 1
    assert "another host named studio-2; nothing was changed" in run.out
    assert env(mac) == settings
    assert mac.ran("launchctl") == []


@pytest.mark.parametrize("code", [4, 6])
def test_a_new_name_the_gateway_cant_take_is_renamed_by_enrolling_again(mac: FakeMac, capsys, code: int) -> None:
    installed(mac)
    mac.rename_codes = [code]

    run = setup(mac, capsys, "--name", "studio-2", answers=(ENROLLMENT,))

    assert run.code == 0, run.out
    assert "The gateway has this host as studio; enrolling again renames it studio-2." in run.out
    assert mac.ran("agent-bridge")[-1] == ("enroll", ENROLLMENT)
    assert "as studio-2" in run.out


def test_a_new_name_left_unrenamed_says_the_gateway_kept_the_old_one(mac: FakeMac, capsys) -> None:
    installed(mac)
    mac.rename_codes = [4]

    run = setup(mac, capsys, "--name", "studio-2")

    assert run.code == 0, run.out
    assert "The gateway still has it as studio, not studio-2" in run.out


def test_a_rename_the_state_file_missed_isnt_asked_to_enroll(mac: FakeMac, capsys) -> None:
    installed(mac)
    mac.rename_codes = [5]

    run = setup(mac, capsys, "--name", "studio-2")

    assert run.code == 0, run.out
    assert run.asked == []
    assert "as studio-2" in run.out


def test_setup_stopping_after_a_rename_says_the_gateway_has_the_new_name(mac: FakeMac, capsys) -> None:
    installed(mac)
    mac.bootstrap_failures = macos.BOOTSTRAPS

    run = setup(mac, capsys, "--name", "studio-2")

    assert run.code == 1
    assert "note: the gateway has this host as studio-2 now; the next run takes that name." in run.out


def test_a_taken_name_on_a_rerun_keeps_the_install(mac: FakeMac, capsys) -> None:
    installed(mac, enrolled=False)
    mac.enroll_codes = [3]

    run = setup(mac, capsys, "--enrollment", ENROLLMENT)

    assert run.code == 1
    assert "run again with another --name" in run.out
    assert env(mac)["AGENT_BRIDGE_TOKEN"] == TOKEN


# --- A new token ----------------------------------------------------------------------


def test_rotate_token_replaces_the_token_and_enrolls_again(mac: FakeMac, capsys) -> None:
    installed(mac)

    run = setup(mac, capsys, "--rotate-token", "--enrollment", ENROLLMENT)

    assert run.code == 0, run.out
    assert env(mac)["AGENT_BRIDGE_TOKEN"] not in (TOKEN, None)
    assert mac.gateway_token == env(mac)["AGENT_BRIDGE_TOKEN"]


def test_rotate_token_without_an_enrollment_string_changes_nothing(mac: FakeMac, capsys) -> None:
    settings = installed(mac)

    run = setup(mac, capsys, "--rotate-token")

    assert run.code == 1
    assert "--rotate-token needs an enrollment string" in run.out
    assert env(mac) == settings
    assert mac.ran("launchctl") == []


@pytest.mark.parametrize("codes", [[1], [3]])
def test_rotate_token_puts_the_installed_token_back_when_enrolling_fails(mac: FakeMac, capsys, codes: list) -> None:
    installed(mac)
    mac.enroll_codes = codes

    run = setup(mac, capsys, "--rotate-token", "--enrollment", ENROLLMENT)

    assert run.code == 1
    assert "the installed one is back" in run.out
    assert env(mac)["AGENT_BRIDGE_TOKEN"] == TOKEN
    assert mac.ran("launchctl")[-1][0] == "bootstrap"


def test_rotate_token_puts_the_installed_token_back_when_launchd_wont_start_it(mac: FakeMac, capsys) -> None:
    installed(mac)
    mac.bootstrap_failures = 2 * macos.BOOTSTRAPS

    run = setup(mac, capsys, "--rotate-token", "--enrollment", ENROLLMENT)

    assert run.code == 1
    assert "launchctl bootstrap failed" in run.out
    assert "the installed one is back" in run.out
    assert env(mac)["AGENT_BRIDGE_TOKEN"] == TOKEN


def test_rotate_token_puts_the_installed_token_back_when_the_plist_cant_be_written(mac: FakeMac, capsys) -> None:
    installed(mac)
    mac.plist.unlink()
    mac.plist.parent.rmdir()
    mac.plist.parent.write_text("not a folder")

    run = setup(mac, capsys, "--rotate-token", "--enrollment", ENROLLMENT)

    assert run.code == 1
    assert f"can't write {mac.plist}" in run.out
    assert "the installed one is back" in run.out
    assert env(mac)["AGENT_BRIDGE_TOKEN"] == TOKEN


def test_rotate_token_puts_the_installed_token_back_on_ctrl_c(mac: FakeMac, capsys) -> None:
    installed(mac)
    mac.enroll_codes = [1]

    def interrupted() -> str:
        raise KeyboardInterrupt

    options = macos.Options.from_args(cli._parser().parse_args(["setup", "--rotate-token", "--enrollment", ENROLLMENT]))
    with pytest.raises(KeyboardInterrupt):
        macos.setup(options, mac, interrupted)

    assert env(mac)["AGENT_BRIDGE_TOKEN"] == TOKEN


def test_rotate_token_keeps_the_new_token_when_enroll_says_the_gateway_took_it(mac: FakeMac, capsys) -> None:
    installed(mac)
    mac.enroll_codes = [5]

    run = setup(mac, capsys, "--rotate-token", "--enrollment", ENROLLMENT)

    assert env(mac)["AGENT_BRIDGE_TOKEN"] != TOKEN
    assert "state file couldn't be written" in run.out


def test_rotate_token_keeps_the_new_token_when_the_gateway_has_it(mac: FakeMac, capsys) -> None:
    installed(mac)
    original = mac.bridge

    def took_it_but_lost_the_answer(*args: str, capture: bool = False) -> tuple[int, str]:
        if args[0] == "enroll":
            mac.gateway_token = mac.env("TOKEN")
            return 1, ""
        return original(*args, capture=capture)

    mac.bridge = took_it_but_lost_the_answer

    run = setup(mac, capsys, "--rotate-token", "--enrollment", ENROLLMENT)

    assert run.code == 0, run.out
    assert env(mac)["AGENT_BRIDGE_TOKEN"] == mac.gateway_token != TOKEN


def test_a_new_token_on_a_host_enrolled_before_asks_for_an_enrollment_string(mac: FakeMac, capsys) -> None:
    installed(mac, TOKEN=None)

    run = setup(mac, capsys, answers=("",))

    assert run.asked == ["Enrollment string it printed, crm1@… [Enter to enroll later"]
    assert "with an older token, so it can't reach it" in run.out


# --- Uninstalling ---------------------------------------------------------------------


def uninstall(mac: FakeMac, capsys: pytest.CaptureFixture) -> tuple[int, str]:
    code = macos.uninstall(mac)
    out, err = capsys.readouterr()
    return code, out + err


def test_uninstall_leaves_the_gateway_then_removes_the_service_and_files(mac: FakeMac, capsys) -> None:
    installed(mac)
    mac.log.parent.mkdir(parents=True)
    mac.log.write_text("log\n")

    code, out = uninstall(mac, capsys)

    assert code == 0
    assert mac.ran("agent-bridge") == [("unenroll",)]
    assert mac.gateway_token is None
    assert mac.ran("launchctl") == [("bootout", f"gui/501/{LABEL}")]
    assert not any(path.exists() for path in (mac.plist, mac.log, mac.support))
    assert "agent-bridge is removed" in out


def test_uninstall_of_a_host_never_enrolled_says_how_to_remove_one_added_by_hand(mac: FakeMac, capsys) -> None:
    installed(mac, enrolled=False)

    _, out = uninstall(mac, capsys)

    assert "added with bridges add" in out


def test_uninstall_removes_what_an_interrupted_write_left(mac: FakeMac, capsys) -> None:
    installed(mac)
    for path in (mac.env_file, mac.plist):
        path.with_name(f"{path.name}.tmp").write_text(TOKEN)

    uninstall(mac, capsys)

    assert not mac.support.exists()
    assert not mac.plist.with_name(f"{mac.plist.name}.tmp").exists()


def test_uninstall_keeps_a_folder_with_other_files_in_it(mac: FakeMac, capsys) -> None:
    installed(mac)
    (mac.support / "notes").write_text("mine")

    _, out = uninstall(mac, capsys)

    assert (mac.support / "notes").read_text() == "mine"
    assert not mac.env_file.exists()
    assert f"Left {mac.support}: it holds other files." in out


@pytest.mark.parametrize("failure", ["unenroll fails", "the bridge can't run"])
def test_uninstall_still_removes_the_service_and_files(mac: FakeMac, capsys, failure: str) -> None:
    installed(mac)
    if failure == "unenroll fails":
        mac.unenroll_code = 1
    else:
        def missing(*args: str, capture: bool = False) -> tuple[int, str]:
            raise macos.Refused(f"can't run {COMMAND} (No such file or directory)")

        mac.bridge = missing

    code, out = uninstall(mac, capsys)

    assert code == 0
    assert "sudo mcp-gateway bridges remove studio" in out
    assert not mac.plist.exists() and not mac.support.exists()


def test_uninstalling_twice_says_nothing_about_a_folder_that_is_gone(mac: FakeMac, capsys) -> None:
    installed(mac)
    uninstall(mac, capsys)

    code, out = uninstall(mac, capsys)

    assert code == 0
    assert "Left" not in out


def test_uninstall_refuses_sudo(mac: FakeMac, capsys) -> None:
    installed(mac)
    mac.root = True

    code, out = uninstall(mac, capsys)

    assert code == 1
    assert "not with sudo" in out
    assert mac.env_file.exists()


# --- Refusals, before anything changes ------------------------------------------------


def nothing_changed(mac: FakeMac) -> bool:
    return not mac.support.exists() and not mac.plist.exists() and mac.calls == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"roots": "/"}, "too broad"),
        ({"roots": "Projects"}, "must be a full path"),
        ({"roots": "~nosuchuser-crm/work"}, "must be a full path"),
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


def test_writes_quotes_and_backslashes_as_the_bridge_reads_them(mac: FakeMac, capsys) -> None:
    roots = f"{mac.home}/O'Brien\\"

    run = setup(mac, capsys, *every_flag(mac, roots=roots))

    assert run.code == 0, run.out
    assert env(mac)["AGENT_BRIDGE_ALLOWED_ROOTS"] == roots


def test_refuses_settings_that_wouldnt_read_back(mac: FakeMac, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "dotenv_values", lambda path: {})

    run = setup(mac, capsys, *every_flag(mac))

    assert run.code == 1
    assert "wouldn't read back as written" in run.out
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
    assert "uv tool install --editable ./agent-bridge" in run.out
    assert nothing_changed(mac)


def test_refuses_under_uvx(mac: FakeMac, capsys) -> None:
    mac.cached = True

    run = setup(mac, capsys, *every_flag(mac))

    assert run.code == 1
    assert "uv tool install claude-remote-mcp-agent-bridge" in run.out
    assert nothing_changed(mac)


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


@pytest.mark.parametrize(("job", "serves"), [("123", True), ("456", False), (None, False)])
def test_the_service_holds_the_port_when_launchd_runs_the_listener(
    outputs: dict, monkeypatch: pytest.MonkeyPatch, job: str | None, serves: bool
) -> None:
    monkeypatch.setattr(macos.os, "getuid", lambda: 501)
    outputs[LSOF] = "123\n"
    if job is not None:
        outputs[("launchctl", "print", f"gui/501/{LABEL}")] = f"gui/501/{LABEL} = {{\n\tstate = running\n\tpid = {job}\n}}\n"

    assert macos.MacSystem().serves(8765) is serves


@pytest.mark.parametrize(("listed", "disabled"), [
    (f'\t"{LABEL}" => disabled\n', True),
    (f'\t"{LABEL}" => true\n', True),
    (f'\t"{LABEL}" => enabled\n', False),
    (f'\t"{LABEL}-TEST" => disabled\n', False),
    ("", False),
])
def test_reads_whether_the_launch_agent_is_disabled(
    outputs: dict, monkeypatch: pytest.MonkeyPatch, listed: str, disabled: bool
) -> None:
    monkeypatch.setattr(macos.os, "getuid", lambda: 501)
    outputs[("launchctl", "print-disabled", "gui/501")] = f"disabled services = {{\n{listed}}}\n"

    assert macos.MacSystem().disabled() is disabled


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


def test_finds_herdr_on_the_path_else_in_the_usual_places(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, set_home: Callable[[Path], None]
) -> None:
    set_home(tmp_path)
    local = tmp_path / ".local" / "bin" / "herdr"
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\n")
    local.chmod(0o644)
    on_path = tmp_path / "path" / "herdr"
    on_path.parent.mkdir()
    on_path.write_text("#!/bin/sh\n")
    on_path.chmod(0o755)
    monkeypatch.setattr(flow.shutil, "which", lambda name: str(on_path))
    assert macos.MacSystem().find_herdr() == str(on_path)

    monkeypatch.setattr(flow.shutil, "which", lambda name: None)
    # Not executable: passed over.
    assert macos.MacSystem().find_herdr() in (None, "/opt/homebrew/bin/herdr", "/usr/local/bin/herdr")
    local.chmod(0o755)
    assert macos.MacSystem().find_herdr() == str(local)


def test_cant_run_a_missing_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(argv: list[str], **options) -> object:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(flow.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", [COMMAND, "setup"])

    with pytest.raises(macos.Refused, match=f"can't run {COMMAND} \\(No such file or directory\\)"):
        macos.MacSystem().bridge("check", capture=True)


def test_runs_the_bridge_without_its_settings_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = {}

    def run(argv: list[str], **options) -> object:
        ran.update(argv=argv, **options)
        return type("Result", (), {"returncode": 0, "stdout": "ok\n"})()

    monkeypatch.setattr(flow.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", [COMMAND, "setup"])
    monkeypatch.setenv("AGENT_BRIDGE_PORT", "1")
    monkeypatch.setenv("HOME", "/Users/me")

    assert macos.MacSystem().bridge("check", capture=True) == (0, "ok\n")
    assert ran["argv"] == [COMMAND, "check"]
    # Its errors come back with its output.
    assert ran["stderr"] == flow.subprocess.STDOUT
    assert "AGENT_BRIDGE_PORT" not in ran["env"]
    assert ran["env"]["HOME"] == "/Users/me"


@pytest.mark.parametrize("command", ["setup", "uninstall"])
def test_setup_and_uninstall_are_not_for_other_platforms(monkeypatch: pytest.MonkeyPatch, capsys, command: str) -> None:
    monkeypatch.setattr(sys, "platform", "freebsd14")

    assert cli.main([command]) == 1
    assert f"agent-bridge {command} is for macOS, Linux and Windows" in capsys.readouterr().err
