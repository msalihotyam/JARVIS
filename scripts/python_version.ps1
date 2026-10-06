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

function Test-JarvisPythonHasPySide6 {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Candidate
    )

    try {
        & $Candidate -c "import PySide6" 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

# Rule: an explicit -PythonPath (first in the list) wins when it is 3.11+.
# Otherwise the first 3.11+ candidate that can import PySide6; if none can,
# the first 3.11+ one (UI modules will fail - the caller warns).
function Select-JarvisPython {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Candidates,
        [string]$Explicit = ""
    )

    $Fallback = $null
    foreach ($Candidate in $Candidates) {
        $Version = Get-JarvisPythonVersion -Candidate $Candidate
        if (-not ($Version -and (Test-JarvisPythonVersion -VersionText $Version))) {
            continue
        }
        $HasUi = Test-JarvisPythonHasPySide6 -Candidate $Candidate
        if ($HasUi -or ($Explicit -and $Candidate -eq $Explicit)) {
            return [pscustomobject]@{ Path = $Candidate; HasPySide6 = $HasUi }
        }
        if (-not $Fallback) {
            $Fallback = [pscustomobject]@{ Path = $Candidate; HasPySide6 = $false }
        }
    }
    return $Fallback
}
