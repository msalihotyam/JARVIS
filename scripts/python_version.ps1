function Test-JarvisPythonVersion {
    param(
        [Parameter(Mandatory = $true)]
        [string]$VersionText
    )

    $Match = [regex]::Match($VersionText.Trim(), '^(\d+)\.(\d+)(?:\.\d+)?$')
    if (-not $Match.Success) {
        return $false
    }

    $Major = [int]$Match.Groups[1].Value
    $Minor = [int]$Match.Groups[2].Value
    return ($Major -gt 3) -or ($Major -eq 3 -and $Minor -ge 11)
}

function Get-JarvisPythonVersion {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Candidate
    )

    try {
        $Output = & $Candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>$null
        if ($LASTEXITCODE -ne 0) {
            return $null
        }
        return ($Output | Select-Object -Last 1).Trim()
    } catch {
        return $null
    }
}
