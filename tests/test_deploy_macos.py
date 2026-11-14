"""agent-bridge/deploy/macos/setup.sh and uninstall.sh, run from a copy of the checkout against a
temporary HOME, with uv, launchctl, lsof, ps, netstat and the venv's agent-bridge replaced by
stubs: what they write, ask and run."""

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from dotenv import dotenv_values

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="the macOS deploy is for macOS")

DEPLOY = Path(__file__).parent.parent / "agent-bridge" / "deploy"
LABEL = "dev.claude-remote.agent-bridge"
ENROLLMENT = "crm1@10.0.0.1:8766#" + "a" * 24
# What setup.sh prints before reading an answer.
PROMPT = re.compile(r"\]: ")

STUBS = {
    "uv": "exit 0",
    "herdr": "exit 0",
    # `netstat -anv -p tcp`: the listeners in $HOME/netstat, if any.
    "netstat": '[ -f "$HOME/netstat" ] && cat "$HOME/netstat"\nexit 0',
    # Logs, and answers `print` with the PID in $HOME/launchd-pid, as a loaded job would.
    "launchctl": """
echo "$*" >> "$HOME/launchctl.log"
if [ "$1" = print ] && [ -f "$HOME/launchd-pid" ]; then printf '\\tpid = %s\\n' "$(cat "$HOME/launchd-pid")"; fi
exit 0""",
    # `lsof -nP -iTCP:<port> -sTCP:LISTEN -t`: the PID in $HOME/port-<port>, if any.
    "lsof": """
for arg; do case $arg in -iTCP:*) port=${arg#-iTCP:} ;; esac; done
[ -f "$HOME/port-$port" ] && cat "$HOME/port-$port"
exit 0""",
    # `ps -o command= -p <pid>`: the command line in $HOME/ps-<pid>; `-o ppid=`: $HOME/ppid-<pid>.
    "ps": """
for arg; do pid=$arg; done
case $* in *ppid=*) file="$HOME/ppid-$pid" ;; *) file="$HOME/ps-$pid" ;; esac
[ -f "$file" ] && cat "$file"
exit 0""",
}

# The venv's agent-bridge: enroll exits with the next code in $HOME/enroll-codes (0 when
# there is none) and on 0 writes the state file with the name from the env file.
AGENT_BRIDGE = """#!/bin/sh
echo "$*" >> "$HOME/agent-bridge.log"
dir="$HOME/Library/Application Support/agent-bridge"
case $1 in
  check)
    case $2 in
      --name-format) case $3 in ''|*[!a-z0-9-]*) echo "The name must be lowercase." >&2; exit 2 ;; esac ;;
      --token-format) ;;
      *) echo "ok    agent-bridge answers"
         [ -f "$HOME/gateway-took-token" ] && echo "ok    the gateway at http://10.0.0.1:8766 has this host as mac" ;;
    esac ;;
  enroll)
    code=$(head -n 1 "$HOME/enroll-codes" 2>/dev/null)
    sed -i '' 1d "$HOME/enroll-codes" 2>/dev/null
    # As cli.py words it when the gateway took it but the state file couldn't be written.
    [ "$code" = state ] && { echo "The gateway enrolled this host as mac, but can't write the state." >&2; exit 1; }
    [ "${code:-0}" = 0 ] || { echo "Couldn't enroll (stub exit $code)." >&2; exit "$code"; }
    name=$(sed -n "s/^AGENT_BRIDGE_NAME='\\(.*\\)'$/\\1/p" "$dir/env")
    printf '{"version": 1, "bridge_id": "b1", "gateway": {"url": "http://10.0.0.1:8766", "gateway_id": "g1", "name": "%s"}}' \\
      "$name" > "$dir/state.json"
    echo "Enrolled as $name." ;;
  unenroll)
    [ -f "$HOME/unenroll-fails" ] && { echo "$dir/state.json isn't agent-bridge state." >&2; exit 1; }
    rm -f "$dir/state.json"; echo "Left the gateway." ;;
esac
exit 0
"""


