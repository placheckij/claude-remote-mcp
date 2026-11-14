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
    if (-not $LocalPort) {
        return (Get-Fake).listen | Where-Object { $_.port } | ForEach-Object { [pscustomobject]@{ LocalPort = $_.port; OwningProcess = $_.pid } } }
    $l = Get-Listener $LocalPort
    if ($l) { [pscustomobject]@{ LocalPort = $LocalPort; OwningProcess = $l.pid } } }
function Get-CimInstance { param($ClassName, $Filter, $ErrorAction)
    if ($Filter -match "Name='explorer.exe'") {
        if ($env:FAKE_DESKTOP_USER) { return [pscustomobject]@{ Name = 'explorer.exe' } }
        return }
    $all = @((Get-Fake).listen | ForEach-Object { [pscustomobject]@{ ProcessId = $_.pid; Name = $_.name; CommandLine = $_.cmd; ExecutablePath = $_.path } })
    if ($Filter -match 'ProcessId=(\d+)') { return $all | Where-Object { $_.ProcessId -eq [int]$Matches[1] } }
    $all }
function Invoke-CimMethod { param($InputObject, $MethodName, $ErrorAction)
    $domain, $user = $env:FAKE_DESKTOP_USER -split '\\', 2
    [pscustomobject]@{ Domain = $domain; User = $user } }
function netsh { foreach ($range in ($env:FAKE_EXCLUDED_PORTS -split ',')) { if ($range) { "  $($range -replace '-', '  ')" } } }
function Stop-Process { param([int]$Id, [switch]$Force, $ErrorAction)
    Add-Call "Stop-Process $Id"
    $f = Get-Fake; $f.listen = @($f.listen | Where-Object { $_.pid -ne $Id }); Set-Fake $f }
function Start-Sleep { param([int]$Seconds, [int]$Milliseconds) }
# The user's and the machine's lasting environment variables, read from the registry.
function Get-ItemProperty { param($LiteralPath, $ErrorAction)
    if ($LiteralPath -eq 'HKCU:\Environment' -and $env:FAKE_USER_VARIABLE) {
        $o = [pscustomobject]@{ Path = 'C:\Windows' }
        $o | Add-Member -NotePropertyName $env:FAKE_USER_VARIABLE -NotePropertyValue 'x'
        return $o }
    [pscustomobject]@{ Path = 'C:\Windows' } }

function Get-ScheduledTask { param($TaskName, $TaskPath, $ErrorAction)
    if ((Get-Fake).task) { [pscustomobject]@{ TaskName = $TaskName } } }
function Register-ScheduledTask { param($TaskName, $TaskPath, $Xml, [switch]$Force)
    Add-Call "Register-ScheduledTask $TaskPath$TaskName"
    Set-Content -LiteralPath $env:FAKE_TASK_XML -Value $Xml -Encoding Unicode
    $f = Get-Fake; $f.task = $true; $f.enabled = $true; Set-Fake $f
    # Its registration trigger starts it, as Task Scheduler does.
    Start-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath
    [pscustomobject]@{} }
function Export-ScheduledTask { param($TaskName, $TaskPath)
    Get-Content -LiteralPath $env:FAKE_TASK_XML -Raw -Encoding Unicode }
function Wait-Process { param($Id, $Timeout, $ErrorAction) }
function Unregister-ScheduledTask { param($TaskName, $TaskPath, $Confirm)
    Add-Call "Unregister-ScheduledTask $TaskPath$TaskName"
    $f = Get-Fake; $f.task = $false; Set-Fake $f }
function Stop-ScheduledTask { param($TaskName, $TaskPath) Add-Call "Stop-ScheduledTask $TaskPath$TaskName" }
function Disable-ScheduledTask { param($TaskName, $TaskPath)
    Add-Call "Disable-ScheduledTask $TaskPath$TaskName"
    $f = Get-Fake; $f.enabled = $false; Set-Fake $f }
