"""agent-bridge/deploy/linux/setup.sh and uninstall.sh, run from a copy of the checkout against a
temporary HOME, with uv, systemctl, loginctl, ss, ps, ip, herdr and the venv's agent-bridge
replaced by stubs: what they write, ask and run."""

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the Linux deploy is for Linux")

DEPLOY = Path(__file__).parent.parent / "agent-bridge" / "deploy"
UNIT = "agent-bridge.service"
HERDR_UNIT = "agent-bridge-herdr.service"
ENROLLMENT = "crm1@10.0.0.1:8766#" + "a" * 24
# What setup.sh prints before reading an answer.
PROMPT = re.compile(r"\]: ")

STUBS = {
    "uv": "exit 0",
    # `herdr status server`: running when $HOME/herdr-running exists.
    "herdr": """
if [ "$1 $2" = "status server" ]; then
  if [ -f "$HOME/herdr-running" ]; then echo "status: running"; else echo "status: not running"; fi
fi
exit 0""",
    # Logs. `show -p MainPID --value <unit>`: $HOME/mainpid, else 0. `is-active`: whether
    # $HOME/active-<unit> exists, which `start` and `restart` create. No user manager when
    # $HOME/no-user-manager exists.
    "systemctl": """
[ ! -f "$HOME/no-user-manager" ] || exit 1
echo "$*" >> "$HOME/systemctl.log"
for arg; do unit=$arg; done
case $2 in
  show) cat "$HOME/mainpid" 2>/dev/null || echo 0 ;;
  is-active) [ -f "$HOME/active-$unit" ] || exit 3 ;;
  start|restart) touch "$HOME/active-$unit" ;;
esac
exit 0""",
    # `show-user <user> -p Linger --value`: $HOME/linger, else no. `enable-linger` fails when
    # $HOME/linger-denied exists.
    "loginctl": """
echo "$*" >> "$HOME/loginctl.log"
case $1 in
  show-user) cat "$HOME/linger" 2>/dev/null || echo no ;;
  enable-linger) [ ! -f "$HOME/linger-denied" ] || exit 1; echo yes > "$HOME/linger" ;;
esac
exit 0""",
    # `ss -Hltnp "sport = :<port>"`: a listener when $HOME/port-<port> exists, with the PID
    # in it, or none shown for "?".
    "ss": """
for arg; do case $arg in "sport = :"*) port=${arg#sport = :} ;; esac; done
[ -f "$HOME/port-$port" ] || exit 0
pid=$(cat "$HOME/port-$port")
if [ "$pid" = "?" ]; then echo "LISTEN 0 128 0.0.0.0:$port 0.0.0.0:*"
else echo "LISTEN 0 128 0.0.0.0:$port 0.0.0.0:* users:((\\"prog\\",pid=$pid,fd=3))"; fi""",
    # `ps -o args= -p <pid>`: the command line in $HOME/ps-<pid>.
    "ps": """
for arg; do pid=$arg; done
[ -f "$HOME/ps-$pid" ] && cat "$HOME/ps-$pid"
exit 0""",
    "ip": """
case "$*" in
  *"route get"*) echo "1.1.1.1 via 10.0.0.254 dev eth0 src 10.0.0.5 uid 1000" ;;
  *"addr show"*) echo "2: eth0    inet 10.0.0.5/24 brd 10.0.0.255 scope global eth0" ;;
esac""",
}

