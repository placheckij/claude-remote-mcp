"""agent-bridge/deploy/windows/setup.ps1 and uninstall.ps1 run against temporary %APPDATA%,
%LOCALAPPDATA% and %USERPROFILE%. The cmdlets for scheduled tasks, the firewall, the network
and processes are replaced by PowerShell functions that keep their state in a JSON file, and
agent-bridge by a fake, so a test never touches this machine's tasks, rules or processes."""

import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="setup.ps1 is for Windows")

DEPLOY = Path(__file__).parent.parent / "agent-bridge" / "deploy" / "windows"
SYSTEM_ROOT = os.environ.get("SystemRoot", r"C:\Windows")
POWERSHELL = rf"{SYSTEM_ROOT}\System32\WindowsPowerShell\v1.0\powershell.exe"
STRING = "crm1@127.0.0.1:8766#0123456789abcdef01234567"

# Functions win over cmdlets of the same name, also inside the scripts they call.
STUBS = r"""
$ErrorActionPreference = 'Stop'
function Get-Fake { Get-Content -LiteralPath $env:FAKE_STATE -Raw | ConvertFrom-Json }
function Set-Fake($Fake) { $Fake | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $env:FAKE_STATE }
function Add-Call([string]$Call) { Add-Content -LiteralPath $env:FAKE_CALLS -Value $Call }
function Get-Listener([int]$Port) { (Get-Fake).listen | Where-Object { $_.port -eq $Port } | Select-Object -First 1 }

function Get-NetTCPConnection { param([int]$LocalPort, $State, $ErrorAction)
    $l = Get-Listener $LocalPort
    if ($l) { [pscustomobject]@{ OwningProcess = $l.pid } } }
function Get-CimInstance { param($ClassName, $Filter, $ErrorAction)
    if ($ClassName -eq 'Win32_ComputerSystem') { return [pscustomobject]@{ UserName = $env:FAKE_CONSOLE_USER } }
    $all = @((Get-Fake).listen | ForEach-Object { [pscustomobject]@{ ProcessId = $_.pid; Name = $_.name; CommandLine = $_.cmd } })
    if ($Filter -match 'ProcessId=(\d+)') { return $all | Where-Object { $_.ProcessId -eq [int]$Matches[1] } }
    $all }
function Stop-Process { param([int]$Id, [switch]$Force, $ErrorAction)
    Add-Call "Stop-Process $Id"
    $f = Get-Fake; $f.listen = @($f.listen | Where-Object { $_.pid -ne $Id }); Set-Fake $f }
function Start-Sleep { param([int]$Seconds, [int]$Milliseconds) }

function Get-ScheduledTask { param($TaskName, $TaskPath, $ErrorAction)
    if ((Get-Fake).task) { [pscustomobject]@{ TaskName = $TaskName } } }
function Register-ScheduledTask { param($TaskName, $TaskPath, $Xml, [switch]$Force)
    Add-Call "Register-ScheduledTask $TaskPath$TaskName"
    Set-Content -LiteralPath $env:FAKE_TASK_XML -Value $Xml -Encoding Unicode
    $f = Get-Fake; $f.task = $true; Set-Fake $f; [pscustomobject]@{} }
function Unregister-ScheduledTask { param($TaskName, $TaskPath, $Confirm)
    Add-Call "Unregister-ScheduledTask $TaskPath$TaskName"
    $f = Get-Fake; $f.task = $false; Set-Fake $f }
function Stop-ScheduledTask { param($TaskName, $TaskPath) Add-Call "Stop-ScheduledTask $TaskPath$TaskName" }
function Start-ScheduledTask { param($TaskName, $TaskPath)
    Add-Call "Start-ScheduledTask $TaskPath$TaskName"
    $port = 8765
    foreach ($line in Get-Content -LiteralPath (Join-Path $env:APPDATA 'agent-bridge\env')) {
        if ($line -match '^AGENT_BRIDGE_PORT=(\d+)') { $port = [int]$Matches[1] } }
    # What Task Scheduler starts: python.exe running the agent-bridge.exe launcher.
    $f = Get-Fake
    $f.listen = @($f.listen) + @([pscustomobject]@{ port = $port; pid = 4242; name = 'python.exe';
        cmd = "`"C:\Python\python.exe`" `"$env:FAKE_BRIDGE`" serve" })
    Set-Fake $f }

function Get-NetFirewallRule { param($Name, $ErrorAction)
    $r = (Get-Fake).rule
    if ($r) { [pscustomobject]@{ Name = $Name; Enabled = 'True'; Direction = 'Inbound'; Action = 'Allow'; Profile = $r.profile; Port = $r.port } } }
function Get-NetFirewallPortFilter { param($AssociatedNetFirewallRule)
    [pscustomobject]@{ Protocol = 'TCP'; LocalPort = [string]$AssociatedNetFirewallRule.Port } }
function Start-Process { param($FilePath, $Verb, [switch]$Wait, [switch]$PassThru, $WindowStyle, $ArgumentList)
    $command = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($ArgumentList[-1]))
    Add-Call "Elevated $command"
    $f = Get-Fake
    if ($command -match 'New-NetFirewallRule .*-LocalPort (\d+) -Profile (\w+)') { $f.rule = @{ port = [int]$Matches[1]; profile = $Matches[2] } }
    elseif ($command -match 'Remove-NetFirewallRule') { $f.rule = $null }
    Set-Fake $f
    [pscustomobject]@{ ExitCode = [int]$env:FAKE_ELEVATED_EXIT } }

function Find-NetRoute { param($RemoteIPAddress, $ErrorAction)
    [pscustomobject]@{ CimClass = [pscustomobject]@{ CimClassName = 'MSFT_NetIPAddress' }; IPAddress = '127.0.0.1' } }
function Get-NetIPAddress { param($IPAddress, $ErrorAction)
    if ($IPAddress -eq '127.0.0.1') { [pscustomobject]@{ InterfaceIndex = 7 } } }
function Get-NetConnectionProfile { param($InterfaceIndex, $ErrorAction)
    [pscustomobject]@{ NetworkCategory = $env:FAKE_CATEGORY; Name = 'Home Wi-Fi' } }
"""