function Enable-ScheduledTask { param($TaskName, $TaskPath)
    Add-Call "Enable-ScheduledTask $TaskPath$TaskName"
    $f = Get-Fake; $f.enabled = $true; Set-Fake $f }
function Start-ScheduledTask { param($TaskName, $TaskPath)
    Add-Call "Start-ScheduledTask $TaskPath$TaskName"
    # As Task Scheduler does, which won't start a disabled task.
    if (-not (Get-Fake).enabled) { throw 'The task is disabled.' }
    # MultipleInstancesPolicy IgnoreNew.
    if (@((Get-Fake).listen | Where-Object { $_.pid -eq 4242 }).Count -gt 0) { return }
    $port = 8765
    foreach ($line in Get-Content -LiteralPath (Join-Path $env:APPDATA 'agent-bridge\env')) {
        if ($line -match '^AGENT_BRIDGE_PORT=(\d+)') { $port = [int]$Matches[1] } }
    # What Task Scheduler starts: cmd.exe on run.cmd, the agent-bridge.exe launcher, and
    # python.exe running it, which is the one that listens. Port 0: not listening.
    if ($env:FAKE_NO_START) { return }
    $run = Join-Path $env:LOCALAPPDATA 'agent-bridge\run.cmd'
    $f = Get-Fake
    $f.listen = @($f.listen) + @(
        [pscustomobject]@{ port = 0; pid = 4240; name = 'cmd.exe'; path = 'C:\Windows\System32\cmd.exe';
            cmd = "C:\Windows\System32\cmd.exe /d /c call `"$run`"" },
        [pscustomobject]@{ port = 0; pid = 4241; name = 'agent-bridge.exe'; path = $env:FAKE_BRIDGE;
            cmd = "`"$env:FAKE_BRIDGE`" serve" },
        [pscustomobject]@{ port = $port; pid = 4242; name = 'python.exe'; path = 'C:\Python\python.exe';
            cmd = "`"C:\Python\python.exe`" `"$env:FAKE_BRIDGE`" serve" })
    Set-Fake $f }

function Get-NetFirewallRule { param($Name, $ErrorAction)
    # Piped an application filter: the rule that blocks that program.
    $piped = @($input)
    if ($piped.Count) { return $piped | ForEach-Object { [pscustomobject]@{ Direction = 'Inbound'; Action = 'Block'; Enabled = 'True' } } }
    $r = (Get-Fake).rule
    if ($r) { [pscustomobject]@{ Name = $Name; Enabled = 'True'; Direction = 'Inbound'; Action = 'Allow'; Profile = $r.profile; Port = $r.port } } }
function Get-NetFirewallApplicationFilter { param($Program, $ErrorAction)
    if ($env:FAKE_BLOCKED_PROGRAM -and $Program -eq $env:FAKE_BLOCKED_PROGRAM) { [pscustomobject]@{ Program = $Program } } }
function Get-NetFirewallPortFilter { param($AssociatedNetFirewallRule)
    [pscustomobject]@{ Protocol = 'TCP'; LocalPort = [string]$AssociatedNetFirewallRule.Port } }
function Start-Process { param($FilePath, $Verb, [switch]$Wait, [switch]$PassThru, $WindowStyle, $ArgumentList)
    if ($env:FAKE_UAC_DECLINED) { throw 'The operation was canceled by the user.' }
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
        with calls.open("a", encoding="utf-8") as log:
            log.write("token " + os.environ.get("AGENT_BRIDGE_TOKEN", "from the env file") + "\n")
        # As the real one: the settings first, from AGENT_BRIDGE_ENV_FILE when it is set.
        env_file = os.environ.get("AGENT_BRIDGE_ENV_FILE") or str(Path(os.environ["APPDATA"]) / "agent-bridge" / "env")
        bad = os.environ.get("FAKE_BAD_SETTINGS")
        if bad and bad in Path(env_file).read_text(encoding="utf-8"):
            print(f"FAIL  settings (Invalid settings: {bad} won't do)")
            sys.exit(1)
        print(f"ok    settings in {env_file}")
        print("ok    fake checks")
        sys.exit(0)
    if os.environ.get("FAKE_OLD_CHECK"):
        print("usage: agent-bridge check [-h]", file=sys.stderr)
        print("agent-bridge: error: unrecognized arguments", file=sys.stderr)
        sys.exit(2)
    if os.environ.get("FAKE_BROKEN_CHECK"):
        print("ImportError: no module named fastapi", file=sys.stderr)
        sys.exit(1)
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
    if code:
        print(f"Couldn't enroll (fake exit code {code}).", file=sys.stderr)
        sys.exit(code)
    env = (Path(os.environ["APPDATA"]) / "agent-bridge" / "env").read_text()
    name = re.search(r"^AGENT_BRIDGE_NAME=(.*)$", env, re.M).group(1)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"version": 1, "bridge_id": "b1", "gateway": {
        "url": "http://127.0.0.1:8766", "gateway_id": "g1", "name": name}}))
    print(f"Enrolled as {name} with the gateway at http://127.0.0.1:8766.")
    sys.exit(0)
if args[0] == "unenroll":
    if os.environ.get("FAKE_BROKEN_UNENROLL"):
        print("Traceback: ImportError", file=sys.stderr)
        sys.exit(1)
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
        self.save({"task": False, "enabled": False, "rule": None, "listen": []})

    def save(self, fake: dict) -> None:
        self.state.write_text(json.dumps(fake))

    def fake(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8-sig"))

    def listen(self, port: int, pid: int, name: str, cmd: str) -> None:
        fake = self.fake()
        fake["listen"].append({"port": port, "pid": pid, "name": name, "cmd": cmd, "path": ""})
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
            "FAKE_DESKTOP_USER": "",
            "FAKE_EXCLUDED_PORTS": "",
            "FAKE_UAC_DECLINED": "",
            "FAKE_BROKEN_CHECK": "",
            "FAKE_OLD_CHECK": "",
            "FAKE_BROKEN_UNENROLL": "",
            "FAKE_NO_START": "",
            "FAKE_BAD_SETTINGS": "",
            "FAKE_USER_VARIABLE": "",
            "AGENT_BRIDGE_STATE_FILE": r"C:\leftover\state.json",
            "FAKE_ELEVATED_EXIT": "0",
            "FAKE_BLOCKED_PROGRAM": "",
            **fakes,
        }
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(runner)],
            # PowerShell's own errors come in the console's code page, localized.
            env=env, input=input, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
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
    # Last, before the fake's note of which token it saw.
    assert calls[-2] == "agent-bridge check"
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
    # Its repetition covers the time until the next logon.
    assert task.find("t:Triggers/t:RegistrationTrigger/t:Repetition/t:Interval", ns).text == "PT1M"
    assert task.find("t:Principals/t:Principal/t:LogonType", ns).text == "InteractiveToken"
    assert task.find("t:Settings/t:Hidden", ns).text == "true"
    assert task.find("t:Settings/t:ExecutionTimeLimit", ns).text == "PT0S"
    exec_ = task.find("t:Actions/t:Exec", ns)
    assert exec_.find("t:Command", ns).text == rf"{SYSTEM_ROOT}\System32\conhost.exe"
    # One quoted path only: conhost.exe quotes each argument again, which breaks quotes inside.
    run = machine.localappdata / "agent-bridge" / "run.cmd"
    assert exec_.find("t:Arguments", ns).text == rf'--headless "{SYSTEM_ROOT}\System32\cmd.exe" /d /c call "{run}"'
    assert exec_.find("t:WorkingDirectory", ns).text == str(machine.localappdata / "agent-bridge")