# The venv's agent-bridge: enroll exits with the next code in $HOME/enroll-codes (0 when
# there is none) and on 0 writes the state file with the name from the env file.
AGENT_BRIDGE = """#!/bin/sh
echo "$*" >> "$HOME/agent-bridge.log"
env="$HOME/.config/agent-bridge/env"
state="$HOME/.local/state/agent-bridge/state.json"
case $1 in
  check)
    case $2 in
      --name-format) case $3 in ''|*[!a-z0-9-]*) echo "The name must be lowercase." >&2; exit 2 ;; esac ;;
      --token-format) ;;
      *) echo "ok    agent-bridge answers" ;;
    esac ;;
  enroll)
    code=$(head -n 1 "$HOME/enroll-codes" 2>/dev/null)
    sed -i 1d "$HOME/enroll-codes" 2>/dev/null
    [ "${code:-0}" = 0 ] || { echo "Couldn't enroll (stub exit $code)." >&2; exit "$code"; }
    name=$(sed -n "s/^AGENT_BRIDGE_NAME='\\(.*\\)'$/\\1/p" "$env")
    mkdir -p "$(dirname "$state")"
    printf '{\\n  "version": 1,\\n  "bridge_id": "b1",\\n  "gateway": {\\n    "url": "http://10.0.0.1:8766",\\n    "gateway_id": "g1",\\n    "name": "%s"\\n  }\\n}' \\
      "$name" > "$state"
    echo "Enrolled as $name." ;;
  unenroll) rm -f "$state"; echo "Left the gateway." ;;
esac
exit 0
"""


@dataclass
class Host:
    home: Path
    repo: Path
    stubs: Path

    @property
    def env_file(self) -> Path:
        return self.home / ".config" / "agent-bridge" / "env"

    @property
    def state_file(self) -> Path:
        return self.home / ".local" / "state" / "agent-bridge" / "state.json"

    def unit(self, name: str) -> Path:
        return self.home / ".config" / "systemd" / "user" / name

    def run(self, script: str, input: str | None = None, check: bool = True, **env: str) -> subprocess.CompletedProcess:
        for log in ("systemctl.log", "loginctl.log", "agent-bridge.log"):
            (self.home / log).unlink(missing_ok=True)
        result = subprocess.run(
            [str(self.repo / "agent-bridge" / "deploy" / script)],
            env={"HOME": str(self.home), "PATH": f"{self.stubs}:/usr/bin:/bin", **env},
            input=input,
            stdin=None if input is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        if check:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    def setup(self, input: str | None = None, check: bool = True, **env: str) -> subprocess.CompletedProcess:
        return self.run("linux/setup.sh", input, check, **env)

    def install(self, **env: str) -> subprocess.CompletedProcess:
        """A fresh install with every answer given, folder trust off."""
        return self.setup(**{
            "HOST": "127.0.0.1", "ALLOWED_ROOTS": str(self.home / "projects"), "NAME": "linux-box",
            "AGENT_BRIDGE_CLAUDE_CONFIG_DIR": "", "TRUST_ROOT": "", "ENROLLMENT": ENROLLMENT, **env,
        })

    def env(self) -> dict[str, str]:
        lines = self.env_file.read_text().splitlines()
        return {key: value.strip("'") for key, _, value in (line.partition("=") for line in lines if line)}

    def log(self, name: str) -> list[str]:
        path = self.home / name
        return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def host(tmp_path: Path) -> Host:
    repo = tmp_path / "repo"
    shutil.copytree(DEPLOY, repo / "agent-bridge" / "deploy")
    (repo / "agent-bridge" / "pyproject.toml").touch()
    bin_dir = repo / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "agent-bridge").write_text(AGENT_BRIDGE)
    (bin_dir / "agent-bridge").chmod(0o755)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in STUBS.items():
        (stubs / name).write_text(f"#!/bin/sh\n{body}\n")
        (stubs / name).chmod(0o755)
    home = tmp_path / "home"
    (home / "projects").mkdir(parents=True)
    return Host(home, repo, stubs)


