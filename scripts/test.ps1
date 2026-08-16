param(
    [string]$PythonPath = ""
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
    & $JarvisPython -m unittest discover -s tests -v
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