def test_the_task_runs_a_wrapper_that_logs_agent_bridge_serve(machine: Machine) -> None:
    machine.setup("-Enrollment ''", input="\n\n")

    run = (machine.localappdata / "agent-bridge" / "run.cmd").read_bytes()
    # cmd misreads a batch file with LF line endings after chcp.
    assert b"\r\n" in run and b"\n" not in run.replace(b"\r\n", b"")
    text = run.decode("utf-8")
    log = machine.localappdata / "agent-bridge" / "agent-bridge.log"
    assert "{{" not in text
    assert f'set "LOG={log}"' in text
    assert f'"{machine.bridge}" serve >> "%LOG%" 2>&1' in text
    assert 'set "PYTHONUTF8=1"' in text


def test_a_rerun_keeps_the_token_and_other_settings_and_asks_nothing(machine: Machine) -> None:
    machine.setup(f"-Enrollment '{STRING}'", input="\n\n")
    token = machine.env()["AGENT_BRIDGE_TOKEN"]
    added = "AGENT_BRIDGE_ACCOUNTS='{\"work\": {\"folders\": [\"C:/work\"]}}'\nAGENT_BRIDGE_MAX_AGENTS=4\n"
    with machine.env_file.open("a", encoding="utf-8") as file:
        file.write(added)

    # Kept: another program naming agent-bridge.exe, and an editor on its source.
    machine.listen(0, 5000, "agent-bridge.exe", f'"{machine.bridge}" check')
    machine.listen(0, 5001, "Code.exe", r"Code.exe C:\src\agent_bridge\cli.py")
    run = machine.localappdata / "agent-bridge" / "run.cmd"
    machine.listen(0, 5002, "notepad.exe", f'notepad.exe "{run}"')

    # An answer to any question would stop setup or show below as a new name, port or enrollment.
    output = machine.setup(input="not an answer\n" * 5)

    env = machine.env()
    assert env["AGENT_BRIDGE_TOKEN"] == token
    assert env["AGENT_BRIDGE_PORT"] == "8765"
    assert added in machine.env_file.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert not any(call.startswith("agent-bridge enroll") for call in machine.calls())
    # The first run's bridge, including the cmd.exe and launcher that hold no port.
    calls = machine.calls()
    assert {"Stop-Process 4240", "Stop-Process 4241", "Stop-Process 4242"} <= set(calls)
    assert not {"Stop-Process 5000", "Stop-Process 5001", "Stop-Process 5002"} & set(calls)
    # The installed token never goes on a command line.
    assert not any("token-format" in call or token in call for call in calls)
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
    output = machine.setup("-Enrollment ''", expect=1, FAKE_DESKTOP_USER="SOMEPC\\somebody-else")

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