def test_a_fresh_install_writes_the_settings_starts_the_services_and_enrolls(host: Host) -> None:
    result = host.setup(HOST="127.0.0.1", ALLOWED_ROOTS=str(host.home / "projects"), NAME="linux-box",
                        ENROLLMENT=ENROLLMENT)

    env = host.env()
    assert re.fullmatch(r"[0-9a-f]{64}", env["AGENT_BRIDGE_TOKEN"])
    assert env["AGENT_BRIDGE_ALLOWED_ROOTS"] == str(host.home / "projects")
    assert env["AGENT_BRIDGE_HOST"] == "127.0.0.1"
    assert env["AGENT_BRIDGE_PORT"] == "8765"
    assert env["AGENT_BRIDGE_NAME"] == "linux-box"
    assert env["AGENT_BRIDGE_HERDR_BIN"] == str(host.stubs / "herdr")
    # Folder trust was asked, and no input left took the defaults.
    assert "Separate Claude config folder" in result.stderr
    assert env["AGENT_BRIDGE_CLAUDE_CONFIG_DIR"] == str(host.home / ".claude-agents")
    assert env["AGENT_BRIDGE_TRUST_ROOT"] == str(host.home / "projects")
    assert host.env_file.stat().st_mode & 0o777 == 0o600
    assert host.env_file.parent.stat().st_mode & 0o777 == 0o700

    unit = host.unit(UNIT).read_text()
    assert f"ExecStart={host.repo}/.venv/bin/agent-bridge serve\n" in unit
    assert "AGENT_BRIDGE_" not in unit
    assert f"ExecStart={host.stubs}/herdr server\n" in host.unit(HERDR_UNIT).read_text()
    systemctl = host.log("systemctl.log")
    assert "--user daemon-reload" in systemctl
    assert f"--user enable --quiet {UNIT}" in systemctl
    assert f"--user restart {UNIT}" in systemctl
    assert f"--user enable --quiet {HERDR_UNIT}" in systemctl
    assert f"--user start {HERDR_UNIT}" in systemctl
    # herdr's server is up before the bridge starts, so the bridge's checks see it.
    assert systemctl.index(f"--user start {HERDR_UNIT}") < systemctl.index(f"--user restart {UNIT}")
    assert any(line.startswith("enable-linger") for line in host.log("loginctl.log"))
    assert f"enroll {ENROLLMENT}" in host.log("agent-bridge.log")

    assert "enrolled with the gateway at http://10.0.0.1:8766 as linux-box" in result.stdout
    assert env["AGENT_BRIDGE_TOKEN"] not in result.stdout + result.stderr
    assert "bridges add" not in result.stdout


def test_the_lan_address_is_found(host: Host) -> None:
    host.setup(ALLOWED_ROOTS=str(host.home / "projects"), NAME="linux-box", AGENT_BRIDGE_CLAUDE_CONFIG_DIR="",
               TRUST_ROOT="", ENROLLMENT="")

    assert host.env()["AGENT_BRIDGE_HOST"] == "10.0.0.5"


def test_the_settings_from_the_readme_are_taken_over_and_the_token_kept(host: Host) -> None:
    # The env file the README told people to write by hand, unquoted.
    host.env_file.parent.mkdir(parents=True)
    host.env_file.write_text(
        f"AGENT_BRIDGE_TOKEN={'t' * 40}\n"
        f"AGENT_BRIDGE_ALLOWED_ROOTS={host.home / 'projects'}\n"
        "AGENT_BRIDGE_HOST=127.0.0.1\n"
        "AGENT_BRIDGE_NAME=linux-box\n"
        f"AGENT_BRIDGE_HERDR_BIN={host.stubs / 'herdr'}\n"
    )

    result = host.setup(ENROLLMENT=ENROLLMENT, input="-\n")

    env = host.env()
    assert env["AGENT_BRIDGE_TOKEN"] == "t" * 40
    assert env["AGENT_BRIDGE_HOST"] == "127.0.0.1"
    assert env["AGENT_BRIDGE_NAME"] == "linux-box"
    assert env["AGENT_BRIDGE_ALLOWED_ROOTS"] == str(host.home / "projects")
    # Only folder trust, which that file didn't answer, was asked.
    assert PROMPT.findall(result.stderr) == ["]: "], result.stderr
    assert env["AGENT_BRIDGE_CLAUDE_CONFIG_DIR"] == ""
    assert "as linux-box" in result.stdout


def test_a_rerun_asks_nothing_keeps_the_settings_and_leaves_herdr_running(host: Host) -> None:
    host.install()
    before = host.env()

    result = host.setup()

    assert not PROMPT.search(result.stderr), result.stderr
    assert host.env() == before
    assert not [line for line in host.log("agent-bridge.log") if line.startswith("enroll")]
    systemctl = host.log("systemctl.log")
    assert f"--user restart {UNIT}" in systemctl
    assert not [line for line in systemctl if HERDR_UNIT in line and ("start" in line or "stop" in line)]
    assert f"herdr's server runs as {HERDR_UNIT}" in result.stdout
    assert "enrolled with the gateway at http://10.0.0.1:8766 as linux-box" in result.stdout


