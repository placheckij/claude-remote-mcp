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

$found = Find-Bridge $Bridge
$Exe = $found.Exe

Write-Step 'Leaving the gateway'
if (Test-Path -LiteralPath $Exe) {
    # Exits 0 even when the gateway can't be reached, after printing what to run there.
    Invoke-Native $Exe @('unenroll') | Out-Null
} else {
    $gateway = Get-EnrolledGateway
    if ($gateway) {
        Write-Warn "$Exe is gone, so this host can't take itself off the gateway at $($gateway.url)."
        Write-Host "Remove it on the gateway host with: sudo mcp-gateway bridges remove $($gateway.name)"
    } else {
        Write-Host 'Not enrolled with a gateway.'
    }
}

Write-Step "Removing the scheduled task $TaskName"
$port = 0
if (Test-Path -LiteralPath $EnvFile) {
    $line = Get-Content -LiteralPath $EnvFile -Encoding UTF8 | Where-Object { $_ -match '^\s*AGENT_BRIDGE_PORT\s*=\s*[''"]?(\d+)' } | Select-Object -First 1
    if ($line -and $line -match '(\d+)') { $port = [int]$Matches[1] }
}
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
    if (Invoke-Elevated $removeRule) {
        Write-Host 'removed'
    } else {
        Write-Warn 'not removed. In a PowerShell run as administrator:'
        Write-Host "  $removeRule"
    }
} else {
    Write-Host 'not there'
}

Write-Step 'Removing the settings and state'
foreach ($file in @($EnvFile, $StateFile, "$StateFile.tmp", $LogFile, "$LogFile.1")) {
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