def test_setup_disables_the_task_while_it_replaces_the_bridge(machine: Machine) -> None:
    machine.setup("-Enrollment ''", input="\n\n")

    machine.setup()

    # Otherwise the task's one-minute repeat could start the old bridge in between.
    calls = machine.calls()
    assert calls.index(r"Disable-ScheduledTask \agent-bridge") < calls.index(r"Stop-ScheduledTask \agent-bridge")
    # Registering it again enables it, and its registration trigger starts it.
    assert calls.index(r"Disable-ScheduledTask \agent-bridge") < calls.index(r"Register-ScheduledTask \agent-bridge")


def test_enter_at_the_public_network_question_leaves_it_public(machine: Machine) -> None:
    output = machine.setup("-Enrollment ''", input="\n\n\n", FAKE_CATEGORY="Public")

    elevated = [call for call in machine.calls() if call.startswith("Elevated")]
    assert "Set-NetConnectionProfile" not in elevated[0]
    assert "can't reach agent-bridge until this network is Private" in output


def test_an_unreachable_gateway_leaves_the_bridge_installed_but_not_enrolled(machine: Machine) -> None:
    machine.enroll_codes.write_text("4")

    output = machine.setup(f"-Enrollment '{STRING}'", input="\n\n", expect=1)

    assert r"Start-ScheduledTask \agent-bridge" in machine.calls()
    assert "Not enrolled: see" in output
    assert "Enrolled as" not in output


