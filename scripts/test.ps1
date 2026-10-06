# Serial (the reference when a result is disputed):  scripts\test.ps1
# Parallel (same tests, several processes):          scripts\test.ps1 -Parallel [-Workers N] [-UiWorkers K]
# Slowest tests:                                      -Durations 30   (either mode)
param(
    [string]$PythonPath = "",
    [switch]$Parallel,
    [int]$Workers = 0,
    [int]$UiWorkers = 0,
    [int]$Durations = 0
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "python_version.ps1")
$Candidates = @()
if ($PythonPath) {
    $Candidates += $PythonPath
}
$Candidates += "python"
$Candidates += "py"
$Candidates += Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

$Selected = Select-JarvisPython -Candidates $Candidates -Explicit $PythonPath
if (-not $Selected) {
    throw "Python 3.11+ bulunamadı. -PythonPath ile python.exe yolunu verin."
}
$JarvisPython = $Selected.Path
Write-Host "Python: $JarvisPython"
if (-not $Selected.HasPySide6) {
    Write-Warning "$JarvisPython cannot import PySide6; UI test modules will fail. Pass -PythonPath with a Python that has PySide6."
}

Push-Location $ProjectRoot
try {
    if ($Parallel) {
        $Arguments = @((Join-Path "scripts" "test_parallel.py"),"--python", $JarvisPython)
        if ($Workers -gt 0) { $Arguments += @("--workers", $Workers) }
        if ($UiWorkers -gt 0) { $Arguments += @("--ui-workers", $UiWorkers) }
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
