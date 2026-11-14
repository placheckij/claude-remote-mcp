# What setup.ps1 and uninstall.ps1 share: where things are, finding agent-bridge, running
# it, and stopping the running bridge. Dot-sourced; not meant to be run on its own.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Before anything reads %APPDATA%, which only Windows has.
if ($env:OS -ne 'Windows_NT') {
    Write-Host 'error: these scripts are for Windows; see agent-bridge/deploy for the others' -ForegroundColor Red
    exit 1
}

$script:TaskName = 'agent-bridge'
$script:RuleName = 'agent-bridge'
# As agent_bridge/state.py has them on Windows.
$script:SettingsDir = Join-Path $env:APPDATA 'agent-bridge'
$script:EnvFile = Join-Path $script:SettingsDir 'env'
$script:StateDir = Join-Path $env:LOCALAPPDATA 'agent-bridge'
$script:StateFile = Join-Path $script:StateDir 'state.json'
$script:LogFile = Join-Path $script:StateDir 'agent-bridge.log'
# What the task runs, from run.cmd in this folder.
$script:RunFile = Join-Path $script:StateDir 'run.cmd'
# Set while Stop-Bridge has the task disabled, so leaving early enables it again.
$script:TaskDisabled = $false
# As run.cmd does for the service: agent-bridge reads the env file in the locale's code
# page otherwise, and setup writes it as UTF-8.
$env:PYTHONUTF8 = '1'

function Restore-Task {
    <# Enables the task again if Stop-Bridge disabled it: a setup that stops halfway must not
       leave the bridge off at the next logon. #>
    if ($script:TaskDisabled) {
        Enable-ScheduledTask -TaskName $script:TaskName -TaskPath '\' -ErrorAction SilentlyContinue | Out-Null
        # Stop-Bridge ended the bridge too, and enabling fires no trigger.
        Start-ScheduledTask -TaskName $script:TaskName -TaskPath '\' -ErrorAction SilentlyContinue
        $script:TaskDisabled = $false
    }
}

function Stop-Setup([string]$Message) {
    Write-Host "error: $Message" -ForegroundColor Red
    Restore-Task
    exit 1
}

function Write-Step([string]$Message) {
    Write-Host ''
    Write-Host "==> $Message"
}

function Write-Warn([string]$Message) {
    Write-Host "warning: $Message" -ForegroundColor Yellow
}

function Assert-RunAsYou([string]$Script) {
    <# Refuses another account than the one whose desktop this session is, e.g. an
       administrator's typed in at a UAC prompt: the task and the settings would be that
       account's, away from herdr. Works the same over Remote Desktop. #>
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $session = (Get-Process -Id $PID).SessionId
    $explorer = @(Get-CimInstance Win32_Process -Filter "Name='explorer.exe' AND SessionId=$session" -ErrorAction SilentlyContinue)
    if ($explorer.Count -eq 0) { return }
    $owner = Invoke-CimMethod -InputObject $explorer[0] -MethodName GetOwner -ErrorAction SilentlyContinue
    if (-not $owner -or -not $owner.User) { return }
    $desktop = "$($owner.Domain)\$($owner.User)"
    if ($desktop -ne $me) {
        Stop-Setup ("run $Script as $desktop, whose desktop this is, not as ${me}: agent-bridge must run " +
            "next to your herdr server. Open PowerShell normally; it asks for admin rights only for the firewall rule.")
    }
}

function Get-InstalledExe {
    <# The agent-bridge.exe the installed run.cmd runs, or ''. #>
    if (-not (Test-Path -LiteralPath $script:RunFile)) { return '' }
    foreach ($line in @(Get-Content -LiteralPath $script:RunFile -Encoding UTF8)) {
        if ($line -match '^"(.+)" serve\b') { return $Matches[1] }
    }
    return ''
}

function Find-Bridge([string]$Given, [switch]$Optional, [switch]$PreferInstalled) {
    <# agent-bridge.exe: the one given; the one a package ships beside this folder, or this
       checkout's .venv (Checkout is then the repo, which needs `uv sync`); the installed one
       (which run.cmd names); else the one on PATH. So setup run from a newer package or
       checkout moves the task to it. -PreferInstalled (for uninstall) takes the installed
       one first. Not found, it stops setup, or with -Optional returns $null. #>
    if ($Given) {
        if (Test-Path -LiteralPath $Given -PathType Leaf) { return @{ Exe = (Resolve-Path -LiteralPath $Given).Path; Checkout = $null } }
        if ($Optional) { return $null }
        Stop-Setup "-Bridge: $Given doesn't exist"
    }
    $installed = Get-InstalledExe
    $installedFound = if ($installed -and (Test-Path -LiteralPath $installed -PathType Leaf)) { @{ Exe = $installed; Checkout = $null } } else { $null }
    if ($PreferInstalled -and $installedFound) { return $installedFound }
    $beside = Join-Path $PSScriptRoot 'agent-bridge.exe'
    if (Test-Path -LiteralPath $beside -PathType Leaf) { return @{ Exe = $beside; Checkout = $null } }
    $repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
    if (Test-Path -LiteralPath (Join-Path $repo 'agent-bridge\pyproject.toml')) {
        return @{ Exe = (Join-Path $repo '.venv\Scripts\agent-bridge.exe'); Checkout = $repo }
    }
    if ($installedFound) { return $installedFound }
    $onPath = Get-Command agent-bridge.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($onPath) { return @{ Exe = $onPath.Source; Checkout = $null } }
    if ($Optional) { return $null }
    Stop-Setup 'agent-bridge not found: run this from a checkout of claude-remote-mcp, or pass -Bridge <path to agent-bridge.exe>'
}