def test_a_failed_enrollment_after_rotating_the_token_says_the_gateway_has_the_old_one(machine: Machine) -> None:
    machine.setup(f"-Enrollment '{STRING}'", input="\n\n")
    machine.enroll_codes.write_text("4")

    output = machine.setup("-RotateToken", f"-Enrollment '{STRING}'", expect=1)

    assert "Not enrolled: see" in output
    assert "still has this host's old settings" in output
    assert "Enrolled as win-test" not in output


def test_a_new_name_on_an_enrolled_host_asks_to_enroll_again(machine: Machine) -> None:
    machine.setup(f"-Enrollment '{STRING}'", input="\n\n")

    output = machine.setup("-Name 'win-new'", input=f"{STRING}\n")

    assert "knows this host as win-test" in output
    assert f"agent-bridge enroll {STRING}" in machine.calls()
    assert "Enrolled as win-new" in output


def test_a_firewall_rule_blocking_python_is_reported(machine: Machine) -> None:
    output = machine.setup("-Enrollment ''", input="\n\n", FAKE_BLOCKED_PROGRAM=r"C:\Python\python.exe")

    assert r"a firewall rule blocks C:\Python\python.exe" in output


def test_uninstall_goes_ahead_without_agent_bridge(machine: Machine) -> None:
    machine.setup(f"-Enrollment '{STRING}'", input="\n\n")
    machine.bridge.unlink()

    output = machine.run("uninstall.ps1", f"-Bridge '{machine.bridge}'")

    assert "sudo mcp-gateway bridges remove win-test" in output
    calls = machine.calls()
    assert r"Unregister-ScheduledTask \agent-bridge" in calls
    # Still stopped through the port, since it's an agent-bridge.
    assert "Stop-Process 4242" in calls
    assert not (machine.appdata / "agent-bridge").exists()


def test_a_port_windows_keeps_for_itself_is_not_offered(machine: Machine) -> None:
    output = machine.setup("-Enrollment ''", input="\n\n\n", FAKE_EXCLUDED_PORTS="8765-8770")

    assert "Windows keeps port 8765 for itself" in output
    assert machine.env()["AGENT_BRIDGE_PORT"] == "8771"


def test_agent_bridge_failing_to_run_is_not_called_a_bad_value(machine: Machine) -> None:
    # The name answered at the prompt is checked on its own.
    output = machine.setup("-Enrollment ''", input="\nwin-pc\n", expect=1, FAKE_BROKEN_CHECK="1")

    assert "didn't run (exit 1; see above)" in output
    assert "won't do" not in output


def test_an_error_after_stopping_the_bridge_enables_the_task_again(machine: Machine) -> None:
    machine.setup("-Enrollment ''", input="\n\n")
    # The env file can't be written: a folder is where its temporary file goes.
    (machine.appdata / "agent-bridge" / "env.tmp").mkdir()

    machine.setup(expect=1)

    calls = machine.calls()
    assert calls.index(r"Disable-ScheduledTask \agent-bridge") < calls.index(r"Enable-ScheduledTask \agent-bridge")
    assert machine.fake()["enabled"] is True
    # And registered again, which starts it with its every-minute repeat: Stop-Bridge ended
    # the bridge, and enabling fires no trigger.
    assert calls.index(r"Register-ScheduledTask \agent-bridge") > calls.index(r"Enable-ScheduledTask \agent-bridge")


def test_a_declined_uac_prompt_prints_every_command_to_run(machine: Machine) -> None:
    output = machine.setup("-Enrollment ''", input="\n\ny\n", FAKE_CATEGORY="Public", FAKE_UAC_DECLINED="1")

    assert "New-NetFirewallRule -Name 'agent-bridge'" in output
    assert "Set-NetConnectionProfile -InterfaceIndex 7 -NetworkCategory Private" in output


