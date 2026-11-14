<#
.SYNOPSIS
Installs agent-bridge on Windows as a scheduled task that starts at logon, and enrolls it
with a gateway.

.DESCRIPTION
Run it as yourself (no need for an administrator window), from a checkout of claude-remote-mcp:

    powershell -ExecutionPolicy Bypass -File agent-bridge\deploy\windows\setup.ps1

It asks for the folders agents may start in, this host's name and the enrollment string
that `sudo mcp-gateway enroll` prints on the gateway host. It finds the LAN address and
herdr.exe itself, checks the port, writes the settings to %APPDATA%\agent-bridge\env,
registers the task, adds a firewall rule for the port on Private networks (the one step
that asks for admin rights), starts agent-bridge and enrolls it.

Safe to re-run, e.g. after pulling new code: it keeps the token and the installed
settings, and asks only for what's missing. Pass a parameter to change a setting.

.PARAMETER Roots
Folders agents may start in, separated by ;. Default: %USERPROFILE%\Projects.
.PARAMETER Name
This host's name in agent ids, as Claude will say it, e.g. win-pc. Default: the computer name.
.PARAMETER Enrollment
The string `sudo mcp-gateway enroll` printed (crm1@...). An empty string skips enrolling.
.PARAMETER Address
The LAN address to listen on. Default: the address of the interface the default route uses.
.PARAMETER Port
The port to listen on. Default: 8765.
.PARAMETER HerdrBin
Full path to herdr.exe. Default: %LOCALAPPDATA%\Programs\Herdr\bin\herdr.exe, or herdr.exe on PATH.
.PARAMETER ClaudeConfigDir
A separate Claude config folder for agents (AGENT_BRIDGE_CLAUDE_CONFIG_DIR); empty removes it.
.PARAMETER TrustRoot
Folders under this one are marked trusted in that config (AGENT_BRIDGE_TRUST_ROOT); empty removes it.
.PARAMETER RotateToken
Replace the token with a new one; enroll again afterwards so the gateway learns it.
.PARAMETER Bridge
The agent-bridge.exe to run, when it's neither in this checkout's .venv nor beside this script.
#>
[CmdletBinding()]
param(
    [string]$Roots,
    [string]$Name,
    [string]$Enrollment,
    [string]$Address,
    [int]$Port,
    [string]$HerdrBin,
    [string]$ClaudeConfigDir,
    [string]$TrustRoot,
    [switch]$RotateToken,
    [string]$Bridge
)

. (Join-Path $PSScriptRoot 'common.ps1')
$Bound = $PSBoundParameters

# --- Helpers ---------------------------------------------------------------------------

function Read-Value([string]$Prompt, [string]$Default) {
    <# Asks; Enter takes the default, and so does running without anyone to ask. #>
    $hint = if ($Default) { " [$Default]" } else { '' }
    try {
        $answer = Read-Host "$Prompt$hint"
    } catch [Management.Automation.PSInvalidOperationException] {
        $answer = ''
    }
    if ($answer -and $answer.Trim()) { return $answer.Trim() }
    return $Default
}

function Read-YesNo([string]$Prompt) {
    <# Asks a yes/no question; only an answer starting with y is yes, so Enter, or running
       without anyone to ask, is no. #>
    return (Read-Value "$Prompt [y/N]" '') -match '^(?i)y'
}

function ConvertTo-EnvValue([string]$Value) {
    <# Plain when python-dotenv reads it back unchanged, else in single quotes with \ and '
       escaped. (It would still expand ${NAME}, which no path here has.) #>
    if ($Value -match '^[A-Za-z0-9_.,:;/\\+~=@-]*$') { return $Value }
    return "'" + ($Value -replace "([\\'])", '\$1') + "'"
}