# Stands in for agent-bridge.exe: the format checks, and enroll and unenroll writing the
# state file as the real ones do.
FAKE_BRIDGE = r"""
import json, os, re, sys
from pathlib import Path

calls = Path(os.environ["FAKE_CALLS"])
with calls.open("a", encoding="utf-8") as log:
    log.write("agent-bridge " + " ".join(sys.argv[1:]) + "\n")
state = Path(os.environ["LOCALAPPDATA"]) / "agent-bridge" / "state.json"
args = sys.argv[1:]
if args[0] == "check":
    if len(args) == 1:
        print("ok    fake checks")
        sys.exit(0)
    option, _, value = args[1].partition("=")
    ok = re.fullmatch(r"[a-z][a-z0-9-]{0,31}", value) if option == "--name-format" else len(value) >= 32
    if not ok:
        print(f"The value {value} won't do.", file=sys.stderr)
    sys.exit(0 if ok else 2)
if args[0] == "enroll":
    codes = Path(os.environ["FAKE_ENROLL_CODES"])
    queue = codes.read_text().split() if codes.exists() else []
    code = int(queue.pop(0)) if queue else 0
    codes.write_text(" ".join(queue))
    if code == 3:
        print("Couldn't enroll: the name is taken. Choose another name.", file=sys.stderr)
        sys.exit(3)
    env = (Path(os.environ["APPDATA"]) / "agent-bridge" / "env").read_text()
    name = re.search(r"^AGENT_BRIDGE_NAME=(.*)$", env, re.M).group(1)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"version": 1, "bridge_id": "b1", "gateway": {
        "url": "http://127.0.0.1:8766", "gateway_id": "g1", "name": name}}))
    print(f"Enrolled as {name} with the gateway at http://127.0.0.1:8766.")
    sys.exit(0)
if args[0] == "unenroll":
    data = json.loads(state.read_text())
    data["gateway"] = None
    state.write_text(json.dumps(data))
    print("Left the gateway.")
    sys.exit(0)
sys.exit(1)
"""


