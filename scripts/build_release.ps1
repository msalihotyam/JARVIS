# Build the V1 Windows release folder with PyInstaller.
#
# Disposable output: a fresh --distpath/--workpath under a temp directory by
# default, so a build never leaves generated binaries in the repository tree
# and never touches this developer's own runtime state (data/, .jarvis/,
# uv.lock - none of which packaging/jarvis.spec references in the first
# place). Pass -DistPath / -WorkPath to choose your own location instead.
#
# PyInstaller is a BUILD-time tool only, never a runtime dependency: this
# script does not add it to pyproject.toml or uv.lock. It installs into
# whatever Python interpreter is already used to run JARVIS (the one with
# PySide6 and jarvis importable), which is a plain `pip install`, not a
# project dependency change.
param(
    [string]$DistPath = (Join-Path $env:TEMP "jarvis-release\dist"),
    [string]$WorkPath = (Join-Path $env:TEMP "jarvis-release\build")
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Spec = Join-Path $ProjectRoot "packaging\jarvis.spec"

Push-Location $ProjectRoot
try {
    python -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Output "PyInstaller not found in this interpreter; installing (build-time only, not a project dependency)."
        python -m pip install pyinstaller
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }

    New-Item -ItemType Directory -Force -Path $DistPath | Out-Null
    New-Item -ItemType Directory -Force -Path $WorkPath | Out-Null

    & python -m PyInstaller $Spec `
        --distpath $DistPath `
        --workpath $WorkPath `
        --noconfirm
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    $ExePath = Join-Path $DistPath "JARVIS\JARVIS.exe"
    Write-Output "Built: $ExePath"
} finally {
    Pop-Location
}
