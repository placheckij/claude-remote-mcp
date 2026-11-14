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

Assert-RunAsYou 'uninstall.ps1'

# Everything below goes ahead without agent-bridge.exe, e.g. after its folder was deleted.
$found = Find-Bridge $Bridge -Optional -PreferInstalled
$Exe = if ($found) { $found.Exe } else { '' }

Write-Step 'Leaving the gateway'
# Read first: the state file, which says where to remove this host by hand, goes below.
$gateway = Get-EnrolledGateway
$left = $false
if ($Exe -and (Test-Path -LiteralPath $Exe)) {
    # Exits 0 even when the gateway can't be reached, after printing what to run there, and
    # says so itself when not enrolled; any other exit is agent-bridge failing to run.
    $left = (Invoke-Native $Exe @('unenroll')) -eq 0
    if (-not $left) { Write-Warn "agent-bridge failed (see above), so this host couldn't take itself off the gateway." }
} elseif ($gateway) {
    Write-Warn "agent-bridge.exe isn't there any more, so this host can't take itself off the gateway."
} else {
    Write-Host 'Not enrolled with a gateway.'
}
if ($gateway -and -not $left) {
    Write-Host "Remove it on the gateway host ($($gateway.url)) with: sudo mcp-gateway bridges remove $($gateway.name)"
} elseif (-not $left -and $Exe) {
    # The state file may be what it couldn't read: name the host as the env file does.
    $name = [string](Read-EnvFile $EnvFile).Values['AGENT_BRIDGE_NAME']
    if ($name) { Write-Host "If the gateway still has this host, remove it there with: sudo mcp-gateway bridges remove $name" }
}

Write-Step "Removing the scheduled task $TaskName"
Stop-Bridge $Exe (Get-InstalledPort)
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
foreach ($file in @($EnvFile, "$EnvFile.tmp", $StateFile, "$StateFile.tmp", $LogFile, "$LogFile.1", $RunFile)) {
    if (Test-Path -LiteralPath $file) {
        # A process that hasn't quite exited may still hold the log for a moment.
        for ($try = 1; ; $try++) {
            try {
                Remove-Item -LiteralPath $file -Force
                Write-Host "removed $file"
                break
            } catch {
                if ($try -ge 5) { Write-Warn "couldn't remove $file ($($_.Exception.Message)); delete it by hand"; break }
                Start-Sleep -Seconds 1
            }
        }
    }
}
Remove-Item -LiteralPath $CheckDir -Recurse -Force -ErrorAction SilentlyContinue
foreach ($folder in @($SettingsDir, $StateDir)) {
    if ((Test-Path -LiteralPath $folder) -and -not (Get-ChildItem -LiteralPath $folder -Force)) {
        Remove-Item -LiteralPath $folder -Force
    }
}

Write-Host ''
Write-Host 'Done. agent-bridge is uninstalled; its code is still where it was.'

# The last program's exit code would be the script's otherwise.
exit 0