def test_a_network_that_cant_be_made_private_still_gets_the_rule(machine: Machine) -> None:
    # Bit 1: the second command, the network profile, failed.
    output = machine.setup("-Enrollment ''", input="\n\ny\n", FAKE_CATEGORY="Public", FAKE_ELEVATED_EXIT="2")

    assert machine.fake()["rule"] == {"port": 8765, "profile": "Private"}
    by_hand = output.split("In a PowerShell run as administrator:")[1]
    assert "Set-NetConnectionProfile" in by_hand
    assert "New-NetFirewallRule" not in by_hand


def test_the_firewall_comes_before_the_task_which_starts_the_bridge(machine: Machine) -> None:
    machine.setup("-Enrollment ''", input="\n\n")

    calls = machine.calls()
    elevated = next(i for i, call in enumerate(calls) if call.startswith("Elevated"))
    assert elevated < calls.index(r"Register-ScheduledTask \agent-bridge")


def test_a_program_that_only_mentions_agent_bridge_is_another_program(machine: Machine) -> None:
    machine.listen(8765, 77, "python.exe", r"python.exe C:\src\agent_bridge\tools\debug_server.py")

    output = machine.setup("-Enrollment ''", input="\n\n\n")

    assert "Port 8765 is in use by another program: PID 77" in output
    assert "Stop-Process 77" not in machine.calls()
    assert machine.env()["AGENT_BRIDGE_PORT"] == "8766"


def test_an_agent_bridge_without_the_format_checks_is_called_too_old(machine: Machine) -> None:
    output = machine.setup("-Enrollment ''", input="\nwin-pc\n", expect=1, FAKE_OLD_CHECK="1")

    assert "it is older than this setup.ps1" in output
    assert "won't do" not in output


def test_uninstall_says_how_to_remove_the_host_when_unenroll_fails(machine: Machine) -> None:
    machine.setup(f"-Enrollment '{STRING}'", input="\n\n")

    output = machine.run("uninstall.ps1", f"-Bridge '{machine.bridge}'", FAKE_BROKEN_UNENROLL="1")

    assert "sudo mcp-gateway bridges remove win-test" in output
    assert not (machine.localappdata / "agent-bridge").exists()


def test_uninstall_uses_the_installed_agent_bridge(machine: Machine) -> None:
    # Installed with -Bridge, uninstalled without: run.cmd says which agent-bridge it runs.
    machine.setup(f"-Enrollment '{STRING}'", input="\n\n")

    machine.run("uninstall.ps1")

    assert machine.calls()[0] == "agent-bridge unenroll"


def test_a_bridge_that_does_not_start_is_not_called_done(machine: Machine) -> None:
    output = machine.setup("-Enrollment ''", input="\n\n", expect=1, FAKE_NO_START="1")

    assert "agent-bridge didn't start" in output
    assert "Done. agent-bridge listens" not in output


def test_a_name_answered_while_enrolled_under_another_asks_to_enroll_again(machine: Machine) -> None:
    machine.setup(f"-Enrollment '{STRING}'", input="\n\n")
    machine.env_file.unlink()

    output = machine.setup(input=f"\nwin-new\n{STRING}\n")

    assert "knows this host as win-test" in output
    assert "Enrolled as win-new" in output


def test_uninstall_removes_a_leftover_copy_of_the_env_file(machine: Machine) -> None:
    machine.setup("-Enrollment ''", input="\n\n")
    (machine.appdata / "agent-bridge" / "env.tmp").write_text("AGENT_BRIDGE_TOKEN=secret\n")

    machine.run("uninstall.ps1")

    assert not (machine.appdata / "agent-bridge").exists()


def test_another_os_is_told_so_before_anything_reads_appdata(machine: Machine) -> None:
    output = machine.setup(expect=1, OS="Linux", APPDATA="")

    assert "these scripts are for Windows" in output


