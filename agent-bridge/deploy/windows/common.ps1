# What setup.ps1 and uninstall.ps1 share: where things are, finding agent-bridge, running
# it, and stopping the running bridge. Dot-sourced; not meant to be run on its own.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$TaskName = 'agent-bridge'
$RuleName = 'agent-bridge'
# As agent_bridge/state.py has them on Windows.
$SettingsDir = Join-Path $env:APPDATA 'agent-bridge'
$EnvFile = Join-Path $SettingsDir 'env'
$StateDir = Join-Path $env:LOCALAPPDATA 'agent-bridge'
$StateFile = Join-Path $StateDir 'state.json'
$LogFile = Join-Path $StateDir 'agent-bridge.log'

function Stop-Setup([string]$Message) {
    Write-Host "error: $Message" -ForegroundColor Red
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
    <# Refuses another account than the one logged on here, e.g. an administrator's typed in
       at a UAC prompt: the task and the settings would be that account's, away from herdr. #>
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $console = [string](Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue).UserName
    if ($console -and $console -ne $me) {
        Stop-Setup ("run $Script as $console, who is logged on here, not as ${me}: agent-bridge must run " +
            "next to your herdr server. Open PowerShell normally; it asks for admin rights only for the firewall rule.")
    }
}

function Find-Bridge([string]$Given, [switch]$Optional) {
    <# agent-bridge.exe: the one given, the one a package ships beside this folder, or the
       checkout's .venv (Checkout is then the repo, which needs `uv sync`), else on PATH.
       Not found, it stops setup, or with -Optional returns $null. #>
    if ($Given) {
        if (Test-Path -LiteralPath $Given -PathType Leaf) { return @{ Exe = (Resolve-Path -LiteralPath $Given).Path; Checkout = $null } }
        if ($Optional) { return $null }
        Stop-Setup "-Bridge: $Given doesn't exist"
    }
    $beside = Join-Path $PSScriptRoot 'agent-bridge.exe'
    if (Test-Path -LiteralPath $beside -PathType Leaf) { return @{ Exe = $beside; Checkout = $null } }
    $repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
    if (Test-Path -LiteralPath (Join-Path $repo 'agent-bridge\pyproject.toml')) {
        return @{ Exe = (Join-Path $repo '.venv\Scripts\agent-bridge.exe'); Checkout = $repo }
    }
    $onPath = Get-Command agent-bridge.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($onPath) { return @{ Exe = $onPath.Source; Checkout = $null } }
    if ($Optional) { return $null }
    Stop-Setup 'agent-bridge not found: run this from a checkout of claude-remote-mcp, or pass -Bridge <path to agent-bridge.exe>'
}

function Invoke-Native {
    <# Runs a program, shows everything it prints, and returns its exit code. Its stderr is
       shown as plain lines: Windows PowerShell would otherwise turn each one into an error. #>
    param([Parameter(Mandatory)][string]$FilePath, [string[]]$ArgumentList = @())
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $FilePath @ArgumentList 2>&1 | ForEach-Object {
            if ($_ -is [Management.Automation.ErrorRecord]) { Write-Host $_.Exception.Message } else { Write-Host $_ }
        }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $saved
    }
}

function Test-IsBridge([string]$CommandLine) {
    <# Whether a command line is agent-bridge's service, wherever it is installed: a checkout's
       .venv, a package's folder or shim, or uvicorn with agent_bridge.app:main as installed before. #>
    if (-not $CommandLine) { return $false }
    return ($CommandLine -match '(?i)agent-bridge(\.(exe|cmd|bat))?"?\s+serve\b') -or ($CommandLine -match '(?i)\bagent_bridge\b')
}

function Get-PortHolder([int]$Port) {
    <# The process listening on TCP $Port on any address, as @{ Id; Name; CommandLine; Path },
       or $null. (Get-NetTCPConnection is on every Windows setup.ps1 runs on.) #>
    $ids = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
    if ($ids.Count -eq 0) { return $null }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($ids[0])" -ErrorAction SilentlyContinue
    if (-not $process) { return @{ Id = $ids[0]; Name = '?'; CommandLine = ''; Path = '' } }
    # Empty for another user's process, or an elevated one: then it isn't ours either.
    return @{ Id = $ids[0]; Name = $process.Name; CommandLine = [string]$process.CommandLine; Path = [string]$process.ExecutablePath }
}

function Stop-Bridge([string]$Exe, [int]$Port) {
    <# Stops the task and the bridge it runs, and any agent-bridge still holding $Port. The task
       is left disabled, or its one-minute repeat could start the old bridge again meanwhile;
       registering it again enables it. #>
    $task = Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue
    if ($task) {
        Disable-ScheduledTask -TaskName $TaskName -TaskPath '\' | Out-Null
        Stop-ScheduledTask -TaskName $TaskName -TaskPath '\'
    }
    # Ending the task ends only the conhost.exe it started; cmd.exe and the bridge behind it
    # keep running, so they are ended here by their command lines: ones that run this $Exe.
    if ($Exe) {
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            (Test-IsBridge $_.CommandLine) -and ([string]$_.CommandLine).IndexOf($Exe, [StringComparison]::OrdinalIgnoreCase) -ge 0
        } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }
    if ($Port -le 0) { return }
    $holder = Get-PortHolder $Port
    if ($holder -and (Test-IsBridge $holder.CommandLine)) {
        Write-Host "Stopping the agent-bridge on port $Port (PID $($holder.Id)): $($holder.CommandLine)"
        Stop-Process -Id $holder.Id -Force -ErrorAction SilentlyContinue
    }
    for ($i = 0; $i -lt 20 -and (Get-PortHolder $Port); $i++) { Start-Sleep -Milliseconds 500 }
}

$KeyLine = '^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$'

function ConvertFrom-EnvValue([string]$Raw) {
    <# A value as python-dotenv reads it: quoted, with \\ and \' (and \" in "...") as escapes,
       or plain up to a ` #` comment. #>
    $value = $Raw.Trim()
    if ($value -match "^'(.*)'$") { return $Matches[1] -replace "\\(['\\])", '$1' }
    if ($value -match '^"(.*)"$') { return $Matches[1] -replace "\\([`"'\\])", '$1' }
    return ($value -replace '\s+#.*$', '')
}

function Read-EnvFile([string]$Path) {
    <# The env file's lines, and its values by key, as python-dotenv reads them. #>
    $values = [ordered]@{}
    $lines = @()
    if (Test-Path -LiteralPath $Path) { $lines = @(Get-Content -LiteralPath $Path -Encoding UTF8) }
    foreach ($line in $lines) {
        if ($line -match $KeyLine) { $values[$Matches[1].ToUpperInvariant()] = ConvertFrom-EnvValue $Matches[2] }
    }
    return @{ Lines = $lines; Values = $values }
}

function Read-State {
    <# The state file's contents, or $null before there are any. #>
    if (-not (Test-Path -LiteralPath $StateFile)) { return $null }
    try {
        return Get-Content -LiteralPath $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
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

function Invoke-Elevated([string]$Command) {
    <# Runs $Command in an elevated, hidden Windows PowerShell (one UAC prompt); $true if it
       succeeded. The callers print the command to run by hand when it didn't. #>
    $wrapped = "`$ErrorActionPreference = 'Stop'; try { $Command; exit 0 } catch { exit 1 }"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($wrapped))
    try {
        $process = Start-Process -FilePath powershell.exe -Verb RunAs -Wait -PassThru -WindowStyle Hidden `
            -ArgumentList '-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded
    } catch {
        # Declined at the UAC prompt.
        return $false
    }
    return $process.ExitCode -eq 0
}
