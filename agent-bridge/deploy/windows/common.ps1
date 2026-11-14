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
# Where setup checks a copy of the settings; uninstall removes what a killed setup left.
$script:CheckDir = Join-Path $script:StateDir 'setup-check'
# There while setup has the task disabled: a setup killed meanwhile leaves it, and the next
# one then knows the task was setup's to bring back, not one you turned off.
$script:DisabledMarker = Join-Path $script:StateDir 'task-disabled-by-setup'
# Set while Stop-Bridge has the task disabled, so leaving early enables it again.
$script:TaskDisabled = $false
# Where your lasting environment variables and the machine's are kept: what the scheduled
# task gets, unlike a variable set only in the window setup runs in.
$script:LastingEnvironment = [ordered]@{
    User    = 'HKCU:\Environment'
    Machine = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
}

function Resume-Task {
    <# Enables the task and registers it again as it is, which fires its registration
       trigger: that starts the bridge and the every-minute repeat. Enabling alone starts
       nothing, and the repeat a disabled task had is gone. #>
    Enable-ScheduledTask -TaskName $script:TaskName -TaskPath '\' | Out-Null
    $xml = Export-ScheduledTask -TaskName $script:TaskName -TaskPath '\'
    Register-ScheduledTask -TaskName $script:TaskName -TaskPath '\' -Xml $xml -Force | Out-Null
    $script:TaskDisabled = $false
    Remove-Item -LiteralPath $script:DisabledMarker -Force -ErrorAction SilentlyContinue
}

function Restore-Task {
    <# Brings the task back if Stop-Bridge disabled it: a setup that stops halfway must not
       leave the bridge off. #>
    if ($script:TaskDisabled) {
        # Tried once: Stop-Setup and setup's finally both come here.
        $script:TaskDisabled = $false
        try { Resume-Task } catch { Write-Warn "couldn't start the agent-bridge task again ($($_.Exception.Message)); run setup again" }
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
    if ($owner -and $owner.User) {
        $desktop = "$($owner.Domain)\$($owner.User)"
    } else {
        # Unreadable: another account can't read it, but WMI also fails now and then. The
        # console's user, when there is one, tells them apart.
        $console = [string](Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue).UserName
        if (-not $console) {
            Write-Warn "couldn't tell who owns this desktop; make sure you run $Script as yourself, not another account"
            return
        }
        $desktop = $console
    }
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
        # Uninstall looks on for the one installed now; setup stops.
        if (-not $Optional) { Stop-Setup "-Bridge: $Given doesn't exist" }
    }
    $installed = Get-InstalledExe
    $installedFound = if ($installed -and (Test-Path -LiteralPath $installed -PathType Leaf)) { @{ Exe = $installed; Checkout = $null } } else { $null }
    if ($PreferInstalled -and $installedFound) { return $installedFound }
    $beside = Join-Path $PSScriptRoot 'agent-bridge.exe'
    if (Test-Path -LiteralPath $beside -PathType Leaf) { return @{ Exe = $beside; Checkout = $null } }
    $repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
    $checkoutExe = Join-Path $repo '.venv\Scripts\agent-bridge.exe'
    # Setup syncs a checkout whose .venv isn't there yet; uninstall looks further instead.
    if ((Test-Path -LiteralPath (Join-Path $repo 'agent-bridge\pyproject.toml')) -and
        (-not $Optional -or (Test-Path -LiteralPath $checkoutExe -PathType Leaf))) {
        return @{ Exe = $checkoutExe; Checkout = $repo }
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
       line into an error), and shown unless -Quiet. It runs with none of the session's
       AGENT_BRIDGE_* variables, which would win over the env file, plus $Environment's; and
       from the temp folder, away from a .env in the current one, which agent-bridge reads too. #>
    param([Parameter(Mandatory)][string]$FilePath, [string[]]$ArgumentList = @(), [switch]$Quiet,
        [hashtable]$Environment = @{})
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    # Saved before anything changes; everything is put back in finally, however this ends.
    $savedVariables = @(Get-ChildItem Env: | Where-Object { $_.Name -like 'AGENT_BRIDGE_*' })
    $savedUtf8 = $env:PYTHONUTF8
    $savedEncoding = $null
    $moved = $false
    try {
        foreach ($variable in $savedVariables) { Remove-Item -LiteralPath "Env:$($variable.Name)" }
        foreach ($name in $Environment.Keys) { Set-Item -LiteralPath "Env:$name" -Value $Environment[$name] }
        Push-Location -LiteralPath ([IO.Path]::GetTempPath())
        $moved = $true
        # In UTF-8 mode, as run.cmd runs the service: agent-bridge reads the env file, which
        # setup writes as UTF-8, in the locale's code page otherwise.
        $env:PYTHONUTF8 = '1'
        # It then writes UTF-8, and Windows PowerShell decodes what a program prints with the
        # console's code page.
        try { $savedEncoding = [Console]::OutputEncoding; [Console]::OutputEncoding = New-Object Text.UTF8Encoding $false } catch { $savedEncoding = $null }
        $script:NativeOutput = @(& $FilePath @ArgumentList 2>&1 | ForEach-Object {
                $text = if ($_ -is [Management.Automation.ErrorRecord]) { $_.Exception.Message } else { [string]$_ }
                if (-not $Quiet) { Write-Host $text }
                $text
            })
        return $LASTEXITCODE
    } catch {
        # Blocked from starting (AppLocker, Smart App Control) or not there.
        $script:NativeOutput = @($_.Exception.Message)
        if (-not $Quiet) { Write-Host $_.Exception.Message }
        return -1
    } finally {
        $ErrorActionPreference = $saved
        $env:PYTHONUTF8 = $savedUtf8
        if ($moved) { Pop-Location }
        foreach ($name in $Environment.Keys) { Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue }
        foreach ($variable in $savedVariables) { Set-Item -LiteralPath "Env:$($variable.Name)" -Value $variable.Value }
        if ($savedEncoding) { try { [Console]::OutputEncoding = $savedEncoding } catch { $savedEncoding = $null } }
    }
}