function Write-EnvFile([string]$Path, [string[]]$Lines, $Settings) {
    <# Sets $Settings' keys (an empty value removes the line) and keeps every other line,
       e.g. AGENT_BRIDGE_ACCOUNTS added by hand. Written beside the file and moved over it. #>
    $out = New-Object Collections.Generic.List[string]
    if ($Lines.Count -eq 0) {
        $out.Add('# agent-bridge settings, written by agent-bridge\deploy\windows\setup.ps1. Lines it')
        $out.Add('# doesn''t set are kept; see agent-bridge\README.md for the others.')
    }
    $done = @{}
    foreach ($line in $Lines) {
        if ($line -match $KeyLine -and $Settings.Contains($Matches[1].ToUpperInvariant())) {
            $key = $Matches[1].ToUpperInvariant()
            if (-not $done.ContainsKey($key) -and $Settings[$key]) { $out.Add("$key=$(ConvertTo-EnvValue $Settings[$key])") }
            $done[$key] = $true
        } else {
            $out.Add($line)
        }
    }
    foreach ($key in $Settings.Keys) {
        if (-not $done.ContainsKey($key) -and $Settings[$key]) { $out.Add("$key=$(ConvertTo-EnvValue $Settings[$key])") }
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    $temporary = "$Path.tmp"
    # UTF-8 without a BOM, which python-dotenv would read as part of the first key.
    [IO.File]::WriteAllText($temporary, (($out -join "`r`n") + "`r`n"), (New-Object Text.UTF8Encoding $false))
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function ConvertTo-FullPath([string]$Path, [string]$What) {
    <# An absolute path on a drive, with a leading ~ expanded. #>
    $path = $Path.Trim().Trim('"')
    if ($path -eq '~') { $path = $env:USERPROFILE }
    elseif ($path -match '^~[\\/]') { $path = Join-Path $env:USERPROFILE $path.Substring(2) }
    if ($path -notmatch '^[A-Za-z]:[\\/]') { Stop-Setup "$What must be a full path on a drive, like C:\Users\me\Projects; got $Path" }
    return [IO.Path]::GetFullPath($path)
}

function Test-Format([string]$Option, [string]$Value) {
    <# Whether agent-bridge accepts $Value as a name (--name-format); it says why not. Only
       exit 2 with agent-bridge's own message means a bad value. In a checkout, agent-bridge
       failing to run is first taken as a .venv that needs `uv sync` after a pull. (Never
       used for the token, which would show on a command line.) #>
    while ($true) {
        $code = Invoke-Native $Exe @('check', "$Option=$Value") -Quiet
        $output = $script:NativeOutput
        if ($code -eq 0) { return $true }
        # argparse exits 2 too, after "usage:", for an option it doesn't know.
        $usage = @($output | Where-Object { $_ -match '^usage:' }).Count -gt 0
        $output | ForEach-Object { Write-Host $_ }
        if ($code -eq 2 -and -not $usage) { return $false }
        if ($Checkout -and -not $script:Synced) {
            Write-Host 'agent-bridge didn''t run as it is; installing its dependencies first.'
            # The running bridge holds the .venv's files.
            Stop-Bridge $Exe 0
            Sync-Checkout
            $script:Synced = $true
            continue
        }
        if ($usage) { Stop-Setup "$Exe has no ``check $Option``: it is older than this setup.ps1" }
        Stop-Setup "$Exe didn't run (exit $code; see above)"
    }
}

function Get-DefaultName {
    <# The computer name as a host name: lowercase letters, digits and -. #>
    $slug = ($env:COMPUTERNAME.ToLowerInvariant() -replace '[^a-z0-9-]+', '-').Trim('-')
    if ($slug -notmatch '^[a-z]') { $slug = "pc-$slug" }
    if ($slug.Length -gt 32) { $slug = $slug.Substring(0, 32).TrimEnd('-') }
    return $slug
}

function Read-Name([string]$Prompt, [string]$Default) {
    <# Asks until agent-bridge accepts the name; '' when nothing was given. #>
    while ($true) {
        $value = Read-Value $Prompt $Default
        if (-not $value -or (Test-Format '--name-format' $value)) { return $value }
        $Default = ''
    }
}

function Get-LanAddress {
    <# The IPv4 address of the interface the default route goes through, or ''. #>
    try {
        $source = Find-NetRoute -RemoteIPAddress 192.0.2.1 -ErrorAction Stop |
            Where-Object { $_.CimClass.CimClassName -eq 'MSFT_NetIPAddress' } | Select-Object -First 1
        if ($source) { return [string]$source.IPAddress }
    } catch {
        Write-Verbose "No default route: $_"
    }
    return ''
}

function Test-LocalAddress([string]$Value) {
    return [bool](Get-NetIPAddress -IPAddress $Value -ErrorAction SilentlyContinue)
}

function Find-Herdr {
    <# Where herdr's installer puts the CLI, else herdr.exe on PATH, else ''. #>
    $installed = Join-Path $env:LOCALAPPDATA 'Programs\Herdr\bin\herdr.exe'
    if (Test-Path -LiteralPath $installed -PathType Leaf) { return $installed }
    $onPath = Get-Command herdr.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($onPath) { return $onPath.Source }
    return ''
}

function New-Token {
    $bytes = New-Object byte[] 32
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($bytes) } finally { $random.Dispose() }
    return -join ($bytes | ForEach-Object { $_.ToString('x2') })
}

function Find-FreePort([int]$From) {
    <# The first port from $From on that nothing listens on and Windows doesn't keep, or 0. #>
    $listening = @{}
    foreach ($connection in @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue)) { $listening[[int]$connection.LocalPort] = $true }
    $excluded = @(Get-ExcludedPortRange)
    for ($candidate = $From; $candidate -le 65535; $candidate++) {
        if (-not $listening.ContainsKey($candidate) -and -not (Test-PortExcluded $candidate $excluded)) { return $candidate }
    }
    return 0
}