def test_a_reserved_port_passed_in_says_to_pass_another(machine: Machine) -> None:
    output = machine.setup("-Port 8765", "-Enrollment ''", input="\n\n", expect=1, FAKE_EXCLUDED_PORTS="8765-8765")

    assert "error: pass another -Port" in output
    assert "stop that program" not in output


def test_a_failed_enrollment_is_setups_exit_code(machine: Machine) -> None:
    machine.enroll_codes.write_text("4")

    machine.setup(f"-Enrollment '{STRING}'", input="\n\n", expect=1)


def test_a_rename_on_the_gateway_restarts_the_task_with_its_repeat(machine: Machine) -> None:
    machine.enroll_codes.write_text("3 0")

    machine.setup(f"-Enrollment '{STRING}'", input="\n\nwin-other\n")

    # After the name is changed, the task is registered again rather than only started.
    calls = machine.calls()
    renamed = calls.index(r"Disable-ScheduledTask \agent-bridge")
    assert r"Register-ScheduledTask \agent-bridge" in calls[renamed:]


def test_switching_from_an_agent_bridge_installed_elsewhere_is_said(machine: Machine) -> None:
    machine.setup("-Enrollment ''", input="\n\n")
    other = machine.root / "other" / "agent-bridge.cmd"
    other.parent.mkdir()
    other.write_text(machine.bridge.read_text())

    output = machine.run("setup.ps1", f"-Bridge '{other}'")

    assert f"The task ran {machine.bridge}; it will run {other}." in output


def test_uninstall_goes_on_with_a_port_out_of_range(machine: Machine) -> None:
    machine.setup("-Enrollment ''", input="\n\n")
    with machine.env_file.open("a", encoding="utf-8") as file:
        file.write("AGENT_BRIDGE_PORT=70000\n")

    machine.run("uninstall.ps1")

    assert not (machine.appdata / "agent-bridge").exists()


def test_settings_agent_bridge_refuses_stop_setup_before_the_bridge(machine: Machine) -> None:
    machine.setup("-Enrollment ''", input="\n\n")
    env = machine.env_file.read_bytes()

    output = machine.setup("-Address '192.168.1'", expect=1, FAKE_BAD_SETTINGS="AGENT_BRIDGE_HOST=192.168.1")

    # Its reason, naming the real env file, not the copy it checked.
    assert f"FAIL  settings (Invalid settings: AGENT_BRIDGE_HOST=192.168.1 won't do)" in output
    assert "wouldn't start with these settings" in output
    assert r"Disable-ScheduledTask \agent-bridge" not in machine.calls()
    assert machine.env_file.read_bytes() == env


def test_the_settings_are_checked_in_a_copy_without_a_gateway(machine: Machine) -> None:
    machine.setup("-Enrollment ''", input="\n\n")

    # The first check, before anything changes, reads a copy; a leftover AGENT_BRIDGE_STATE_FILE
    # in the session (set for every run here) never reaches agent-bridge.
    calls = machine.calls()
    assert calls.index("agent-bridge check") < calls.index(r"Register-ScheduledTask \agent-bridge")


def test_the_settings_check_checks_the_kept_token_on_this_host(machine: Machine) -> None:
    machine.setup("-Enrollment ''", input="\n\n")

    machine.setup()

    # The address is this host's, so the copy is checked as it is, its token included.
    tokens = [call for call in machine.calls() if call.startswith("token ")]
    assert tokens == ["token from the env file", "token from the env file"]


def test_agent_bridge_variables_in_the_user_environment_stop_setup(machine: Machine) -> None:
    output = machine.setup("-Enrollment ''", input="\n\n", expect=1, FAKE_USER_VARIABLE="AGENT_BRIDGE_HOST")

    assert "AGENT_BRIDGE_HOST set in your user environment would win over" in output
    assert "[Environment]::SetEnvironmentVariable('AGENT_BRIDGE_HOST', $null, 'User')" in output
    assert not machine.env_file.exists()