function ConvertTo-Port([string]$Value) {
    <# $Value as a TCP port, or 0 when it isn't one. #>
    if ($Value -match '^\d{1,5}$' -and [int]$Value -ge 1 -and [int]$Value -le 65535) { return [int]$Value }
    return 0
}

function Get-InstalledPort {
    <# The port the env file sets, else agent-bridge's default; 0 without an env file, when
       no port is ours and an agent-bridge on 8765 may be anyone's. #>
    if (-not (Test-Path -LiteralPath $script:EnvFile)) { return 0 }
    $port = ConvertTo-Port ([string](Read-EnvFile $script:EnvFile).Values['AGENT_BRIDGE_PORT'])
    if ($port) { return $port }
    return 8765
}

function Format-Quoted([string]$Text) {
    <# $Text in single quotes as PowerShell reads them, for commands printed to copy. #>
    return "'" + $Text.Replace("'", "''") + "'"
}

# The agent-bridge.exe paths this run knows (given with -Bridge, or installed), whatever
# they are named; setup.ps1 and uninstall.ps1 set them.
$script:BridgeExes = @()

function Test-IsBridge([string]$CommandLine) {
    <# Whether a command line is agent-bridge's service, wherever it is installed: a checkout's
       .venv, a package's folder or shim, one of $script:BridgeExes, `python -m
       agent_bridge.cli serve`, or uvicorn with agent_bridge.app:main as installed before.
       Other mentions of agent_bridge don't count. #>
    if (-not $CommandLine) { return $false }
    foreach ($known in $script:BridgeExes) {
        if ($known -and $CommandLine.IndexOf($known, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and $CommandLine -match '(?i)"?\s+serve\b') { return $true }
    }
    return ($CommandLine -match '(?i)agent-bridge(\.(exe|cmd|bat))?"?\s+serve\b') -or
        ($CommandLine -match '(?i)-m\s+agent_bridge(\.cli)?\s+serve\b') -or
        ($CommandLine -match '(?i)\bagent_bridge\.app:main\b')
}

function Get-PortListener([int]$Port) {
    <# Every process listening on TCP $Port on any address, as @{ Id; Name; CommandLine; Path }. #>
    $ids = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($id in $ids) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
        # The command line is empty for another user's process, or an elevated one: then it
        # isn't ours either.
        if ($process) {
            @{ Id = $id; Name = [string]$process.Name; CommandLine = [string]$process.CommandLine; Path = [string]$process.ExecutablePath }
        } else {
            @{ Id = $id; Name = '?'; CommandLine = ''; Path = '' }
        }
    }
}

function Get-PortHolder([int]$Port) {
    <# A process listening on TCP $Port, or $null. With several, one that isn't agent-bridge
       comes first, so a port that agent-bridge shares with another program doesn't count as
       agent-bridge's. #>
    $holders = @(Get-PortListener $Port)
    if ($holders.Count -eq 0) { return $null }
    $other = @($holders | Where-Object { -not (Test-IsBridge $_.CommandLine) })
    if ($other.Count -gt 0) { return $other[0] }
    return $holders[0]
}

function Get-BridgeListener([int]$Port) {
    <# The agent-bridge listening on $Port, even beside another program on another address, or
       $null. #>
    return @(Get-PortListener $Port | Where-Object { Test-IsBridge $_.CommandLine }) | Select-Object -First 1
}

function Get-ExcludedPortRange {
    <# The TCP port ranges Windows keeps for itself (Hyper-V, WSL and Docker reserve them),
       where nothing can listen although nothing does, as objects with First and Last. #>
    # IPv4's and IPv6's: the address may be either.
    foreach ($line in @(netsh int ipv4 show excludedportrange protocol=tcp) + @(netsh int ipv6 show excludedportrange protocol=tcp)) {
        if ($line -match '^\s*(\d+)\s+(\d+)') { [pscustomobject]@{ First = [int]$Matches[1]; Last = [int]$Matches[2] } }
    }
}

function Test-PortExcluded([int]$Port, [object[]]$Ranges) {
    foreach ($range in $Ranges) {
        if ($range -and $Port -ge $range.First -and $Port -le $range.Last) { return $true }
    }
    return $false
}

function Stop-Bridge([string]$Exe, [int[]]$Port) {
    <# Stops the task and the bridge it runs, and any agent-bridge still holding a port in $Port
       (0 for none). The task
       is left disabled, or its one-minute repeat could start the old bridge again meanwhile;
       registering it again, Start-Bridge or Stop-Setup enable it again. #>
    $task = Get-ScheduledTask -TaskName $script:TaskName -TaskPath '\' -ErrorAction SilentlyContinue
    if ($task) {
        Disable-ScheduledTask -TaskName $script:TaskName -TaskPath '\' | Out-Null
        # Brought back by a setup that stops early only if it was on, or setup had turned it
        # off before: one you disabled stays off then. (A setup that finishes starts it, as
        # running setup is for.)
        if ([string]$task.State -ne 'Disabled' -or (Test-Path -LiteralPath $script:DisabledMarker)) {
            $script:TaskDisabled = $true
            if (Test-Path -LiteralPath $script:StateDir) { Set-Content -LiteralPath $script:DisabledMarker -Value 'setup' }
        }
        Stop-ScheduledTask -TaskName $script:TaskName -TaskPath '\'
    }
    # Ending the task ends only the conhost.exe it started; the cmd.exe running run.cmd, the
    # agent-bridge.exe launcher and python.exe behind it keep running, so they are ended here
    # by their command lines: run.cmd's, and the ones that serve with this $Exe or the
    # installed one.
    $exes = @(@($Exe) + $script:BridgeExes | Where-Object { $_ } | Select-Object -Unique)
    # Narrowed by WMI first; the matching below decides.
    $candidates = "Name='cmd.exe' OR CommandLine LIKE '%serve%' OR CommandLine LIKE '%agent_bridge%'"
    $stopped = @(Get-CimInstance Win32_Process -Filter $candidates -ErrorAction SilentlyContinue | Where-Object {
        $line = [string]$_.CommandLine
        # cmd.exe only: an editor with run.cmd open names it too.
        $line -and (($_.Name -eq 'cmd.exe' -and $line.IndexOf($script:RunFile, [StringComparison]::OrdinalIgnoreCase) -ge 0) -or
            ((Test-IsBridge $line) -and @($exes | Where-Object { $line.IndexOf($_, [StringComparison]::OrdinalIgnoreCase) -ge 0 }).Count -gt 0))
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId })
    # Until they are gone and have let go of the .venv's files and the log.
    if ($stopped.Count -gt 0) { Wait-Process -Id $stopped -Timeout 10 -ErrorAction SilentlyContinue }
    foreach ($one in @($Port | Where-Object { $_ -gt 0 } | Select-Object -Unique)) {
        # Even beside another program on the port, on another address.
        $holder = Get-BridgeListener $one
        if ($holder) {
            Write-Host "Stopping the agent-bridge on port $one (PID $($holder.Id)): $($holder.CommandLine)"
            Stop-Process -Id $holder.Id -Force -ErrorAction SilentlyContinue
        }
        # Only an agent-bridge is worth waiting for: nothing here stops another program.
        for ($i = 0; $i -lt 20; $i++) {
            if (-not (Get-BridgeListener $one)) { break }
            Start-Sleep -Milliseconds 500
        }
    }
}

$script:KeyLine = '^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$'

function ConvertFrom-EnvValue([string]$Raw) {
    <# A value as python-dotenv reads it: in '...' with \\ and \' as escapes, in "..." with
       those, \" and \a \b \f \n \r \t \v, either maybe followed by a comment; or plain up to a
       ` #` comment. #>
    $value = $Raw.Trim()
    if ($value -match "^'((?:\\'|[^'])*)'\s*(#.*)?$") { return $Matches[1] -replace "\\(['\\])", '$1' }
    # A quote that isn't closed, or text after the closing one: python-dotenv skips the line.
    if ($value -match '^[''"]' -and $value -notmatch '^"((?:\\"|[^"])*)"\s*(#.*)?$') { return $null }
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
    # From the raw text: whitespace and # start a comment even right after the = (KEY= # x is
    # empty), while a # right after it doesn't (KEY=#x is #x).
    return ($Raw -replace '\s+#.*$', '').Trim()
}