def test_a_herdr_server_started_by_hand_is_left_to_run(host: Host) -> None:
    (host.home / "herdr-running").touch()

    result = host.install()

    systemctl = host.log("systemctl.log")
    assert f"--user enable --quiet {HERDR_UNIT}" in systemctl
    assert f"--user start {HERDR_UNIT}" not in systemctl
    assert "already runs outside systemd" in result.stdout


def test_herdr_service_0_installs_no_herdr_unit_and_removes_one_installed(host: Host) -> None:
    host.install()
    assert host.unit(HERDR_UNIT).exists()

    host.setup(HERDR_SERVICE="0")

    assert not host.unit(HERDR_UNIT).exists()
    systemctl = host.log("systemctl.log")
    assert f"--user disable --quiet {HERDR_UNIT}" in systemctl
    assert not [line for line in systemctl if HERDR_UNIT in line and "stop" in line]


def test_a_rerun_keeps_settings_the_setup_doesnt_manage(host: Host) -> None:
    host.install()
    with host.env_file.open("a") as env:
        env.write("AGENT_BRIDGE_MAX_AGENTS=3\n")

    host.setup()

    assert host.env()["AGENT_BRIDGE_MAX_AGENTS"] == "3"


def test_a_variable_passed_in_changes_an_installed_setting(host: Host) -> None:
    host.install()
    other = host.home / "other"
    other.mkdir()

    host.setup(ALLOWED_ROOTS=str(other))

    assert host.env()["AGENT_BRIDGE_ALLOWED_ROOTS"] == str(other)


def test_rotate_token_replaces_the_token_and_enrolls_again(host: Host) -> None:
    host.install()
    first = host.env()["AGENT_BRIDGE_TOKEN"]

    host.setup(ROTATE_TOKEN="1", ENROLLMENT=ENROLLMENT)

    assert host.env()["AGENT_BRIDGE_TOKEN"] != first
    assert f"enroll {ENROLLMENT}" in host.log("agent-bridge.log")


def test_rotate_token_without_an_enrollment_string_changes_nothing(host: Host) -> None:
    host.install()
    before = host.env()

    result = host.setup(ROTATE_TOKEN="1", check=False)

    assert result.returncode != 0
    assert "enrollment string" in result.stderr
    assert host.env() == before


def test_a_port_another_program_holds_is_named_and_a_free_one_offered(host: Host) -> None:
    (host.home / "port-8765").write_text("999\n")
    (host.home / "ps-999").write_text("/usr/sbin/nginx -g daemon off;\n")

    result = host.install()

    assert "PID 999: /usr/sbin/nginx" in result.stdout
    assert "[8766]: " in result.stderr
    assert host.env()["AGENT_BRIDGE_PORT"] == "8766"


def test_a_port_another_users_program_holds_is_reported_without_a_pid(host: Host) -> None:
    (host.home / "port-8765").write_text("?\n")
    (host.home / "port-8766").write_text("?\n")

    result = host.install()

    assert "another user's program" in result.stdout
    assert "[8767]: " in result.stderr
    assert host.env()["AGENT_BRIDGE_PORT"] == "8767"


def test_the_port_the_installed_service_holds_is_kept(host: Host) -> None:
    (host.home / "port-8765").write_text("4242\n")
    (host.home / "mainpid").write_text("4242\n")
    (host.home / "ps-4242").write_text("/usr/bin/python3 something\n")

    result = host.install()

    assert host.env()["AGENT_BRIDGE_PORT"] == "8765"
    assert not PROMPT.search(result.stderr), result.stderr


