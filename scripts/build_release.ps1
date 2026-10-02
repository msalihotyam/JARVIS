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
#
# -Install: after the build, validate the package and hand it to the local
# release manager (scripts\JarvisRelease.psm1): it becomes
# %USERPROFILE%\Apps\JARVIS\releases\<version>-<git sha>\, `current` is
# re-pointed to it, the Desktop shortcut is verified and old releases are
# retained per policy. The release id comes from the committed tree, so a
# tree with uncommitted release-relevant changes is refused BEFORE building
# (uv.lock and dev/ are known non-release dirtiness and are ignored).
# Without -Install nothing else changes: build only, as before.
param(
    [string]$DistPath = (Join-Path $env:TEMP "jarvis-release\dist"),
    [string]$WorkPath = (Join-Path $env:TEMP "jarvis-release\build"),
    [switch]$Install,
    [string]$InstallRoot = ""
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Spec = Join-Path $ProjectRoot "packaging\jarvis.spec"

$Identity = $null
if ($Install) {
    Import-Module (Join-Path $PSScriptRoot "JarvisRelease.psm1") -Force
    try {
        $Identity = Get-JarvisReleaseIdentity -ProjectRoot $ProjectRoot
    } catch {
        Write-Output "ERROR: $($_.Exception.Message)"
        exit 1
    }
    Write-Output "Release identity: $($Identity.ReleaseId)  (commit $($Identity.Commit), branch $($Identity.Branch))"
    if ($Identity.AllowedDirty.Count -gt 0) {
        Write-Output "  known non-release local changes, not part of the package: $($Identity.AllowedDirty -join ', ')"
    }
}

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

    if ($Install) {
        try {
            $Again = Get-JarvisReleaseIdentity -ProjectRoot $ProjectRoot
            if ($Again.Commit -ne $Identity.Commit) { throw "HEAD changed during the build ($($Identity.ShortCommit) -> $($Again.ShortCommit)); not installing" }
            $Package = Join-Path $DistPath "JARVIS"
            $Check = Test-JarvisReleasePackage -Path $Package -ExpectedVersion $Identity.Version
            if (-not $Check.Valid) { throw ("built package failed validation: " + ($Check.Problems -join '; ')) }
            Write-Output "Package validated: $($Check.Executable.FileVersion), GUI subsystem, bundled tree complete, no operator data inside"

            $Root = Get-JarvisReleaseRoot -InstallRoot $InstallRoot
            $Result = Install-JarvisRelease -Root $Root -SourcePath $Package -ReleaseId $Identity.ReleaseId `
                -Version $Identity.Version -Commit $Identity.Commit -ShortCommit $Identity.ShortCommit -Branch $Identity.Branch
            if ($Result.Migrated) { Write-Output "Imported the previous flat installation as release $($Result.Migrated.ReleaseId)" }
            if ($Result.Reused) { Write-Output "Release $($Result.ReleaseId) was already installed and matches this build: reused, nothing copied" }
            else { Write-Output "Installed: $($Result.ReleasePath)" }
            if ($Result.Activated) { Write-Output "Activated: current -> $($Result.ReleaseId)$(if ($Result.PreviouslyActive) { " (was $($Result.PreviouslyActive))" })" }
            else { Write-Output "Already active: $($Result.ReleaseId)" }
            if ($Result.Shortcut) {
                $S = $Result.Shortcut
                $What = if ($S.Created) { "created" } elseif ($S.Updated) { "updated" } else { "verified" }
                Write-Output "Desktop shortcut $What`: $($S.Path) -> $($S.Target)"
                foreach ($R in $S.Retargeted) { Write-Output "  stale shortcut retargeted: $R" }
            }
            if ($Result.Retention.Removed.Count -gt 0) { Write-Output "Retention removed: $($Result.Retention.Removed -join ', ')" }
            foreach ($W in $Result.Warnings) { Write-Output "WARNING: $W" }
            Format-JarvisReleaseStatus (Get-JarvisReleaseStatus -Root $Root -IncludeShortcut) | Write-Output
        } catch {
            Write-Output "ERROR: $($_.Exception.Message)"
            exit 1
        }
    }
} finally {
    Pop-Location
}

# Every failure path above exits non-zero explicitly; reaching here means success.
# A native command's leftover $LASTEXITCODE (robocopy's 1 = files copied) must not leak.
exit 0