$script:NativeOutput = @()

function Invoke-Native {
    <# Runs a program and returns its exit code. What it printed, stdout and stderr, is kept in
       $script:NativeOutput as plain lines (Windows PowerShell would otherwise turn each stderr
       line into an error), and shown unless -Quiet. #>
    param([Parameter(Mandatory)][string]$FilePath, [string[]]$ArgumentList = @(), [switch]$Quiet)
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    # agent-bridge writes UTF-8 (PYTHONUTF8 above); Windows PowerShell decodes what a program
    # prints with the console's code page.
    $savedEncoding = $null
    try { $savedEncoding = [Console]::OutputEncoding; [Console]::OutputEncoding = New-Object Text.UTF8Encoding $false } catch { $savedEncoding = $null }
    try {
        $script:NativeOutput = @(& $FilePath @ArgumentList 2>&1 | ForEach-Object {
                $text = if ($_ -is [Management.Automation.ErrorRecord]) { $_.Exception.Message } else { [string]$_ }
                if (-not $Quiet) { Write-Host $text }
                $text
            })
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $saved
        if ($savedEncoding) { try { [Console]::OutputEncoding = $savedEncoding } catch { $savedEncoding = $null } }
    }
}

function Get-InstalledPort {
    <# The port the env file sets, else agent-bridge's default. #>
    $port = (Read-EnvFile $script:EnvFile).Values['AGENT_BRIDGE_PORT']
    if ($port -match '^\d{1,5}$') { return [int]$port }
    return 8765
}

function Format-Quoted([string]$Text) {
    <# $Text in single quotes as PowerShell reads them, for commands printed to copy. #>
    return "'" + $Text.Replace("'", "''") + "'"
}

function Test-IsBridge([string]$CommandLine) {
    <# Whether a command line is agent-bridge's service, wherever it is installed: a checkout's
       .venv, a package's folder or shim, `python -m agent_bridge.cli serve`, or uvicorn with
       agent_bridge.app:main as installed before. Other mentions of agent_bridge don't count. #>
    if (-not $CommandLine) { return $false }
    return ($CommandLine -match '(?i)agent-bridge(\.(exe|cmd|bat))?"?\s+serve\b') -or
        ($CommandLine -match '(?i)-m\s+agent_bridge(\.cli)?\s+serve\b') -or
        ($CommandLine -match '(?i)\bagent_bridge\.app:main\b')
}

function Get-PortHolder([int]$Port) {
    <# A process listening on TCP $Port on any address, as @{ Id; Name; CommandLine; Path }, or
       $null. With several, one that isn't agent-bridge comes first, so a port that agent-bridge
       shares with another program doesn't count as agent-bridge's. #>
    $ids = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
    $holders = foreach ($id in $ids) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
        # The command line is empty for another user's process, or an elevated one: then it
        # isn't ours either.
        if ($process) {
            @{ Id = $id; Name = [string]$process.Name; CommandLine = [string]$process.CommandLine; Path = [string]$process.ExecutablePath }
        } else {
            @{ Id = $id; Name = '?'; CommandLine = ''; Path = '' }
        }
    }
    $holders = @($holders)
    if ($holders.Count -eq 0) { return $null }
    $other = @($holders | Where-Object { -not (Test-IsBridge $_.CommandLine) })
    if ($other.Count -gt 0) { return $other[0] }
    return $holders[0]
}

function Get-ExcludedPortRange {
    <# The TCP port ranges Windows keeps for itself (Hyper-V, WSL and Docker reserve them),
       where nothing can listen although nothing does, as objects with First and Last. #>
    foreach ($line in @(netsh int ipv4 show excludedportrange protocol=tcp)) {
        if ($line -match '^\s*(\d+)\s+(\d+)') { [pscustomobject]@{ First = [int]$Matches[1]; Last = [int]$Matches[2] } }
    }
}

function Test-PortExcluded([int]$Port, [object[]]$Ranges) {
    foreach ($range in $Ranges) {
        if ($range -and $Port -ge $range.First -and $Port -le $range.Last) { return $true }
    }
    return $false
}

