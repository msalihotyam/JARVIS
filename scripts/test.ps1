# Serial (the reference when a result is disputed):  scripts\test.ps1
# Parallel (same tests, several processes):          scripts\test.ps1 -Parallel [-Workers N]
# Slowest tests:                                      -Durations 30   (either mode)
param(
    [string]$PythonPath = "",
    [switch]$Parallel,
    [int]$Workers = 0,
    [int]$Durations = 0
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "python_version.ps1")
$Candidates = @()
if ($PythonPath) {
    $Candidates += $PythonPath
}
$Candidates += Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$Candidates += "python"
$Candidates += "py"

$JarvisPython = $null
foreach ($Candidate in $Candidates) {
    $DetectedVersion = Get-JarvisPythonVersion -Candidate $Candidate
    if ($DetectedVersion -and (Test-JarvisPythonVersion -VersionText $DetectedVersion)) {
        $JarvisPython = $Candidate
        break
    }
}

if (-not $JarvisPython) {
    throw "Python 3.11+ bulunamadı. -PythonPath ile python.exe yolunu verin."
}

Push-Location $ProjectRoot
try {
    if ($Parallel) {
        $Arguments = @((Join-Path "scripts" "test_parallel.py"),"--python", $JarvisPython)
        if ($Workers -gt 0) { $Arguments += @("--workers", $Workers) }
        if ($Durations -gt 0) { $Arguments += @("--durations", $Durations) }
        & $JarvisPython @Arguments
    } elseif ($Durations -gt 0) {
        & $JarvisPython -m unittest discover -s tests -v --durations $Durations
    } else {
        & $JarvisPython -m unittest discover -s tests -v
    }
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