function Get-Network([string]$Value) {
    <# The connection profile of the interface with $Value, as @{ Index; Category; Name }, or $null. #>
    $ip = Get-NetIPAddress -IPAddress $Value -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $ip) { return $null }
    $connection = Get-NetConnectionProfile -InterfaceIndex $ip.InterfaceIndex -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $connection) { return $null }
    return @{ Index = $ip.InterfaceIndex; Category = [string]$connection.NetworkCategory; Name = [string]$connection.Name }
}

function Test-FirewallRule([int]$Port) {
    <# Whether our rule is there, as setup makes it: inbound TCP $Port allowed, Private only. #>
    $rule = Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue
    if (-not $rule) { return $false }
    $filter = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $rule
    return ([string]$rule.Enabled -eq 'True') -and ([string]$rule.Direction -eq 'Inbound') -and
        ([string]$rule.Action -eq 'Allow') -and ([string]$rule.Profile -eq 'Private') -and
        ([string]$filter.Protocol -eq 'TCP') -and ([string]$filter.LocalPort -eq [string]$Port)
}

function Wait-Bridge([int]$Port) {
    <# Whether agent-bridge listens on $Port within 15 s. #>
    for ($i = 0; $i -lt 30; $i++) {
        $holder = Get-PortHolder $Port
        if ($holder -and (Test-IsBridge $holder.CommandLine)) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Start-Bridge([int]$Port) {
    <# Starts the task; whether agent-bridge then listens on $Port. #>
    # Stop-Bridge leaves it disabled.
    Enable-ScheduledTask -TaskName $TaskName -TaskPath '\' | Out-Null
    $script:TaskDisabled = $false
    Start-ScheduledTask -TaskName $TaskName -TaskPath '\'
    if (Wait-Bridge $Port) { return $true }
    Write-Warn "agent-bridge isn't listening on port $Port. The end of $LogFile`:"
    if (Test-Path -LiteralPath $LogFile) { Get-Content -LiteralPath $LogFile -Tail 15 | ForEach-Object { Write-Host "  $_" } }
    return $false
}

function Assert-Narrow([string]$Folder, [string]$What) {
    <# Refuses the home folder or anything above it, as agent-bridge does; caught here, it
       doesn't replace a working bridge with one that won't start. #>
    $userHome = [IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\') + '\'
    if ($userHome.StartsWith($Folder.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Setup "$What $Folder is too broad: use a projects folder, not a drive or the home folder itself"
    }
}

function Sync-Checkout {
    Write-Step "Installing dependencies into $Checkout\.venv"
    if ((Invoke-Native uv @('sync', '--all-packages', '--frozen', '--directory', $Checkout)) -ne 0) { Stop-Setup 'uv sync failed' }
}

function Get-BlockRule([string]$Program) {
    <# Inbound rules that block $Program, e.g. python.exe after a Cancel at Windows' firewall
       prompt; they win over the rule setup adds. #>
    return @(Get-NetFirewallApplicationFilter -Program $Program -ErrorAction SilentlyContinue |
        Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object {
            [string]$_.Direction -eq 'Inbound' -and [string]$_.Action -eq 'Block' -and [string]$_.Enabled -eq 'True'
        })
}

# The body runs in try, so that however setup ends (an error, Stop-Setup or Ctrl+C) a task
# that Stop-Bridge disabled is enabled again.
try {
    # --- Checks and questions --------------------------------------------------------------

    Assert-RunAsYou 'setup.ps1'
    # conhost.exe --headless, which hides the task's window, came with Windows 10 1809.
    if ([Environment]::OSVersion.Version.Build -lt 17763) { Stop-Setup 'agent-bridge needs Windows 10 version 1809 or later' }

    $found = Find-Bridge $Bridge
    $Exe = $found.Exe
    $Checkout = $found.Checkout
    $Synced = $false
    if ($Checkout) {
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            Stop-Setup 'uv not found; install it: https://docs.astral.sh/uv/getting-started/installation/'
        }
        # A first install has nothing running yet that holds the .venv's files.
        if (-not (Test-Path -LiteralPath $Exe)) {
            Sync-Checkout
            $Synced = $true
        }
    }
    if (-not (Test-Path -LiteralPath $Exe)) { Stop-Setup "$Exe doesn't exist" }

    $Installed = Read-EnvFile $EnvFile
    function Get-Installed([string]$Key) {
        if ($Installed.Values.Contains("AGENT_BRIDGE_$Key")) { return $Installed.Values["AGENT_BRIDGE_$Key"] }
        return ''
    }

    # Folders agents may start in.
    if (-not $Bound.ContainsKey('Roots')) {
        $Roots = Get-Installed 'ALLOWED_ROOTS'
        if (-not $Roots) { $Roots = Read-Value 'Folders agents may start in, separated by ;' (Join-Path $env:USERPROFILE 'Projects') }
    }
    $RootList = @($Roots -split ';' | Where-Object { $_.Trim() } | ForEach-Object { ConvertTo-FullPath $_ 'Each root' })
    if ($RootList.Count -eq 0) { Stop-Setup 'agents need at least one folder to start in' }
    foreach ($root in $RootList) {
        Assert-Narrow $root 'The root'
        if (-not (Test-Path -LiteralPath $root -PathType Container)) { Write-Warn "$root doesn't exist yet" }
    }
    $Roots = $RootList -join ';'

    # This host's name.
    # Whether it came from the env file: a name given now, passed or answered, has to reach
    # the gateway, while the env file's may differ from it after `bridges rename` there.
    $NameInstalled = $false
    if ($Bound.ContainsKey('Name')) {
        if (-not (Test-Format '--name-format' $Name)) { Stop-Setup "-Name: $Name won't do" }
    } else {
        $Name = Get-Installed 'NAME'
        $NameInstalled = [bool]$Name
        if ($Name -and -not (Test-Format '--name-format' $Name)) { Stop-Setup "AGENT_BRIDGE_NAME in $EnvFile won't do; pass -Name" }
        if (-not $Name) { $Name = Read-Name "This host's name, as Claude will say it" (Get-DefaultName) }
        if (-not $Name) { Stop-Setup 'this host needs a name' }
    }

    # The LAN address: the installed one while this host still has it, else found again.
    if (-not $Bound.ContainsKey('Address')) {
        $Address = Get-Installed 'HOST'
        if ($Address -and -not (Test-LocalAddress $Address)) {
            Write-Warn "this host no longer has $Address; looking for its LAN address again"
            $Address = ''
        }
        if (-not $Address) { $Address = Get-LanAddress }
        if (-not $Address) { $Address = Read-Value 'LAN address to listen on, like 192.168.1.20' '' }
    }
    if (-not $Address) { Stop-Setup 'no LAN address: connect to the network, or pass -Address' }
    # As agent-bridge checks it, before a bad one replaces a working bridge.
    $parsed = $null
    # .NET also reads 192.168.1 and 010.0.0.5 as IPv4 addresses, which Python doesn't: an
    # IPv4 address must be written as .NET writes it back.
    $ipv4 = [Net.Sockets.AddressFamily]::InterNetwork
    if (-not [Net.IPAddress]::TryParse($Address, [ref]$parsed) -or
        ($parsed.AddressFamily -eq $ipv4 -and $parsed.ToString() -ne $Address)) {
        Stop-Setup "the LAN address must be an IP address, like 192.168.1.20; got $Address"
    }
    if ($parsed.Equals([Net.IPAddress]::Any) -or $parsed.Equals([Net.IPAddress]::IPv6Any)) {
        Stop-Setup 'listen on the LAN address, not on every interface'
    }
    if ($Address -ne '127.0.0.1' -and -not (Test-LocalAddress $Address)) {
        Write-Warn "no interface has $Address right now; the task tries again every minute until one does"
    }

    if (-not $Bound.ContainsKey('Port')) {
        $installedPort = Get-Installed 'PORT'
        if ($installedPort -and $installedPort -notmatch '^\d{1,5}$') { Stop-Setup "AGENT_BRIDGE_PORT in $EnvFile isn't a port: $installedPort" }
        $Port = if ($installedPort) { [int]$installedPort } else { 8765 }
    }
    if ($Port -lt 1 -or $Port -gt 65535) { Stop-Setup "the port must be between 1 and 65535, got $Port" }

    # herdr.exe: the installed path while it still exists, else found again.
    if (-not $Bound.ContainsKey('HerdrBin')) {
        $HerdrBin = Get-Installed 'HERDR_BIN'
        if ($HerdrBin -and -not (Test-Path -LiteralPath $HerdrBin -PathType Leaf)) { $HerdrBin = '' }
        if (-not $HerdrBin) { $HerdrBin = Find-Herdr }
        if (-not $HerdrBin) { $HerdrBin = Read-Value 'Full path to herdr.exe' '' }
    }
    if (-not $HerdrBin) { Stop-Setup 'herdr.exe not found: install herdr, or pass -HerdrBin' }
    $HerdrBin = ConvertTo-FullPath $HerdrBin 'herdr.exe'
    if (-not (Test-Path -LiteralPath $HerdrBin -PathType Leaf)) { Stop-Setup "$HerdrBin doesn't exist" }

    # Folder trust: changed only when passed in; the installed lines are kept otherwise.
    $Optional = [ordered]@{}
    if ($Bound.ContainsKey('ClaudeConfigDir')) {
        $Optional['AGENT_BRIDGE_CLAUDE_CONFIG_DIR'] = if ($ClaudeConfigDir) { ConvertTo-FullPath $ClaudeConfigDir '-ClaudeConfigDir' } else { '' }
    }
    if ($Bound.ContainsKey('TrustRoot')) {
        $Optional['AGENT_BRIDGE_TRUST_ROOT'] = if ($TrustRoot) { ConvertTo-FullPath $TrustRoot '-TrustRoot' } else { '' }
        if ($TrustRoot) { Assert-Narrow $Optional['AGENT_BRIDGE_TRUST_ROOT'] '-TrustRoot' }
    }
    $ConfigDir = if ($Optional.Contains('AGENT_BRIDGE_CLAUDE_CONFIG_DIR')) { $Optional['AGENT_BRIDGE_CLAUDE_CONFIG_DIR'] } else { Get-Installed 'CLAUDE_CONFIG_DIR' }
    $Trust = if ($Optional.Contains('AGENT_BRIDGE_TRUST_ROOT')) { $Optional['AGENT_BRIDGE_TRUST_ROOT'] } else { Get-Installed 'TRUST_ROOT' }
    if ($Trust -and -not $ConfigDir) {
        Stop-Setup 'a trust root needs -ClaudeConfigDir: folders are only ever trusted in a separate Claude config'
    }

    # The token: kept, unless there is none yet or it is being replaced.
    $Token = Get-Installed 'TOKEN'
    $NewToken = $RotateToken -or -not $Token
    # An installed token isn't checked here: on a command line it would show to anything that
    # logs process starts. agent-bridge check (below) checks the settings it is in.
    if ($NewToken) { $Token = New-Token }

    # cmd.exe would expand %...% in these, inside quotes too.
    foreach ($path in @($Exe, $LogFile)) { if ($path.Contains('%')) { Stop-Setup "agent-bridge can't run from a path with % in it: $path" } }

    # --- Port ------------------------------------------------------------------------------

    # Before stopping the running bridge, so the process holding the port can still be told apart.
    while ($true) {
        $holder = Get-PortHolder $Port
        if ($holder -and -not (Test-IsBridge $holder.CommandLine)) {
            Write-Host "Port $Port is in use by another program: PID $($holder.Id), $($holder.Name)"
            if ($holder.CommandLine) { Write-Host "  $($holder.CommandLine)" }
            $instead = 'stop that program, or pass another -Port'
        } elseif (-not $holder -and (Test-PortExcluded $Port @(Get-ExcludedPortRange))) {
            Write-Host "Windows keeps port $Port for itself (see: netsh int ipv4 show excludedportrange protocol=tcp)."
            $instead = 'pass another -Port'
        } else {
            break
        }
        if ($Bound.ContainsKey('Port')) { Stop-Setup $instead }
        $free = Find-FreePort ($Port + 1)
        if ($free -eq 0) { Stop-Setup 'no free port found' }
        $answer = Read-Value 'Port to use instead' ([string]$free)
        if ($answer -notmatch '^\d{1,5}$' -or [int]$answer -lt 1 -or [int]$answer -gt 65535) { Stop-Setup "not a port: $answer" }
        $Port = [int]$answer
    }

    # --- Install ---------------------------------------------------------------------------

    # The firewall first: registering the task starts agent-bridge at once, and Windows may ask
    # about Python as soon as it listens on the LAN address.
    Write-Step "Firewall rule for TCP $Port, on Private networks only"
    $network = Get-Network $Address
    $elevated = @()
    $ruleWanted = -not (Test-FirewallRule $Port)
    if ($ruleWanted) {
        $elevated += "Remove-NetFirewallRule -Name '$RuleName' -ErrorAction SilentlyContinue; " +
            "New-NetFirewallRule -Name '$RuleName' -DisplayName 'agent-bridge' -Description 'Lets mcp-gateway on the LAN reach agent-bridge. Added by agent-bridge\deploy\windows\setup.ps1.' " +
            "-Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Private | Out-Null"
    }
    if ($network -and $network.Category -eq 'Public') {
        Write-Host "Windows treats the network $Address is on ($($network.Name)) as Public, where the rule doesn't"
        Write-Host 'apply, so the gateway couldn''t reach agent-bridge. On your own network, make it Private.'
        if (Read-YesNo "Make $($network.Name) a Private network?") {
            $elevated += "Set-NetConnectionProfile -InterfaceIndex $($network.Index) -NetworkCategory Private"
        } else {
            Write-Warn 'the gateway can''t reach agent-bridge until this network is Private'
        }
    } elseif ($network -and $network.Category -ne 'Private') {
        Write-Warn "the network $Address is on is $($network.Category); the rule applies only on Private networks"
    }
    if ($elevated.Count -eq 0) {
        Write-Host 'ok, already there'
    } else {
        Write-Host 'Windows asks for admin rights for this.'
        # Each step on its own, so the rule is added even where the network can't be changed.
        $failed = Invoke-Elevated $elevated
        $byHand = @(for ($i = 0; $i -lt $elevated.Count; $i++) {
            if ($failed -eq -1 -or ($failed -band [int][Math]::Pow(2, $i))) { $elevated[$i] }
        })
        if ($byHand.Count -eq 0) {
            Write-Host 'ok'
        } else {
            Write-Warn 'not done, so the gateway can''t reach agent-bridge. In a PowerShell run as administrator:'
            foreach ($command in $byHand) { Write-Host "  $command" }
        }
    }

    Write-Step 'Stopping the running agent-bridge, if any'
    Stop-Bridge $Exe $Port
    if (Get-PortHolder $Port) { Stop-Setup "port $Port is still in use; stop what holds it and run setup again" }

    if ($Checkout -and -not $Synced) { Sync-Checkout }

    Write-Step "Writing $EnvFile"
    $Settings = [ordered]@{
        AGENT_BRIDGE_TOKEN         = $Token
        AGENT_BRIDGE_ALLOWED_ROOTS = $Roots
        AGENT_BRIDGE_HOST          = $Address
        AGENT_BRIDGE_PORT          = [string]$Port
        AGENT_BRIDGE_NAME          = $Name
        AGENT_BRIDGE_HERDR_BIN     = $HerdrBin
    }
    foreach ($key in $Optional.Keys) { $Settings[$key] = $Optional[$key] }
    Write-EnvFile $EnvFile $Installed.Lines $Settings

    Write-Step "Registering the scheduled task $TaskName, which starts agent-bridge"
    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    # conhost.exe splits the task's arguments and quotes each again, so everything that needs
    # quotes inside goes in run.cmd: CRLF (cmd misreads LF after chcp) and UTF-8, as it says.
    $wrapper = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'run.cmd') -Raw) -replace "`r?`n", "`r`n"
    $wrapper = $wrapper.Replace('{{LOG}}', $LogFile).Replace('{{EXE}}', $Exe)
    [IO.File]::WriteAllText($RunFile, $wrapper, (New-Object Text.UTF8Encoding $false))
    $fields = @{
        '{{USER}}'              = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        '{{COMMAND}}'           = Join-Path $env:SystemRoot 'System32\conhost.exe'
        # `call` first: cmd /c drops the quotes around a path with ( or & in it otherwise.
        '{{ARGUMENTS}}'         = "--headless `"$(Join-Path $env:SystemRoot 'System32\cmd.exe')`" /d /c call `"$RunFile`""
        '{{WORKING_DIRECTORY}}' = $StateDir
    }
    $xml = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'agent-bridge.task.xml') -Raw
    foreach ($field in $fields.Keys) { $xml = $xml.Replace($field, [Security.SecurityElement]::Escape($fields[$field])) }
    Write-Host 'If Windows asks whether to let Python through the firewall, allow it on Private networks:'
    Write-Host 'Cancel adds a rule that blocks Python, which wins over the rule above.'
    Register-ScheduledTask -TaskName $TaskName -TaskPath '\' -Xml $xml -Force | Out-Null
    # Registered enabled.
    $script:TaskDisabled = $false
    $Started = Start-Bridge $Port

    # --- Enroll ----------------------------------------------------------------------------

    $Gateway = Get-EnrolledGateway
    # The gateway knows this host by the name it enrolled with; a new -Name needs enrolling again.
    $NameChanged = $Gateway -and -not $NameInstalled -and $Name -ne $Gateway.name
    $String = ''
    if ($Bound.ContainsKey('Enrollment')) {
        $String = $Enrollment
    } elseif (-not $Gateway -or $NewToken -or $NameChanged) {
        Write-Host ''
        if ($Gateway -and $NewToken) { Write-Host "The gateway at $($Gateway.url) still has the old token: enroll again." }
        if ($NameChanged) { Write-Host "The gateway at $($Gateway.url) knows this host as $($Gateway.name): enroll again to rename it." }
        Write-Host 'To join a gateway, run `sudo mcp-gateway enroll` on the gateway host and paste the string it prints.'
        $String = Read-Value 'Enrollment string (Enter to skip)' ''
    }
    $EnrollCode = $null
    if ($String) {
        Write-Step 'Enrolling with the gateway'
        while ($true) {
            $EnrollCode = Invoke-Native $Exe @('enroll', $String)
            if ($EnrollCode -ne 3) { break }
            # The name is taken by another host on that gateway.
            $Name = Read-Name 'Another name for this host' ''
            if (-not $Name) { break }
            $Settings['AGENT_BRIDGE_NAME'] = $Name
            Write-EnvFile $EnvFile (Read-EnvFile $EnvFile).Lines $Settings
            # So its /info has the new name too.
            Stop-Bridge $Exe $Port
            $Started = Start-Bridge $Port
        }
    }

    Write-Step 'Checks'
    $CheckFailed = (Invoke-Native $Exe @('check')) -ne 0
    # Read once: the bridge may stop between two looks.
    $listener = Get-PortHolder $Port
    $Blocked = if ($listener -and $listener.Path -and (Test-IsBridge $listener.CommandLine)) { $listener.Path } else { '' }
    $Blocking = @(if ($Blocked) { Get-BlockRule $Blocked })

    # --- Done ------------------------------------------------------------------------------

    $UrlHost = if ($Address.Contains(':')) { "[$Address]" } else { $Address }
    $Gateway = Get-EnrolledGateway
    Write-Host ''
    if ($Started) {
        Write-Host "Done. agent-bridge listens on http://${UrlHost}:$Port and starts at every logon."
    } else {
        Write-Host "Installed, but agent-bridge didn't start: see the end of its log above. The task tries again"
        Write-Host 'every minute; once it is fixed, run setup again.'
    }
    Write-Host "Its log: $LogFile"
    if ($CheckFailed) { Write-Host 'A check failed: see the log (a 502 from herdr means its server isn''t running).' }
    if ($Blocking.Count -gt 0) {
        Write-Warn "a firewall rule blocks $Blocked, so the gateway can't reach agent-bridge. In a PowerShell run as administrator:"
        Write-Host "  Get-NetFirewallApplicationFilter -Program $(Format-Quoted $Blocked) | Get-NetFirewallRule | Where-Object Action -eq Block | Remove-NetFirewallRule"
    }
    if ($ConfigDir -and -not (Test-Path -LiteralPath (Join-Path $ConfigDir '.claude.json'))) {
        Write-Host ''
        Write-Host "Run Claude once in the agents' config and log in, or new agents can't start:"
        Write-Host "  `$env:CLAUDE_CONFIG_DIR = $(Format-Quoted $ConfigDir); claude"
    }
    Write-Host ''
    if ($null -ne $EnrollCode -and $EnrollCode -ne 0) {
        Write-Host 'Not enrolled: see "Enrolling with the gateway" above. Once that is fixed, run here'
        Write-Host '(with a new string from `sudo mcp-gateway enroll` if this one has expired):'
        Write-Host "  & $(Format-Quoted $Exe) enroll '<the string it printed>'"
        if ($Gateway) { Write-Warn "the gateway at $($Gateway.url) still has this host's old settings until then" }
    } elseif ($Gateway) {
        Write-Host "Enrolled as $($Gateway.name) with the gateway at $($Gateway.url). Ask Claude to list agents."
        if (($NewToken -or $NameChanged) -and $null -eq $EnrollCode) {
            Write-Warn 'the gateway still has the old token or name: enroll again'
        }
    } else {
        Write-Host 'Not enrolled with a gateway yet. Run `sudo mcp-gateway enroll` on the gateway host, then here:'
        Write-Host "  & $(Format-Quoted $Exe) enroll '<the string it printed>'"
    }
    # A failed check or start is setup's exit code; otherwise the last program's would be.
    if ($CheckFailed -or -not $Started) { exit 1 }
    exit 0
} finally {
    Restore-Task
}