function Get-WindowOnlyName([string[]]$Names) {
    <# Those of $Names this window has but a logon doesn't: not in your lasting variables, the
       machine's or the session's (Volatile Environment), nor one Windows sets for every
       process. The scheduled task, started at logon, won't have them. #>
    $everyProcess = 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'HOMEDRIVE', 'HOMEPATH', 'HOMESHARE', 'USERNAME',
    'USERDOMAIN', 'USERDNSDOMAIN', 'LOGONSERVER', 'COMPUTERNAME', 'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE',
    'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432', 'COMMONPROGRAMFILES', 'COMMONPROGRAMFILES(X86)',
    'COMMONPROGRAMW6432', 'PROGRAMDATA', 'PUBLIC', 'ALLUSERSPROFILE', 'TEMP', 'TMP', 'PATH', 'PATHEXT',
    'COMSPEC', 'OS', 'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE', 'SESSIONNAME'
    $lasting = @{}
    foreach ($path in @($script:LastingEnvironment.Values) + 'HKCU:\Volatile Environment') {
        $variables = Get-ItemProperty -LiteralPath $path -ErrorAction SilentlyContinue
        if ($variables) { foreach ($property in $variables.PSObject.Properties) { $lasting[$property.Name.ToUpperInvariant()] = $true } }
    }
    return @($Names | Where-Object {
            $null -ne [Environment]::GetEnvironmentVariable($_) -and
            $everyProcess -notcontains $_.ToUpperInvariant() -and -not $lasting.ContainsKey($_.ToUpperInvariant())
        } | Select-Object -Unique)
}