@dataclass
class Mac:
    home: Path
    repo: Path
    stubs: Path

    @property
    def support(self) -> Path:
        return self.home / "Library" / "Application Support" / "agent-bridge"

    @property
    def plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    def run(self, script: str, input: str | None = None, check: bool = True, **env: str) -> subprocess.CompletedProcess:
        for log in ("launchctl.log", "agent-bridge.log"):
            (self.home / log).unlink(missing_ok=True)
        result = subprocess.run(
            [str(self.repo / "agent-bridge" / "deploy" / script)],
            env={"HOME": str(self.home), "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin", **env},
            input=input,
            stdin=None if input is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        if check:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    def setup(self, input: str | None = None, check: bool = True, **env: str) -> subprocess.CompletedProcess:
        return self.run("macos/setup.sh", input, check, **env)

    def install(self, **env: str) -> subprocess.CompletedProcess:
        """A fresh install with every answer given, folder trust off."""
        return self.setup(**{
            "HOST": "127.0.0.1", "ALLOWED_ROOTS": str(self.home / "projects"), "NAME": "mac",
            "AGENT_BRIDGE_CLAUDE_CONFIG_DIR": "", "TRUST_ROOT": "", "ENROLLMENT": ENROLLMENT, **env,
        })

    def env(self) -> dict[str, str]:
        lines = (self.support / "env").read_text().splitlines()
        return {key: value.strip("'") for key, _, value in (line.partition("=") for line in lines if line)}

    def log(self, name: str) -> str:
        path = self.home / name
        return path.read_text() if path.exists() else ""


@pytest.fixture
def mac(tmp_path: Path) -> Mac:
    repo = tmp_path / "repo"
    shutil.copytree(DEPLOY, repo / "agent-bridge" / "deploy")
    (repo / "agent-bridge" / "pyproject.toml").touch()
    bin_dir = repo / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "agent-bridge").write_text(AGENT_BRIDGE)
    (bin_dir / "agent-bridge").chmod(0o755)
    # The venv's python, which setup.sh reads the env file with: this one has python-dotenv.
    (bin_dir / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (bin_dir / "python").chmod(0o755)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in STUBS.items():
        (stubs / name).write_text(f"#!/bin/sh\n{body}\n")
        (stubs / name).chmod(0o755)
    home = tmp_path / "home"
    (home / "projects").mkdir(parents=True)
    return Mac(home, repo, stubs)


def test_a_fresh_install_writes_the_settings_starts_the_service_and_enrolls(mac: Mac) -> None:
    result = mac.setup(HOST="127.0.0.1", ALLOWED_ROOTS=str(mac.home / "projects"), NAME="mac",
                       ENROLLMENT=ENROLLMENT)

    env = mac.env()
    assert re.fullmatch(r"[0-9a-f]{64}", env["AGENT_BRIDGE_TOKEN"])
    assert env["AGENT_BRIDGE_ALLOWED_ROOTS"] == str(mac.home / "projects")
    assert env["AGENT_BRIDGE_HOST"] == "127.0.0.1"
    assert env["AGENT_BRIDGE_PORT"] == "8765"
    assert env["AGENT_BRIDGE_NAME"] == "mac"
    assert env["AGENT_BRIDGE_HERDR_BIN"] == str(mac.stubs / "herdr")
    # Folder trust was asked, and no input left took the defaults.
    assert "Separate Claude config folder" in result.stderr
    assert env["AGENT_BRIDGE_CLAUDE_CONFIG_DIR"] == str(mac.home / ".claude-agents")
    assert env["AGENT_BRIDGE_TRUST_ROOT"] == str(mac.home / "projects")
    assert (mac.support / "env").stat().st_mode & 0o777 == 0o600
    assert mac.support.stat().st_mode & 0o777 == 0o700

    plist = plistlib.loads(mac.plist.read_bytes())
    assert plist["ProgramArguments"] == [str(mac.repo / ".venv" / "bin" / "agent-bridge"), "serve"]
    assert "EnvironmentVariables" not in plist
    assert f"bootstrap gui/{os.getuid()}" in mac.log("launchctl.log")
    assert f"enroll {ENROLLMENT}" in mac.log("agent-bridge.log")

    assert "enrolled with the gateway at http://10.0.0.1:8766 as mac" in result.stdout
    assert env["AGENT_BRIDGE_TOKEN"] not in result.stdout + result.stderr
    # Never on a command line, where ps would show it.
    assert env["AGENT_BRIDGE_TOKEN"] not in mac.log("agent-bridge.log")
    assert "bridges add" not in result.stdout


def test_an_upgrade_moves_the_plist_settings_into_the_env_file_and_keeps_the_token(mac: Mac) -> None:
    mac.plist.parent.mkdir(parents=True)
    accounts = '{"work": {"config_dir": null, "folders": ["/Users/me/work"]}}'
    mac.plist.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [str(mac.repo / ".venv" / "bin" / "uvicorn"), "agent_bridge.app:main", "--factory",
                             "--host", "127.0.0.1", "--port", "8770"],
        "EnvironmentVariables": {
            "AGENT_BRIDGE_TOKEN": "t" * 40,
            "AGENT_BRIDGE_ALLOWED_ROOTS": str(mac.home / "projects"),
            "AGENT_BRIDGE_HERDR_BIN": str(mac.stubs / "herdr"),
            "AGENT_BRIDGE_CLAUDE_CONFIG_DIR": str(mac.home / ".claude-agents"),
            "AGENT_BRIDGE_TRUST_ROOT": str(mac.home / "projects"),
            "AGENT_BRIDGE_ACCOUNTS": accounts,
        },
    }))

    result = mac.setup(NAME="mac", ENROLLMENT=ENROLLMENT)

    env = mac.env()
    assert env["AGENT_BRIDGE_TOKEN"] == "t" * 40
    assert env["AGENT_BRIDGE_HOST"] == "127.0.0.1"
    assert env["AGENT_BRIDGE_PORT"] == "8770"
    assert env["AGENT_BRIDGE_ALLOWED_ROOTS"] == str(mac.home / "projects")
    assert env["AGENT_BRIDGE_CLAUDE_CONFIG_DIR"] == str(mac.home / ".claude-agents")
    assert env["AGENT_BRIDGE_TRUST_ROOT"] == str(mac.home / "projects")
    assert env["AGENT_BRIDGE_ACCOUNTS"] == accounts
    assert not PROMPT.search(result.stderr), result.stderr
    plist = plistlib.loads(mac.plist.read_bytes())
    assert "EnvironmentVariables" not in plist
    assert plist["ProgramArguments"][-1] == "serve"
    assert "as mac" in result.stdout