class Machine:
    """A stand-in Windows user: folders, the fake cmdlets' state, and agent-bridge."""

    def __init__(self, root: Path):
        self.root = root
        self.appdata = root / "Roaming"
        self.localappdata = root / "Local"
        self.profile = root / "me"
        for folder in (self.appdata, self.localappdata, self.profile / "Projects"):
            folder.mkdir(parents=True)
        self.herdr = self.localappdata / "Programs" / "Herdr" / "bin" / "herdr.exe"
        self.herdr.parent.mkdir(parents=True)
        self.herdr.write_bytes(b"")
        self.state = root / "fake.json"
        self.calls_file = root / "calls.log"
        self.task_xml = root / "task.xml"
        self.enroll_codes = root / "enroll-codes"
        (root / "stubs.ps1").write_text(STUBS)
        (root / "fake_bridge.py").write_text(FAKE_BRIDGE)
        self.bridge = root / "bin" / "agent-bridge.cmd"
        self.bridge.parent.mkdir()
        self.bridge.write_text(f'@"{sys.executable}" "{root / "fake_bridge.py"}" %*\r\n@exit /b %ERRORLEVEL%\r\n')
        self.save({"task": False, "rule": None, "listen": []})

    def save(self, fake: dict) -> None:
        self.state.write_text(json.dumps(fake))

    def fake(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8-sig"))

    def listen(self, port: int, pid: int, name: str, cmd: str) -> None:
        fake = self.fake()
        fake["listen"].append({"port": port, "pid": pid, "name": name, "cmd": cmd})
        self.save(fake)

    @property
    def env_file(self) -> Path:
        return self.appdata / "agent-bridge" / "env"

    def env(self) -> dict[str, str]:
        from dotenv import dotenv_values

        return dotenv_values(self.env_file)

    def calls(self) -> list[str]:
        return self.calls_file.read_text(encoding="utf-8-sig").splitlines() if self.calls_file.exists() else []

    def run(self, script: str, *args: str, input: str = "", expect: int = 0, **fakes: str) -> str:
        """Runs a deploy script with `args` (PowerShell syntax), answering its questions with
        `input`; returns what it printed."""
        self.calls_file.unlink(missing_ok=True)
        runner = self.root / "run.ps1"
        runner.write_text(
            f". '{self.root / 'stubs.ps1'}'\r\n& '{DEPLOY / script}' {' '.join(args)}\r\nexit $LASTEXITCODE\r\n"
        )
        env = {
            **os.environ,
            "COMPUTERNAME": "WIN-TEST",
            "USERPROFILE": str(self.profile),
            "APPDATA": str(self.appdata),
            "LOCALAPPDATA": str(self.localappdata),
            "FAKE_STATE": str(self.state),
            "FAKE_CALLS": str(self.calls_file),
            "FAKE_TASK_XML": str(self.task_xml),
            "FAKE_ENROLL_CODES": str(self.enroll_codes),
            "FAKE_BRIDGE": str(self.bridge),
            "FAKE_CATEGORY": "Private",
            "FAKE_CONSOLE_USER": "",
            "FAKE_ELEVATED_EXIT": "0",
            **fakes,
        }
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(runner)],
            env=env, input=input, capture_output=True, text=True, timeout=120,
        )
        output = result.stdout + result.stderr
        assert result.returncode == expect, output
        return output

    def setup(self, *args: str, **kwargs) -> str:
        return self.run("setup.ps1", f"-Bridge '{self.bridge}'", *args, **kwargs)


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    return Machine(tmp_path)


def test_a_first_install_writes_the_settings_registers_the_task_and_enrolls(machine: Machine) -> None:
    output = machine.setup(f"-Enrollment '{STRING}'", input="\n\n")

    env = machine.env()
    assert re.fullmatch(r"[0-9a-f]{64}", env["AGENT_BRIDGE_TOKEN"])
    assert env["AGENT_BRIDGE_ALLOWED_ROOTS"] == str(machine.profile / "Projects")
    assert env["AGENT_BRIDGE_NAME"] == "win-test"
    assert env["AGENT_BRIDGE_HOST"] == "127.0.0.1"
    assert env["AGENT_BRIDGE_PORT"] == "8765"
    assert env["AGENT_BRIDGE_HERDR_BIN"] == str(machine.herdr)
    # No BOM: python-dotenv would read it as part of the first key.
    assert not machine.env_file.read_bytes().startswith(b"\xef\xbb\xbf")

    calls = machine.calls()
    assert "Register-ScheduledTask \\agent-bridge" in calls
    assert "Start-ScheduledTask \\agent-bridge" in calls
    assert f"agent-bridge enroll {STRING}" in calls
    assert calls[-1] == "agent-bridge check"
    assert machine.fake()["rule"] == {"port": 8765, "profile": "Private"}
    assert "Enrolled as win-test with the gateway at http://127.0.0.1:8766" in output
    assert env["AGENT_BRIDGE_TOKEN"] not in output


