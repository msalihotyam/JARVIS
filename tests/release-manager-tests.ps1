# Tests for the local release manager (scripts\JarvisRelease.psm1, release.ps1).
#
# Every test works on a disposable temporary install root and a temporary
# "desktop" directory. The real %USERPROFILE%\Apps\JARVIS, the real Desktop,
# ~\.jarvis, the repository's data\ and uv.lock are never written - the last
# section proves it by comparing their listings before and after the run.
#
# JARVIS.exe inside the fake packages is a COPY of a real built JARVIS.exe when
# one can be found (so the PE-subsystem and version-resource checks run for
# real); otherwise a placeholder file is used and those two checks are skipped.

param([string]$ExeSample = '')

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
Import-Module (Join-Path $RepoRoot 'scripts\JarvisRelease.psm1') -Force
$ReleasePs = Join-Path $RepoRoot 'scripts\release.ps1'

$script:pass = 0
$script:fail = 0
$script:failures = @()
$script:tempRoots = @()

function Check {
    param([string]$Name, [bool]$Condition)
    if ($Condition) { $script:pass++; Write-Host "  PASS $Name" }
    else { $script:fail++; $script:failures += $Name; Write-Host "  FAIL $Name" -ForegroundColor Red }
}

function New-TempRoot {
    $root = Join-Path ([System.IO.Path]::GetTempPath()) ("relmgr-test-" + [guid]::NewGuid().ToString('N'))
    $null = New-Item -ItemType Directory -Path $root
    $script:tempRoots += $root
    return $root
}

function Throws {
    # The exception message, or '' when the block did not throw.
    param([scriptblock]$Block)
    try { & $Block | Out-Null; return '' } catch { return $_.Exception.Message }
}

function Get-Signature {
    # Names, sizes and mtimes below a path (reparse points are listed, not followed).
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return '(absent)' }
    $items = @(Get-ChildItem -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue -Attributes !ReparsePoint) +
        @(Get-ChildItem -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue -Attributes ReparsePoint)
    $lines = foreach ($i in ($items | Sort-Object FullName)) {
        $rel = $i.FullName.Substring([Math]::Min($Path.Length, $i.FullName.Length))
        if ($i.PSIsContainer) { "$rel|D|$([int]$i.Attributes)" } else { "$rel|F|$($i.Length)|$($i.LastWriteTimeUtc.Ticks)" }
    }
    return ($lines -join "`n")
}

function Read-State { param([string]$Root) return (Read-JarvisReleaseState -Root $Root).State }
function Get-CurrentId { param([string]$Root) return (Resolve-JarvisCurrent -Root $Root).ReleaseId }

# ---- a real executable sample, when available ---------------------------------

function Find-ExeSample {
    param([string]$Given)
    $candidates = @()
    if ($Given) { $candidates += $Given }
    $apps = Join-Path $env:USERPROFILE 'Apps\JARVIS'
    $candidates += (Join-Path $apps 'JARVIS.exe')
    $candidates += (Join-Path $apps 'current\JARVIS.exe')
    if (Test-Path -LiteralPath (Join-Path $apps 'releases')) {
        $candidates += @(Get-ChildItem -LiteralPath (Join-Path $apps 'releases') -Directory -Force -ErrorAction SilentlyContinue | ForEach-Object { Join-Path $_.FullName 'JARVIS.exe' })
    }
    $candidates += (Join-Path $env:TEMP 'jarvis-release\dist\JARVIS\JARVIS.exe')
    foreach ($c in $candidates) { if ($c -and (Test-Path -LiteralPath $c)) { return (Get-Item -LiteralPath $c).FullName } }
    return ''
}

$Sample = Find-ExeSample $ExeSample
$CheckExe = [bool]$Sample
$SampleVersion = '1.0.0'
if ($Sample) {
    $v = $null
    if ([System.Version]::TryParse([string](Get-Item -LiteralPath $Sample).VersionInfo.FileVersion, [ref]$v)) { $SampleVersion = "$($v.Major).$($v.Minor).$([Math]::Max(0, $v.Build))" }
    Write-Host "executable sample: $Sample (version $SampleVersion)"
}
else { Write-Host 'no real JARVIS.exe found: PE/version checks are skipped in this run' -ForegroundColor Yellow }

function New-FakePackage {
    # The shape packaging\jarvis.spec produces: JARVIS.exe + _internal\ with the
    # bundled UI tree. $Stamp makes packages differ in content.
    param([string]$Path, [string]$Stamp = 'x')
    $internal = Join-Path $Path '_internal'
    foreach ($d in 'jarvis\ui\qml\theme', 'jarvis\ui\qml\components', 'jarvis\ui\qml\pages', 'jarvis\ui\qml\overlays', 'jarvis\ui\qml\fonts', 'jarvis\ui\qml\assets', 'jarvis\ui\i18n', 'PySide6') {
        $null = New-Item -ItemType Directory -Path (Join-Path $internal $d) -Force
    }
    Set-Content -LiteralPath (Join-Path $internal 'jarvis\ui\qml\Main.qml') -Value "// $Stamp" -Encoding Ascii
    Set-Content -LiteralPath (Join-Path $internal 'jarvis\ui\qml\theme\Theme.qml') -Value "// theme $Stamp" -Encoding Ascii
    Set-Content -LiteralPath (Join-Path $internal 'jarvis\ui\qml\assets\app.ico') -Value 'ico' -Encoding Ascii
    Set-Content -LiteralPath (Join-Path $internal 'jarvis\ui\i18n\jarvis_tr-TR.qm') -Value 'qm' -Encoding Ascii
    Set-Content -LiteralPath (Join-Path $internal 'PySide6\payload.bin') -Value ("payload " + $Stamp) -Encoding Ascii
    Set-Content -LiteralPath (Join-Path $internal 'python313.dll') -Value 'dll' -Encoding Ascii
    if ($Sample) { Copy-Item -LiteralPath $Sample -Destination (Join-Path $Path 'JARVIS.exe') }
    else { Set-Content -LiteralPath (Join-Path $Path 'JARVIS.exe') -Value 'MZ placeholder' -Encoding Ascii }
    return $Path
}

function Install-Fake {
    param([string]$Root, [string]$Id, [string]$Version = $SampleVersion, [string]$Commit = '', [string]$Stamp = '', [string]$Desktop = '', [int]$Keep = 3)
    $src = New-FakePackage -Path (Join-Path (New-TempRoot) 'dist\JARVIS') -Stamp $(if ($Stamp) { $Stamp } else { $Id })
    if (-not $Commit) { $Commit = ("commit-of-$Id".PadRight(40, '0')) }
    $args = @{ Root = $Root; SourcePath = $src; ReleaseId = $Id; Version = $Version; Commit = $Commit
        ShortCommit = $Commit.Substring(0, 7); Branch = 'test'; Keep = $Keep; CheckExecutable = $CheckExe }
    if ($Desktop) { $args.DesktopPath = $Desktop } else { $args.NoShortcut = $true }
    return Install-JarvisRelease @args
}

