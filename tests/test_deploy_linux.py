"""agent-bridge/deploy/linux/setup.sh and uninstall.sh, run from a copy of the checkout against a
temporary HOME, with uv, systemctl, loginctl, ss, ps, ip, herdr and the venv's agent-bridge
replaced by stubs: what they write, ask and run."""

import json
import platform
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
    # No default route when $HOME/no-route exists.
    "ip": """
case "$*" in
  *"route get"*) [ ! -f "$HOME/no-route" ] || { echo "RTNETLINK answers: Network is unreachable" >&2; exit 2; }
    echo "1.1.1.1 via 10.0.0.254 dev eth0 src 10.0.0.5 uid 1000" ;;
  *"addr show"*) echo "2: eth0    inet 10.0.0.5/24 brd 10.0.0.255 scope global eth0" ;;
esac""",
}

# The venv's agent-bridge: enroll exits with the next code in $HOME/enroll-codes (0 when
# there is none) and on 0 writes the state file with the name from the env file. unenroll
# fails when $HOME/unenroll-fails exists.
AGENT_BRIDGE = """#!/bin/sh
echo "$*" >> "$HOME/agent-bridge.log"
env="$HOME/.config/agent-bridge/env"
state="$HOME/.local/state/agent-bridge/state.json"
case $1 in
  check)
    case $2 in
      --name-format) case $3 in ''|*[!a-z0-9-]*) echo "The name must be lowercase." >&2; exit 2 ;; esac ;;
      *)
        echo "ok    agent-bridge answers"
        # As check saves the name the gateway answers with: $HOME/gateway-name, if any, and
        # only for the token in $HOME/gateway-token, if that exists.
        token=$(sed -n "s/^AGENT_BRIDGE_TOKEN='\\(.*\\)'$/\\1/p" "$env")
        if [ -f "$HOME/gateway-token" ] && [ "$token" != "$(cat "$HOME/gateway-token")" ]; then :
        elif [ -f "$HOME/gateway-name" ] && [ -f "$state" ]; then
          sed -i "s/\\"name\\": \\"[^\\"]*\\"/\\"name\\": \\"$(cat "$HOME/gateway-name")\\"/" "$state"
        fi ;;
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
  unenroll)
    [ ! -f "$HOME/unenroll-fails" ] || { echo "$state isn't agent-bridge state." >&2; exit 1; }
    rm -f "$state"; echo "Left the gateway." ;;
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
    # The real one, with agent_bridge: setup.sh checks the settings with its loader.
    (bin_dir / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (bin_dir / "python").chmod(0o755)
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
    assert f'ExecStart="{host.repo}/.venv/bin/agent-bridge" serve\n' in unit
    assert "AGENT_BRIDGE_" not in unit
    assert f'ExecStart="{host.stubs}/herdr" server\n' in host.unit(HERDR_UNIT).read_text()
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


@pytest.mark.parametrize("config", [".claude", ".claude/agents", "."])
def test_your_own_claude_config_is_refused_for_the_agents(host: Host, config: str) -> None:
    result = host.install(AGENT_BRIDGE_CLAUDE_CONFIG_DIR=str(host.home / config), check=False)

    assert result.returncode != 0
    assert "is the default Claude config" in result.stderr
    assert not host.env_file.exists()


@pytest.mark.parametrize(("setting", "value", "refusal"), [
    ("ALLOWED_ROOTS", "~", "AGENT_BRIDGE_ALLOWED_ROOTS"),
    ("HOST", "myhost.local", "AGENT_BRIDGE_HOST"),
    ("PORT", "70000", "AGENT_BRIDGE_PORT"),
])
def test_settings_agent_bridge_would_refuse_leave_the_installed_ones(
        host: Host, setting: str, value: str, refusal: str) -> None:
    host.install()
    before = host.env_file.read_text()

    result = host.setup(check=False, **{setting: value})

    assert result.returncode != 0
    assert refusal in result.stderr
    assert "nothing was changed" in result.stderr
    assert host.env_file.read_text() == before
    assert f"--user restart {UNIT}" not in host.log("systemctl.log")


def test_settings_are_checked_with_the_env_file_serve_also_reads_in_the_home_folder(host: Host) -> None:
    host.install()
    before = host.env_file.read_text()
    # systemd starts the unit in the home folder, and serve reads a .env there too.
    (host.home / ".env").write_text("AGENT_BRIDGE_MAX_AGENTS=0\n")

    result = host.setup(check=False)

    assert result.returncode != 0
    assert "AGENT_BRIDGE_MAX_AGENTS" in result.stderr
    assert host.env_file.read_text() == before


def test_without_a_default_route_it_asks_for_the_address(host: Host) -> None:
    (host.home / "no-route").touch()

    result = host.setup(ALLOWED_ROOTS=str(host.home / "projects"), NAME="linux-box", AGENT_BRIDGE_CLAUDE_CONFIG_DIR="",
                        TRUST_ROOT="", ENROLLMENT="", check=False)

    assert result.returncode != 0
    assert "pass it, e.g. HOST=" in result.stderr


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


def test_uninstall_goes_on_when_leaving_the_gateway_fails(host: Host) -> None:
    host.install()
    (host.home / "unenroll-fails").touch()

    result = host.run("linux/uninstall.sh")

    assert "sudo mcp-gateway bridges remove" in result.stdout
    assert not host.unit(UNIT).exists()
    assert not host.env_file.exists()
    assert not host.state_file.exists()


def test_on_macos_it_points_to_the_macos_setup(host: Host) -> None:
    (host.stubs / "uname").write_text("#!/bin/sh\necho Darwin\n")
    (host.stubs / "uname").chmod(0o755)

    result = host.install(check=False)

    assert result.returncode != 0
    script = re.search(r"run (agent-bridge/deploy/\S+)", result.stderr)
    assert script and (DEPLOY.parent.parent / script[1]).is_file(), result.stderr


def test_unit_paths_with_spaces_dollars_and_percents_are_quoted_for_systemd(host: Host) -> None:
    folder = host.home / "my $tools 100%"
    folder.mkdir()
    herdr = folder / "herdr"
    shutil.copy(host.stubs / "herdr", herdr)

    host.install(HERDR_BIN=str(herdr))

    # As `systemd-analyze verify` accepts it: $ is taken literally in the program's path.
    assert f'ExecStart="{host.home}/my $tools 100%%/herdr" server\n' in host.unit(HERDR_UNIT).read_text()
    assert host.env()["AGENT_BRIDGE_HERDR_BIN"] == str(herdr)


def test_a_unit_path_systemd_cant_run_is_refused_before_anything_is_written(host: Host) -> None:
    folder = host.home / 'say "hi"'
    folder.mkdir()
    herdr = folder / "herdr"
    shutil.copy(host.stubs / "herdr", herdr)

    result = host.install(HERDR_BIN=str(herdr), check=False)

    assert result.returncode != 0
    assert "systemd can't run" in result.stderr
    assert not host.env_file.exists()


def test_backslashes_in_a_setting_reach_the_bridge_unchanged_and_are_kept(host: Host) -> None:
    from dotenv import dotenv_values

    accounts = '{"work": {"config_dir": "/home/me/a\\\\b"}}'

    host.install(AGENT_BRIDGE_ACCOUNTS=accounts)
    assert dotenv_values(host.env_file)["AGENT_BRIDGE_ACCOUNTS"] == accounts

    host.setup()
    assert dotenv_values(host.env_file)["AGENT_BRIDGE_ACCOUNTS"] == accounts


def test_accounts_are_kept_on_a_rerun_and_removed_when_passed_empty(host: Host) -> None:
    accounts = '{"work": {"config_dir": "/home/me/.claude-work"}}'
    host.install(AGENT_BRIDGE_ACCOUNTS=accounts)

    host.setup()
    assert host.env()["AGENT_BRIDGE_ACCOUNTS"] == accounts

    host.setup(AGENT_BRIDGE_ACCOUNTS="")
    assert "AGENT_BRIDGE_ACCOUNTS" not in host.env()


def test_an_installed_address_the_host_no_longer_has_is_found_again(host: Host) -> None:
    host.install(HOST="10.0.0.9")

    result = host.setup()

    assert host.env()["AGENT_BRIDGE_HOST"] == "10.0.0.5"
    assert "The LAN address is now 10.0.0.5 (was 10.0.0.9)" in result.stdout


def test_a_refused_enrollment_string_is_asked_again(host: Host) -> None:
    (host.home / "enroll-codes").write_text("1\n0\n")
    other = "crm1@10.0.0.1:8766#" + "b" * 24

    result = host.install(input=f"{other}\n")

    enrolls = [line for line in host.log("agent-bridge.log") if line.startswith("enroll")]
    assert enrolls == [f"enroll {ENROLLMENT}", f"enroll {other}"]
    assert "as linux-box" in result.stdout


def test_a_failed_rotation_puts_the_old_token_back(host: Host) -> None:
    host.install()
    first = host.env()["AGENT_BRIDGE_TOKEN"]
    (host.home / "enroll-codes").write_text("1\n")

    result = host.setup(ROTATE_TOKEN="1", ENROLLMENT=ENROLLMENT, check=False)

    assert result.returncode != 0
    assert "not rotated" in result.stderr
    assert host.env()["AGENT_BRIDGE_TOKEN"] == first
    # Restarted with the new token, then again with the old one.
    assert host.log("systemctl.log").count(f"--user restart {UNIT}") == 2


def test_a_new_name_on_an_enrolled_host_says_how_to_rename_it_on_the_gateway(host: Host) -> None:
    host.install()

    result = host.setup(NAME="newbox")

    assert not PROMPT.search(result.stderr), result.stderr
    assert not [line for line in host.log("agent-bridge.log") if line.startswith("enroll")]
    assert "sudo mcp-gateway bridges rename linux-box newbox" in result.stdout
    assert host.env()["AGENT_BRIDGE_NAME"] == "linux-box"


def test_an_enrollment_string_doesnt_rename_an_enrolled_host(host: Host) -> None:
    host.install()

    result = host.setup(NAME="newbox", ENROLLMENT=ENROLLMENT)

    assert "as linux-box" in result.stdout
    assert "sudo mcp-gateway bridges rename linux-box newbox" in result.stdout
    assert host.env()["AGENT_BRIDGE_NAME"] == "linux-box"


def test_a_rename_done_on_the_gateway_is_followed(host: Host) -> None:
    host.install()
    # What the bridge saves after `sudo mcp-gateway bridges rename linux-box studio`.
    host.state_file.write_text(host.state_file.read_text().replace('"linux-box"', '"studio"'))

    result = host.setup()

    assert not PROMPT.search(result.stderr), result.stderr
    assert host.env()["AGENT_BRIDGE_NAME"] == "studio"
    assert not [line for line in host.log("agent-bridge.log") if line.startswith("enroll")]
    assert "as studio" in result.stdout
    assert "bridges rename" not in result.stdout


def test_a_rename_on_the_gateway_the_bridge_hasnt_seen_isnt_undone_by_enrolling(host: Host) -> None:
    host.install()
    # Renamed on the gateway while the bridge ran: its state file has the old name until it
    # next asks the gateway, which answers only to the token it has.
    (host.home / "gateway-name").write_text("studio")
    (host.home / "gateway-token").write_text(host.env()["AGENT_BRIDGE_TOKEN"])

    result = host.setup(ROTATE_TOKEN="1", ENROLLMENT=ENROLLMENT)

    assert host.env()["AGENT_BRIDGE_NAME"] == "studio"
    assert "as studio" in result.stdout


def test_a_failed_rotation_puts_back_the_gateways_name_after_another_was_given(host: Host) -> None:
    host.install()
    first = host.env()["AGENT_BRIDGE_TOKEN"]
    (host.home / "enroll-codes").write_text("3\n4\n")

    result = host.setup(ROTATE_TOKEN="1", ENROLLMENT=ENROLLMENT, input="box2\n", check=False)

    assert result.returncode != 0
    assert "not rotated" in result.stderr
    assert host.env()["AGENT_BRIDGE_TOKEN"] == first
    assert host.env()["AGENT_BRIDGE_NAME"] == "linux-box"


def test_settings_written_with_export_are_taken_over(host: Host) -> None:
    host.env_file.parent.mkdir(parents=True)
    host.env_file.write_text(
        f"export AGENT_BRIDGE_TOKEN={'t' * 40}\n"
        f"export AGENT_BRIDGE_ALLOWED_ROOTS={host.home / 'projects'}\n"
        "export AGENT_BRIDGE_HOST=127.0.0.1\n"
        "export AGENT_BRIDGE_NAME=linux-box\n"
    )

    host.setup(AGENT_BRIDGE_CLAUDE_CONFIG_DIR="", TRUST_ROOT="", ENROLLMENT="")

    text = host.env_file.read_text()
    assert "export" not in text
    assert host.env()["AGENT_BRIDGE_TOKEN"] == "t" * 40
    assert host.env()["AGENT_BRIDGE_NAME"] == "linux-box"


def test_settings_are_read_as_the_loader_reads_them(host: Host) -> None:
    host.install()
    other = host.home / "other"
    other.mkdir()
    lines = host.env_file.read_text().splitlines()
    host.env_file.write_text("".join(
        f"AGENT_BRIDGE_ALLOWED_ROOTS={other}  # main\n" if line.startswith("AGENT_BRIDGE_ALLOWED_ROOTS=")
        else "AGENT_BRIDGE_PORT=\"8770\"  # moved\n" if line.startswith("AGENT_BRIDGE_PORT=")
        else f"{line}\n" for line in lines))

    host.setup()

    assert host.env()["AGENT_BRIDGE_ALLOWED_ROOTS"] == str(other)
    assert host.env()["AGENT_BRIDGE_PORT"] == "8770"


def test_a_setting_written_with_spaces_or_in_lower_case_is_replaced(host: Host) -> None:
    from dotenv import dotenv_values

    host.install()
    first = host.env()["AGENT_BRIDGE_TOKEN"]
    with host.env_file.open("a") as env:
        env.write("  AGENT_BRIDGE_PORT = 9000\nagent_bridge_token=" + "o" * 40 + "\nAGENT_BRIDGE_MAX_AGENTS = 3\n")

    host.setup(PORT="9100")

    values = {key.upper(): value for key, value in dotenv_values(host.env_file).items()}
    assert values["AGENT_BRIDGE_PORT"] == "9100"
    # The lower-case token was the installed one (the last wins), and is kept, now written by the setup.
    assert values["AGENT_BRIDGE_TOKEN"] == "o" * 40 != first
    assert values["AGENT_BRIDGE_MAX_AGENTS"] == "3"
    text = host.env_file.read_text()
    assert "9000" not in text and "agent_bridge_token" not in text


def test_a_variable_in_a_setting_is_read_expanded_as_the_loader_reads_it(host: Host) -> None:
    host.install()
    other = host.home / "other"
    other.mkdir()
    lines = host.env_file.read_text().splitlines()
    host.env_file.write_text("".join(
        "AGENT_BRIDGE_ALLOWED_ROOTS=${HOME}/other\n" if line.startswith("AGENT_BRIDGE_ALLOWED_ROOTS=")
        else f"{line}\n" for line in lines))

    host.setup()

    assert host.env()["AGENT_BRIDGE_ALLOWED_ROOTS"] == str(other)


def test_a_value_the_loader_would_expand_is_refused(host: Host) -> None:
    folder = host.home / "a${x}"
    folder.mkdir()

    result = host.install(ALLOWED_ROOTS=str(folder), check=False)

    assert result.returncode != 0
    assert "can't contain" in result.stderr
    assert not host.env_file.exists()


def test_accounts_written_by_hand_over_several_lines_are_kept(host: Host) -> None:
    from dotenv import dotenv_values

    host.install()
    # As the README shows it, in single quotes.
    accounts = {
        "work": {"config_dir": None, "folders": ["/home/me/projects/work"]},
        "personal": {"config_dir": "/home/me/.claude-personal", "folders": ["/home/me/projects/own"]},
    }
    with host.env_file.open("a") as env:
        env.write(f"AGENT_BRIDGE_ACCOUNTS='{json.dumps(accounts, indent=2)}'\n")

    host.setup()

    value = dotenv_values(host.env_file)["AGENT_BRIDGE_ACCOUNTS"]
    assert "\n" not in value
    assert json.loads(value) == accounts


def test_accounts_with_an_apostrophe_or_a_dollar_are_kept(host: Host) -> None:
    from dotenv import dotenv_values

    host.install()
    accounts = {"work": {"config_dir": "/home/me/Jo's $stuff", "folders": []}}
    with host.env_file.open("a") as env:
        env.write(f"AGENT_BRIDGE_ACCOUNTS={json.dumps(json.dumps(accounts))}\n")
    assert json.loads(dotenv_values(host.env_file)["AGENT_BRIDGE_ACCOUNTS"]) == accounts

    host.setup()

    assert json.loads(dotenv_values(host.env_file)["AGENT_BRIDGE_ACCOUNTS"]) == accounts


def test_a_double_quoted_escape_is_read_as_the_loader_reads_it(host: Host) -> None:
    from dotenv import dotenv_values

    host.install()
    accounts = '{"work": {"config_dir": "/home/me/a\\\\b"}}'
    with host.env_file.open("a") as env:
        env.write('AGENT_BRIDGE_ACCOUNTS="{\\"work\\": {\\"config_dir\\": \\"/home/me/a\\\\\\\\b\\"}}"\n')
    assert dotenv_values(host.env_file)["AGENT_BRIDGE_ACCOUNTS"] == accounts

    host.setup()

    assert dotenv_values(host.env_file)["AGENT_BRIDGE_ACCOUNTS"] == accounts


def test_settings_that_cant_be_read_stop_the_setup_and_keep_the_token(host: Host) -> None:
    host.install()
    before = host.env_file.read_text()
    (host.repo / ".venv" / "bin" / "python").write_text("#!/bin/sh\nexit 1\n")

    result = host.setup(check=False, input="")

    assert result.returncode != 0
    assert "couldn't read" in result.stderr
    # Before asking anything it would throw away.
    assert not PROMPT.search(result.stderr), result.stderr
    assert host.env_file.read_text() == before


def test_rotate_token_0_is_off(host: Host) -> None:
    host.install()
    first = host.env()["AGENT_BRIDGE_TOKEN"]

    host.setup(ROTATE_TOKEN="0")

    assert host.env()["AGENT_BRIDGE_TOKEN"] == first


def test_a_rotation_the_gateway_cant_be_reached_for_puts_the_old_token_back(host: Host) -> None:
    host.install()
    first = host.env()["AGENT_BRIDGE_TOKEN"]
    (host.home / "enroll-codes").write_text("4\n")

    result = host.setup(ROTATE_TOKEN="1", ENROLLMENT=ENROLLMENT, check=False)

    assert result.returncode != 0
    assert "not rotated" in result.stderr
    assert host.env()["AGENT_BRIDGE_TOKEN"] == first


def test_a_rotation_stopped_by_a_taken_name_puts_the_old_token_and_name_back(host: Host) -> None:
    host.install()
    first = host.env()["AGENT_BRIDGE_TOKEN"]
    (host.home / "enroll-codes").write_text("3\n")

    result = host.setup(ROTATE_TOKEN="1", ENROLLMENT=ENROLLMENT, check=False)

    assert result.returncode != 0
    assert "not rotated, as the gateway has another host named linux-box" in result.stderr
    assert host.env()["AGENT_BRIDGE_TOKEN"] == first
    assert host.env()["AGENT_BRIDGE_NAME"] == "linux-box"


def test_the_default_name_needs_no_hostname_command(host: Host) -> None:
    (host.stubs / "hostname").write_text("#!/bin/sh\nexit 127\n")
    (host.stubs / "hostname").chmod(0o755)
    expected = re.sub(r"[^a-z0-9]+", "-", platform.node().split(".")[0].lower()).strip("-")

    host.setup(HOST="127.0.0.1", ALLOWED_ROOTS=str(host.home / "projects"), AGENT_BRIDGE_CLAUDE_CONFIG_DIR="",
               TRUST_ROOT="", ENROLLMENT="")

    assert host.env()["AGENT_BRIDGE_NAME"] == (expected or "agent-host")


def test_an_enrolled_host_whose_token_is_gone_asks_to_enroll_again(host: Host) -> None:
    host.install()
    lines = host.env_file.read_text().splitlines()
    host.env_file.write_text("".join(f"{line}\n" for line in lines if not line.startswith("AGENT_BRIDGE_TOKEN=")))

    result = host.setup()

    assert "had no token" in result.stdout
    assert "Enrollment string" in result.stderr
    assert "old token and its calls will be refused" in result.stdout

    result = host.setup(ENROLLMENT=ENROLLMENT)
    assert f"enroll {ENROLLMENT}" in host.log("agent-bridge.log")
    assert "old token" not in result.stdout


def test_the_token_is_never_on_a_command_line(host: Host) -> None:
    host.install()

    token = host.env()["AGENT_BRIDGE_TOKEN"]
    assert not [line for line in host.log("agent-bridge.log") if token in line]


def test_an_installed_token_that_wont_do_stops_the_setup(host: Host) -> None:
    host.install()
    lines = host.env_file.read_text().splitlines()
    host.env_file.write_text("".join(
        "AGENT_BRIDGE_TOKEN='short'\n" if line.startswith("AGENT_BRIDGE_TOKEN=") else f"{line}\n" for line in lines))

    result = host.setup(check=False)

    assert result.returncode != 0
    assert "ROTATE_TOKEN=1" in result.stderr


def test_a_relative_path_is_refused(host: Host) -> None:
    result = host.install(ALLOWED_ROOTS="projects", check=False)

    assert result.returncode != 0
    assert "ALLOWED_ROOTS must be a full path" in result.stderr
    assert not host.env_file.exists()


def test_an_agents_config_without_a_login_says_how_to_log_in(host: Host) -> None:
    config = host.home / ".claude-agents"

    result = host.install(AGENT_BRIDGE_CLAUDE_CONFIG_DIR=str(config), TRUST_ROOT=str(host.home / "projects"))

    assert f"CLAUDE_CONFIG_DIR={config} claude" in result.stdout
    (config / ".claude.json").parent.mkdir(parents=True, exist_ok=True)
    (config / ".claude.json").write_text("{}")
    assert "Log in to Claude" not in host.setup().stdout