def test_an_address_this_host_lacks_is_checked_without_sending_the_token_there(machine: Machine) -> None:
    machine.setup("-Address '192.168.77.5'", "-Enrollment ''", input="\n\n")

    # Two checks of the copy: the real token against 127.0.0.1, the address with a throwaway.
    tokens = [call for call in machine.calls() if call.startswith("token ")]
    token = machine.env()["AGENT_BRIDGE_TOKEN"]
    assert tokens[0] == "token from the env file"
    assert tokens[1] not in ("token from the env file", f"token {token}")


def test_a_bridge_that_does_not_start_is_not_enrolled(machine: Machine) -> None:
    output = machine.setup(f"-Enrollment '{STRING}'", input="\n\n", expect=1, FAKE_NO_START="1")

    assert "Not enrolling now" in output
    assert not any(call.startswith("agent-bridge enroll") for call in machine.calls())


def test_an_aborted_setup_leaves_the_old_firewall_rule(machine: Machine) -> None:
    machine.setup("-Enrollment ''", input="\n\n")
    # The env file can't be written, after the bridge was stopped.
    (machine.appdata / "agent-bridge" / "env.tmp").mkdir()

    machine.setup("-Port 9000", expect=1)

    assert machine.fake()["rule"] == {"port": 8765, "profile": "Private"}


def test_references_in_the_env_file_are_read_as_agent_bridge_reads_them(machine: Machine) -> None:
    machine.setup("-Enrollment ''", input="\n\n")
    text = machine.env_file.read_text(encoding="utf-8")
    text = text.replace(f"AGENT_BRIDGE_ALLOWED_ROOTS={machine.profile / 'Projects'}", r"AGENT_BRIDGE_ALLOWED_ROOTS=${USERPROFILE}\Projects")
    machine.env_file.write_text(text, encoding="utf-8")

    machine.setup()

    assert machine.env()["AGENT_BRIDGE_ALLOWED_ROOTS"] == str(machine.profile / "Projects")


def test_the_settings_copy_lives_in_the_state_folder_and_uninstall_removes_a_leftover(machine: Machine) -> None:
    machine.setup("-Enrollment ''", input="\n\n")
    leftover = machine.localappdata / "agent-bridge" / "setup-check"
    leftover.mkdir()
    (leftover / "env").write_text("AGENT_BRIDGE_TOKEN=secret\n")

    machine.run("uninstall.ps1")

    assert not (machine.localappdata / "agent-bridge").exists()


def test_a_free_port_is_found_below_a_busy_one_near_the_top(machine: Machine) -> None:
    machine.setup("-Enrollment ''", input="\n\n")
    text = machine.env_file.read_text(encoding="utf-8").replace("AGENT_BRIDGE_PORT=8765", "AGENT_BRIDGE_PORT=65535")
    machine.env_file.write_text(text, encoding="utf-8")
    machine.listen(65535, 77, "other.exe", "other.exe")

    machine.setup(input="\n", FAKE_EXCLUDED_PORTS="1024-1030")

    # Past 65535 the search goes on from 1024, skipping what Windows keeps.
    assert machine.env()["AGENT_BRIDGE_PORT"] == "1031"


def test_a_value_with_a_dotenv_reference_is_refused(machine: Machine) -> None:
    folder = machine.profile / "${client}"
    output = machine.setup(f"-Roots '{folder}'", "-Enrollment ''", input="\n\n", expect=1)

    assert "can't hold ${" in output
    assert not machine.env_file.exists()


def test_printed_commands_run_agent_bridge_in_utf8_mode(machine: Machine) -> None:
    output = machine.setup("-Enrollment ''", input="\n\n")

    assert "$env:PYTHONUTF8 = '1'; & " in output