$A = "$SampleVersion-aaaaaaa"; $B = "$SampleVersion-bbbbbbb"; $C = "$SampleVersion-ccccccc"

# ---- guard: real locations, before -----------------------------------------------

$RealRoot = Join-Path $env:USERPROFILE 'Apps\JARVIS'
$RealDesktop = [Environment]::GetFolderPath([Environment+SpecialFolder]::Desktop)
$RealJarvisDir = Join-Path $env:USERPROFILE '.jarvis'
$RepoData = Join-Path $RepoRoot 'data'
$UvLock = Join-Path $RepoRoot 'uv.lock'
$guardBefore = @{
    root    = Get-Signature $RealRoot
    desktop = (@(Get-ChildItem -LiteralPath $RealDesktop -Force -File -ErrorAction SilentlyContinue | Where-Object { $_.Extension -ieq '.lnk' } | Sort-Object Name | ForEach-Object { "$($_.Name)|$($_.Length)|$($_.LastWriteTimeUtc.Ticks)" }) -join "`n")
    jarvis  = Get-Signature $RealJarvisDir
    data    = Get-Signature $RepoData
    uvlock  = $(if (Test-Path -LiteralPath $UvLock) { (Get-FileHash -LiteralPath $UvLock -Algorithm SHA256).Hash } else { '(absent)' })
}

# ==== 1. fresh managed install ==============================================================
Write-Host '== 1. fresh managed install'
$root1 = Join-Path (New-TempRoot) 'Apps\JARVIS'
$desk1 = Join-Path (New-TempRoot) 'Desktop'; $null = New-Item -ItemType Directory -Path $desk1
$r = Install-Fake -Root $root1 -Id $A -Desktop $desk1
$layout1 = Get-JarvisReleaseLayout $root1
Check '1 release directory installed' (Test-Path -LiteralPath (Join-Path $layout1.ReleasesDir "$A\JARVIS.exe"))
Check '1 whole tree installed' (Test-Path -LiteralPath (Join-Path $layout1.ReleasesDir "$A\_internal\PySide6\payload.bin"))
Check '1 release.json written' (Test-Path -LiteralPath (Join-Path $layout1.ReleasesDir "$A\release.json"))
$cur = Resolve-JarvisCurrent -Root $root1
Check '1 current is a junction' ($cur.Exists -and $cur.IsJunction)
Check '1 current resolves to the release' ($cur.ReleaseId -eq $A -and -not $cur.Dangling)
Check '1 executable reachable through current' (Test-Path -LiteralPath (Join-Path $layout1.CurrentPath 'JARVIS.exe'))
$st = Read-State $root1
Check '1 state active / previous' ($st['active'] -eq $A -and $null -eq $st['previous'])
Check '1 state history: one install entry' (@($st['history']).Count -eq 1 -and $st['history'][0].action -eq 'install')
Check '1 result flags' ((-not $r.Reused) -and $r.Activated -and $null -eq $r.Migrated -and $null -eq $r.PreviouslyActive)
$meta = Read-JarvisReleaseMetadata -ReleasePath (Join-Path $layout1.ReleasesDir $A)
Check '1 metadata identity' ($meta.release_id -eq $A -and $meta.version -eq $SampleVersion -and $meta.commit -like 'commit-of-*' -and $meta.legacy -eq $false -and $meta.source -eq 'build')
$metaText = [System.IO.File]::ReadAllText((Join-Path $layout1.ReleasesDir "$A\release.json"))
Check '1 metadata holds no secrets or identities' (($metaText -notmatch 'token|password|secret|oauth|@') -and $metaText -match '"artifact"')
Check '1 no staging left' (@(Get-ChildItem -LiteralPath $layout1.ReleasesDir -Force | Where-Object { $_.Name -like '.staging-*' }).Count -eq 0)
$status = Get-JarvisReleaseStatus -Root $root1 -CheckExecutable $CheckExe
Check '1 status consistent' ($status.Consistent -and $status.Layout -eq 'managed' -and $status.Problems.Count -eq 0)
Check '1 shortcut created on the temp desktop' ($r.Shortcut.Created -and (Test-Path -LiteralPath (Join-Path $desk1 'JARVIS.lnk')))
$lnk = Get-JarvisShortcutStatus -Root $root1 -DesktopPath $desk1
Check '1 shortcut targets current\JARVIS.exe' ($lnk.Exists -and $lnk.Correct -and $lnk.Canonical.Target -ieq (Join-Path $layout1.CurrentPath 'JARVIS.exe') -and $lnk.Canonical.WorkingDirectory -ieq $layout1.CurrentPath)