def test_the_task_runs_agent_bridge_serve_hidden_at_logon_as_this_user(machine: Machine) -> None:
    machine.setup("-Enrollment ''", input="\n\n")

    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    task = ET.fromstring(machine.task_xml.read_bytes())
    sid = task.find("t:Principals/t:Principal/t:UserId", ns).text
    assert re.fullmatch(r"S-1-5-[0-9-]+", sid)
    assert task.find("t:Triggers/t:LogonTrigger/t:UserId", ns).text == sid
    assert task.find("t:Principals/t:Principal/t:LogonType", ns).text == "InteractiveToken"
    assert task.find("t:Settings/t:Hidden", ns).text == "true"
    assert task.find("t:Settings/t:ExecutionTimeLimit", ns).text == "PT0S"
    exec_ = task.find("t:Actions/t:Exec", ns)
    assert exec_.find("t:Command", ns).text == rf"{SYSTEM_ROOT}\System32\conhost.exe"
    log = machine.localappdata / "agent-bridge" / "agent-bridge.log"
    assert exec_.find("t:Arguments", ns).text == (
        rf'--headless "{SYSTEM_ROOT}\System32\cmd.exe" /d /s /c "(if exist "{log}" move /y "{log}" "{log}.1" >nul) '
        rf'& "{machine.bridge}" serve > "{log}" 2>&1"'
    )
    assert exec_.find("t:WorkingDirectory", ns).text == str(machine.localappdata / "agent-bridge")


def test_a_rerun_keeps_the_token_and_other_settings_and_asks_nothing(machine: Machine) -> None:
    machine.setup(f"-Enrollment '{STRING}'", input="\n\n")
    token = machine.env()["AGENT_BRIDGE_TOKEN"]
    added = "AGENT_BRIDGE_ACCOUNTS='{\"work\": {\"folders\": [\"C:/work\"]}}'\nAGENT_BRIDGE_MAX_AGENTS=4\n"
    with machine.env_file.open("a", encoding="utf-8") as file:
        file.write(added)

    # No input: a question would get Enter, and a new name, port or enrollment would show below.
    output = machine.setup()

    env = machine.env()
    assert env["AGENT_BRIDGE_TOKEN"] == token
    assert env["AGENT_BRIDGE_PORT"] == "8765"
    assert added in machine.env_file.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert not any(call.startswith("agent-bridge enroll") for call in machine.calls())
    # The bridge the first run started was stopped by its command line.
    assert "Stop-Process 4242" in machine.calls()
    assert "Enrolled as win-test" in output
    assert "Elevated" not in "\n".join(machine.calls()), "the firewall rule was there already"


def test_rotate_token_replaces_it_and_asks_to_enroll_again(machine: Machine) -> None:
    machine.setup(f"-Enrollment '{STRING}'", input="\n\n")
    token = machine.env()["AGENT_BRIDGE_TOKEN"]

    output = machine.setup("-RotateToken", input=f"{STRING}\n")

    assert machine.env()["AGENT_BRIDGE_TOKEN"] != token
    assert "still has the old token" in output
    assert f"agent-bridge enroll {STRING}" in machine.calls()


def test_values_with_spaces_and_quotes_read_back_unchanged(machine: Machine) -> None:
    roots = machine.profile / "My Projects"
    roots.mkdir()
    machine.setup(f"-Roots '{roots}'", "-Name 'win-pc'", "-ClaudeConfigDir 'C:\\Users\\it''s\\agents'", "-Enrollment ''")
    # A re-run reads them back as it wrote them.
    machine.setup()

    env = machine.env()
    assert env["AGENT_BRIDGE_ALLOWED_ROOTS"] == str(roots)
    assert env["AGENT_BRIDGE_CLAUDE_CONFIG_DIR"] == "C:\\Users\\it's\\agents"


