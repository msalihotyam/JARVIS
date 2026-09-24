#Requires -Version 5.1
<#
.SYNOPSIS
  JARVIS local Windows release manager (developer/operator tooling only).

.LAYOUT
  <root> = %USERPROFILE%\Apps\JARVIS  (override: -InstallRoot on the entry points)
    current\             one Windows directory JUNCTION to the active release
    releases\<id>\       one complete PyInstaller --onedir tree + release.json
    release-state.json   { schema, active, previous, updated_at, history[] }

  The Desktop shortcut (JARVIS.lnk) targets <root>\current\JARVIS.exe forever;
  activation only re-points the junction, so the shortcut never changes.

.RELEASE ID
  <package version from pyproject.toml>-<git short sha>, e.g. 1.0.0-71ba549.
  An imported pre-manager flat install is legacy-<exe version>-<exe sha256[0:7]>.
  A release directory is immutable: the same id is validated and reused, a
  conflicting directory under that id is an error, never an overwrite.

.DIRTY TREE RULE
  `git status --porcelain --untracked-files=all` must list ONLY paths under the
  known non-release set (uv.lock, dev/). Anything else is release-relevant
  source that would be packaged under a commit id it does not belong to, so
  identity resolution refuses. Nothing is ever stashed, reset or cleaned.

.CONSISTENCY
  release-state.json and the current junction are compared, never repaired by
  guesswork: -Status reports a disagreement; Install and Rollback refuse on
  it; an explicit -Activate <id> is honoured only when current is absent or
  dangling (the target is named by the operator, so the correction is not a
  guess).

.RETENTION
  After a successful activation: keep the active release, the rollback target
  and the most recently active others up to 3 in total; prune the rest.
  Only direct children of releases\ that validate as managed releases are
  ever deleted, and never one containing a reparse point.

.TEST HOOKS
  Set-JarvisReleaseProcessSource / Set-JarvisReleaseFailpoint exist for the
  automated tests only (fake process lists, injected failures).
#>

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:StateSchema = 1
$script:MetadataSchema = 1
$script:StateFileName = 'release-state.json'
$script:MetadataFileName = 'release.json'
$script:CurrentName = 'current'
$script:ReleasesName = 'releases'
$script:ExecutableName = 'JARVIS.exe'
$script:InternalName = '_internal'
$script:StagingPrefix = '.staging-'
$script:ReleaseIdPattern = '^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$'
$script:DefaultKeep = 3
$script:HistoryLimit = 50
# Known NON-release dirtiness in the canonical tree: the sacred untracked
# lockfile and the developer dashboard (neither is packaged). Everything else
# that git reports dirty blocks a commit-identified install.
$script:AllowedDirtyPrefixes = @('uv.lock', 'dev/')
# Files that must never travel inside a package (operator state, credentials).
$script:ForbiddenFileNames = @('conversation.json', 'ui-preferences.json', 'provider-catalog.json', 'weather-location.json',
    'uv.lock', 'credentials.enc', 'token_cache.json', 'client_secret.json', 'jarvis.db', 'jarvis-credential-attestation.json')
$script:ForbiddenDirNames = @('browser-profile', 'gws-gmail', '.jarvis', '.git')
# What the spec bundles; a package missing one of these cannot start.
$script:RequiredBundled = @('jarvis\ui\qml\Main.qml', 'jarvis\ui\qml\theme', 'jarvis\ui\qml\components', 'jarvis\ui\qml\pages',
    'jarvis\ui\qml\overlays', 'jarvis\ui\qml\fonts', 'jarvis\ui\qml\assets\app.ico', 'jarvis\ui\i18n\jarvis_tr-TR.qm')

$script:ProcessSource = {
    Get-CimInstance -ClassName Win32_Process -Filter "Name = 'JARVIS.exe'" -ErrorAction Stop |
        ForEach-Object { [pscustomobject]@{ ProcessId = $_.ProcessId; Name = $_.Name; ExecutablePath = $_.ExecutablePath } }
}
$script:Failpoint = ''

# ---- small helpers -----------------------------------------------------------

function Set-JarvisReleaseProcessSource {
    param([scriptblock]$ScriptBlock, [switch]$Reset)
    if ($Reset) {
        $script:ProcessSource = {
            Get-CimInstance -ClassName Win32_Process -Filter "Name = 'JARVIS.exe'" -ErrorAction Stop |
                ForEach-Object { [pscustomobject]@{ ProcessId = $_.ProcessId; Name = $_.Name; ExecutablePath = $_.ExecutablePath } }
        }
    }
    else { $script:ProcessSource = $ScriptBlock }
}

function Set-JarvisReleaseFailpoint { param([string]$Name = '') $script:Failpoint = $Name }

function Invoke-JarvisFailpoint { param([string]$Name) if ($script:Failpoint -eq $Name) { throw "injected failure at '$Name'" } }

