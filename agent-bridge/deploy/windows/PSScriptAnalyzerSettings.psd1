# What CI's PSScriptAnalyzer job checks the Windows deploy scripts against.
@{
    Severity     = @('Error', 'Warning')
    ExcludeRules = @(
        # Interactive setup scripts: what they print is for the person running them.
        'PSAvoidUsingWriteHost',
        # Helpers inside a script, not cmdlets: -WhatIf and -Confirm wouldn't mean anything.
        'PSUseShouldProcessForStateChangingFunctions'
    )
    Rules        = @{
        # Windows PowerShell 5.1 is what every Windows has.
        PSUseCompatibleSyntax = @{ Enable = $true; TargetVersions = @('5.1') }
    }
}