function Expand-EnvReference([string]$Value, $Earlier) {
    <# ${NAME} and ${NAME:-default} in $Value, as python-dotenv expands them: from the file's
       earlier values first, by their exact names, then this window's environment (as the
       agent-bridge setup runs has it; Get-WindowOnlyName tells setup what the task lacks),
       whose names Python on Windows has upper-cased, so ${userprofile} isn't found there. #>
    return [regex]::Replace($Value, '\$\{([^}:]*)(?::-([^}]*))?\}', {
            param($reference)
            $name = $reference.Groups[1].Value
            if ($Earlier.ContainsKey($name)) { return [string]$Earlier[$name] }
            $fromEnvironment = [Environment]::GetEnvironmentVariable($name)
            if ($null -ne $fromEnvironment -and $name -ceq $name.ToUpperInvariant()) { return $fromEnvironment }
            return $reference.Groups[2].Value
        })
}

function ConvertFrom-EnvLine([string]$Line, $Exact) {
    <# A line's @{ Name; Value } as python-dotenv reads it, ${NAME} expanded from $Exact (the
       values so far, by exact name); Value is $null for a line python-dotenv skips. $null for
       a line without a setting. #>
    if ($Line -notmatch $script:KeyLine) { return $null }
    $name = $Matches[1]
    $raw = ConvertFrom-EnvValue $Matches[2]
    $value = if ($null -eq $raw) { $null } else { Expand-EnvReference $raw $Exact }
    return @{ Name = $name; Value = $value }
}

function New-EnvValueTable {
    <# Values by exact name, as python-dotenv keeps them, for ${NAME}. #>
    return , (New-Object 'Collections.Generic.Dictionary[string,string]' ([StringComparer]::Ordinal))
}

function Read-EnvFile([string]$Path) {
    <# The env file's lines, and its values by key, as python-dotenv reads them. #>
    $values = [ordered]@{}
    $exact = New-EnvValueTable
    $lines = @()
    if (Test-Path -LiteralPath $Path) { $lines = @(Get-Content -LiteralPath $Path -Encoding UTF8) }
    foreach ($line in $lines) {
        $entry = ConvertFrom-EnvLine $line $exact
        if ($entry -and $null -ne $entry.Value) {
            $exact[$entry.Name] = $entry.Value
            $values[$entry.Name.ToUpperInvariant()] = $entry.Value
        }
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