def test_an_agent_bridge_run_by_hand_on_the_port_must_be_stopped_first(host: Host) -> None:
    (host.home / "port-8765").write_text("4242\n")
    (host.home / "ps-4242").write_text(f"{host.repo}/.venv/bin/python {host.repo}/.venv/bin/agent-bridge serve\n")

    result = host.install(check=False)

    assert result.returncode != 0
    assert "outside the service, PID 4242" in result.stdout
    assert "stop agent-bridge (PID 4242)" in result.stderr
    assert not host.env_file.exists()
    assert f"--user restart {UNIT}" not in host.log("systemctl.log")


def test_lingering_already_on_is_left_alone(host: Host) -> None:
    (host.home / "linger").write_text("yes\n")

    host.install()

    assert not [line for line in host.log("loginctl.log") if line.startswith("enable-linger")]


def test_lingering_refused_still_installs_and_says_how_to_turn_it_on(host: Host) -> None:
    (host.home / "linger-denied").touch()

    result = host.install()

    assert "sudo loginctl enable-linger" in result.stdout
    assert f"--user restart {UNIT}" in host.log("systemctl.log")


def test_without_a_user_manager_it_stops_before_changing_anything(host: Host) -> None:
    (host.home / "no-user-manager").touch()

    result = host.install(check=False)

    assert result.returncode != 0
    assert "systemd user manager" in result.stderr
    assert not host.env_file.exists()


def test_a_taken_name_is_asked_again(host: Host) -> None:
    (host.home / "enroll-codes").write_text("3\n0\n")

    result = host.setup(HOST="127.0.0.1", ALLOWED_ROOTS=str(host.home / "projects"), NAME="linux-box",
                        AGENT_BRIDGE_CLAUDE_CONFIG_DIR="", TRUST_ROOT="", ENROLLMENT=ENROLLMENT, input="neura\n")

    assert host.env()["AGENT_BRIDGE_NAME"] == "neura"
    assert host.log("agent-bridge.log").count(f"enroll {ENROLLMENT}") == 2
    assert "as neura" in result.stdout


def test_a_taken_name_with_no_input_left_stops(host: Host) -> None:
    (host.home / "enroll-codes").write_text("3\n")

    result = host.install(check=False)

    assert result.returncode != 0
    assert "NAME" in result.stderr


def test_an_unreachable_gateway_leaves_the_service_running_unenrolled(host: Host) -> None:
    (host.home / "enroll-codes").write_text("4\n")

    result = host.install()

    assert "not enrolled" in result.stdout
    assert f"{host.repo}/.venv/bin/agent-bridge enroll" in result.stdout
    assert f"--user restart {UNIT}" in host.log("systemctl.log")


def test_enter_at_the_enrollment_question_installs_without_enrolling(host: Host) -> None:
    result = host.install(ENROLLMENT="")

    assert not [line for line in host.log("agent-bridge.log") if line.startswith("enroll")]
    assert "not enrolled" in result.stdout
    # The next run asks again, since it is what's missing.
    result = host.setup()
    assert "Enrollment string" in result.stderr


def test_uninstall_leaves_the_gateway_then_removes_the_services_and_files(host: Host) -> None:
    host.install()
    assert json.loads(host.state_file.read_text())["gateway"]["name"] == "linux-box"

    result = host.run("linux/uninstall.sh")

    assert "unenroll" in host.log("agent-bridge.log")
    systemctl = host.log("systemctl.log")
    assert f"--user disable --now --quiet {UNIT}" in systemctl
    # herdr's unit is removed, but its server, and so the agents, keep running.
    assert f"--user disable --quiet {HERDR_UNIT}" in systemctl
    assert not [line for line in systemctl if HERDR_UNIT in line and "stop" in line]
    assert "herdr's server keeps running" in result.stdout
    assert not host.unit(UNIT).exists()
    assert not host.unit(HERDR_UNIT).exists()
    assert not host.env_file.parent.exists()
    assert not host.state_file.parent.exists()
    assert "loginctl disable-linger" in result.stdout


def test_uninstall_keeps_folders_that_hold_other_files(host: Host) -> None:
    host.install()
    (host.state_file.parent / "bridge.log").write_text("old log\n")

    result = host.run("linux/uninstall.sh")

    assert not host.state_file.exists()
    assert (host.state_file.parent / "bridge.log").exists()
    assert f"Left {host.state_file.parent}" in result.stdout
