<#
.SYNOPSIS
Removes what setup.ps1 installed: takes agent-bridge off its gateway, then removes the
scheduled task, the firewall rule, the settings and the state.

.DESCRIPTION
Run it as yourself (no need for an administrator window), from the same folder as setup.ps1:

    powershell -ExecutionPolicy Bypass -File agent-bridge\deploy\windows\uninstall.ps1

The code stays where it is (a checkout's .venv, or a package). If the gateway can't be
reached, it prints the command that removes this host on the gateway host instead.

.PARAMETER Bridge
The agent-bridge.exe setup.ps1 was given with -Bridge, if it was.
#>
[CmdletBinding()]
param([string]$Bridge)

. (Join-Path $PSScriptRoot 'common.ps1')

if ($env:OS -ne 'Windows_NT') { Stop-Setup 'this script is for Windows' }
Assert-RunAsYou 'uninstall.ps1'

# Everything below goes ahead without agent-bridge.exe, e.g. after its folder was deleted.
$found = Find-Bridge $Bridge -Optional
$Exe = if ($found) { $found.Exe } else { '' }

Write-Step 'Leaving the gateway'
# Read first: the state file, which says where to remove this host by hand, goes below.
$gateway = Get-EnrolledGateway
$left = $false
if ($Exe -and (Test-Path -LiteralPath $Exe)) {
    # Exits 0 even when the gateway can't be reached, after printing what to run there; any
    # other exit is agent-bridge failing to run.
    $left = (Invoke-Native $Exe @('unenroll')) -eq 0
    if (-not $left) { Write-Warn "$Exe didn't run, so this host couldn't take itself off the gateway." }
} elseif ($gateway) {
    Write-Warn "agent-bridge.exe isn't there any more, so this host can't take itself off the gateway."
}
if ($gateway -and -not $left) {
    Write-Host "Remove it on the gateway host ($($gateway.url)) with: sudo mcp-gateway bridges remove $($gateway.name)"
} elseif (-not $gateway) {
    Write-Host 'Not enrolled with a gateway.'
}

Write-Step "Removing the scheduled task $TaskName"
$installed = (Read-EnvFile $EnvFile).Values
$port = 8765
if ($installed.Contains('AGENT_BRIDGE_PORT') -and $installed['AGENT_BRIDGE_PORT'] -match '^\d{1,5}$') { $port = [int]$installed['AGENT_BRIDGE_PORT'] }
Stop-Bridge $Exe $port
if (Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -TaskPath '\' -Confirm:$false
    Write-Host 'removed'
} else {
    Write-Host 'not there'
}

Write-Step 'Removing the firewall rule'
if (Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue) {
    $removeRule = "Remove-NetFirewallRule -Name '$RuleName'"
    Write-Host 'Windows asks for admin rights for this.'
    if ((Invoke-Elevated @($removeRule)) -eq 0) {
        Write-Host 'removed'
    } else {
        Write-Warn 'not removed. In a PowerShell run as administrator:'
        Write-Host "  $removeRule"
    }
} else {
    Write-Host 'not there'
}

Write-Step 'Removing the settings and state'
foreach ($file in @($EnvFile, $StateFile, "$StateFile.tmp", $LogFile, "$LogFile.1", $RunFile)) {
    if (Test-Path -LiteralPath $file) {
        Remove-Item -LiteralPath $file -Force
        Write-Host "removed $file"
    }
}
foreach ($folder in @($SettingsDir, $StateDir)) {
    if ((Test-Path -LiteralPath $folder) -and -not (Get-ChildItem -LiteralPath $folder -Force)) {
        Remove-Item -LiteralPath $folder -Force
    }
}

Write-Host ''
Write-Host 'Done. agent-bridge is uninstalled; its code is still where it was.'

# The last program's exit code would be the script's otherwise.
exit 0