# ==== 2. legacy flat-install migration ======================================================
Write-Host '== 2. legacy flat-install migration'
$root2 = Join-Path (New-TempRoot) 'Apps\JARVIS'
$null = New-FakePackage -Path $root2 -Stamp 'legacy-payload'
Check '2 flat layout detected' ((Get-JarvisLayoutKind $root2) -eq 'legacy-flat')
$sigBefore = Get-Signature $root2
$s2 = Get-JarvisReleaseStatus -Root $root2 -CheckExecutable $CheckExe
Check '2 status names the legacy layout and mutates nothing' ($s2.Layout -eq 'legacy-flat' -and (-not $s2.Consistent) -and ($s2.Problems -join ' ') -match 'legacy' -and (Get-Signature $root2) -eq $sigBefore)
$exeHash = (Get-FileHash -LiteralPath (Join-Path $root2 'JARVIS.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
$r2 = Install-Fake -Root $root2 -Id $A
$layout2 = Get-JarvisReleaseLayout $root2
Check '2 legacy release imported' ($null -ne $r2.Migrated -and $r2.Migrated.ReleaseId -like 'legacy-*' -and $r2.Migrated.ReleaseId.EndsWith($exeHash.Substring(0, 7)))
$legacyId = $r2.Migrated.ReleaseId
Check '2 legacy files preserved under releases' ((Get-Content -LiteralPath (Join-Path $layout2.ReleasesDir "$legacyId\_internal\PySide6\payload.bin")) -eq 'payload legacy-payload')
$lm = Read-JarvisReleaseMetadata -ReleasePath (Join-Path $layout2.ReleasesDir $legacyId)
Check '2 legacy metadata marked' ($lm.legacy -eq $true -and $lm.source -eq 'legacy-import' -and $lm.imported_from -ieq $layout2.Root -and $null -eq $lm.commit)
Check '2 current -> new release' ((Get-CurrentId $root2) -eq $A)
$st2 = Read-State $root2
Check '2 legacy is the rollback target' ($st2['previous'] -eq $legacyId -and $st2['active'] -eq $A)
Check '2 history: migrate then install' (@($st2['history']).Count -eq 2 -and $st2['history'][0].action -eq 'migrate' -and $st2['history'][1].action -eq 'install' -and $st2['history'][1].from -eq $legacyId)
Check '2 root is no longer flat' (-not (Test-Path -LiteralPath (Join-Path $layout2.Root 'JARVIS.exe')) -and (Get-JarvisLayoutKind $root2) -eq 'managed')
Check '2 no migration residue beside the root' (@(Get-ChildItem -LiteralPath (Split-Path -Parent $root2) -Force | Where-Object { $_.Name -like 'JARVIS.migrating-*' }).Count -eq 0)
$rb2 = Invoke-JarvisRollback -Root $root2 -CheckExecutable $CheckExe
Check '2 rollback to the legacy release works' ($rb2.ReleaseId -eq $legacyId -and (Get-CurrentId $root2) -eq $legacyId -and (Test-Path -LiteralPath (Join-Path $layout2.CurrentPath 'JARVIS.exe')))
$null = Invoke-JarvisActivate -Root $root2 -ReleaseId $A -CheckExecutable $CheckExe
# failure midway restores the flat installation
$root3 = Join-Path (New-TempRoot) 'Apps\JARVIS'
$null = New-FakePackage -Path $root3 -Stamp 'flat3'
$sig3 = Get-Signature $root3
Set-JarvisReleaseFailpoint 'migrate:after-move'
$msg = Throws { Install-Fake -Root $root3 -Id $A }
Set-JarvisReleaseFailpoint ''
Check '2 failed migration reports restoration' ($msg -match 'flat installation restored')
Check '2 failed migration: flat install back in place, identical' ((Get-JarvisLayoutKind $root3) -eq 'legacy-flat' -and (Get-Signature $root3) -eq $sig3)
Check '2 failed migration: no residue' (@(Get-ChildItem -LiteralPath (Split-Path -Parent $root3) -Force | Where-Object { $_.Name -ne 'JARVIS' }).Count -eq 0)
# a flat install carrying stray runtime data (seen on the real machine) still migrates, as it is, with a warning
$root2b = Join-Path (New-TempRoot) 'Apps\JARVIS'
$null = New-FakePackage -Path $root2b -Stamp 'with-db'
$null = New-Item -ItemType Directory -Path (Join-Path $root2b '_internal\data')
Set-Content -LiteralPath (Join-Path $root2b '_internal\data\jarvis.db') -Value 'sqlite' -Encoding Ascii
$r2b = Install-Fake -Root $root2b -Id $A
$layout2b = Get-JarvisReleaseLayout $root2b
Check '2 stray runtime data does not block the import' ($null -ne $r2b.Migrated -and (Test-Path -LiteralPath (Join-Path $layout2b.ReleasesDir "$($r2b.Migrated.ReleaseId)\_internal\data\jarvis.db")))
Check '2 ...but is reported as a warning' (($r2b.Warnings -join ' ') -match 'kept as-is' -and ($r2b.Warnings -join ' ') -match 'jarvis.db')
$s2b = Get-JarvisReleaseStatus -Root $root2b -CheckExecutable $CheckExe
$legacyRel = $s2b.Releases | Where-Object { $_.ReleaseId -eq $r2b.Migrated.ReleaseId }
Check '2 imported release stays a valid rollback target' ($s2b.Consistent -and $legacyRel.Valid -and $legacyRel.Warnings.Count -eq 1 -and $s2b.Rollback.Valid)
Check '2 the list shows the warning' (((Format-JarvisReleaseList $s2b) -join "`n") -match 'warning: .*jarvis.db')
# a running legacy JARVIS blocks migration
Set-JarvisReleaseProcessSource -ScriptBlock ([scriptblock]::Create("@([pscustomobject]@{ ProcessId = 4242; Name = 'JARVIS.exe'; ExecutablePath = '$($root3 -replace "'", "''")\JARVIS.exe' })"))
$msg = Throws { Install-Fake -Root $root3 -Id $A }
Set-JarvisReleaseProcessSource -Reset
Check '2 running legacy JARVIS blocks migration' ($msg -match 'JARVIS is running' -and (Get-JarvisLayoutKind $root3) -eq 'legacy-flat' -and (Get-Signature $root3) -eq $sig3)

# ==== 3. successful activation ===============================================================
Write-Host '== 3. successful activation'
$null = Install-Fake -Root $root1 -Id $B -Desktop $desk1
Check '3 second install activates B, previous A' ((Get-CurrentId $root1) -eq $B -and (Read-State $root1)['previous'] -eq $A)
$act = Invoke-JarvisActivate -Root $root1 -ReleaseId $A -CheckExecutable $CheckExe
$st = Read-State $root1
Check '3 activate A: current, state, history' ($act.Changed -and (Get-CurrentId $root1) -eq $A -and $st['active'] -eq $A -and $st['previous'] -eq $B -and $st['history'][-1].action -eq 'activate' -and $st['history'][-1].from -eq $B)
Check '3 the release that was active is intact' ((Test-Path -LiteralPath (Join-Path $layout1.ReleasesDir "$B\JARVIS.exe")) -and (Test-Path -LiteralPath (Join-Path $layout1.ReleasesDir "$B\_internal\PySide6\payload.bin")))
Check '3 contents seen through current are A' ((Get-Content -LiteralPath (Join-Path $layout1.CurrentPath '_internal\PySide6\payload.bin')) -eq "payload $A")
$again = Invoke-JarvisActivate -Root $root1 -ReleaseId $A -CheckExecutable $CheckExe
Check '3 re-activating the active release is a no-op' ((-not $again.Changed) -and @((Read-State $root1)['history']).Count -eq @($st['history']).Count)
Check '3 no temporary junctions left' (@(Get-ChildItem -LiteralPath $layout1.Root -Force | Where-Object { $_.Name -like 'current.*' }).Count -eq 0)
Check '3 shortcut unchanged by activation' ((Get-JarvisShortcutStatus -Root $root1 -DesktopPath $desk1).Correct)

# ==== 4. failed activation preserves the old current =============================================
Write-Host '== 4. failed activation preserves old current'
$stateBefore = [System.IO.File]::ReadAllText($layout1.StatePath)
Set-JarvisReleaseFailpoint 'switch:after-swap'
$msg = Throws { Invoke-JarvisActivate -Root $root1 -ReleaseId $B -CheckExecutable $CheckExe }
Set-JarvisReleaseFailpoint ''
Check '4 failure after the swap is reported as restored' ($msg -match 'previous current restored')
Check '4 current still A' ((Get-CurrentId $root1) -eq $A -and (Test-Path -LiteralPath (Join-Path $layout1.CurrentPath 'JARVIS.exe')))
Check '4 state untouched' ([System.IO.File]::ReadAllText($layout1.StatePath) -eq $stateBefore)
Check '4 no temporary junctions left' (@(Get-ChildItem -LiteralPath $layout1.Root -Force | Where-Object { $_.Name -like 'current.*' }).Count -eq 0)
$bExe = Join-Path $layout1.ReleasesDir "$B\JARVIS.exe"
Move-Item -LiteralPath $bExe -Destination "$bExe.aside"
$msg = Throws { Invoke-JarvisActivate -Root $root1 -ReleaseId $B -CheckExecutable $CheckExe }
Move-Item -LiteralPath "$bExe.aside" -Destination $bExe
Check '4 invalid target refused before any change' ($msg -match 'not a valid installed release' -and (Get-CurrentId $root1) -eq $A -and [System.IO.File]::ReadAllText($layout1.StatePath) -eq $stateBefore)

# ==== 5. atomic state handling =================================================================
Write-Host '== 5. atomic state handling'
Set-JarvisReleaseFailpoint 'state:before-replace'
$msg = Throws { Invoke-JarvisActivate -Root $root1 -ReleaseId $B -CheckExecutable $CheckExe }
Set-JarvisReleaseFailpoint ''
Check '5 state write failure: old state intact' ([System.IO.File]::ReadAllText($layout1.StatePath) -eq $stateBefore)
Check '5 state write failure: no temp file left' (@(Get-ChildItem -LiteralPath $layout1.Root -Force -File | Where-Object { $_.Name -like 'release-state.json.tmp-*' }).Count -eq 0)
Check '5 state write failure: current restored to A' ((Get-CurrentId $root1) -eq $A -and $msg -match 'restored')
[System.IO.File]::WriteAllText($layout1.StatePath, '{ this is not json')
$s5 = Get-JarvisReleaseStatus -Root $root1 -CheckExecutable $CheckExe
Check '5 corrupt state is reported, not repaired' ((-not $s5.Consistent) -and ($s5.Problems -join ' ') -match 'not valid JSON' -and [System.IO.File]::ReadAllText($layout1.StatePath) -eq '{ this is not json')
$msg = Throws { Invoke-JarvisRollback -Root $root1 -CheckExecutable $CheckExe }
Check '5 rollback refuses on corrupt state' ($msg -match 'inconsistent')
[System.IO.File]::WriteAllText($layout1.StatePath, $stateBefore)
Check '5 state restored for the next tests' ((Get-JarvisReleaseStatus -Root $root1 -CheckExecutable $CheckExe).Consistent)

# ==== 6. same release install is idempotent ==================================================
Write-Host '== 6. idempotent reinstall'
$relSig = Get-Signature (Join-Path $layout1.ReleasesDir $A)
$histCount = @((Read-State $root1)['history']).Count
$r6 = Install-Fake -Root $root1 -Id $A -Stamp 'would-differ' -Desktop $desk1
Check '6 reused, not copied, still active' ($r6.Reused -and (-not $r6.Activated) -and (Get-CurrentId $root1) -eq $A)
Check '6 immutable release untouched' ((Get-Signature (Join-Path $layout1.ReleasesDir $A)) -eq $relSig)
Check '6 no staging, no history entry' (@(Get-ChildItem -LiteralPath $layout1.ReleasesDir -Force | Where-Object { $_.Name -like '.staging-*' }).Count -eq 0 -and @((Read-State $root1)['history']).Count -eq $histCount)

# ==== 7. conflicting immutable release rejected ==============================================
Write-Host '== 7. conflicting immutable release'
$msg = Throws { Install-Fake -Root $root1 -Id $A -Commit ('deadbeef'.PadRight(40, 'f')) }
Check '7 different commit under the same id is refused' ($msg -match 'NOT overwritten' -and $msg -match 'commit')
Check '7 release untouched, current unchanged' ((Get-Signature (Join-Path $layout1.ReleasesDir $A)) -eq $relSig -and (Get-CurrentId $root1) -eq $A)
$msg = Throws { Install-Fake -Root $root1 -Id $A -Version '9.9.9' }
Check '7 different version under the same id is refused' ($msg -match 'not valid|NOT overwritten')

# ==== 8. rollback ================================================================================
Write-Host '== 8. rollback'
$root5 = Join-Path (New-TempRoot) 'Apps\JARVIS'
$null = Install-Fake -Root $root5 -Id $A
$null = Install-Fake -Root $root5 -Id $B
$layout5 = Get-JarvisReleaseLayout $root5
$relSig5 = Get-Signature $layout5.ReleasesDir
$rb = Invoke-JarvisRollback -Root $root5 -CheckExecutable $CheckExe
$st = Read-State $root5
Check '8 rollback B -> A' ($rb.ReleaseId -eq $A -and $rb.From -eq $B -and (Get-CurrentId $root5) -eq $A -and $st['active'] -eq $A -and $st['previous'] -eq $B -and $st['history'][-1].action -eq 'rollback')
$rb = Invoke-JarvisRollback -Root $root5 -CheckExecutable $CheckExe
Check '8 rollback is reversible: A -> B' ($rb.ReleaseId -eq $B -and (Get-CurrentId $root5) -eq $B -and (Read-State $root5)['previous'] -eq $A)
Check '8 rollback copied nothing' ((Get-Signature $layout5.ReleasesDir) -eq $relSig5)

# ==== 9. rollback with no candidate ==============================================================
Write-Host '== 9. rollback with no candidate'
$root6 = Join-Path (New-TempRoot) 'Apps\JARVIS'
$null = Install-Fake -Root $root6 -Id $A
$layout6 = Get-JarvisReleaseLayout $root6
$before6 = [System.IO.File]::ReadAllText($layout6.StatePath)
$msg = Throws { Invoke-JarvisRollback -Root $root6 -CheckExecutable $CheckExe }
Check '9 refused with a clear message' ($msg -match 'no previous release')
Check '9 nothing changed' ((Get-CurrentId $root6) -eq $A -and [System.IO.File]::ReadAllText($layout6.StatePath) -eq $before6)
$root6b = Join-Path (New-TempRoot) 'Apps\JARVIS'
$msg = Throws { Invoke-JarvisRollback -Root $root6b -CheckExecutable $CheckExe }
Check '9 no installation at all is refused' ($msg -match 'no managed installation')

# ==== 10. status / list are read-only =========================================================
Write-Host '== 10. status/list read-only'
$sig10 = Get-Signature $root5
$s10 = Get-JarvisReleaseStatus -Root $root5 -CheckExecutable $CheckExe
$textStatus = (Format-JarvisReleaseStatus $s10) -join "`n"
$textList = (Format-JarvisReleaseList $s10) -join "`n"
$null = Get-JarvisInstalledReleases -Root $root5 -CheckExecutable $CheckExe
Check '10 status text' ($textStatus -match 'consistent:\s+YES' -and $textStatus -match [regex]::Escape("active (state):  $B") -and $textStatus -match 'rollback available')
Check '10 list text marks the active release' (($textList -split "`n" | Where-Object { $_ -match "^\s*\*\s+$([regex]::Escape($B))" }).Count -eq 1 -and $textList -match "$([regex]::Escape($A)).*previous")
Check '10 in-process status/list mutated nothing' ((Get-Signature $root5) -eq $sig10)
$prevPref = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
$outStatus = & powershell -NoProfile -ExecutionPolicy Bypass -File $ReleasePs -Status -InstallRoot $root5 2>&1; $codeStatus = $LASTEXITCODE
$outList = & powershell -NoProfile -ExecutionPolicy Bypass -File $ReleasePs -List -InstallRoot $root5 2>&1; $codeList = $LASTEXITCODE
$outUsage = & powershell -NoProfile -ExecutionPolicy Bypass -File $ReleasePs -InstallRoot $root5 2>&1; $codeUsage = $LASTEXITCODE
$ErrorActionPreference = $prevPref
Check '10 release.ps1 -Status exit 0 and names the root' ($codeStatus -eq 0 -and (($outStatus | Out-String) -match [regex]::Escape($root5)))
Check '10 release.ps1 -List exit 0 and lists both' ($codeList -eq 0 -and (($outList | Out-String) -match [regex]::Escape($A)) -and (($outList | Out-String) -match [regex]::Escape($B)))
Check '10 release.ps1 without a verb is usage (exit 2)' ($codeUsage -eq 2)
Check '10 release.ps1 status/list mutated nothing' ((Get-Signature $root5) -eq $sig10)
$rootLegacy = Join-Path (New-TempRoot) 'Apps\JARVIS'
$null = New-FakePackage -Path $rootLegacy -Stamp 'ro'
$sigL = Get-Signature $rootLegacy
$prevPref = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
$outL = & powershell -NoProfile -ExecutionPolicy Bypass -File $ReleasePs -Status -InstallRoot $rootLegacy 2>&1; $codeL = $LASTEXITCODE
$ErrorActionPreference = $prevPref
Check '10 status on a flat install reports it and does not migrate' ($codeL -eq 1 -and (($outL | Out-String) -match 'legacy') -and (Get-Signature $rootLegacy) -eq $sigL)

# ==== 11. shortcut logic ==========================================================================
Write-Host '== 11. desktop shortcut'
$desk5 = Join-Path (New-TempRoot) 'Desktop'; $null = New-Item -ItemType Directory -Path $desk5
$plan = Get-JarvisShortcutPlan -Root $root5 -DesktopPath $desk5
Check '11 plan: JARVIS.lnk -> current\JARVIS.exe, working dir current' ($plan.Path -ieq (Join-Path $desk5 'JARVIS.lnk') -and $plan.Target -ieq (Join-Path $layout5.CurrentPath 'JARVIS.exe') -and $plan.WorkingDirectory -ieq $layout5.CurrentPath)
$sc = Sync-JarvisDesktopShortcut -Root $root5 -DesktopPath $desk5
Check '11 missing -> created' ($sc.Created -and (Test-Path -LiteralPath $plan.Path))
$sc = Sync-JarvisDesktopShortcut -Root $root5 -DesktopPath $desk5
Check '11 correct -> verified, still exactly one' ($sc.Verified -and -not $sc.Created -and -not $sc.Updated -and $sc.Count -eq 1)
$wsh = New-Object -ComObject WScript.Shell
$stale = $wsh.CreateShortcut((Join-Path $desk5 'JARVIS - old.lnk')); $stale.TargetPath = (Join-Path $root5 'JARVIS.exe'); $stale.Save()
$foreign = $wsh.CreateShortcut((Join-Path $desk5 'Notepad.lnk')); $foreign.TargetPath = "$env:WINDIR\notepad.exe"; $foreign.Save()
$broken = $wsh.CreateShortcut($plan.Path); $broken.TargetPath = (Join-Path $root5 'JARVIS.exe'); $broken.WorkingDirectory = $root5; $broken.Save()
$sc = Sync-JarvisDesktopShortcut -Root $root5 -DesktopPath $desk5
$oldLnk = $wsh.CreateShortcut((Join-Path $desk5 'JARVIS - old.lnk'))
$notepad = $wsh.CreateShortcut((Join-Path $desk5 'Notepad.lnk'))
Check '11 wrong canonical -> updated' ($sc.Updated -and (Get-JarvisShortcutStatus -Root $root5 -DesktopPath $desk5).Correct)
Check '11 stale other JARVIS shortcut retargeted to current' ($sc.Retargeted.Count -eq 1 -and $oldLnk.TargetPath -ieq $plan.Target)
Check '11 foreign shortcut untouched' ($notepad.TargetPath -ieq "$env:WINDIR\notepad.exe")
Check '11 never a second JARVIS.lnk' (@(Get-ChildItem -LiteralPath $desk5 -Force -File | Where-Object { $_.Name -ieq 'JARVIS.lnk' }).Count -eq 1)

# ==== 12/13. retention ==========================================================================
Write-Host '== 12/13. retention'
$root7 = Join-Path (New-TempRoot) 'Apps\JARVIS'
$layout7 = Get-JarvisReleaseLayout $root7
$ids = 1..5 | ForEach-Object { "$SampleVersion-r$('{0:d6}' -f $_)" }
$last = $null
foreach ($id in $ids) { $last = Install-Fake -Root $root7 -Id $id; Start-Sleep -Milliseconds 30 }
$present = @(Get-ChildItem -LiteralPath $layout7.ReleasesDir -Directory -Force | ForEach-Object { $_.Name } | Sort-Object)
Check '13 five installs leave active + previous 2' (($present -join ',') -eq (($ids[2..4] | Sort-Object) -join ','))
# r1 was already pruned by the r4 install (keep r4, r3, r2); the r5 install prunes r2.
Check '13 the pruned one is reported' ((@($last.Retention.Removed) -join ',') -eq $ids[1])
Check '13 active and previous are in the kept set' ($last.Retention.Kept -contains $ids[4] -and $last.Retention.Kept -contains $ids[3] -and (Get-CurrentId $root7) -eq $ids[4])
$null = Invoke-JarvisActivate -Root $root7 -ReleaseId $ids[2] -CheckExecutable $CheckExe
$r6th = Install-Fake -Root $root7 -Id "$SampleVersion-r000006"
$present = @(Get-ChildItem -LiteralPath $layout7.ReleasesDir -Directory -Force | ForEach-Object { $_.Name } | Sort-Object)
Check '13 recency order: previous and most recently active kept' (($present -join ',') -eq ((@($ids[2], $ids[4], "$SampleVersion-r000006") | Sort-Object) -join ',') -and $r6th.Retention.Removed -contains $ids[3])
$ret = Invoke-JarvisRetention -Root $root7 -Keep 1 -CheckExecutable $CheckExe
$present = @(Get-ChildItem -LiteralPath $layout7.ReleasesDir -Directory -Force | ForEach-Object { $_.Name } | Sort-Object)
Check '12 active and rollback target survive even Keep=1' (($present -join ',') -eq ((@($ids[2], "$SampleVersion-r000006") | Sort-Object) -join ',') -and (Get-CurrentId $root7) -eq "$SampleVersion-r000006")
$junk = Join-Path $layout7.ReleasesDir 'not a release'
$null = New-Item -ItemType Directory -Path $junk
$ret = Invoke-JarvisRetention -Root $root7 -Keep 1 -CheckExecutable $CheckExe
Check '12 unrecognised entries are left in place with a warning' ((Test-Path -LiteralPath $junk) -and ($ret.Warnings -join ' ') -match 'unrecognised')
$badState = Join-Path $layout7.Root 'release-state.json'
$keepState = [System.IO.File]::ReadAllText($badState)
[System.IO.File]::WriteAllText($badState, '[]')
$ret = Invoke-JarvisRetention -Root $root7 -Keep 1 -CheckExecutable $CheckExe
[System.IO.File]::WriteAllText($badState, $keepState)
Check '12 retention skips an inconsistent manager' (($ret.Warnings -join ' ') -match 'skipped' -and $ret.Removed.Count -eq 0)

# ==== 14. unsafe release ids ======================================================================
Write-Host '== 14. unsafe release ids'
$bad = @('..', '.', '../x', '..\x', 'a/b', 'a\b', '.staging-x', '', 'C:\x', 'a b', "$SampleVersion-aaaaaaa/../..", '-x')
$allRefused = $true
foreach ($id in $bad) {
    if (Test-JarvisReleaseId $id) { $allRefused = $false; Write-Host "    accepted bad id '$id'" }
    if ((Throws { Get-JarvisReleasePath -Root $root5 -ReleaseId $id }) -eq '') { $allRefused = $false; Write-Host "    path resolved for bad id '$id'" }
}
Check '14 traversal / malformed ids refused' $allRefused
Check '14 good ids accepted' ((Test-JarvisReleaseId '1.0.0-71ba549') -and (Test-JarvisReleaseId 'legacy-1.0.0-5976769') -and (Get-JarvisReleasePath -Root $root5 -ReleaseId $A) -ieq (Join-Path $layout5.ReleasesDir $A))
$sig14 = Get-Signature $root5
$msg = Throws { Invoke-JarvisActivate -Root $root5 -ReleaseId '..\..\evil' -CheckExecutable $CheckExe }
Check '14 activate with a traversal id is refused untouched' ($msg -match 'invalid release id' -and (Get-Signature $root5) -eq $sig14)

# ==== 15. stale / broken current ==================================================================
Write-Host '== 15. stale/broken current'
$root8 = Join-Path (New-TempRoot) 'Apps\JARVIS'
$null = Install-Fake -Root $root8 -Id $A
$null = Install-Fake -Root $root8 -Id $B
$layout8 = Get-JarvisReleaseLayout $root8
[System.IO.Directory]::Delete((Join-Path $layout8.ReleasesDir $B), $true)
$s15 = Get-JarvisReleaseStatus -Root $root8 -CheckExecutable $CheckExe
Check '15 dangling current detected' ($s15.Current.Dangling -and (-not $s15.Consistent) -and ($s15.Problems -join ' ') -match 'missing')
$msg = Throws { Invoke-JarvisRollback -Root $root8 -CheckExecutable $CheckExe }
Check '15 rollback refuses on a broken current' ($msg -match 'inconsistent')
$msg = Throws { Install-Fake -Root $root8 -Id $C }
Check '15 install refuses on a broken current' ($msg -match 'inconsistent' -and -not (Test-Path -LiteralPath (Join-Path $layout8.ReleasesDir $C)))
$act = Invoke-JarvisActivate -Root $root8 -ReleaseId $A -CheckExecutable $CheckExe
$st = Read-State $root8
Check '15 explicit activate repairs unambiguously' ($act.Changed -and (Get-CurrentId $root8) -eq $A -and $st['active'] -eq $A -and $null -eq $st['previous'] -and (Get-JarvisReleaseStatus -Root $root8 -CheckExecutable $CheckExe).Consistent)
Check '15 no temporary junctions left' (@(Get-ChildItem -LiteralPath $layout8.Root -Force | Where-Object { $_.Name -like 'current.*' }).Count -eq 0)
$dir8 = Join-Path $layout8.Root 'current'
[System.IO.Directory]::Delete($dir8)
$null = New-Item -ItemType Directory -Path $dir8
$s15b = Get-JarvisReleaseStatus -Root $root8 -CheckExecutable $CheckExe
$msg = Throws { Invoke-JarvisActivate -Root $root8 -ReleaseId $A -CheckExecutable $CheckExe }
Check '15 a real directory named current is refused, never deleted' ((-not $s15b.Consistent) -and ($s15b.Problems -join ' ') -match 'not a junction' -and $msg -match 'not a junction|inconsistent' -and (Test-Path -LiteralPath $dir8) -and -not (Test-JarvisReleaseId (Resolve-JarvisCurrent -Root $root8).ReleaseId))

# ==== 16. state / current mismatch ==============================================================
Write-Host '== 16. state/current mismatch'
$root9 = Join-Path (New-TempRoot) 'Apps\JARVIS'
$null = Install-Fake -Root $root9 -Id $A
$null = Install-Fake -Root $root9 -Id $B
$layout9 = Get-JarvisReleaseLayout $root9
[System.IO.Directory]::Delete($layout9.CurrentPath)
$null = New-Item -ItemType Junction -Path $layout9.CurrentPath -Target (Join-Path $layout9.ReleasesDir $A)
$s16 = Get-JarvisReleaseStatus -Root $root9 -CheckExecutable $CheckExe
Check '16 mismatch detected' ((-not $s16.Consistent) -and ($s16.Problems -join ' ') -match "state says '$([regex]::Escape($B))'.*points to '$([regex]::Escape($A))'")
$sig16 = Get-Signature $layout9.ReleasesDir
$m1 = Throws { Invoke-JarvisRollback -Root $root9 -CheckExecutable $CheckExe }
$m2 = Throws { Install-Fake -Root $root9 -Id $C }
$m3 = Throws { Invoke-JarvisActivate -Root $root9 -ReleaseId $A -CheckExecutable $CheckExe }
Check '16 rollback / install / activate all refuse' ($m1 -match 'inconsistent' -and $m2 -match 'inconsistent' -and $m3 -match 'inconsistent')
Check '16 nothing changed' ((Get-Signature $layout9.ReleasesDir) -eq $sig16 -and (Get-CurrentId $root9) -eq $A -and (Read-State $root9)['active'] -eq $B)
[System.IO.Directory]::Delete($layout9.CurrentPath)
$null = New-Item -ItemType Junction -Path $layout9.CurrentPath -Target (Join-Path $layout9.ReleasesDir $B)
Check '16 consistent again once current agrees' ((Get-JarvisReleaseStatus -Root $root9 -CheckExecutable $CheckExe).Consistent)

# ==== 17. running JARVIS refusal ==================================================================
Write-Host '== 17. running-JARVIS refusal'
$runningExe = Join-Path $layout9.CurrentPath 'JARVIS.exe'
Set-JarvisReleaseProcessSource -ScriptBlock ([scriptblock]::Create("@([pscustomobject]@{ ProcessId = 4242; Name = 'JARVIS.exe'; ExecutablePath = '$($runningExe -replace "'", "''")' })"))
$running = @(Get-JarvisRunningInstalledProcess -Root $root9)
Check '17 installed process detected by path' ($running.Count -eq 1 -and $running[0].ProcessId -eq 4242)
$m1 = Throws { Invoke-JarvisRollback -Root $root9 -CheckExecutable $CheckExe }
$m2 = Throws { Invoke-JarvisActivate -Root $root9 -ReleaseId $A -CheckExecutable $CheckExe }
$m3 = Throws { Install-Fake -Root $root9 -Id $C }
Check '17 rollback / activate / install refuse with an actionable message' (($m1 -match 'JARVIS is running' -and $m1 -match 'Quit JARVIS') -and $m2 -match 'JARVIS is running' -and $m3 -match 'JARVIS is running')
Check '17 nothing changed while running' ((Get-CurrentId $root9) -eq $B -and (Read-State $root9)['active'] -eq $B -and -not (Test-Path -LiteralPath (Join-Path $layout9.ReleasesDir $C)))
Set-JarvisReleaseProcessSource -ScriptBlock ([scriptblock]::Create("@([pscustomobject]@{ ProcessId = 4343; Name = 'JARVIS.exe'; ExecutablePath = '$(($env:TEMP -replace "'", "''"))\somewhere-else\JARVIS.exe' })"))
Check '17 a JARVIS.exe elsewhere does not block' (@(Get-JarvisRunningInstalledProcess -Root $root9).Count -eq 0)
Set-JarvisReleaseProcessSource -ScriptBlock { @([pscustomobject]@{ ProcessId = 4444; Name = 'JARVIS.exe'; ExecutablePath = $null }) }
Check '17 an unreadable process path blocks (conservative)' (@(Get-JarvisRunningInstalledProcess -Root $root9).Count -eq 1)
Set-JarvisReleaseProcessSource -Reset
Check '17 real process source: none running from the temp root' (@(Get-JarvisRunningInstalledProcess -Root $root9).Count -eq 0)

# ==== 18. staging cleanup / failure ==============================================================
Write-Host '== 18. staging cleanup and failures'
$root10 = Join-Path (New-TempRoot) 'Apps\JARVIS'
$null = Install-Fake -Root $root10 -Id $A
$layout10 = Get-JarvisReleaseLayout $root10
$stale = Join-Path $layout10.ReleasesDir '.staging-old-abc'
$null = New-Item -ItemType Directory -Path (Join-Path $stale 'sub')
Set-Content -LiteralPath (Join-Path $stale 'sub\f.txt') -Value 'stale' -Encoding Ascii
$strayLink = Join-Path $layout10.Root 'current.old-deadbeef'
$null = New-Item -ItemType Junction -Path $strayLink -Target (Join-Path $layout10.ReleasesDir $A)
$r18 = Install-Fake -Root $root10 -Id $B
Check '18 stale staging and stray junction cleaned' ((-not (Test-Path -LiteralPath $stale)) -and (-not (Test-Path -LiteralPath $strayLink)) -and $r18.StagingCleaned.Count -eq 2)
Check '18 release A intact after removing the stray junction' (Test-Path -LiteralPath (Join-Path $layout10.ReleasesDir "$A\JARVIS.exe"))
$stateBefore18 = [System.IO.File]::ReadAllText($layout10.StatePath)
foreach ($fp in 'install:after-copy', 'install:before-finalize') {
    Set-JarvisReleaseFailpoint $fp
    $msg = Throws { Install-Fake -Root $root10 -Id $C }
    Set-JarvisReleaseFailpoint ''
    Check "18 $fp -> no release, no staging, current unchanged" ($msg -match 'injected' -and -not (Test-Path -LiteralPath (Join-Path $layout10.ReleasesDir $C)) -and @(Get-ChildItem -LiteralPath $layout10.ReleasesDir -Force | Where-Object { $_.Name -like '.staging-*' }).Count -eq 0 -and (Get-CurrentId $root10) -eq $B -and [System.IO.File]::ReadAllText($layout10.StatePath) -eq $stateBefore18)
}
$srcBad = New-FakePackage -Path (Join-Path (New-TempRoot) 'dist\JARVIS') -Stamp 'bad'
Remove-Item -LiteralPath (Join-Path $srcBad '_internal\jarvis\ui\qml\Main.qml') -Force
$msg = Throws { Install-JarvisRelease -Root $root10 -SourcePath $srcBad -ReleaseId $C -Version $SampleVersion -Commit 'c' -NoShortcut -CheckExecutable $CheckExe }
Check '18 an incomplete build is refused before anything is touched' ($msg -match 'not valid' -and $msg -match 'Main.qml' -and -not (Test-Path -LiteralPath (Join-Path $layout10.ReleasesDir $C)) -and (Get-CurrentId $root10) -eq $B)

# ==== 19. release identity from a git tree ======================================================
Write-Host '== 19. release identity and the dirty-tree rule'
$repo = New-TempRoot
function GitRun {
    # git writes hints to stderr; keep those from becoming terminating errors.
    param([string[]]$GitArgs)
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $out = & git -C $repo @GitArgs 2>&1; $code = $LASTEXITCODE } finally { $ErrorActionPreference = $prev }
    return [pscustomobject]@{ Code = $code; Text = ((@($out) | ForEach-Object { $_.ToString() }) -join "`n") }
}
$gitOk = $false
try {
    $gitOk = ((GitRun @('init', '-q')).Code -eq 0)
    $null = GitRun @('config', 'user.email', 't@example.invalid')
    $null = GitRun @('config', 'user.name', 'tests')
    Set-Content -LiteralPath (Join-Path $repo 'pyproject.toml') -Value "[build-system]`nrequires = [`"x`"]`n`n[project]`nname = `"t`"`nversion = `"9.9.9`"`n" -Encoding Ascii
    $null = New-Item -ItemType Directory -Path (Join-Path $repo 'jarvis')
    Set-Content -LiteralPath (Join-Path $repo 'jarvis\a.py') -Value 'x = 1' -Encoding Ascii
    $null = GitRun @('add', '-A')
    $gitOk = $gitOk -and ((GitRun @('commit', '-q', '-m', 'init')).Code -eq 0)
} catch { $gitOk = $false }
if ($gitOk) {
    $short = (GitRun @('rev-parse', '--short', 'HEAD')).Text.Trim()
    $idn = Get-JarvisReleaseIdentity -ProjectRoot $repo
    Check '19 clean tree -> <version>-<short sha>' ($idn.ReleaseId -eq "9.9.9-$short" -and $idn.Version -eq '9.9.9' -and $idn.Commit.Length -eq 40 -and $idn.AllowedDirty.Count -eq 0)
    Set-Content -LiteralPath (Join-Path $repo 'uv.lock') -Value 'lock' -Encoding Ascii
    $null = New-Item -ItemType Directory -Path (Join-Path $repo 'dev\dashboard')
    Set-Content -LiteralPath (Join-Path $repo 'dev\dashboard\status.json') -Value '{}' -Encoding Ascii
    $idn = Get-JarvisReleaseIdentity -ProjectRoot $repo
    Check '19 uv.lock and dev/ dirt are known non-release changes' ($idn.ReleaseId -eq "9.9.9-$short" -and $idn.AllowedDirty.Count -eq 2)
    Set-Content -LiteralPath (Join-Path $repo 'jarvis\new.py') -Value 'y = 2' -Encoding Ascii
    $msg = Throws { Get-JarvisReleaseIdentity -ProjectRoot $repo }
    Check '19 untracked source blocks a commit-identified install' ($msg -match 'uncommitted release-relevant' -and $msg -match 'jarvis/new.py')
    Remove-Item -LiteralPath (Join-Path $repo 'jarvis\new.py') -Force
    Add-Content -LiteralPath (Join-Path $repo 'jarvis\a.py') -Value 'z = 3' -Encoding Ascii
    $msg = Throws { Get-JarvisReleaseIdentity -ProjectRoot $repo }
    Check '19 modified tracked source blocks too' ($msg -match 'jarvis/a.py')
    $null = GitRun @('commit', '-q', '-am', 'change')
    $short2 = (GitRun @('rev-parse', '--short', 'HEAD')).Text.Trim()
    $idn = Get-JarvisReleaseIdentity -ProjectRoot $repo
    Check '19 committing yields a new id' ($idn.ReleaseId -eq "9.9.9-$short2" -and $short2 -ne $short)
}
else { Write-Host '  (git unavailable: section 19 skipped)' -ForegroundColor Yellow }

# ==== 20. package validation ======================================================================
Write-Host '== 20. package validation'
$pkg = New-FakePackage -Path (Join-Path (New-TempRoot) 'dist\JARVIS') -Stamp 'v'
$v20 = Test-JarvisReleasePackage -Path $pkg -ExpectedVersion $SampleVersion -CheckExecutable $CheckExe
Check '20 a complete package validates' ($v20.Valid)
if ($CheckExe) {
    Check '20 real exe: GUI subsystem and version resource read' ($v20.Executable.Subsystem -eq 2 -and $v20.Executable.FileVersion -like "$SampleVersion*")
    $bad = Test-JarvisReleasePackage -Path $pkg -ExpectedVersion '0.0.1' -CheckExecutable $true
    Check '20 real exe: version drift is a problem' ((-not $bad.Valid) -and ($bad.Problems -join ' ') -match 'does not match package version')
}
Set-Content -LiteralPath (Join-Path $pkg '_internal\conversation.json') -Value '{}' -Encoding Ascii
$null = New-Item -ItemType Junction -Path (Join-Path $pkg '_internal\link') -Target (Join-Path $pkg '_internal\PySide6')
Remove-Item -LiteralPath (Join-Path $pkg '_internal\jarvis\ui\qml\Main.qml') -Force
$v20 = Test-JarvisReleasePackage -Path $pkg -CheckExecutable $false
$probs = $v20.Problems -join ' '
Check '20 operator data, reparse points and missing bundles are problems' ((-not $v20.Valid) -and $probs -match 'forbidden file' -and $probs -match 'reparse point' -and $probs -match 'missing bundled jarvis\\ui\\qml\\Main.qml')
[System.IO.Directory]::Delete((Join-Path $pkg '_internal\link'))

# ---- guard: real locations, after -------------------------------------------------------------
Write-Host '== guard: real locations untouched'
$guardAfter = @{
    root    = Get-Signature $RealRoot
    desktop = (@(Get-ChildItem -LiteralPath $RealDesktop -Force -File -ErrorAction SilentlyContinue | Where-Object { $_.Extension -ieq '.lnk' } | Sort-Object Name | ForEach-Object { "$($_.Name)|$($_.Length)|$($_.LastWriteTimeUtc.Ticks)" }) -join "`n")
    jarvis  = Get-Signature $RealJarvisDir
    data    = Get-Signature $RepoData
    uvlock  = $(if (Test-Path -LiteralPath $UvLock) { (Get-FileHash -LiteralPath $UvLock -Algorithm SHA256).Hash } else { '(absent)' })
}
foreach ($k in 'root', 'desktop', 'jarvis', 'data', 'uvlock') { Check "guard: real $k untouched" ($guardBefore[$k] -eq $guardAfter[$k]) }

# ---- summary --------------------------------------------------------------------------------------
Set-JarvisReleaseFailpoint ''
Set-JarvisReleaseProcessSource -Reset
foreach ($t in $script:tempRoots) {
    # .NET's recursive delete refuses a tree that still contains a junction:
    # unlink every junction first (link only, never its target), then delete.
    try {
        foreach ($link in @(Get-ChildItem -LiteralPath $t -Recurse -Force -ErrorAction SilentlyContinue -Attributes ReparsePoint)) { [System.IO.Directory]::Delete($link.FullName) }
        foreach ($ro in @(Get-ChildItem -LiteralPath $t -Recurse -Force -File -ErrorAction SilentlyContinue -Attributes ReadOnly)) { $ro.IsReadOnly = $false }   # git objects
        [System.IO.Directory]::Delete($t, $true)
    } catch { Write-Host "  (could not remove temp root: $t)" }
}

Write-Host ''
Write-Host "RESULT: $script:pass passed, $script:fail failed"
if ($script:fail -gt 0) {
    $script:failures | ForEach-Object { Write-Host "  FAILED: $_" -ForegroundColor Red }
    exit 1
}
exit 0