function ConvertTo-JarvisFullPath { param([string]$Path) return ([System.IO.Path]::GetFullPath($Path)).TrimEnd('\') }

function Test-JarvisSamePath { param([string]$A, [string]$B) return ((ConvertTo-JarvisFullPath $A) -ieq (ConvertTo-JarvisFullPath $B)) }

function Test-JarvisPathUnder {
    param([string]$Path, [string]$Parent)
    $p = ConvertTo-JarvisFullPath $Path
    $base = (ConvertTo-JarvisFullPath $Parent) + '\'
    return $p.StartsWith($base, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-JarvisProp {
    param($Object, [string]$Name, $Default = $null)
    if ($null -eq $Object) { return $Default }
    $prop = $Object.PSObject.Properties[$Name]
    if ($null -eq $prop) { return $Default }
    return $prop.Value
}

function Get-JarvisUtcNow { return [DateTime]::UtcNow.ToString('o') }

function Write-JarvisTextFile {
    param([string]$Path, [string]$Text)
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function Test-JarvisReparsePoint {
    param([string]$Path)
    $item = Get-Item -LiteralPath $Path -Force
    return (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)
}

function Invoke-JarvisNative {
    # Run an external command with stderr kept apart from $ErrorActionPreference
    # (a stderr line must not become a terminating error) and UTF-8 decoding.
    param([string]$Command, [string[]]$Arguments)
    $prevPref = $ErrorActionPreference
    $prevEnc = [Console]::OutputEncoding
    $ErrorActionPreference = 'Continue'
    try {
        try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
        $raw = & $Command @Arguments 2>&1
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $prevPref
        try { [Console]::OutputEncoding = $prevEnc } catch { }
    }
    $lines = @(@($raw) | ForEach-Object { $_.ToString() })
    return [pscustomobject]@{ ExitCode = $code; Lines = $lines; Text = ($lines -join "`n") }
}

# ---- layout ------------------------------------------------------------------

function Get-JarvisReleaseRoot {
    param([string]$InstallRoot = '')
    if ($InstallRoot) { return ConvertTo-JarvisFullPath $InstallRoot }
    return ConvertTo-JarvisFullPath (Join-Path $env:USERPROFILE 'Apps\JARVIS')
}

function Get-JarvisReleaseLayout {
    param([Parameter(Mandatory = $true)][string]$Root)
    $r = ConvertTo-JarvisFullPath $Root
    return [pscustomobject]@{
        Root        = $r
        ReleasesDir = Join-Path $r $script:ReleasesName
        CurrentPath = Join-Path $r $script:CurrentName
        StatePath   = Join-Path $r $script:StateFileName
    }
}

function Test-JarvisReleaseId {
    param([string]$ReleaseId)
    if ([string]::IsNullOrEmpty($ReleaseId)) { return $false }
    if ($ReleaseId -eq '.' -or $ReleaseId -eq '..') { return $false }
    return [bool]($ReleaseId -match $script:ReleaseIdPattern)
}

function Get-JarvisReleasePath {
    # The one place a release id becomes a path: grammar first, then the
    # resolved path must be a direct child of releases\ - no traversal, no
    # absolute paths, no staging names.
    param([Parameter(Mandatory = $true)][string]$Root, [Parameter(Mandatory = $true)][string]$ReleaseId)
    if (-not (Test-JarvisReleaseId $ReleaseId)) { throw "invalid release id '$ReleaseId'" }
    $layout = Get-JarvisReleaseLayout $Root
    $path = ConvertTo-JarvisFullPath (Join-Path $layout.ReleasesDir $ReleaseId)
    $parent = Split-Path -Parent $path
    if (-not (Test-JarvisSamePath $parent $layout.ReleasesDir) -or ((Split-Path -Leaf $path) -ne $ReleaseId)) {
        throw "release id '$ReleaseId' does not resolve inside the releases directory"
    }
    return $path
}

function Get-JarvisLayoutKind {
    # absent | empty | legacy-flat | managed | unknown
    param([Parameter(Mandatory = $true)][string]$Root)
    $layout = Get-JarvisReleaseLayout $Root
    if (-not (Test-Path -LiteralPath $layout.Root)) { return 'absent' }
    $hasManaged = (Test-Path -LiteralPath $layout.ReleasesDir) -or (Test-Path -LiteralPath $layout.CurrentPath) -or (Test-Path -LiteralPath $layout.StatePath)
    if ($hasManaged) { return 'managed' }
    $exe = Join-Path $layout.Root $script:ExecutableName
    if ((Test-Path -LiteralPath $exe) -and (Test-Path -LiteralPath (Join-Path $layout.Root $script:InternalName))) { return 'legacy-flat' }
    $entries = @(Get-ChildItem -LiteralPath $layout.Root -Force)
    if ($entries.Count -eq 0) { return 'empty' }
    return 'unknown'
}

# ---- identity (repo -> release id) ------------------------------------------

function Get-JarvisPackageVersion {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)
    $pyproject = Join-Path $ProjectRoot 'pyproject.toml'
    if (-not (Test-Path -LiteralPath $pyproject)) { throw "pyproject.toml not found under $ProjectRoot" }
    $inProject = $false
    foreach ($line in [System.IO.File]::ReadAllLines($pyproject)) {
        $t = $line.Trim()
        if ($t -match '^\[(.+)\]$') { $inProject = ($Matches[1] -eq 'project'); continue }
        if ($inProject -and $t -match '^version\s*=\s*"([^"]+)"') { return $Matches[1] }
    }
    throw "no [project] version in $pyproject"
}

function Get-JarvisGitStatusPaths {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)
    $r = Invoke-JarvisNative 'git' @('-c', 'core.quotepath=false', '-C', $ProjectRoot, 'status', '--porcelain', '--untracked-files=all')
    if ($r.ExitCode -ne 0) { throw "git status failed in $ProjectRoot`: $($r.Text)" }
    $paths = @()
    foreach ($line in $r.Lines) {
        if ($line.Length -lt 4) { continue }
        $p = $line.Substring(3)
        if ($p -match '^(.*) -> (.*)$') { $paths += $Matches[1]; $paths += $Matches[2] } else { $paths += $p }
    }
    return @($paths | ForEach-Object { $_.Trim('"').Replace('\', '/') })
}

function Get-JarvisReleaseIdentity {
    # Version + commit -> immutable id. Throws when the tree carries
    # release-relevant uncommitted changes (see DIRTY TREE RULE).
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)
    $version = Get-JarvisPackageVersion $ProjectRoot
    $head = Invoke-JarvisNative 'git' @('-C', $ProjectRoot, 'rev-parse', 'HEAD')
    if ($head.ExitCode -ne 0) { throw "not a git repository (or no commit yet): $ProjectRoot" }
    $short = Invoke-JarvisNative 'git' @('-C', $ProjectRoot, 'rev-parse', '--short', 'HEAD')
    $branch = Invoke-JarvisNative 'git' @('-C', $ProjectRoot, 'rev-parse', '--abbrev-ref', 'HEAD')
    $dirty = @(Get-JarvisGitStatusPaths $ProjectRoot)
    $allowed = @(); $blocking = @()
    foreach ($p in $dirty) {
        $ok = $false
        foreach ($prefix in $script:AllowedDirtyPrefixes) { if ($p -eq $prefix.TrimEnd('/') -or $p.StartsWith($prefix)) { $ok = $true; break } }
        if ($ok) { $allowed += $p } else { $blocking += $p }
    }
    if ($blocking.Count -gt 0) {
        throw ("the working tree has uncommitted release-relevant changes; commit them (or leave them out) before installing " +
            "under commit $($short.Text): " + ($blocking -join ', '))
    }
    $id = "$version-$($short.Text.Trim())"
    if (-not (Test-JarvisReleaseId $id)) { throw "derived release id '$id' is not a valid id" }
    return [pscustomobject]@{
        Version      = $version
        Commit       = $head.Text.Trim()
        ShortCommit  = $short.Text.Trim()
        Branch       = $branch.Text.Trim()
        ReleaseId    = $id
        AllowedDirty = @($allowed)
    }
}

# ---- package validation -------------------------------------------------------

function Get-JarvisPeSubsystem {
    param([string]$Path)
    $fs = [System.IO.File]::OpenRead($Path)
    try {
        $br = New-Object System.IO.BinaryReader($fs)
        if ($fs.Length -lt 0x40) { return $null }
        $null = $fs.Seek(0x3C, [System.IO.SeekOrigin]::Begin)
        $lfanew = $br.ReadInt32()
        if ($lfanew -le 0 -or ($lfanew + 24 + 70) -gt $fs.Length) { return $null }
        $null = $fs.Seek($lfanew, [System.IO.SeekOrigin]::Begin)
        if ($br.ReadUInt32() -ne 0x00004550) { return $null }
        $null = $fs.Seek($lfanew + 24 + 68, [System.IO.SeekOrigin]::Begin)
        return [int]$br.ReadUInt16()
    }
    finally { $fs.Dispose() }
}

function Get-JarvisExecutableInfo {
    param([string]$ExePath)
    $item = Get-Item -LiteralPath $ExePath -Force
    $vi = $item.VersionInfo
    $fv = $null; $pv = $null
    $null = [System.Version]::TryParse([string]$vi.FileVersion, [ref]$fv)
    $null = [System.Version]::TryParse([string]$vi.ProductVersion, [ref]$pv)
    return [pscustomobject]@{
        Path           = $item.FullName
        Size           = $item.Length
        Sha256         = (Get-FileHash -LiteralPath $ExePath -Algorithm SHA256).Hash.ToLowerInvariant()
        FileVersion    = [string]$vi.FileVersion
        ProductVersion = [string]$vi.ProductVersion
        FileVersionParsed = $fv
        ProductVersionParsed = $pv
        Subsystem      = (Get-JarvisPeSubsystem $ExePath)
        ProductName    = [string]$vi.ProductName
    }
}

function Test-JarvisVersionMatches {
    param([System.Version]$Actual, [string]$Expected)
    if ($null -eq $Actual) { return $false }
    $e = $null
    if (-not [System.Version]::TryParse($Expected, [ref]$e)) { return $false }
    if ($Actual.Major -ne $e.Major -or $Actual.Minor -ne $e.Minor) { return $false }
    if ($e.Build -ge 0 -and $Actual.Build -ne $e.Build) { return $false }
    return $true
}

function Get-JarvisPackageInventory {
    param([string]$Path)
    $files = @(Get-ChildItem -LiteralPath $Path -Recurse -File -Force)
    $bytes = 0; foreach ($f in $files) { $bytes += $f.Length }
    return [pscustomobject]@{ FileCount = $files.Count; TotalBytes = $bytes }
}

function Test-JarvisReleasePackage {
    # Structural validation of one --onedir tree (a fresh build, a staged copy
    # or an installed release). -CheckExecutable adds the PE subsystem and
    # version-resource checks. Never mutates anything.
    # Operator-data findings (see $ForbiddenFileNames) are blocking for a fresh
    # build; with -HygieneBlocking $false (installed / imported releases, which
    # are preserved as they are) they are returned as Hygiene warnings instead.
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$ExpectedVersion = '',
        [bool]$CheckExecutable = $true,
        [bool]$HygieneBlocking = $true
    )
    $problems = @(); $hygiene = @()
    if (-not (Test-Path -LiteralPath $Path)) { return [pscustomobject]@{ Valid = $false; Problems = @("package directory missing: $Path"); Hygiene = @(); Executable = $null } }
    if (Test-JarvisReparsePoint $Path) { $problems += 'package directory is a reparse point' }
    $exe = Join-Path $Path $script:ExecutableName
    $internal = Join-Path $Path $script:InternalName
    if (-not (Test-Path -LiteralPath $exe)) { $problems += "missing $($script:ExecutableName)" }
    if (-not (Test-Path -LiteralPath $internal)) { $problems += "missing $($script:InternalName)\" }
    else {
        foreach ($rel in $script:RequiredBundled) {
            if (-not (Test-Path -LiteralPath (Join-Path $internal $rel))) { $problems += "missing bundled $rel" }
        }
    }
    $entries = @(Get-ChildItem -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue)
    foreach ($e in $entries) {
        if (($e.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { $problems += "reparse point inside package: $($e.FullName)" }
        if ($e.PSIsContainer) { if ($script:ForbiddenDirNames -contains $e.Name) { $hygiene += "forbidden directory inside package: $($e.FullName.Substring($Path.Length).TrimStart('\'))" } }
        elseif ($script:ForbiddenFileNames -contains $e.Name) { $hygiene += "forbidden file inside package: $($e.FullName.Substring($Path.Length).TrimStart('\'))" }
    }
    if ($HygieneBlocking) { $problems += $hygiene }
    $info = $null
    if ((Test-Path -LiteralPath $exe) -and $CheckExecutable) {
        $info = Get-JarvisExecutableInfo $exe
        if ($info.Subsystem -ne 2) { $problems += "$($script:ExecutableName) is not a GUI-subsystem executable (subsystem=$($info.Subsystem))" }
        if ($ExpectedVersion) {
            if (-not (Test-JarvisVersionMatches $info.FileVersionParsed $ExpectedVersion)) { $problems += "FileVersion '$($info.FileVersion)' does not match package version $ExpectedVersion" }
            if (-not (Test-JarvisVersionMatches $info.ProductVersionParsed $ExpectedVersion)) { $problems += "ProductVersion '$($info.ProductVersion)' does not match package version $ExpectedVersion" }
        }
    }
    return [pscustomobject]@{ Valid = ($problems.Count -eq 0); Problems = @($problems); Hygiene = @($hygiene); Executable = $info }
}

# ---- metadata / state ---------------------------------------------------------

function Read-JarvisReleaseMetadata {
    param([Parameter(Mandatory = $true)][string]$ReleasePath)
    $file = Join-Path $ReleasePath $script:MetadataFileName
    if (-not (Test-Path -LiteralPath $file)) { return $null }
    try { return (ConvertFrom-Json ([System.IO.File]::ReadAllText($file))) } catch { return $null }
}

function Write-JarvisReleaseMetadata {
    param([Parameter(Mandatory = $true)][string]$ReleasePath, [Parameter(Mandatory = $true)]$Metadata)
    $file = Join-Path $ReleasePath $script:MetadataFileName
    Write-JarvisTextFile $file (ConvertTo-Json -InputObject $Metadata -Depth 8)
}

function New-JarvisReleaseMetadata {
    param(
        [string]$ReleaseId, [string]$Version, [string]$Commit, [string]$ShortCommit, [string]$Branch,
        [string]$Source, [bool]$Legacy, [string]$ImportedFrom, $ExecutableInfo, $Inventory
    )
    $artifact = [ordered]@{
        executable      = $script:ExecutableName
        file_version    = $(if ($ExecutableInfo) { $ExecutableInfo.FileVersion } else { '' })
        product_version = $(if ($ExecutableInfo) { $ExecutableInfo.ProductVersion } else { '' })
        exe_sha256      = $(if ($ExecutableInfo) { $ExecutableInfo.Sha256 } else { '' })
        file_count      = $Inventory.FileCount
        total_bytes     = $Inventory.TotalBytes
    }
    $m = [ordered]@{
        schema       = $script:MetadataSchema
        release_id   = $ReleaseId
        version      = $Version
        commit       = $(if ($Commit) { $Commit } else { $null })
        short_commit = $(if ($ShortCommit) { $ShortCommit } else { $null })
        branch       = $(if ($Branch) { $Branch } else { $null })
        installed_at = (Get-JarvisUtcNow)
        source       = $Source
        legacy       = $Legacy
        artifact     = $artifact
    }
    if ($ImportedFrom) { $m['imported_from'] = $ImportedFrom }
    return $m
}

function Read-JarvisReleaseState {
    # {State, Exists, Problem}: State is $null when missing or invalid.
    param([Parameter(Mandatory = $true)][string]$Root)
    $layout = Get-JarvisReleaseLayout $Root
    if (-not (Test-Path -LiteralPath $layout.StatePath)) { return [pscustomobject]@{ State = $null; Exists = $false; Problem = '' } }
    try { $obj = ConvertFrom-Json ([System.IO.File]::ReadAllText($layout.StatePath)) }
    catch { return [pscustomobject]@{ State = $null; Exists = $true; Problem = "state file is not valid JSON: $($_.Exception.Message)" } }
    if ((Get-JarvisProp $obj 'schema') -ne $script:StateSchema) { return [pscustomobject]@{ State = $null; Exists = $true; Problem = 'state file has an unknown schema' } }
    foreach ($key in 'active', 'previous') {
        $v = Get-JarvisProp $obj $key
        if ($null -ne $v -and -not (Test-JarvisReleaseId ([string]$v))) { return [pscustomobject]@{ State = $null; Exists = $true; Problem = "state '$key' is not a valid release id" } }
    }
    $history = Get-JarvisProp $obj 'history' @()
    if ($null -eq $history) { $history = @() }
    $state = [ordered]@{
        schema     = $script:StateSchema
        active     = Get-JarvisProp $obj 'active'
        previous   = Get-JarvisProp $obj 'previous'
        updated_at = [string](Get-JarvisProp $obj 'updated_at' '')
        history    = @($history)
    }
    return [pscustomobject]@{ State = $state; Exists = $true; Problem = '' }
}

function Write-JarvisReleaseState {
    # Atomic: temp file in the same directory, then File.Replace / File.Move.
    param([Parameter(Mandatory = $true)][string]$Root, [Parameter(Mandatory = $true)]$State)
    $layout = Get-JarvisReleaseLayout $Root
    $State['updated_at'] = Get-JarvisUtcNow
    if ($State['history'].Count -gt $script:HistoryLimit) { $State['history'] = @($State['history'] | Select-Object -Last $script:HistoryLimit) }
    $tmp = "$($layout.StatePath).tmp-$([guid]::NewGuid().ToString('N'))"
    try {
        Write-JarvisTextFile $tmp (ConvertTo-Json -InputObject $State -Depth 8)
        Invoke-JarvisFailpoint 'state:before-replace'
        # [NullString]::Value: PowerShell would bind $null as "" here, which File.Replace rejects.
        if (Test-Path -LiteralPath $layout.StatePath) { [System.IO.File]::Replace($tmp, $layout.StatePath, [NullString]::Value) }
        else { [System.IO.File]::Move($tmp, $layout.StatePath) }
    }
    catch {
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
        throw
    }
}

function New-JarvisHistoryEntry {
    param([string]$Action, [string]$Release, $From)
    return [ordered]@{ at = (Get-JarvisUtcNow); action = $Action; release = $Release; from = $From }
}

# ---- current junction ---------------------------------------------------------

function Resolve-JarvisCurrent {
    # {Exists, IsJunction, Target, ReleaseId, Dangling, Problem}
    param([Parameter(Mandatory = $true)][string]$Root)
    $layout = Get-JarvisReleaseLayout $Root
    $out = [ordered]@{ Path = $layout.CurrentPath; Exists = $false; IsJunction = $false; Target = $null; ReleaseId = $null; Dangling = $false; Problem = '' }
    $item = Get-Item -LiteralPath $layout.CurrentPath -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) { return [pscustomobject]$out }
    $out.Exists = $true
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0) { $out.Problem = 'current exists but is not a junction'; return [pscustomobject]$out }
    $out.IsJunction = $true
    $target = [string](@($item.Target) | Select-Object -First 1)
    if ($target.StartsWith('\??\')) { $target = $target.Substring(4) }
    if ([string]::IsNullOrEmpty($target)) { $out.Problem = 'current is a reparse point without a readable target'; return [pscustomobject]$out }
    $out.Target = ConvertTo-JarvisFullPath $target
    if (-not (Test-Path -LiteralPath $out.Target)) { $out.Dangling = $true; $out.Problem = "current points to a missing directory: $($out.Target)" }
    if (Test-JarvisPathUnder $out.Target $layout.ReleasesDir) {
        $leaf = Split-Path -Leaf $out.Target
        if ((Test-JarvisSamePath (Split-Path -Parent $out.Target) $layout.ReleasesDir) -and (Test-JarvisReleaseId $leaf)) { $out.ReleaseId = $leaf }
    }
    if ($null -eq $out.ReleaseId -and -not $out.Dangling) { $out.Problem = "current points outside the releases directory: $($out.Target)" }
    return [pscustomobject]$out
}

function New-JarvisJunction {
    param([string]$LinkPath, [string]$TargetPath)
    if (Test-Path -LiteralPath $LinkPath) { throw "cannot create junction, path exists: $LinkPath" }
    $null = New-Item -ItemType Junction -Path $LinkPath -Target $TargetPath
    $item = Get-Item -LiteralPath $LinkPath -Force
    $resolved = [string](@($item.Target) | Select-Object -First 1)
    if ($resolved.StartsWith('\??\')) { $resolved = $resolved.Substring(4) }
    if (-not (Test-JarvisSamePath $resolved $TargetPath)) {
        Remove-JarvisJunction $LinkPath
        throw "junction $LinkPath resolved to '$resolved', expected '$TargetPath'"
    }
}

function Remove-JarvisJunction {
    # Removes the LINK only. Refuses anything that is not a reparse point, so a
    # real directory (and the release behind a link) can never be deleted here.
    param([string]$LinkPath)
    if (-not (Test-Path -LiteralPath $LinkPath)) { return }
    if (-not (Test-JarvisReparsePoint $LinkPath)) { throw "refusing to remove '$LinkPath': not a junction" }
    [System.IO.Directory]::Delete($LinkPath)
}

function Clear-JarvisStrayJunctions {
    # Leftovers of an interrupted swap: current.new-* / current.old-* links.
    param([string]$Root)
    $layout = Get-JarvisReleaseLayout $Root
    $removed = @(); $warnings = @()
    if (-not (Test-Path -LiteralPath $layout.Root)) { return [pscustomobject]@{ Removed = @(); Warnings = @() } }
    foreach ($e in @(Get-ChildItem -LiteralPath $layout.Root -Force | Where-Object { $_.PSIsContainer -and ($_.Name -like 'current.new-*' -or $_.Name -like 'current.old-*') })) {
        if (($e.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { Remove-JarvisJunction $e.FullName; $removed += $e.FullName }
        else { $warnings += "stray non-junction directory left in place: $($e.FullName)" }
    }
    foreach ($f in @(Get-ChildItem -LiteralPath $layout.Root -Force -File | Where-Object { $_.Name -like "$($script:StateFileName).tmp-*" })) {
        Remove-Item -LiteralPath $f.FullName -Force; $removed += $f.FullName
    }
    return [pscustomobject]@{ Removed = @($removed); Warnings = @($warnings) }
}

# ---- installed releases / status ---------------------------------------------

function Test-JarvisInstalledRelease {
    # An installed release = valid package + release.json agreeing with its
    # directory name (and, when given, with the expected version / commit).
    param([string]$ReleasePath, [string]$ExpectedId = '', [string]$ExpectedVersion = '', [string]$ExpectedCommit = '', [bool]$CheckExecutable = $true)
    $problems = @()
    # An installed release is kept as it is: stray operator data inside it is a warning, not invalidity.
    $pkg = Test-JarvisReleasePackage -Path $ReleasePath -ExpectedVersion $ExpectedVersion -CheckExecutable $CheckExecutable -HygieneBlocking $false
    $problems += $pkg.Problems
    $meta = Read-JarvisReleaseMetadata $ReleasePath
    if ($null -eq $meta) { $problems += "missing or unreadable $($script:MetadataFileName)" }
    else {
        $id = [string](Get-JarvisProp $meta 'release_id' '')
        $leaf = Split-Path -Leaf $ReleasePath
        if ($id -ne $leaf) { $problems += "metadata release_id '$id' does not match directory '$leaf'" }
        if ($ExpectedId -and $id -ne $ExpectedId) { $problems += "metadata release_id '$id' does not match expected '$ExpectedId'" }
        if ($ExpectedVersion -and ([string](Get-JarvisProp $meta 'version' '')) -ne $ExpectedVersion) { $problems += "metadata version '$(Get-JarvisProp $meta 'version' '')' does not match expected '$ExpectedVersion'" }
        if ($ExpectedCommit) {
            $c = Get-JarvisProp $meta 'commit'
            if ([string]$c -ne $ExpectedCommit) { $problems += "metadata commit '$c' does not match expected '$ExpectedCommit'" }
        }
    }
    return [pscustomobject]@{ Valid = ($problems.Count -eq 0); Problems = @($problems); Warnings = @($pkg.Hygiene); Metadata = $meta; Package = $pkg }
}

function Get-JarvisInstalledReleases {
    param([Parameter(Mandatory = $true)][string]$Root, [bool]$CheckExecutable = $true)
    $layout = Get-JarvisReleaseLayout $Root
    $list = @()
    if (-not (Test-Path -LiteralPath $layout.ReleasesDir)) { return @() }
    foreach ($d in @(Get-ChildItem -LiteralPath $layout.ReleasesDir -Force | Where-Object { $_.PSIsContainer } | Sort-Object Name)) {
        if ($d.Name.StartsWith($script:StagingPrefix)) { continue }
        $isLink = (($d.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)
        $idOk = Test-JarvisReleaseId $d.Name
        $check = $null
        if ($idOk -and -not $isLink) { $check = Test-JarvisInstalledRelease -ReleasePath $d.FullName -CheckExecutable $CheckExecutable }
        $meta = $null; if ($check) { $meta = $check.Metadata }
        $list += [pscustomobject]@{
            ReleaseId   = $d.Name
            Path        = $d.FullName
            Valid       = ($idOk -and -not $isLink -and $check.Valid)
            Problems    = $(if ($check) { @($check.Problems) } elseif ($isLink) { @('release entry is a reparse point') } else { @('not a valid release id') })
            Warnings    = $(if ($check) { @($check.Warnings) } else { @() })
            Metadata    = $meta
            Version     = [string](Get-JarvisProp $meta 'version' '')
            Commit      = [string](Get-JarvisProp $meta 'commit' '')
            ShortCommit = [string](Get-JarvisProp $meta 'short_commit' '')
            Legacy      = [bool](Get-JarvisProp $meta 'legacy' $false)
            InstalledAt = [string](Get-JarvisProp $meta 'installed_at' '')
            Created     = $d.CreationTimeUtc
        }
    }
    return @($list)
}

function Get-JarvisLastActivation {
    param($State, [string]$ReleaseId)
    $last = ''
    if ($null -eq $State) { return $last }
    foreach ($h in @($State['history'])) {
        if ([string](Get-JarvisProp $h 'release' '') -eq $ReleaseId) {
            $at = [string](Get-JarvisProp $h 'at' '')
            if ($at -gt $last) { $last = $at }
        }
    }
    return $last
}

function Get-JarvisReleaseStatus {
    # Read-only picture of the whole manager, including whether the state
    # file and the current junction agree.
    param([Parameter(Mandatory = $true)][string]$Root, [bool]$CheckExecutable = $true, [switch]$IncludeShortcut, [string]$DesktopPath = '')
    $layout = Get-JarvisReleaseLayout $Root
    $kind = Get-JarvisLayoutKind $Root
    $problems = @()
    $stateRead = Read-JarvisReleaseState $Root
    $state = $stateRead.State
    if ($stateRead.Problem) { $problems += $stateRead.Problem }
    $current = Resolve-JarvisCurrent $Root
    if ($current.Problem) { $problems += $current.Problem }
    $releases = @()
    if ($kind -eq 'managed') { $releases = @(Get-JarvisInstalledReleases -Root $Root -CheckExecutable $CheckExecutable) }
    $activeFromState = $null; $previous = $null
    if ($state) { $activeFromState = $state['active']; $previous = $state['previous'] }
    # Fresh = nothing has ever been activated: no state file and no current
    # junction (release directories may already exist, e.g. mid-install).
    $fresh = ($kind -in 'absent', 'empty') -or ($kind -eq 'managed' -and -not $stateRead.Exists -and -not $current.Exists)
    $consistent = $false
    if ($fresh) { $consistent = $true }
    elseif ($kind -eq 'managed') {
        if (-not $stateRead.Exists) { $problems += 'state file missing' }
        elseif ($null -eq $state) { }
        elseif ($null -eq $activeFromState) { if ($current.Exists) { $problems += 'state names no active release but current exists' } }
        elseif (-not $current.Exists) { $problems += "state says '$activeFromState' is active but current is missing" }
        elseif ($current.ReleaseId -ne $activeFromState) { if (-not $current.Problem) { $problems += "state says '$activeFromState' is active but current points to '$($current.ReleaseId)'" } }
        else {
            $rel = $releases | Where-Object { $_.ReleaseId -eq $activeFromState } | Select-Object -First 1
            if ($null -eq $rel) { $problems += "active release '$activeFromState' is not installed" }
            elseif (-not $rel.Valid) { $problems += "active release '$activeFromState' is invalid: " + ($rel.Problems -join '; ') }
        }
        $consistent = ($problems.Count -eq 0)
    }
    elseif ($kind -eq 'legacy-flat') { $problems += 'legacy flat installation (not yet managed): run build_release.ps1 -Install to import it' }
    else { $problems += "unrecognised layout '$kind' at $($layout.Root)" }
    $rollback = $null
    if ($previous) {
        $prevRel = $releases | Where-Object { $_.ReleaseId -eq $previous } | Select-Object -First 1
        $rollback = [pscustomobject]@{ ReleaseId = $previous; Installed = ($null -ne $prevRel); Valid = ($null -ne $prevRel -and $prevRel.Valid) }
    }
    $activeRel = $null
    if ($activeFromState) { $activeRel = $releases | Where-Object { $_.ReleaseId -eq $activeFromState } | Select-Object -First 1 }
    $shortcut = $null
    if ($IncludeShortcut) { try { $shortcut = Get-JarvisShortcutStatus -Root $Root -DesktopPath $DesktopPath } catch { $shortcut = $null } }
    return [pscustomobject]@{
        Root            = $layout.Root
        Layout          = $kind
        Fresh           = $fresh
        State           = $state
        StateExists     = $stateRead.Exists
        Current         = $current
        Releases        = @($releases)
        ActiveFromState = $activeFromState
        ActiveRelease   = $activeRel
        Previous        = $previous
        Rollback        = $rollback
        Consistent      = $consistent
        Problems        = @($problems)
        Shortcut        = $shortcut
    }
}

# ---- running-app detection ----------------------------------------------------

function Get-JarvisRunningInstalledProcess {
    # JARVIS.exe processes whose image lives under the manager root. A process
    # whose path cannot be read is reported too (conservative). Never kills.
    param([Parameter(Mandatory = $true)][string]$Root)
    $root = ConvertTo-JarvisFullPath $Root
    $hits = @()
    foreach ($p in @(& $script:ProcessSource)) {
        $path = [string](Get-JarvisProp $p 'ExecutablePath' '')
        if ([string]::IsNullOrEmpty($path)) { $hits += [pscustomobject]@{ ProcessId = $p.ProcessId; ExecutablePath = '(unreadable)'; Reason = 'path unreadable' }; continue }
        if ((Test-JarvisPathUnder $path $root) -or (Test-JarvisSamePath (Split-Path -Parent $path) $root)) {
            $hits += [pscustomobject]@{ ProcessId = $p.ProcessId; ExecutablePath = $path; Reason = 'installed JARVIS' }
        }
    }
    return @($hits)
}

function Assert-JarvisNotRunning {
    param([Parameter(Mandatory = $true)][string]$Root)
    $running = @(Get-JarvisRunningInstalledProcess $Root)
    if ($running.Count -gt 0) {
        $desc = ($running | ForEach-Object { "pid $($_.ProcessId) [$($_.ExecutablePath)]" }) -join ', '
        throw "JARVIS is running ($desc). Quit JARVIS (tray > Quit JARVIS) and run this command again; nothing was changed."
    }
}

# ---- activation (the one primitive) ------------------------------------------

function Switch-JarvisCurrent {
    # Point current at releases\<id>: temp junction -> verify -> swap -> state.
    # Any failure after the swap restores the old junction. The old release is
    # never deleted here.
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$ReleaseId,
        [ValidateSet('install', 'activate', 'rollback', 'migrate')][string]$Action = 'activate',
        [bool]$CheckExecutable = $true
    )
    $layout = Get-JarvisReleaseLayout $Root
    $targetPath = Get-JarvisReleasePath -Root $Root -ReleaseId $ReleaseId
    $check = Test-JarvisInstalledRelease -ReleasePath $targetPath -ExpectedId $ReleaseId -CheckExecutable $CheckExecutable
    if (-not $check.Valid) { throw "release '$ReleaseId' is not a valid installed release: " + ($check.Problems -join '; ') }
    Assert-JarvisNotRunning $Root
    $status = Get-JarvisReleaseStatus -Root $Root -CheckExecutable $CheckExecutable
    $current = $status.Current
    if (-not $status.Consistent) {
        $recoverable = ($Action -eq 'activate') -and ($status.Layout -eq 'managed') -and $status.StateExists -and ($null -ne $status.State) -and (-not $current.Exists -or $current.Dangling)
        if (-not $recoverable) { throw "manager state is inconsistent, refusing to change the active release: " + ($status.Problems -join '; ') }
    }
    if ($current.Exists -and -not $current.IsJunction) { throw "refusing: $($layout.CurrentPath) exists but is not a junction" }
    $null = Clear-JarvisStrayJunctions $Root
    $oldId = $null
    if ($current.Exists -and -not $current.Dangling) { $oldId = $current.ReleaseId }
    elseif ($status.State -and $status.State['active']) {
        $prevRel = $status.Releases | Where-Object { $_.ReleaseId -eq $status.State['active'] -and $_.Valid } | Select-Object -First 1
        if ($prevRel) { $oldId = $prevRel.ReleaseId }
    }
    if ($current.Exists -and -not $current.Dangling -and $current.ReleaseId -eq $ReleaseId) {
        return [pscustomobject]@{ ReleaseId = $ReleaseId; Changed = $false; PreviouslyActive = $oldId; Action = $Action }
    }
    $tag = [guid]::NewGuid().ToString('N')
    $tmpNew = Join-Path $layout.Root "current.new-$tag"
    $tmpOld = Join-Path $layout.Root "current.old-$tag"
    New-JarvisJunction -LinkPath $tmpNew -TargetPath $targetPath
    if (-not (Test-Path -LiteralPath (Join-Path $tmpNew $script:ExecutableName))) { Remove-JarvisJunction $tmpNew; throw "temporary junction does not expose $($script:ExecutableName)" }
    $movedOld = $false
    try {
        if ($current.Exists) { [System.IO.Directory]::Move($layout.CurrentPath, $tmpOld); $movedOld = $true }
        [System.IO.Directory]::Move($tmpNew, $layout.CurrentPath)
        Invoke-JarvisFailpoint 'switch:after-swap'
        $verify = Resolve-JarvisCurrent $Root
        if (-not $verify.IsJunction -or -not (Test-JarvisSamePath $verify.Target $targetPath)) { throw "current does not resolve to $targetPath after the swap" }
        $state = $status.State
        if ($null -eq $state) { $state = [ordered]@{ schema = $script:StateSchema; active = $null; previous = $null; updated_at = ''; history = @() } }
        $previous = $state['previous']
        if ($oldId -and $oldId -ne $ReleaseId) { $previous = $oldId }
        if ($previous -eq $ReleaseId) { $previous = $null }        # a release is never its own rollback target
        $state['active'] = $ReleaseId
        $state['previous'] = $previous
        $state['history'] = @($state['history']) + @(New-JarvisHistoryEntry -Action $Action -Release $ReleaseId -From $oldId)
        Write-JarvisReleaseState -Root $Root -State $state
    }
    catch {
        $err = $_
        try {
            $cur = Get-Item -LiteralPath $layout.CurrentPath -Force -ErrorAction SilentlyContinue
            if ($cur -and (($cur.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) {
                $rt = [string](@($cur.Target) | Select-Object -First 1)
                if ($rt.StartsWith('\??\')) { $rt = $rt.Substring(4) }
                if (Test-JarvisSamePath $rt $targetPath) { [System.IO.Directory]::Move($layout.CurrentPath, $tmpNew) }
            }
            if ($movedOld -and (Test-Path -LiteralPath $tmpOld) -and -not (Test-Path -LiteralPath $layout.CurrentPath)) { [System.IO.Directory]::Move($tmpOld, $layout.CurrentPath) }
            if (Test-Path -LiteralPath $tmpNew) { Remove-JarvisJunction $tmpNew }
        }
        catch { throw "activation failed ($($err.Exception.Message)) AND restoring the previous current failed ($($_.Exception.Message)); inspect $($layout.Root)" }
        throw "activation of '$ReleaseId' failed, previous current restored: $($err.Exception.Message)"
    }
    if ($movedOld) { try { Remove-JarvisJunction $tmpOld } catch { Write-Warning "old junction left behind at $tmpOld ($($_.Exception.Message))" } }
    return [pscustomobject]@{ ReleaseId = $ReleaseId; Changed = $true; PreviouslyActive = $oldId; Action = $Action }
}

# ---- desktop shortcut -----------------------------------------------------------

function Get-JarvisDesktopPath {
    param([string]$DesktopPath = '')
    if ($DesktopPath) { return ConvertTo-JarvisFullPath $DesktopPath }
    return ConvertTo-JarvisFullPath ([Environment]::GetFolderPath([Environment+SpecialFolder]::Desktop))
}

function Get-JarvisShortcutPlan {
    param([Parameter(Mandatory = $true)][string]$Root, [string]$DesktopPath = '')
    $layout = Get-JarvisReleaseLayout $Root
    $desktop = Get-JarvisDesktopPath $DesktopPath
    $target = Join-Path $layout.CurrentPath $script:ExecutableName
    return [pscustomobject]@{
        Desktop          = $desktop
        Path             = Join-Path $desktop 'JARVIS.lnk'
        Target           = $target
        WorkingDirectory = $layout.CurrentPath
        IconLocation     = "$target,0"
    }
}

function Read-JarvisShortcut {
    param([string]$Path)
    $wsh = New-Object -ComObject WScript.Shell
    $s = $wsh.CreateShortcut($Path)
    return [pscustomobject]@{ Path = $Path; Target = [string]$s.TargetPath; WorkingDirectory = [string]$s.WorkingDirectory; IconLocation = [string]$s.IconLocation }
}

function Get-JarvisShortcutStatus {
    # Read-only: the canonical JARVIS.lnk and every other Desktop .lnk that
    # points into the manager root.
    param([Parameter(Mandatory = $true)][string]$Root, [string]$DesktopPath = '')
    $plan = Get-JarvisShortcutPlan -Root $Root -DesktopPath $DesktopPath
    $ours = @()
    if (Test-Path -LiteralPath $plan.Desktop) {
        foreach ($f in @(Get-ChildItem -LiteralPath $plan.Desktop -Force -File | Where-Object { $_.Extension -ieq '.lnk' })) {
            $s = Read-JarvisShortcut $f.FullName
            if ($s.Target -and ((Test-JarvisPathUnder $s.Target $Root) -or (Test-JarvisSamePath (Split-Path -Parent $s.Target) $Root))) { $ours += $s }
        }
    }
    $canonical = $ours | Where-Object { Test-JarvisSamePath $_.Path $plan.Path } | Select-Object -First 1
    $correct = $false
    if ($canonical) { $correct = (Test-JarvisSamePath $canonical.Target $plan.Target) -and (Test-JarvisSamePath $canonical.WorkingDirectory $plan.WorkingDirectory) }
    return [pscustomobject]@{ Plan = $plan; Exists = ($null -ne $canonical); Correct = $correct; Canonical = $canonical; Others = @($ours | Where-Object { -not (Test-JarvisSamePath $_.Path $plan.Path) }); Count = $ours.Count }
}

function Sync-JarvisDesktopShortcut {
    # missing -> create; wrong -> update; stale other JARVIS shortcuts on the
    # Desktop -> retarget to current. Never creates a second one.
    param([Parameter(Mandatory = $true)][string]$Root, [string]$DesktopPath = '')
    $status = Get-JarvisShortcutStatus -Root $Root -DesktopPath $DesktopPath
    $plan = $status.Plan
    if (-not (Test-Path -LiteralPath $plan.Desktop)) { throw "Desktop folder not found: $($plan.Desktop)" }
    $wsh = New-Object -ComObject WScript.Shell
    $created = $false; $updated = $false; $retargeted = @()
    $write = {
        param($path)
        $s = $wsh.CreateShortcut($path)
        $s.TargetPath = $plan.Target
        $s.WorkingDirectory = $plan.WorkingDirectory
        $s.IconLocation = $plan.IconLocation
        $s.Description = 'JARVIS'
        $s.Save()
    }
    if (-not $status.Exists) { & $write $plan.Path; $created = $true }
    elseif (-not $status.Correct) { & $write $plan.Path; $updated = $true }
    foreach ($o in $status.Others) {
        if (-not (Test-JarvisSamePath $o.Target $plan.Target)) { & $write $o.Path; $retargeted += $o.Path }
    }
    $after = Get-JarvisShortcutStatus -Root $Root -DesktopPath $DesktopPath
    if (-not $after.Correct) { throw "shortcut at $($plan.Path) does not verify after writing" }
    return [pscustomobject]@{ Path = $plan.Path; Target = $plan.Target; WorkingDirectory = $plan.WorkingDirectory; Created = $created; Updated = $updated; Verified = (-not $created -and -not $updated); Retargeted = @($retargeted); Count = $after.Count }
}

# ---- retention ----------------------------------------------------------------

function Remove-JarvisReleaseDirectory {
    # Defensive recursive delete of one managed release: must be a direct,
    # non-reparse child of releases\ without reparse points inside.
    param([string]$Root, [string]$ReleaseId)
    $path = Get-JarvisReleasePath -Root $Root -ReleaseId $ReleaseId
    if (-not (Test-Path -LiteralPath $path)) { return }
    if (Test-JarvisReparsePoint $path) { throw "refusing to delete '$path': it is a reparse point" }
    $links = @(Get-ChildItem -LiteralPath $path -Recurse -Force | Where-Object { ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 })
    if ($links.Count -gt 0) { throw "refusing to delete '$path': it contains reparse points" }
    [System.IO.Directory]::Delete($path, $true)
}

function Invoke-JarvisRetention {
    # Keep: active, the rollback target, then the most recently active others,
    # $Keep in total. Runs only on a consistent manager; failures are warnings.
    param([Parameter(Mandatory = $true)][string]$Root, [int]$Keep = $script:DefaultKeep, [bool]$CheckExecutable = $true)
    $result = [ordered]@{ Kept = @(); Removed = @(); Warnings = @() }
    if ($Keep -lt 1) { $Keep = 1 }
    $status = Get-JarvisReleaseStatus -Root $Root -CheckExecutable $CheckExecutable
    if (-not $status.Consistent -or -not $status.ActiveFromState) { $result.Warnings += 'retention skipped: manager state is not consistent or nothing is active'; return [pscustomobject]$result }
    $active = $status.ActiveFromState; $previous = $status.Previous
    $others = @($status.Releases | Where-Object { $_.Valid -and $_.ReleaseId -ne $active } | ForEach-Object {
            $last = Get-JarvisLastActivation -State $status.State -ReleaseId $_.ReleaseId
            $key = $last; if (-not $key) { $key = $_.InstalledAt }; if (-not $key) { $key = $_.Created.ToString('o') }
            [pscustomobject]@{ ReleaseId = $_.ReleaseId; Key = $key; IsPrevious = ($_.ReleaseId -eq $previous) }
        } | Sort-Object @{ Expression = 'IsPrevious'; Descending = $true }, @{ Expression = 'Key'; Descending = $true }, @{ Expression = 'ReleaseId'; Descending = $false })
    $keepIds = @($active) + @($others | Select-Object -First ([Math]::Max(0, $Keep - 1)) | ForEach-Object { $_.ReleaseId })
    $result.Kept = @($keepIds)
    foreach ($o in $others) {
        if ($keepIds -contains $o.ReleaseId) { continue }
        if ($o.ReleaseId -eq $active -or $o.ReleaseId -eq $previous) { continue }
        try { Remove-JarvisReleaseDirectory -Root $Root -ReleaseId $o.ReleaseId; $result.Removed += $o.ReleaseId }
        catch { $result.Warnings += "could not remove release '$($o.ReleaseId)': $($_.Exception.Message)" }
    }
    foreach ($r in @($status.Releases | Where-Object { -not $_.Valid })) { $result.Warnings += "unrecognised entry left in place: $($r.Path) (" + ($r.Problems -join '; ') + ')' }
    return [pscustomobject]$result
}

# ---- legacy migration -------------------------------------------------------------

function Invoke-JarvisLegacyMigration {
    # <root> is itself a flat --onedir install: rename it aside, recreate the
    # root, move it under releases\legacy-<version>-<sha7>, write metadata and
    # activate it through the same primitive as every other release. A failure
    # midway moves the flat install back.
    param([Parameter(Mandatory = $true)][string]$Root, [bool]$CheckExecutable = $true)
    $layout = Get-JarvisReleaseLayout $Root
    if ((Get-JarvisLayoutKind $Root) -ne 'legacy-flat') { throw "no legacy flat installation at $($layout.Root)" }
    # The flat install is preserved exactly as it is; stray operator data inside
    # it is reported, never deleted (it leaves with that release under retention).
    $pkg = Test-JarvisReleasePackage -Path $layout.Root -CheckExecutable $CheckExecutable -HygieneBlocking $false
    if (-not $pkg.Valid) { throw "the flat installation at $($layout.Root) is not a complete package, refusing to migrate: " + ($pkg.Problems -join '; ') }
    Assert-JarvisNotRunning $Root
    $exeInfo = $pkg.Executable
    if ($null -eq $exeInfo) { $exeInfo = Get-JarvisExecutableInfo (Join-Path $layout.Root $script:ExecutableName) }
    $ver = '0.0.0'
    if ($exeInfo.ProductVersionParsed) { $v = $exeInfo.ProductVersionParsed; $ver = "$($v.Major).$($v.Minor).$([Math]::Max(0, $v.Build))" }
    $legacyId = "legacy-$ver-$($exeInfo.Sha256.Substring(0, 7))"
    if (-not (Test-JarvisReleaseId $legacyId)) { throw "derived legacy id '$legacyId' is invalid" }
    $inventory = Get-JarvisPackageInventory $layout.Root
    $holding = "$($layout.Root).migrating-$([guid]::NewGuid().ToString('N'))"
    [System.IO.Directory]::Move($layout.Root, $holding)
    $releasePath = $null
    try {
        $null = New-Item -ItemType Directory -Path $layout.Root
        $null = New-Item -ItemType Directory -Path $layout.ReleasesDir
        $releasePath = Get-JarvisReleasePath -Root $Root -ReleaseId $legacyId
        [System.IO.Directory]::Move($holding, $releasePath)
        Invoke-JarvisFailpoint 'migrate:after-move'
        $meta = New-JarvisReleaseMetadata -ReleaseId $legacyId -Version $ver -Commit '' -ShortCommit '' -Branch '' -Source 'legacy-import' -Legacy $true -ImportedFrom $layout.Root -ExecutableInfo $exeInfo -Inventory $inventory
        Write-JarvisReleaseMetadata -ReleasePath $releasePath -Metadata $meta
        $null = Switch-JarvisCurrent -Root $Root -ReleaseId $legacyId -Action 'migrate' -CheckExecutable $CheckExecutable
    }
    catch {
        $err = $_
        try {
            if ($releasePath -and (Test-Path -LiteralPath $releasePath) -and -not (Test-Path -LiteralPath $holding)) {
                $metaFile = Join-Path $releasePath $script:MetadataFileName
                if (Test-Path -LiteralPath $metaFile) { Remove-Item -LiteralPath $metaFile -Force }
                [System.IO.Directory]::Move($releasePath, $holding)
            }
            if (Test-Path -LiteralPath $layout.CurrentPath) { Remove-JarvisJunction $layout.CurrentPath }
            if (Test-Path -LiteralPath $layout.StatePath) { Remove-Item -LiteralPath $layout.StatePath -Force }
            if (Test-Path -LiteralPath $layout.ReleasesDir) { [System.IO.Directory]::Delete($layout.ReleasesDir) }
            if (Test-Path -LiteralPath $layout.Root) { [System.IO.Directory]::Delete($layout.Root) }
            [System.IO.Directory]::Move($holding, $layout.Root)
        }
        catch { throw "legacy migration failed ($($err.Exception.Message)) AND restoring the flat installation failed ($($_.Exception.Message)); the files are under $holding or $releasePath" }
        throw "legacy migration failed, flat installation restored at $($layout.Root): $($err.Exception.Message)"
    }
    return [pscustomobject]@{ ReleaseId = $legacyId; Path = $releasePath; Version = $ver; Warnings = @($pkg.Hygiene | ForEach-Object { "imported flat installation kept as-is with $_" }) }
}

# ---- install --------------------------------------------------------------------

function Clear-JarvisStagingDirectories {
    param([string]$Root)
    $layout = Get-JarvisReleaseLayout $Root
    $removed = @(); $warnings = @()
    if (-not (Test-Path -LiteralPath $layout.ReleasesDir)) { return [pscustomobject]@{ Removed = @(); Warnings = @() } }
    foreach ($d in @(Get-ChildItem -LiteralPath $layout.ReleasesDir -Force | Where-Object { $_.PSIsContainer -and $_.Name.StartsWith($script:StagingPrefix) })) {
        if (($d.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { $warnings += "stale staging entry is a reparse point, left in place: $($d.FullName)"; continue }
        $links = @(Get-ChildItem -LiteralPath $d.FullName -Recurse -Force | Where-Object { ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 })
        if ($links.Count -gt 0) { $warnings += "stale staging directory contains reparse points, left in place: $($d.FullName)"; continue }
        try { [System.IO.Directory]::Delete($d.FullName, $true); $removed += $d.FullName } catch { $warnings += "could not remove stale staging directory $($d.FullName): $($_.Exception.Message)" }
    }
    return [pscustomobject]@{ Removed = @($removed); Warnings = @($warnings) }
}

function Copy-JarvisPackageTree {
    param([string]$Source, [string]$Destination)
    $r = Invoke-JarvisNative 'robocopy' @($Source, $Destination, '/E', '/COPY:DAT', '/DCOPY:DAT', '/XJ', '/R:2', '/W:1', '/NFL', '/NDL', '/NJH', '/NJS', '/NP')
    # robocopy: 0-7 success (bit flags), 8+ failure.
    if ($r.ExitCode -ge 8) { throw "robocopy failed with exit code $($r.ExitCode): $($r.Text)" }
}

function Install-JarvisRelease {
    # Stage the complete --onedir tree next to its final place, validate it,
    # rename it into releases\<id>, activate, sync the shortcut, then retain.
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$ReleaseId,
        [Parameter(Mandatory = $true)][string]$Version,
        [string]$Commit = '', [string]$ShortCommit = '', [string]$Branch = '',
        [string]$DesktopPath = '', [switch]$NoShortcut,
        [int]$Keep = $script:DefaultKeep,
        [bool]$CheckExecutable = $true
    )
    $layout = Get-JarvisReleaseLayout $Root
    $warnings = @()
    if (-not (Test-JarvisReleaseId $ReleaseId)) { throw "invalid release id '$ReleaseId'" }
    $src = Test-JarvisReleasePackage -Path $SourcePath -ExpectedVersion $Version -CheckExecutable $CheckExecutable
    if (-not $src.Valid) { throw "the built package at $SourcePath is not valid: " + ($src.Problems -join '; ') }
    $migrated = $null
    $kind = Get-JarvisLayoutKind $Root
    if ($kind -eq 'legacy-flat') { $migrated = Invoke-JarvisLegacyMigration -Root $Root -CheckExecutable $CheckExecutable; $warnings += $migrated.Warnings }
    elseif ($kind -eq 'unknown') { throw "$($layout.Root) exists but is neither a managed root nor a flat JARVIS installation; refusing to install into it" }
    if (-not (Test-Path -LiteralPath $layout.ReleasesDir)) { $null = New-Item -ItemType Directory -Path $layout.ReleasesDir -Force }
    Assert-JarvisNotRunning $Root
    $status = Get-JarvisReleaseStatus -Root $Root -CheckExecutable $CheckExecutable
    if (-not $status.Consistent) { throw "manager state is inconsistent, refusing to install: " + ($status.Problems -join '; ') }
    $cleaned = Clear-JarvisStagingDirectories $Root
    $warnings += $cleaned.Warnings
    $strays = Clear-JarvisStrayJunctions $Root
    $warnings += $strays.Warnings
    $releasePath = Get-JarvisReleasePath -Root $Root -ReleaseId $ReleaseId
    $reused = $false
    if (Test-Path -LiteralPath $releasePath) {
        $existing = Test-JarvisInstalledRelease -ReleasePath $releasePath -ExpectedId $ReleaseId -ExpectedVersion $Version -ExpectedCommit $Commit -CheckExecutable $CheckExecutable
        if (-not $existing.Valid) { throw "release '$ReleaseId' is already installed but does not match this build; it was NOT overwritten: " + ($existing.Problems -join '; ') }
        $reused = $true
    }
    else {
        $staging = Join-Path $layout.ReleasesDir "$($script:StagingPrefix)$ReleaseId-$([guid]::NewGuid().ToString('N'))"
        try {
            Copy-JarvisPackageTree -Source $SourcePath -Destination $staging
            Invoke-JarvisFailpoint 'install:after-copy'
            $staged = Test-JarvisReleasePackage -Path $staging -ExpectedVersion $Version -CheckExecutable $CheckExecutable
            if (-not $staged.Valid) { throw "staged package is not valid: " + ($staged.Problems -join '; ') }
            $srcInv = Get-JarvisPackageInventory $SourcePath
            $stgInv = Get-JarvisPackageInventory $staging
            if ($srcInv.FileCount -ne $stgInv.FileCount -or $srcInv.TotalBytes -ne $stgInv.TotalBytes) { throw "staged copy differs from the build (files $($stgInv.FileCount)/$($srcInv.FileCount), bytes $($stgInv.TotalBytes)/$($srcInv.TotalBytes))" }
            $srcHash = (Get-FileHash -LiteralPath (Join-Path $SourcePath $script:ExecutableName) -Algorithm SHA256).Hash
            $stgHash = (Get-FileHash -LiteralPath (Join-Path $staging $script:ExecutableName) -Algorithm SHA256).Hash
            if ($srcHash -ne $stgHash) { throw 'staged executable hash differs from the build' }
            $exeInfo = $staged.Executable
            if ($null -eq $exeInfo) { $exeInfo = Get-JarvisExecutableInfo (Join-Path $staging $script:ExecutableName) }
            $meta = New-JarvisReleaseMetadata -ReleaseId $ReleaseId -Version $Version -Commit $Commit -ShortCommit $ShortCommit -Branch $Branch -Source 'build' -Legacy $false -ImportedFrom '' -ExecutableInfo $exeInfo -Inventory $stgInv
            Write-JarvisReleaseMetadata -ReleasePath $staging -Metadata $meta
            Invoke-JarvisFailpoint 'install:before-finalize'
            if (Test-Path -LiteralPath $releasePath) { throw "release directory appeared during staging: $releasePath" }
            [System.IO.Directory]::Move($staging, $releasePath)
        }
        catch {
            if (Test-Path -LiteralPath $staging) { try { [System.IO.Directory]::Delete($staging, $true) } catch { Write-Warning "could not remove staging directory $staging" } }
            throw
        }
    }
    $switch = Switch-JarvisCurrent -Root $Root -ReleaseId $ReleaseId -Action 'install' -CheckExecutable $CheckExecutable
    $shortcut = $null
    if (-not $NoShortcut) {
        try { $shortcut = Sync-JarvisDesktopShortcut -Root $Root -DesktopPath $DesktopPath }
        catch { $warnings += "desktop shortcut could not be updated: $($_.Exception.Message)" }
    }
    $retention = Invoke-JarvisRetention -Root $Root -Keep $Keep -CheckExecutable $CheckExecutable
    $warnings += $retention.Warnings
    return [pscustomobject]@{
        ReleaseId        = $ReleaseId
        ReleasePath      = $releasePath
        Reused           = $reused
        Migrated         = $migrated
        Activated        = $switch.Changed
        PreviouslyActive = $switch.PreviouslyActive
        Shortcut         = $shortcut
        Retention        = $retention
        StagingCleaned   = @($cleaned.Removed) + @($strays.Removed)
        Warnings         = @($warnings)
    }
}

# ---- rollback / activate --------------------------------------------------------

function Invoke-JarvisRollback {
    param([Parameter(Mandatory = $true)][string]$Root, [bool]$CheckExecutable = $true)
    $status = Get-JarvisReleaseStatus -Root $Root -CheckExecutable $CheckExecutable
    if ($status.Layout -ne 'managed') { throw "no managed installation at $($status.Root) (layout: $($status.Layout))" }
    if (-not $status.Consistent) { throw "manager state is inconsistent, refusing to roll back: " + ($status.Problems -join '; ') }
    if (-not $status.Previous) { throw 'no previous release is recorded; nothing to roll back to' }
    if (-not $status.Rollback.Installed) { throw "previous release '$($status.Previous)' is no longer installed; nothing to roll back to" }
    if (-not $status.Rollback.Valid) { throw "previous release '$($status.Previous)' is not structurally usable; refusing to roll back to it" }
    $switch = Switch-JarvisCurrent -Root $Root -ReleaseId $status.Previous -Action 'rollback' -CheckExecutable $CheckExecutable
    return [pscustomobject]@{ ReleaseId = $switch.ReleaseId; From = $switch.PreviouslyActive; Changed = $switch.Changed }
}

function Invoke-JarvisActivate {
    param([Parameter(Mandatory = $true)][string]$Root, [Parameter(Mandatory = $true)][string]$ReleaseId, [bool]$CheckExecutable = $true)
    $null = Get-JarvisReleasePath -Root $Root -ReleaseId $ReleaseId
    if ((Get-JarvisLayoutKind $Root) -ne 'managed') { throw "no managed installation at $Root" }
    $switch = Switch-JarvisCurrent -Root $Root -ReleaseId $ReleaseId -Action 'activate' -CheckExecutable $CheckExecutable
    return [pscustomobject]@{ ReleaseId = $switch.ReleaseId; From = $switch.PreviouslyActive; Changed = $switch.Changed }
}

# ---- text output --------------------------------------------------------------------

function Format-JarvisReleaseStatus {
    param([Parameter(Mandatory = $true)]$Status)
    $lines = @('JARVIS release manager')
    $lines += "  root:            $($Status.Root)"
    $lines += "  layout:          $($Status.Layout)"
    $act = $Status.ActiveFromState; if (-not $act) { $act = '(none)' }
    $lines += "  active (state):  $act"
    $cur = $Status.Current
    $curText = '(missing)'
    if ($cur.Exists) { if ($cur.IsJunction) { $curText = "$($cur.Target)  [junction]" } else { $curText = "$($cur.Path)  [NOT a junction]" } }
    if ($cur.Dangling) { $curText += '  [DANGLING]' }
    $lines += "  current ->       $curText"
    $lines += "  consistent:      $(if ($Status.Consistent) { 'YES' } else { 'NO' })"
    if ($Status.ActiveRelease) {
        $lines += "  version:         $($Status.ActiveRelease.Version)"
        $c = $Status.ActiveRelease.Commit; if (-not $c) { $c = '(unknown)' }
        $lines += "  commit:          $c"
        if ($Status.ActiveRelease.Legacy) { $lines += '  kind:            LEGACY (imported flat install)' }
    }
    if ($Status.Rollback) {
        $rb = $Status.Rollback
        $lines += "  previous:        $($rb.ReleaseId)  $(if ($rb.Valid) { '[rollback available]' } elseif ($rb.Installed) { '[installed but INVALID]' } else { '[NOT installed]' })"
    }
    else { $lines += '  previous:        (none)' }
    $lines += "  releases:        $($Status.Releases.Count) installed"
    if ($Status.Shortcut) {
        $s = $Status.Shortcut
        if ($s.Exists) { $lines += "  shortcut:        $($s.Plan.Path)  $(if ($s.Correct) { '[OK -> ' + $s.Plan.Target + ']' } else { '[STALE: ' + $s.Canonical.Target + ']' })" }
        else { $lines += "  shortcut:        (missing) expected at $($s.Plan.Path)" }
        if ($s.Others.Count -gt 0) { $lines += "  other shortcuts: " + (($s.Others | ForEach-Object { $_.Path }) -join ', ') }
    }
    foreach ($p in $Status.Problems) { $lines += "  PROBLEM:         $p" }
    return $lines
}

function Format-JarvisReleaseList {
    param([Parameter(Mandatory = $true)]$Status)
    $lines = @("Installed releases under $($Status.Root)\releases ($($Status.Releases.Count)):")
    if ($Status.Layout -ne 'managed') { $lines += "  (layout: $($Status.Layout))"; return $lines }
    foreach ($r in $Status.Releases) {
        $mark = ' '; if ($r.ReleaseId -eq $Status.ActiveFromState) { $mark = '*' }
        $flags = @()
        if ($r.ReleaseId -eq $Status.Previous) { $flags += 'previous' }
        if ($r.Legacy) { $flags += 'LEGACY' }
        if (-not $r.Valid) { $flags += 'INVALID' }
        $last = Get-JarvisLastActivation -State $Status.State -ReleaseId $r.ReleaseId
        $ver = $r.Version; if (-not $ver) { $ver = '?' }
        $sc = $r.ShortCommit; if (-not $sc) { $sc = '-' }
        $lines += ("  {0} {1,-28} v{2,-8} {3,-8} installed {4,-24} activated {5,-24} {6}" -f $mark, $r.ReleaseId, $ver, $sc, $r.InstalledAt, $(if ($last) { $last } else { '-' }), ($flags -join ','))
        if (-not $r.Valid) { $lines += "      " + ($r.Problems -join '; ') }
        foreach ($w in $r.Warnings) { $lines += "      warning: $w" }
    }
    return $lines
}

Export-ModuleMember -Function @(
    'Get-JarvisReleaseRoot', 'Get-JarvisReleaseLayout', 'Get-JarvisLayoutKind', 'Test-JarvisReleaseId', 'Get-JarvisReleasePath',
    'Get-JarvisPackageVersion', 'Get-JarvisReleaseIdentity', 'Test-JarvisReleasePackage', 'Get-JarvisExecutableInfo',
    'Read-JarvisReleaseMetadata', 'Read-JarvisReleaseState', 'Write-JarvisReleaseState', 'Resolve-JarvisCurrent',
    'Test-JarvisInstalledRelease', 'Get-JarvisInstalledReleases', 'Get-JarvisReleaseStatus',
    'Get-JarvisRunningInstalledProcess', 'Assert-JarvisNotRunning', 'Switch-JarvisCurrent',
    'Get-JarvisShortcutPlan', 'Get-JarvisShortcutStatus', 'Sync-JarvisDesktopShortcut',
    'Invoke-JarvisRetention', 'Invoke-JarvisLegacyMigration', 'Install-JarvisRelease', 'Invoke-JarvisRollback', 'Invoke-JarvisActivate',
    'Format-JarvisReleaseStatus', 'Format-JarvisReleaseList',
    'Set-JarvisReleaseProcessSource', 'Set-JarvisReleaseFailpoint'
)