def test_an_upgrade_carries_the_plists_other_settings_into_the_env_file(mac: Mac) -> None:
    mac.plist.parent.mkdir(parents=True)
    mac.plist.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [str(mac.repo / ".venv" / "bin" / "uvicorn"), "agent_bridge.app:main", "--factory",
                             "--host", "127.0.0.1", "--port", "8765"],
        "EnvironmentVariables": {"AGENT_BRIDGE_TOKEN": "t" * 40,
                                 "AGENT_BRIDGE_ALLOWED_ROOTS": str(mac.home / "projects"),
                                 "AGENT_BRIDGE_MAX_AGENTS": "3", "AGENT_BRIDGE_CLAUDE_ARGS": "",
                                 "PATH": "/usr/bin"},
    }))

    mac.setup(NAME="mac", ENROLLMENT=ENROLLMENT)

    env = mac.env()
    assert env["AGENT_BRIDGE_MAX_AGENTS"] == "3"
    assert env["AGENT_BRIDGE_CLAUDE_ARGS"] == ""
    assert "PATH" not in env


def test_quotes_and_backslashes_in_settings_survive_the_upgrade_and_reruns(mac: Mac) -> None:
    roots = mac.home / "Jakub's projects"
    roots.mkdir()
    args = "--append-system-prompt 'be brief' --x C:\\y"
    mac.plist.parent.mkdir(parents=True)
    mac.plist.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [str(mac.repo / ".venv" / "bin" / "uvicorn"), "agent_bridge.app:main", "--factory",
                             "--host", "127.0.0.1", "--port", "8765"],
        "EnvironmentVariables": {"AGENT_BRIDGE_TOKEN": "t" * 40, "AGENT_BRIDGE_ALLOWED_ROOTS": str(roots),
                                 "AGENT_BRIDGE_CLAUDE_ARGS": args},
    }))

    mac.setup(NAME="mac", ENROLLMENT=ENROLLMENT)
    mac.setup()

    # As the bridge reads them.
    env = dotenv_values(mac.support / "env")
    assert env["AGENT_BRIDGE_ALLOWED_ROOTS"] == str(roots)
    assert env["AGENT_BRIDGE_CLAUDE_ARGS"] == args