function Stop-Bridge([string]$Exe, [int]$Port) {
    <# Stops the task and the bridge it runs, and any agent-bridge still holding $Port. The task
       is left disabled, or its one-minute repeat could start the old bridge again meanwhile;
       registering it again, Start-Bridge or Stop-Setup enable it again. #>
    $task = Get-ScheduledTask -TaskName $script:TaskName -TaskPath '\' -ErrorAction SilentlyContinue
    if ($task) {
        Disable-ScheduledTask -TaskName $script:TaskName -TaskPath '\' | Out-Null
        $script:TaskDisabled = $true
        Stop-ScheduledTask -TaskName $script:TaskName -TaskPath '\'
    }
    # Ending the task ends only the conhost.exe it started; the cmd.exe running run.cmd, the
    # agent-bridge.exe launcher and python.exe behind it keep running, so they are ended here
    # by their command lines: run.cmd's, and the ones that serve with this $Exe or the
    # installed one.
    $exes = @(@($Exe, (Get-InstalledExe)) | Where-Object { $_ })
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $line = [string]$_.CommandLine
        # cmd.exe only: an editor with run.cmd open names it too.
        $line -and (($_.Name -eq 'cmd.exe' -and $line.IndexOf($script:RunFile, [StringComparison]::OrdinalIgnoreCase) -ge 0) -or
            ((Test-IsBridge $line) -and @($exes | Where-Object { $line.IndexOf($_, [StringComparison]::OrdinalIgnoreCase) -ge 0 }).Count -gt 0))
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    if ($Port -le 0) { return }
    $holder = Get-PortHolder $Port
    if ($holder -and (Test-IsBridge $holder.CommandLine)) {
        Write-Host "Stopping the agent-bridge on port $Port (PID $($holder.Id)): $($holder.CommandLine)"
        Stop-Process -Id $holder.Id -Force -ErrorAction SilentlyContinue
    }
    # Only an agent-bridge is worth waiting for: nothing here stops another program.
    for ($i = 0; $i -lt 20; $i++) {
        $holder = Get-PortHolder $Port
        if (-not $holder -or -not (Test-IsBridge $holder.CommandLine)) { break }
        Start-Sleep -Milliseconds 500
    }
}

$script:KeyLine = '^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$'

function ConvertFrom-EnvValue([string]$Raw) {
    <# A value as python-dotenv reads it: in '...' with \\ and \' as escapes, in "..." with
       those, \" and \a \b \f \n \r \t \v, either maybe followed by a comment; or plain up to a
       ` #` comment. #>
    $value = $Raw.Trim()
    if ($value -match "^'((?:\\'|[^'])*)'\s*(#.*)?$") { return $Matches[1] -replace "\\(['\\])", '$1' }
    if ($value -match '^"((?:\\"|[^"])*)"\s*(#.*)?$') {
        return [regex]::Replace($Matches[1], '\\([\\''"abfnrtv])', {
                param($escape)
                switch -CaseSensitive ($escape.Groups[1].Value) {
                    'a' { [string][char]7 } 'b' { [string][char]8 } 'f' { [string][char]12 }
                    'n' { "`n" } 'r' { "`r" } 't' { "`t" } 'v' { [string][char]11 }
                    default { $escape.Groups[1].Value }
                }
            })
    }
    return ($value -replace '\s+#.*$', '')
}

function Read-EnvFile([string]$Path) {
    <# The env file's lines, and its values by key, as python-dotenv reads them. #>
    $values = [ordered]@{}
    $lines = @()
    if (Test-Path -LiteralPath $Path) { $lines = @(Get-Content -LiteralPath $Path -Encoding UTF8) }
    foreach ($line in $lines) {
        if ($line -match $script:KeyLine) { $values[$Matches[1].ToUpperInvariant()] = ConvertFrom-EnvValue $Matches[2] }
    }
    return @{ Lines = $lines; Values = $values }
}

function Read-State {
    <# The state file's contents, or $null before there are any. #>
    if (-not (Test-Path -LiteralPath $script:StateFile)) { return $null }
    try {
        return Get-Content -LiteralPath $script:StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Get-EnrolledGateway {
    <# The gateway the state file says this bridge enrolled with, or $null. #>
    $state = Read-State
    if ($state -and ($state.PSObject.Properties.Name -contains 'gateway') -and $state.gateway) { return $state.gateway }
    return $null
}

function Invoke-Elevated([string[]]$Commands) {
    <# Runs each command in an elevated, hidden Windows PowerShell (one UAC prompt for all).
       Returns which failed, as bit i set for $Commands[i] (0: all worked), or -1 when the UAC
       prompt was declined. The callers print what to run by hand. #>
    $steps = for ($i = 0; $i -lt $Commands.Count; $i++) { "try { $($Commands[$i]) } catch { `$failed += $([int][Math]::Pow(2, $i)) }" }
    $wrapped = "`$ErrorActionPreference = 'Stop'; `$failed = 0; $($steps -join '; '); exit `$failed"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($wrapped))
    try {
        $process = Start-Process -FilePath powershell.exe -Verb RunAs -Wait -PassThru -WindowStyle Hidden `
            -ArgumentList '-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded
    } catch {
        return -1
    }
    return $process.ExitCode
}