def test_a_port_held_by_another_program_offers_a_free_one(machine: Machine) -> None:
    machine.listen(8765, 77, "nginx.exe", "nginx.exe -g daemon")
    machine.listen(8766, 78, "other.exe", "other.exe")

    output = machine.setup("-Enrollment ''", input="\n\n\n")

    assert "Port 8765 is in use by another program: PID 77, nginx.exe" in output
    assert "nginx.exe -g daemon" in output
    assert machine.env()["AGENT_BRIDGE_PORT"] == "8767"
    assert machine.fake()["rule"]["port"] == 8767
    assert "Stop-Process 77" not in machine.calls()


def test_a_port_passed_in_and_held_by_another_program_stops_setup(machine: Machine) -> None:
    machine.listen(8765, 77, "nginx.exe", "nginx.exe")

    output = machine.setup("-Port 8765", "-Enrollment ''", input="\n\n", expect=1)

    assert "stop that program, or pass another -Port" in output
    assert not machine.env_file.exists()


def test_a_port_held_by_the_bridge_installed_before_is_taken_over(machine: Machine) -> None:
    # As setup-macos.sh's era ran it: uvicorn with the app factory.
    machine.listen(8765, 55, "uvicorn.exe", r"C:\old\.venv\Scripts\uvicorn.exe agent_bridge.app:main --factory")

    machine.setup("-Enrollment ''", input="\n\n")

    assert "Stop-Process 55" in machine.calls()
    assert machine.env()["AGENT_BRIDGE_PORT"] == "8765"


def test_a_taken_name_is_asked_for_again(machine: Machine) -> None:
    machine.enroll_codes.write_text("3 0")

    output = machine.setup(f"-Enrollment '{STRING}'", input="\n\nwin-other\n")

    assert machine.env()["AGENT_BRIDGE_NAME"] == "win-other"
    assert [call for call in machine.calls() if call.startswith("agent-bridge enroll")] == [f"agent-bridge enroll {STRING}"] * 2
    assert "Enrolled as win-other" in output


def test_a_public_network_is_made_private_when_agreed(machine: Machine) -> None:
    machine.setup("-Enrollment ''", input="\n\ny\n", FAKE_CATEGORY="Public")

    elevated = [call for call in machine.calls() if call.startswith("Elevated")]
    assert len(elevated) == 1
    assert "Set-NetConnectionProfile -InterfaceIndex 7 -NetworkCategory Private" in elevated[0]
    assert "New-NetFirewallRule" in elevated[0]


def test_a_declined_uac_prompt_prints_the_command_to_run(machine: Machine) -> None:
    output = machine.setup("-Enrollment ''", input="\n\n", FAKE_ELEVATED_EXIT="1")

    assert "In a PowerShell run as administrator:" in output
    assert "New-NetFirewallRule -Name 'agent-bridge'" in output


def test_another_account_than_the_one_logged_on_is_refused(machine: Machine) -> None:
    output = machine.setup("-Enrollment ''", expect=1, FAKE_CONSOLE_USER="SOMEPC\\somebody-else")

    assert "run setup.ps1 as SOMEPC\\somebody-else" in output
    assert not machine.env_file.exists()


def test_uninstall_leaves_the_gateway_and_removes_everything(machine: Machine) -> None:
    machine.setup(f"-Enrollment '{STRING}'", input="\n\n")
    log = machine.localappdata / "agent-bridge" / "agent-bridge.log"
    log.write_text("a log")

    machine.run("uninstall.ps1", f"-Bridge '{machine.bridge}'")

    calls = machine.calls()
    assert calls[0] == "agent-bridge unenroll"
    assert "Unregister-ScheduledTask \\agent-bridge" in calls
    assert "Stop-Process 4242" in calls
    assert any(call.startswith("Elevated") and "Remove-NetFirewallRule -Name 'agent-bridge'" in call for call in calls)
    fake = machine.fake()
    assert fake["task"] is False and fake["rule"] is None and fake["listen"] == []
    assert not (machine.appdata / "agent-bridge").exists()
    assert not (machine.localappdata / "agent-bridge").exists()