def test_an_upgrade_without_folder_trust_doesnt_ask_for_it(mac: Mac) -> None:
    mac.plist.parent.mkdir(parents=True)
    mac.plist.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [str(mac.repo / ".venv" / "bin" / "uvicorn"), "agent_bridge.app:main", "--factory",
                             "--host", "127.0.0.1", "--port", "8765"],
        "EnvironmentVariables": {"AGENT_BRIDGE_TOKEN": "t" * 40,
                                 "AGENT_BRIDGE_ALLOWED_ROOTS": str(mac.home / "projects")},
    }))

    result = mac.setup(NAME="mac", ENROLLMENT=ENROLLMENT)

    assert not PROMPT.search(result.stderr), result.stderr
    assert mac.env()["AGENT_BRIDGE_CLAUDE_CONFIG_DIR"] == ""


def test_a_rerun_asks_nothing_and_keeps_the_settings_and_the_enrollment(mac: Mac) -> None:
    mac.install()
    before = mac.env()

    result = mac.setup()

    assert not PROMPT.search(result.stderr), result.stderr
    assert mac.env() == before
    assert "enroll" not in mac.log("agent-bridge.log").replace("unenroll", "")
    assert "enrolled with the gateway at http://10.0.0.1:8766 as mac" in result.stdout


def test_a_rerun_keeps_settings_the_setup_doesnt_manage(mac: Mac) -> None:
    mac.install()
    with (mac.support / "env").open("a") as env:
        env.write("AGENT_BRIDGE_MAX_AGENTS=3\n")

    mac.setup()

    assert mac.env()["AGENT_BRIDGE_MAX_AGENTS"] == "3"


def test_a_hand_edited_env_file_is_read_as_the_bridge_reads_it(mac: Mac) -> None:
    mac.install()
    accounts = '{\n  "work": {"config_dir": null, "folders": ["/Users/me/work"]}\n}'
    env = (mac.support / "env").read_text()
    env = re.sub(r"(?m)^AGENT_BRIDGE_PORT=.*$", "export AGENT_BRIDGE_PORT=9000  # moved", env)
    (mac.support / "env").write_text(env + f"AGENT_BRIDGE_ACCOUNTS='{accounts}'\nagent_bridge_max_agents=3\n")

    mac.setup()

    env = dotenv_values(mac.support / "env")
    assert env["AGENT_BRIDGE_PORT"] == "9000"
    assert env["AGENT_BRIDGE_ACCOUNTS"] == accounts
    assert env["AGENT_BRIDGE_MAX_AGENTS"] == "3"
    # One line per setting: none left over to override what setup wrote.
    keys = [line.split("=")[0] for line in (mac.support / "env").read_text().splitlines() if "=" in line]
    assert len(keys) == len(set(keys)), keys


def test_rotate_token_replaces_a_hand_written_export_line(mac: Mac) -> None:
    mac.install()
    first = mac.env()["AGENT_BRIDGE_TOKEN"]
    env = (mac.support / "env").read_text().replace("AGENT_BRIDGE_TOKEN=", "export AGENT_BRIDGE_TOKEN=")
    (mac.support / "env").write_text(env)

    mac.setup(ROTATE_TOKEN="1", ENROLLMENT=ENROLLMENT)

    assert dotenv_values(mac.support / "env")["AGENT_BRIDGE_TOKEN"] not in (None, first)


def test_a_token_passed_in_is_ignored_with_a_warning(mac: Mac) -> None:
    result = mac.install(AGENT_BRIDGE_TOKEN="a" * 40)

    assert "AGENT_BRIDGE_TOKEN is ignored" in result.stdout
    assert mac.env()["AGENT_BRIDGE_TOKEN"] != "a" * 40


def test_a_new_token_on_a_host_enrolled_before_asks_for_an_enrollment_string(mac: Mac) -> None:
    # Enrolled from a foreground run: a state file, but no env file or plist with its token.
    mac.support.mkdir(parents=True)
    (mac.support / "state.json").write_text(json.dumps(
        {"version": 1, "bridge_id": "b1", "gateway": {"url": "http://10.0.0.1:8766", "gateway_id": "g1", "name": "mac"}}))

    result = mac.setup(HOST="127.0.0.1", ALLOWED_ROOTS=str(mac.home / "projects"), NAME="mac",
                       AGENT_BRIDGE_CLAUDE_CONFIG_DIR="", TRUST_ROOT="")

    assert "Enrollment string" in result.stderr
    assert "older token" in result.stdout
    assert "It is enrolled" not in result.stdout


def test_an_installed_ipv6_address_the_mac_still_has_is_kept(mac: Mac) -> None:
    (mac.stubs / "ifconfig").write_text("#!/bin/sh\necho '\tinet6 fd00::20 prefixlen 64'\n")
    (mac.stubs / "ifconfig").chmod(0o755)
    mac.install(HOST="fd00::20")

    result = mac.setup()

    assert mac.env()["AGENT_BRIDGE_HOST"] == "fd00::20"
    assert "http://[fd00::20]:8765" in result.stdout


def test_a_variable_passed_in_changes_an_installed_setting(mac: Mac) -> None:
    mac.install()
    other = mac.home / "other"
    other.mkdir()

    mac.setup(ALLOWED_ROOTS=str(other))

    assert mac.env()["AGENT_BRIDGE_ALLOWED_ROOTS"] == str(other)


def test_rotate_token_replaces_the_token_and_enrolls_again(mac: Mac) -> None:
    mac.install()
    first = mac.env()["AGENT_BRIDGE_TOKEN"]

    mac.setup(ROTATE_TOKEN="1", ENROLLMENT=ENROLLMENT)

    assert mac.env()["AGENT_BRIDGE_TOKEN"] != first
    assert f"enroll {ENROLLMENT}" in mac.log("agent-bridge.log")


@pytest.mark.parametrize("code", ["4", "1"])
def test_rotate_token_puts_the_installed_token_back_when_enrolling_fails(mac: Mac, code: str) -> None:
    mac.install()
    first = mac.env()["AGENT_BRIDGE_TOKEN"]
    (mac.home / "enroll-codes").write_text(f"{code}\n")

    result = mac.setup(ROTATE_TOKEN="1", ENROLLMENT=ENROLLMENT, check=False)

    assert result.returncode != 0
    assert "installed one is back" in result.stderr
    assert mac.env()["AGENT_BRIDGE_TOKEN"] == first


def test_rotate_token_puts_the_installed_token_back_when_the_name_is_taken(mac: Mac) -> None:
    mac.install()
    first = mac.env()["AGENT_BRIDGE_TOKEN"]
    (mac.home / "enroll-codes").write_text("3\n")

    result = mac.setup(ROTATE_TOKEN="1", ENROLLMENT=ENROLLMENT, check=False)

    assert result.returncode != 0
    assert mac.env()["AGENT_BRIDGE_TOKEN"] == first
    assert mac.env()["AGENT_BRIDGE_NAME"] == "mac"


def test_rotate_token_keeps_the_new_token_when_the_gateway_took_it(mac: Mac) -> None:
    mac.install()
    first = mac.env()["AGENT_BRIDGE_TOKEN"]
    # enroll failed after the gateway enrolled it, e.g. on a state file it couldn't write.
    (mac.home / "enroll-codes").write_text("1\n")
    (mac.home / "gateway-took-token").touch()

    mac.setup(ROTATE_TOKEN="1", ENROLLMENT=ENROLLMENT)

    assert mac.env()["AGENT_BRIDGE_TOKEN"] != first


def test_rotate_token_keeps_the_new_token_when_enroll_says_the_gateway_took_it(mac: Mac) -> None:
    # Not enrolled before (added by hand with `bridges add`), so check can't ask the gateway.
    mac.install(ENROLLMENT="")
    first = mac.env()["AGENT_BRIDGE_TOKEN"]
    (mac.home / "enroll-codes").write_text("state\n")

    mac.setup(ROTATE_TOKEN="1", ENROLLMENT=ENROLLMENT)

    assert mac.env()["AGENT_BRIDGE_TOKEN"] != first


def test_a_rename_on_the_gateway_is_kept(mac: Mac) -> None:
    mac.install()
    state = json.loads((mac.support / "state.json").read_text())
    state["gateway"]["name"] = "studio"
    (mac.support / "state.json").write_text(json.dumps(state))

    result = mac.setup(ROTATE_TOKEN="1", ENROLLMENT=ENROLLMENT)

    assert mac.env()["AGENT_BRIDGE_NAME"] == "studio"
    assert "as studio" in result.stdout


def test_a_new_name_on_an_enrolled_host_enrolls_again(mac: Mac) -> None:
    mac.install()

    result = mac.setup(NAME="studio", ENROLLMENT=ENROLLMENT)

    assert f"enroll {ENROLLMENT}" in mac.log("agent-bridge.log")
    assert "as studio" in result.stdout


def test_a_new_name_without_enrolling_says_the_gateway_kept_the_old_one(mac: Mac) -> None:
    mac.install()

    result = mac.setup(NAME="studio")

    assert "Enrollment string" in result.stderr
    assert "still has it as mac, not studio" in result.stdout


def test_rotate_token_without_an_enrollment_string_changes_nothing(mac: Mac) -> None:
    mac.install()
    before = mac.env()

    result = mac.setup(ROTATE_TOKEN="1", check=False)

    assert result.returncode != 0
    assert "enrollment string" in result.stderr
    assert mac.env() == before


def test_a_port_another_program_holds_is_named_and_a_free_one_offered(mac: Mac) -> None:
    (mac.home / "port-8765").write_text("999\n")
    (mac.home / "ps-999").write_text("/usr/local/bin/nginx -g daemon off;\n")

    result = mac.install()

    assert "nginx" in result.stdout + result.stderr
    assert "999" in result.stdout + result.stderr
    assert "[8766]: " in result.stderr
    assert mac.env()["AGENT_BRIDGE_PORT"] == "8766"


def test_a_port_only_netstat_sees_is_another_users_program(mac: Mac) -> None:
    (mac.home / "netstat").write_text("tcp4  0  0  *.8765  *.*  LISTEN  131072  131072  0  0\n")

    result = mac.install()

    assert "another user's program" in result.stdout
    assert mac.env()["AGENT_BRIDGE_PORT"] == "8766"


def test_an_installed_address_the_mac_no_longer_has_is_replaced(mac: Mac) -> None:
    mac.install(HOST="192.0.2.99")
    for name, body in {"route": "echo '  interface: en9'", "ipconfig": "echo 192.0.2.7"}.items():
        (mac.stubs / name).write_text(f"#!/bin/sh\n{body}\n")
        (mac.stubs / name).chmod(0o755)

    result = mac.setup()

    assert mac.env()["AGENT_BRIDGE_HOST"] == "192.0.2.7"
    assert "The LAN address is now 192.0.2.7 (was 192.0.2.99)" in result.stdout


@pytest.mark.parametrize("holder", ["launchd job", "command line"])
def test_the_port_the_installed_service_holds_is_kept(mac: Mac, holder: str) -> None:
    (mac.home / "port-8765").write_text("4242\n")
    if holder == "launchd job":
        (mac.home / "launchd-pid").write_text("4242")
        (mac.home / "ps-4242").write_text("/usr/bin/python3 something\n")
    else:
        (mac.home / "ps-4242").write_text(f"{mac.repo}/.venv/bin/uvicorn agent_bridge.app:main --factory\n")
        (mac.home / "ppid-4242").write_text("    1\n")

    result = mac.install()

    assert mac.env()["AGENT_BRIDGE_PORT"] == "8765"
    assert not PROMPT.search(result.stderr), result.stderr


def test_a_bridge_started_by_hand_on_the_port_is_named_and_a_free_port_offered(mac: Mac) -> None:
    (mac.home / "port-8765").write_text("555\n")
    (mac.home / "ps-555").write_text(f"{mac.repo}/.venv/bin/python {mac.repo}/.venv/bin/agent-bridge serve\n")
    (mac.home / "ppid-555").write_text("  777\n")

    result = mac.install()

    assert "PID 555" in result.stdout
    assert mac.env()["AGENT_BRIDGE_PORT"] == "8766"


def test_a_taken_name_is_asked_again(mac: Mac) -> None:
    (mac.home / "enroll-codes").write_text("3\n0\n")

    result = mac.setup(HOST="127.0.0.1", ALLOWED_ROOTS=str(mac.home / "projects"), NAME="mac",
                       AGENT_BRIDGE_CLAUDE_CONFIG_DIR="", TRUST_ROOT="", ENROLLMENT=ENROLLMENT, input="studio\n")

    assert mac.env()["AGENT_BRIDGE_NAME"] == "studio"
    assert mac.log("agent-bridge.log").count(f"enroll {ENROLLMENT}") == 2
    assert "as studio" in result.stdout


def test_a_taken_name_with_no_input_left_stops(mac: Mac) -> None:
    (mac.home / "enroll-codes").write_text("3\n")

    result = mac.run("macos/setup.sh", check=False, HOST="127.0.0.1", ALLOWED_ROOTS=str(mac.home / "projects"),
                     NAME="mac", AGENT_BRIDGE_CLAUDE_CONFIG_DIR="", TRUST_ROOT="", ENROLLMENT=ENROLLMENT)

    assert result.returncode != 0
    assert "NAME" in result.stderr


def test_an_unreachable_gateway_leaves_the_service_running_unenrolled(mac: Mac) -> None:
    (mac.home / "enroll-codes").write_text("4\n")

    result = mac.install()

    assert "not enrolled" in result.stdout
    assert f"{mac.repo}/.venv/bin/agent-bridge enroll" in result.stdout
    assert f"bootstrap gui/{os.getuid()}" in mac.log("launchctl.log")


def test_enter_at_the_enrollment_question_installs_without_enrolling(mac: Mac) -> None:
    result = mac.install(ENROLLMENT="")

    assert "enroll" not in mac.log("agent-bridge.log")
    assert "not enrolled" in result.stdout
    # The next run asks again, since it is what's missing.
    result = mac.setup()
    assert "Enrollment string" in result.stderr


def test_uninstall_leaves_the_gateway_then_removes_the_service_and_files(mac: Mac) -> None:
    mac.install()
    (mac.home / "Library" / "Logs").mkdir(parents=True, exist_ok=True)

    mac.run("macos/uninstall.sh")

    assert "unenroll" in mac.log("agent-bridge.log")
    assert f"bootout gui/{os.getuid()}/{LABEL}" in mac.log("launchctl.log")
    assert not mac.plist.exists()
    assert not mac.support.exists()


def test_uninstall_still_removes_the_service_and_files_when_unenroll_fails(mac: Mac) -> None:
    mac.install()
    (mac.home / "unenroll-fails").touch()

    result = mac.run("macos/uninstall.sh")

    assert "unenroll failed" in result.stdout
    assert not mac.plist.exists()
    assert not mac.support.exists()


def test_uninstall_without_the_venv_still_removes_the_service_and_files(mac: Mac) -> None:
    mac.install()
    (mac.repo / ".venv" / "bin" / "agent-bridge").unlink()

    result = mac.run("macos/uninstall.sh")

    assert "sudo mcp-gateway bridges remove mac" in result.stdout
    assert not mac.plist.exists()
    assert not mac.support.exists()


def test_the_old_script_runs_the_new_one(mac: Mac) -> None:
    result = mac.run("setup-macos.sh", HOST="127.0.0.1", ALLOWED_ROOTS=str(mac.home / "projects"), NAME="mac",
                     AGENT_BRIDGE_CLAUDE_CONFIG_DIR="", TRUST_ROOT="", ENROLLMENT=ENROLLMENT)

    assert "deploy/macos/setup.sh" in result.stderr
    assert json.loads((mac.support / "state.json").read_text())["gateway"]["name"] == "mac"
