# Developer-tooling tests for the JARVIS worktree helper (dev.ps1).
# Uses disposable local temporary git repositories only - no network access,
# never touches the real JARVIS repositories.
#
# Exit-code contract asserted throughout:
#   new/status           -> 0 on success, 1 on refusal
#   finish/finish-current-> 0 iff integration succeeded (cleanup outcome is
#                           reported separately and truthfully), else 1
#   cleanup              -> 0 iff CLEANED, else 1 (deferred/recovery/refused)
#
# The exclusive handoff env value is auto-provisioned from task metadata by
# Run-Dev for finish/cleanup calls (the normal controller workflow); handoff
# attacks suppress that with $script:SuppressAutoHandoff.
#
# Runs SHARDED by default: with no arguments the parent process starts one
# child powershell.exe per entry in $Shards, each running its own sections
# against its own temp base, and aggregates their results. -Serial runs every
# section in this one process and -Section A,B runs just those: the same test
# code on every path, only the selection differs.

param(
    [string[]]$Section,   # run only these sections, in this process
    [switch]$Serial,      # run every section in this process
    [string]$ResultFile,  # child shard: write the {pass, fail, failures} JSON here
    [string]$TempBase     # child shard: parent directory for every temp root
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$DevPs = Join-Path $RepoRoot 'dev.ps1'

# ---- sharding --------------------------------------------------------------

# Every section of this file, in file order. Test-Section refuses a name that
# is not listed here, so a typo can never silently skip a block of tests.
$AllSections = @('NEW', 'FINISH', 'CLEANUP', 'STATUS', 'CLI', 'FINISH-CURRENT',
    'ADVERSARIAL', 'ADVERSARIAL-V2', 'ADVERSARIAL-V3', 'ADVERSARIAL-P1',
    'WINDOWS-A', 'WINDOWS-B', 'WINDOWS-C', 'WINDOWS-D')

# Fixed shard plan, balanced on measured SERIAL seconds per section (2026-09,
# 12-core laptop): NEW 8, FINISH 31, CLEANUP 7, STATUS 4, CLI 1,
# FINISH-CURRENT 17, ADVERSARIAL 21, -V2 41, -V3 53, -P1 34, WINDOWS-A 36,
# -B 34, -C ~52, -D ~15; 355 in total, so ~90 per shard. The Windows finish /
# partial-removal block alone is a third of the suite, which is why it is
# split in four (W1-W12, W13-W19b, W21-W23, W24) instead of becoming the one
# shard that decides the total. A shard is a serial run of whole sections:
# tests inside a section share state with their neighbours. Rebalance here
# when the numbers drift; never by moving tests between sections.
$Shards = @(
    @{ Name = 'shard-1'; Sections = @('ADVERSARIAL-V3', 'ADVERSARIAL-P1', 'CLI') },
    @{ Name = 'shard-2'; Sections = @('WINDOWS-C', 'WINDOWS-B', 'STATUS') },
    @{ Name = 'shard-3'; Sections = @('ADVERSARIAL-V2', 'WINDOWS-A', 'NEW') },
    @{ Name = 'shard-4'; Sections = @('FINISH', 'ADVERSARIAL', 'FINISH-CURRENT', 'WINDOWS-D', 'CLEANUP') }
)

function Invoke-ShardedRun {
    # Parent: one isolated child PROCESS per shard, nothing parallel inside a
    # process. Isolation is the process boundary itself: every child owns its
    # environment block (handoff and test-seam variables, git identity,
    # GIT_CONFIG_GLOBAL), its own canonical template and temp roots under its
    # own -TempBase, its own working directory and its own dev.ps1 children.
    # The parent shares nothing mutable with them; it only reads each child's
    # log and result file afterwards, in fixed shard order.
    $planned = @($Shards | ForEach-Object { $_.Sections })
    $twice = @($planned | Group-Object | Where-Object { $_.Count -gt 1 } | ForEach-Object { $_.Name })
    $missing = @($AllSections | Where-Object { $planned -notcontains $_ })
    $unknown = @($planned | Where-Object { $AllSections -notcontains $_ })
    if ($twice.Count -or $missing.Count -or $unknown.Count) {
        throw "shard plan must cover every section exactly once (twice: $twice; missing: $missing; unknown: $unknown)"
    }
    $base = Join-Path ([System.IO.Path]::GetTempPath()) ('devhelper-shards-' + [guid]::NewGuid().ToString('N'))
    $null = New-Item -ItemType Directory -Path $base
    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    $runs = @()
    Write-Host "sharded run: $($Shards.Count) processes, temp base $base"
    try {
        foreach ($s in $Shards) {
            $dir = Join-Path $base $s.Name
            $null = New-Item -ItemType Directory -Path $dir
            $sp = @{
                FilePath               = 'powershell.exe'
                ArgumentList           = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"",
                    '-Section', ($s.Sections -join ','),
                    '-ResultFile', "`"$(Join-Path $dir 'result.json')`"",
                    '-TempBase', "`"$(Join-Path $dir 'tmp')`"")
                WorkingDirectory       = $RepoRoot
                RedirectStandardOutput = (Join-Path $dir 'stdout.log')
                RedirectStandardError  = (Join-Path $dir 'stderr.log')
                NoNewWindow            = $true
                PassThru               = $true
            }
            $p = Start-Process @sp
            $null = $p.Handle   # PS 5.1: cache the handle now, or ExitCode stays empty
            $runs += [pscustomobject]@{ Name = $s.Name; Sections = $s.Sections; Dir = $dir; Process = $p; Started = $clock.Elapsed; Elapsed = $null; Result = $null; Status = 'RUNNING' }
            Write-Host "  $($s.Name) (pid $($p.Id)): $($s.Sections -join ', ')"
        }
        $deadline = [DateTime]::UtcNow.AddMinutes(30)
        while (@($runs | Where-Object { $_.Status -eq 'RUNNING' }).Count -gt 0) {
            foreach ($r in @($runs | Where-Object { $_.Status -eq 'RUNNING' })) {
                if (-not $r.Process.HasExited) { continue }
                $r.Elapsed = $clock.Elapsed - $r.Started
                $resultPath = Join-Path $r.Dir 'result.json'
                if (Test-Path -LiteralPath $resultPath) {
                    $r.Result = Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
                    $r.Status = if ($r.Process.ExitCode -eq 0 -and $r.Result.fail -eq 0 -and $r.Result.pass -gt 0) { 'OK' } else { 'FAILED' }
                }
                else { $r.Status = 'CRASHED' }
                Write-Host ("  {0}: {1} after {2:mm\:ss} (exit {3}, {4} passed, {5} failed)" -f $r.Name, $r.Status, $r.Elapsed, $r.Process.ExitCode, [int]$r.Result.pass, [int]$r.Result.fail)
            }
            if ([DateTime]::UtcNow -gt $deadline) { break }
            Start-Sleep -Milliseconds 500
        }
    }
    finally {
        # nothing may outlive the parent: a shard still running here (deadline
        # or Ctrl+C) is killed together with its whole dev.ps1 / git tree
        foreach ($r in $runs) {
            if (-not $r.Process.HasExited) {
                & taskkill.exe /PID $r.Process.Id /T /F *> $null
                $r.Status = 'TIMEOUT'
            }
        }
    }
    foreach ($r in $runs) {
        Write-Host ''
        Write-Host "===== $($r.Name): $($r.Sections -join ', ') ====="
        foreach ($f in @('stdout.log', 'stderr.log')) {
            $log = Join-Path $r.Dir $f
            if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log -Encoding Oem | ForEach-Object { Write-Host $_ } }
        }
    }
    $pass = 0; $fail = 0; $failures = @(); $ok = $true
    Write-Host ''
    foreach ($r in $runs) {
        $pass += [int]$r.Result.pass; $fail += [int]$r.Result.fail
        if ($r.Result -and $r.Result.failures) { $failures += @($r.Result.failures | ForEach-Object { "$($r.Name): $_" }) }
        if ($r.Status -ne 'OK') { $ok = $false }
        Write-Host ("  {0,-8} {1,-8} {2,4} passed {3,3} failed  {4:mm\:ss}  {5}" -f $r.Name, $r.Status, [int]$r.Result.pass, [int]$r.Result.fail, $r.Elapsed, ($r.Sections -join ', '))
    }
    try { Remove-Item -LiteralPath $base -Recurse -Force -ErrorAction Stop } catch { Write-Host "  (could not remove shard temp base: $base)" }
    Write-Host ''
    Write-Host ("RESULT: $pass passed, $fail failed  ($($runs.Count) shards, wall-clock {0:mm\:ss\.f})" -f $clock.Elapsed)
    if (-not $ok) {
        $failures | ForEach-Object { Write-Host "  FAILED: $_" -ForegroundColor Red }
        foreach ($r in @($runs | Where-Object { $_.Status -ne 'OK' })) { Write-Host "  SHARD $($r.Name): $($r.Status)" -ForegroundColor Red }
        exit 1
    }
    exit 0
}

if (-not $Serial -and -not $Section) { Invoke-ShardedRun }

# The sections THIS process runs; both selections go through the same gate,
# Test-Section, called once at the top of every section block.
$script:selected = if ($Serial) { $AllSections } else { @($Section | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
foreach ($n in $script:selected) {
    if ($AllSections -notcontains $n) { throw "unknown section '$n' (known: $($AllSections -join ', '))" }
}

function Test-Section {
    param([string]$Name)
    if ($AllSections -notcontains $Name) { throw "section '$Name' is not registered in `$AllSections" }
    return [bool]($script:selected -contains $Name)
}

$script:pass = 0
$script:fail = 0
$script:failures = @()
$script:tempRoots = @()
$script:SuppressAutoHandoff = $false

function Check {
    param([string]$Name, [bool]$Condition)
    if ($Condition) {
        $script:pass++
        Write-Host "  PASS $Name"
    }
    else {
        $script:fail++
        $script:failures += $Name
        Write-Host "  FAIL $Name" -ForegroundColor Red
    }
}

# ---- helpers ---------------------------------------------------------------

function New-TempRoot {
    # A child shard keeps every root inside its own -TempBase, so shards never
    # see each other's state and the parent can drop the whole tree afterwards.
    $base = if ($TempBase) { $TempBase } else { [System.IO.Path]::GetTempPath() }
    $root = Join-Path $base ("devhelper-test-" + [guid]::NewGuid().ToString('N'))
    $null = New-Item -ItemType Directory -Path $root
    $script:tempRoots += $root
    return $root
}

function GitRun {
    param([string]$Repo, [string[]]$GitArgs)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $out = & git -C $Repo @GitArgs 2>&1; $code = $LASTEXITCODE } finally { $ErrorActionPreference = $prev }
    if ($code -ne 0) {
        $text = (@($out) | ForEach-Object { $_.ToString() }) -join ' | '
        throw "git $($GitArgs -join ' ') failed in $Repo`: $text"
    }
}

function GitOut {
    param([string]$Repo, [string[]]$GitArgs)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $out = & git -C $Repo @GitArgs 2>&1 } finally { $ErrorActionPreference = $prev }
    if ($LASTEXITCODE -ne 0) { throw "git $($GitArgs -join ' ') failed in $Repo" }
    return ((@($out) | ForEach-Object { $_.ToString() }) -join "`n")
}

$script:canonTemplate = $null

function New-CanonicalRepo {
    # The canonical fixture is built once (lazily, so the git env isolation
    # below is already in place) and every test gets its own physical
    # directory copy of it: same contents, config, history and refs as the
    # former per-test build (fixed commit dates keep the init SHA identical),
    # no shared mutable repo, no clone/remote semantics.
    param([string]$Root)
    if (-not $script:canonTemplate) {
        $tpl = Join-Path $isoRoot 'canonical-template'
        $null = New-Item -ItemType Directory -Path $tpl
        GitRun $tpl @('init')
        GitRun $tpl @('symbolic-ref', 'HEAD', 'refs/heads/main')
        GitRun $tpl @('config', 'user.email', 'test@example.local')
        GitRun $tpl @('config', 'user.name', 'tester')
        GitRun $tpl @('config', 'commit.gpgsign', 'false')
        Set-Content -Path (Join-Path $tpl 'README.md') -Value 'base'
        Set-Content -Path (Join-Path $tpl 'shared.txt') -Value 'base'
        GitRun $tpl @('add', 'README.md', 'shared.txt')
        GitRun $tpl @('commit', '-m', 'init')
        $script:canonTemplate = $tpl
    }
    $canon = Join-Path $Root 'JARVIS'
    Copy-Item -LiteralPath $script:canonTemplate -Destination $canon -Recurse
    # git init marks .git hidden on Windows; Copy-Item does not carry that over.
    (Get-Item -Force -LiteralPath (Join-Path $canon '.git')).Attributes += 'Hidden'
    return $canon
}

function CommitFile {
    param([string]$Repo, [string]$Rel, [string[]]$Content, [string]$Msg)
    $p = Join-Path $Repo $Rel
    $dir = Split-Path -Parent $p
    if (-not (Test-Path -LiteralPath $dir)) { $null = New-Item -ItemType Directory -Force -Path $dir }
    Set-Content -Path $p -Value $Content
    GitRun $Repo @('add', $Rel)
    GitRun $Repo @('commit', '-m', $Msg)
}

function Test-SamePathLocal {
    param([string]$A, [string]$B)
    $a = ([System.IO.Path]::GetFullPath($A)).Replace('/', '\')
    $b = ([System.IO.Path]::GetFullPath($B)).Replace('/', '\')
    return [string]::Equals($a, $b, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-MetaPathLocal {
    param([string]$Canon, [string]$Worker, [string]$Task)
    return Join-Path (Join-Path $Canon ".git\dev-helper\tasks\$Worker") "$Task.json"
}

function Edit-RawMeta {
    # Adversarial helper: mutate the raw metadata JSON the way an attacker
    # would, then write it back.
    param([string]$Canon, [string]$Worker, [string]$Task, [scriptblock]$Edit)
    $p = Get-MetaPathLocal $Canon $Worker $Task
    $meta = Get-Content -LiteralPath $p -Raw | ConvertFrom-Json
    & $Edit $meta
    $json = ConvertTo-Json -InputObject $meta
    [System.IO.File]::WriteAllText($p, $json, (New-Object System.Text.UTF8Encoding($false)))
}

function Get-MarkerPathLocal {
    # The ownership marker of a linked worktree, resolved through Git itself.
    param([string]$Wt)
    $gitDir = (GitOut $Wt @('rev-parse', '--git-dir')).Replace('/', '\')
    return (Join-Path $gitDir 'dev-helper-ownership.json')
}

function Edit-Marker {
    param([string]$Wt, [scriptblock]$Edit)
    $p = Get-MarkerPathLocal $Wt
    $marker = Get-Content -LiteralPath $p -Raw | ConvertFrom-Json
    & $Edit $marker
    $json = ConvertTo-Json -InputObject $marker
    [System.IO.File]::WriteAllText($p, $json, (New-Object System.Text.UTF8Encoding($false)))
}

function New-ForgedMetaJson {
    param([string]$Canon, [string]$Worker, [string]$Task, [string]$BaseSha)
    $branch = "$Worker/$Task"
    $wtPath = Join-Path (Split-Path -Parent $Canon) ("JARVIS-{0}-{1}" -f $(if ($Worker -eq 'glm') { 'GLM' } else { 'Claude' }), $Task)
    $meta = [pscustomobject]@{
        worker           = $Worker
        task             = $Task
        branch           = $branch
        worktreePath     = $wtPath
        baseSha          = $BaseSha
        state            = 'ACTIVE'
        createdAt        = '2026-01-01T00:00:00.0000000Z'
        integratedHead   = $null
        mainHeadAtFinish = $null
        finishedAt       = $null
        sourceTipSha     = $null
        sourceCommitShas = $null
        ownershipNonce   = ('a' * 64)
    }
    return (ConvertTo-Json -InputObject $meta)
}

function Get-TaskHandoffToken {
    # Mirror of the helper's deterministic closed handoff serialization
    # (jarvis-dev-handoff-v1:<worker>:<task>:<nonce>) built from the task's
    # own metadata, exactly as the controller would from `dev.ps1 status`.
    param([string]$Canon, [string]$Worker, [string]$Task)
    $meta = Get-Content -LiteralPath (Get-MetaPathLocal $Canon $Worker $Task) -Raw | ConvertFrom-Json
    return "jarvis-dev-handoff-v1:$Worker`:$Task`:$($meta.ownershipNonce)"
}

function Run-Dev {
    # -StdIn feeds one line to the helper's standard input, so a test can
    # answer the interactive handoff confirmation. The helper's own terminal
    # detection still sees captured output and therefore refuses to prompt
    # unless DEV_HELPER_TEST_ASSUME_INTERACTIVE forces the branch - which is
    # exactly what the H5 tests exercise from both sides.
    param([string]$Cwd, [string[]]$DevArgs, [string]$StdIn = '')
    # Auto-provision the exclusive handoff for finish/cleanup calls from the
    # task's own metadata (the normal controller workflow). An explicitly set
    # env value (attack tests) is never clobbered; suppression is available
    # for handoff-absence tests.
    $op = $null; $w = $null; $t = $null
    if ($DevArgs.Count -ge 3 -and ($DevArgs[0] -eq 'finish' -or $DevArgs[0] -eq 'cleanup')) {
        $op = $DevArgs[0]; $w = $DevArgs[1]; $t = $DevArgs[2]
    }
    $hadHandoff = -not [string]::IsNullOrEmpty($env:JARVIS_DEV_EXCLUSIVE_HANDOFF)
    if ($op -and (-not $hadHandoff) -and (-not $script:SuppressAutoHandoff)) {
        $metaPath = Get-MetaPathLocal $Cwd $w $t
        if (Test-Path -LiteralPath $metaPath) {
            try {
                $meta = Get-Content -LiteralPath $metaPath -Raw | ConvertFrom-Json
                $env:JARVIS_DEV_EXCLUSIVE_HANDOFF = "jarvis-dev-handoff-v1:$w`:$t`:$($meta.ownershipNonce)"
            }
            catch { }
        }
    }
    $old = (Get-Location).ProviderPath
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        Set-Location $Cwd
        if ($StdIn -ne '') {
            $raw = $StdIn | & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $DevPs @DevArgs 2>&1
        } else {
            $raw = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $DevPs @DevArgs 2>&1
        }
        $code = $LASTEXITCODE
    }
    finally {
        Set-Location $old
        $ErrorActionPreference = $prev
        $env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $null
    }
    return [pscustomobject]@{
        Code = $code
        Out  = (@($raw) | ForEach-Object { $_.ToString() }) -join "`n"
    }
}

function Run-Driver {
    # Run a generated PowerShell driver file from a given working directory.
    # The caller must set any required env (handoff) beforehand.
    param([string]$DriverPath, [string]$Cwd)
    $old = (Get-Location).ProviderPath
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        Set-Location $Cwd
        $raw = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $DriverPath 2>&1
        $code = $LASTEXITCODE
    }
    finally {
        Set-Location $old
        $ErrorActionPreference = $prev
    }
    return [pscustomobject]@{
        Code = $code
        Out  = (@($raw) | ForEach-Object { $_.ToString() }) -join "`n"
    }
}

# Shared by the WINDOWS-* sections (one block, split four ways for the shard
# plan) and by the H5 block.
function New-PartialRemovalResidue {
    # EXACT observed residue: git-owned removal completed in Git terms, only
    # the final rmdir did not - the folder stands empty and unregistered and
    # the private admin dir (with the ownership marker) is gone.
    param([string]$Canon, [string]$Wt)
    GitRun $Canon @('worktree', 'remove', $Wt)
    $null = New-Item -ItemType Directory -Path $Wt
}

function New-IntegratedTask {
    # ACTIVE task -> one commit -> integration with cleanup deferred, so the
    # worktree is still registered and the task is INTEGRATED.
    param([string]$Canon, [string]$Root, [string]$Worker, [string]$Task, [string]$WtName)
    $wt = Join-Path $Root $WtName
    $null = Run-Dev $Canon @('new', $Worker, $Task)
    CommitFile $wt 'task.txt' @("$Task work") "$Task work"
    $env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
    $null = Run-Dev $Canon @('finish', $Worker, $Task)
    $env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
    return $wt
}

function Test-BranchExists {
    param([string]$Canon, [string]$Branch)
    try { $null = GitOut $Canon @('rev-parse', '--verify', "refs/heads/$Branch"); return $true } catch { return $false }
}

# ---- environment isolation -------------------------------------------------

$isoRoot = New-TempRoot
$globalCfg = Join-Path $isoRoot 'gitconfig-global'
Set-Content -Path $globalCfg -Value "[user]`n`tname = tester`n`temail = test@example.local`n[init]`n`tdefaultBranch = main`n[commit]`n`tgpgsign = false`n[core]`n`tautocrlf = false"
$env:GIT_CONFIG_GLOBAL = $globalCfg
$env:GIT_CONFIG_NOSYSTEM = '1'
$env:GIT_AUTHOR_NAME = 'tester'
$env:GIT_AUTHOR_EMAIL = 'test@example.local'
$env:GIT_COMMITTER_NAME = 'tester'
$env:GIT_COMMITTER_EMAIL = 'test@example.local'
# Fixed dates: cherry-picks then reproduce byte-identical commits (same SHA)
# whenever the pick parent matches, deterministically exercising the
# identical-SHA ancestry-collapse path the integration proof retains.
$env:GIT_AUTHOR_DATE = '2026-01-01T12:00:00 +0000'
$env:GIT_COMMITTER_DATE = '2026-01-01T12:00:00 +0000'
Remove-Item Env:\DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED -ErrorAction SilentlyContinue

if (Test-Section 'NEW') {
# ============================================================================
Write-Host '== NEW =='
# ============================================================================

# T1: successful GLM worktree creation, current main HEAD auto-recorded as base
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$baseSha = GitOut $canon @('rev-parse', 'HEAD')
$r = Run-Dev $canon @('new', 'glm', 'gmail')
Check 'new glm: exit 0' ($r.Code -eq 0)
Check 'new glm: prints WORKTREE READY' ($r.Out -match 'WORKTREE READY')
Check 'new glm: worktree dir exists' (Test-Path -LiteralPath (Join-Path $root 'JARVIS-GLM-gmail'))
Check 'new glm: branch glm/gmail exists' ((GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/gmail')) -ne '')
$metaPath = Get-MetaPathLocal $canon 'glm' 'gmail'
Check 'new glm: metadata file exists' (Test-Path -LiteralPath $metaPath)
if (Test-Path -LiteralPath $metaPath) {
    $meta = Get-Content -LiteralPath $metaPath -Raw | ConvertFrom-Json
    Check 'new glm: recorded base == current main HEAD' ($meta.baseSha -eq $baseSha)
    Check 'new glm: state ACTIVE' ($meta.state -eq 'ACTIVE')
    Check 'new glm: records branch' ($meta.branch -eq 'glm/gmail')
    Check 'new glm: records worktree path' (Test-SamePathLocal $meta.worktreePath (Join-Path $root 'JARVIS-GLM-gmail'))
    Check 'new glm: nonce is 64-hex generation evidence' ($meta.ownershipNonce -cmatch '^[0-9a-f]{64}$')
} else { Check 'new glm: metadata readable' $false }
# the ownership marker exists in the linked worktree's private admin dir
$markerPath = Get-MarkerPathLocal (Join-Path $root 'JARVIS-GLM-gmail')
Check 'new glm: ownership marker exists' (Test-Path -LiteralPath $markerPath)
$marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
Check 'new glm: marker nonce == metadata nonce' ($marker.ownershipNonce -eq $meta.ownershipNonce)

# T2: successful Claude worktree creation (naming convention)
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$r = Run-Dev $canon @('new', 'claude', 'scheduler')
Check 'new claude: exit 0' ($r.Code -eq 0)
Check 'new claude: worktree JARVIS-Claude-scheduler' (Test-Path -LiteralPath (Join-Path $root 'JARVIS-Claude-scheduler'))
Check 'new claude: branch claude/scheduler exists' ((GitOut $canon @('rev-parse', '--verify', 'refs/heads/claude/scheduler')) -ne '')

# T3: dirty canonical main rejected
$root = New-TempRoot
$canon = New-CanonicalRepo $root
Set-Content -Path (Join-Path $canon 'dirty.txt') -Value 'uncommitted'
$r = Run-Dev $canon @('new', 'glm', 'gmail')
Check 'new dirty main: exit 1' ($r.Code -eq 1)
Check 'new dirty main: no worktree created' (-not (Test-Path -LiteralPath (Join-Path $root 'JARVIS-GLM-gmail')))

# T4: invalid worker rejected
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$r = Run-Dev $canon @('new', 'codex', 'gmail')
Check 'new invalid worker: exit 1' ($r.Code -eq 1)
Check 'new invalid worker: message' ($r.Out -match 'unknown worker')

# T5: malicious / invalid task slugs rejected
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$badSlugs = @('../evil', 'foo bar', 'a;b', '-x', 'Foo', 'a--b', 'x/y', '.', '..')
$allRejected = $true
foreach ($s in $badSlugs) {
    $r = Run-Dev $canon @('new', 'glm', $s)
    if ($r.Code -ne 1) { $allRejected = $false; Write-Host "    slug '$s' was NOT rejected (exit $($r.Code))" }
}
Check 'new bad slugs: all rejected' $allRejected
Check 'new bad slugs: nothing created' ((Get-ChildItem -LiteralPath $root | Where-Object { $_.Name -ne 'JARVIS' }).Count -eq 0)

# T6: collisions rejected (same task twice, pre-existing branch)
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$null = Run-Dev $canon @('new', 'glm', 'gmail')
$r = Run-Dev $canon @('new', 'glm', 'gmail')
Check 'new collision: same task rejected' ($r.Code -eq 1)
Check 'new collision: message' ($r.Out -match 'already exists')
GitRun $canon @('branch', 'glm/manual')
$r = Run-Dev $canon @('new', 'glm', 'manual')
Check 'new collision: pre-existing branch rejected' ($r.Code -eq 1)

# T7: canonical must be on main
$root = New-TempRoot
$canon = New-CanonicalRepo $root
GitRun $canon @('checkout', '-b', 'notmain')
$r = Run-Dev $canon @('new', 'glm', 'gmail')
Check 'new non-main canonical: exit 1' ($r.Code -eq 1)
}

if (Test-Section 'FINISH') {
# ============================================================================
Write-Host '== FINISH =='
# ============================================================================

# T8: clean committed task cherry-picks into main; finish KEEPS the worktree
# and branch (hard lifecycle rule); a separate explicit cleanup removes them.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-fix1'
$null = Run-Dev $canon @('new', 'glm', 'fix1')
CommitFile $wt 'task.txt' @('hello') 'add task file'
$r = Run-Dev $canon @('finish', 'glm', 'fix1')
Check 'finish happy: exit 0' ($r.Code -eq 0)
Check 'finish happy: INTEGRATED' ($r.Out -match 'INTEGRATED')
Check 'finish happy: not CLEANED (separate step)' ($r.Out -notmatch 'CLEANED')
Check 'finish happy: not deferred' ($r.Out -notmatch 'CLEANUP DEFERRED')
Check 'finish happy: change on main' ((Get-Content -LiteralPath (Join-Path $canon 'task.txt') -Raw).Trim() -eq 'hello')
Check 'finish happy: worktree kept' (Test-Path -LiteralPath $wt)
$branchKept = $false
try { $null = GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/fix1'); $branchKept = $true } catch { }
Check 'finish happy: branch kept in refs' $branchKept
Check 'finish happy: metadata kept, INTEGRATED' ((Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'fix1') -Raw | ConvertFrom-Json).state -eq 'INTEGRATED')
$st = GitOut $canon @('status', '--porcelain')
Check 'finish happy: main clean' ([string]::IsNullOrWhiteSpace($st))

# T8b: the separate, explicit cleanup step then removes worktree + branch.
$r = Run-Dev $canon @('cleanup', 'glm', 'fix1')
Check 'cleanup after finish happy: exit 0' ($r.Code -eq 0)
Check 'cleanup after finish happy: CLEANED' ($r.Out -match 'CLEANED')
Check 'cleanup after finish happy: worktree removed' (-not (Test-Path -LiteralPath $wt))
$branchGone = $true
try { $null = GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/fix1'); $branchGone = $false } catch { $branchGone = $true }
Check 'cleanup after finish happy: branch gone from refs' $branchGone
Check 'cleanup after finish happy: metadata removed' (-not (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'fix1')))

# T8c: HARDENING - the property this suite exists to prove. A worktree can
# never be integrated and deleted in one forgotten step: `finish` integrates
# and stops, states the required chat-rebind gate and the exact next
# command, and only a genuinely separate, later `cleanup` invocation removes
# anything.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-hard1'
$null = Run-Dev $canon @('new', 'glm', 'hard1')
CommitFile $wt 'task.txt' @('hardening') 'hardening task'
$r = Run-Dev $canon @('finish', 'glm', 'hard1')
Check 'hardening: finish exit 0' ($r.Code -eq 0)
Check 'hardening: finish never prints CLEANED' ($r.Out -notmatch 'CLEANED')
Check 'hardening: finish states cleanup is a separate step' ($r.Out -match 'separate, explicit step')
Check 'hardening: finish states the chat-rebind gate' ($r.Out -match 'rebind the persistent Claude development chat')
Check 'hardening: finish names the canonical repository path' ($r.Out -match [regex]::Escape($canon))
Check 'hardening: finish names the exact next cleanup command' ($r.Out -match 'cleanup glm hard1')
Check 'hardening: worktree exists after finish' (Test-Path -LiteralPath $wt)
$branchKept = $false
try { $null = GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/hard1'); $branchKept = $true } catch { }
Check 'hardening: branch exists after finish' $branchKept
Check 'hardening: metadata is INTEGRATED after finish' ((Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'hard1') -Raw | ConvertFrom-Json).state -eq 'INTEGRATED')
# cleanup is a genuinely separate, later command: only calling it removes
# anything.
$r = Run-Dev $canon @('cleanup', 'glm', 'hard1')
Check 'hardening: explicit cleanup exit 0' ($r.Code -eq 0)
Check 'hardening: explicit cleanup removes the worktree' (-not (Test-Path -LiteralPath $wt))
$branchGone = $true
try { $null = GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/hard1'); $branchGone = $false } catch { $branchGone = $true }
Check 'hardening: explicit cleanup removes the branch' $branchGone

# T9: main may advance after task creation; task still integrates
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-fix2'
$null = Run-Dev $canon @('new', 'glm', 'fix2')
CommitFile $canon 'mainline.txt' @('mainline') 'mainline advance'
CommitFile $wt 'task.txt' @('from task') 'task change'
$r = Run-Dev $canon @('finish', 'glm', 'fix2')
Check 'finish advanced main: exit 0' ($r.Code -eq 0)
Check 'finish advanced main: task change present' ((Get-Content -LiteralPath (Join-Path $canon 'task.txt') -Raw).Trim() -eq 'from task')
Check 'finish advanced main: mainline change present' ((Get-Content -LiteralPath (Join-Path $canon 'mainline.txt') -Raw).Trim() -eq 'mainline')
$subjects = @(& git -C $canon log --format=%s -3 | ForEach-Object { $_.ToString() })
Check 'finish advanced main: history sane' ($subjects[0] -eq 'task change' -and $subjects[1] -eq 'mainline advance')

# T10: multiple task commits preserve chronological order
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-fix3'
$null = Run-Dev $canon @('new', 'glm', 'fix3')
CommitFile $wt 'seq.txt' @('one') 'task-one'
CommitFile $wt 'seq.txt' @('one', 'two') 'task-two'
$r = Run-Dev $canon @('finish', 'glm', 'fix3')
Check 'finish order: exit 0' ($r.Code -eq 0)
$lines = @(Get-Content -LiteralPath (Join-Path $canon 'seq.txt'))
Check 'finish order: final content correct' ($lines.Count -eq 2 -and $lines[0] -eq 'one' -and $lines[1] -eq 'two')
$subjects = @(& git -C $canon log --format=%s -2 | ForEach-Object { $_.ToString() })
Check 'finish order: chronological order preserved' ($subjects[0] -eq 'task-two' -and $subjects[1] -eq 'task-one')

# T11: dirty task worktree rejected
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-fix4'
$null = Run-Dev $canon @('new', 'glm', 'fix4')
Set-Content -Path (Join-Path $wt 'uncommitted.txt') -Value 'not committed'
$r = Run-Dev $canon @('finish', 'glm', 'fix4')
Check 'finish dirty task: exit 1' ($r.Code -eq 1)
Check 'finish dirty task: not integrated' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'fix4'))
$meta = Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'fix4') -Raw | ConvertFrom-Json
Check 'finish dirty task: still ACTIVE' ($meta.state -eq 'ACTIVE')

# T12: dirty canonical main rejected at finish
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-fix5'
$null = Run-Dev $canon @('new', 'glm', 'fix5')
CommitFile $wt 'task.txt' @('x') 'task work'
$preHead = GitOut $canon @('rev-parse', 'HEAD')
Set-Content -Path (Join-Path $canon 'noise.txt') -Value 'uncommitted'
$r = Run-Dev $canon @('finish', 'glm', 'fix5')
Check 'finish dirty main: exit 1' ($r.Code -eq 1)
Check 'finish dirty main: main HEAD unchanged' ((GitOut $canon @('rev-parse', 'HEAD')) -eq $preHead)

# T13: task with no commits rejected
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$null = Run-Dev $canon @('new', 'glm', 'fix6')
$r = Run-Dev $canon @('finish', 'glm', 'fix6')
Check 'finish no commits: exit 1' ($r.Code -eq 1)
Check 'finish no commits: message' ($r.Out -match 'no commits')

# T14: wrong branch / unexpected substitution rejected
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-fix7'
$null = Run-Dev $canon @('new', 'glm', 'fix7')
CommitFile $wt 'task.txt' @('x') 'task work'
GitRun $wt @('checkout', '-b', 'rogue')
$r = Run-Dev $canon @('finish', 'glm', 'fix7')
Check 'finish wrong branch: exit 1' ($r.Code -eq 1)
Check 'finish wrong branch: message' ($r.Out -match 'branch')

# T15: cherry-pick conflict aborts, PROVES main restored, keeps task intact
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-fix8'
$null = Run-Dev $canon @('new', 'glm', 'fix8')
CommitFile $wt 'shared.txt' @('from task') 'task edits shared'
CommitFile $canon 'shared.txt' @('from main') 'main edits shared'
$preHead = GitOut $canon @('rev-parse', 'HEAD')
$r = Run-Dev $canon @('finish', 'glm', 'fix8')
Check 'finish conflict: exit 1' ($r.Code -eq 1)
Check 'finish conflict: FAILED reported' ($r.Out -match 'FINISH FAILED')
Check 'finish conflict: rollback proven' ($r.Out -match 'proven restored')
Check 'finish conflict: main HEAD restored' ((GitOut $canon @('rev-parse', 'HEAD')) -eq $preHead)
$st = GitOut $canon @('status', '--porcelain')
Check 'finish conflict: main clean' ([string]::IsNullOrWhiteSpace($st))
Check 'finish conflict: no leftover pick state' (-not (Test-Path -LiteralPath (Join-Path $canon '.git\CHERRY_PICK_HEAD')))
Check 'finish conflict: no sequencer state (B6)' (-not (Test-Path -LiteralPath (Join-Path $canon '.git\sequencer')))
Check 'finish conflict: worktree kept' (Test-Path -LiteralPath $wt)
$branchKept = $false
try { $null = GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/fix8'); $branchKept = $true } catch { }
Check 'finish conflict: branch kept' $branchKept
$meta = Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'fix8') -Raw | ConvertFrom-Json
Check 'finish conflict: metadata kept ACTIVE' ($meta.state -eq 'ACTIVE')

# T16: deterministic lock seam - integration succeeds; the SEPARATE explicit
# cleanup step can defer, then a later explicit cleanup retry succeeds. The
# force-deferred env var only affects Remove-WorktreeDir, which now runs
# exclusively inside `cleanup` - finish no longer touches it at all.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-fix9'
$null = Run-Dev $canon @('new', 'glm', 'fix9')
CommitFile $wt 'task.txt' @('deferred') 'deferred task'
$r = Run-Dev $canon @('finish', 'glm', 'fix9')
Check 'finish deferred: exit 0' ($r.Code -eq 0)
Check 'finish deferred: INTEGRATED' ($r.Out -match 'INTEGRATED')
Check 'finish deferred: hints cleanup command' ($r.Out -match 'cleanup glm fix9')
Check 'finish deferred: change on main' ((Get-Content -LiteralPath (Join-Path $canon 'task.txt') -Raw).Trim() -eq 'deferred')
Check 'finish deferred: worktree kept' (Test-Path -LiteralPath $wt)
$branchKept = $false
try { $null = GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/fix9'); $branchKept = $true } catch { }
Check 'finish deferred: branch kept' $branchKept
$meta = Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'fix9') -Raw | ConvertFrom-Json
Check 'finish deferred: state INTEGRATED' ($meta.state -eq 'INTEGRATED')
$intHead = $meta.integratedHead
Check 'finish deferred: integration HEAD recorded' ($intHead -ne $null -and $intHead -ne '')
Check 'status deferred: shows INTEGRATED' ((Run-Dev $canon @('status')).Out -match 'INTEGRATED')

$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$r = Run-Dev $canon @('cleanup', 'glm', 'fix9')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
Check 'cleanup deferred: exit 1' ($r.Code -eq 1)
Check 'cleanup deferred: CLEANUP DEFERRED' ($r.Out -match 'CLEANUP DEFERRED')
Check 'cleanup deferred: worktree kept' (Test-Path -LiteralPath $wt)
$branchKept = $false
try { $null = GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/fix9'); $branchKept = $true } catch { }
Check 'cleanup deferred: branch kept' $branchKept

$r = Run-Dev $canon @('cleanup', 'glm', 'fix9')
Check 'cleanup after deferred: exit 0' ($r.Code -eq 0)
Check 'cleanup after deferred: CLEANED' ($r.Out -match 'CLEANED')
Check 'cleanup after deferred: worktree gone' (-not (Test-Path -LiteralPath $wt))
$branchGone = $true
try { $null = GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/fix9'); $branchGone = $false } catch { }
Check 'cleanup after deferred: branch gone' $branchGone
Check 'cleanup after deferred: metadata gone' (-not (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'fix9')))

# T17: real Windows file lock held by another process
# Finish no longer attempts cleanup (hard lifecycle rule: integration and
# destructive cleanup are always separate, explicit invocations), so the
# lock is now held during the SEPARATE, explicit `cleanup` call instead of
# during `finish` - that is the step it can actually affect.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-fix10'
$null = Run-Dev $canon @('new', 'glm', 'fix10')
CommitFile $wt 'README.md' @('task version') 'task edits readme'
$r = Run-Dev $canon @('finish', 'glm', 'fix10')
Check 'finish real lock: exit 0' ($r.Code -eq 0)
Check 'finish real lock: INTEGRATED' ($r.Out -match 'INTEGRATED')
Check 'finish real lock: worktree kept (cleanup is separate)' (Test-Path -LiteralPath $wt)
Check 'finish real lock: change on main' ((Get-Content -LiteralPath (Join-Path $canon 'README.md') -Raw).Trim() -eq 'task version')
$meta = Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'fix10') -Raw | ConvertFrom-Json
Check 'finish real lock: state INTEGRATED' ($meta.state -eq 'INTEGRATED')

# Realistic lock: another process holds README.md open so it cannot be
# DELETED, but git can still read it (e.g. an editor or ZCode session).
$lock = [System.IO.File]::Open((Join-Path $wt 'README.md'), 'Open', 'Read', 'Read')
try {
    $r = Run-Dev $canon @('cleanup', 'glm', 'fix10')
} finally {
    $lock.Close()
}
Check 'cleanup real lock: exit 1' ($r.Code -eq 1)
Check 'cleanup real lock: CLEANUP DEFERRED' ($r.Out -match 'CLEANUP DEFERRED')
# git partially removed the worktree: dir remains but git deregistered it.
# The helper never deletes that unregistered leftover itself.
Check 'cleanup real lock: leftover folder kept' (Test-Path -LiteralPath $wt)
Check 'cleanup real lock: locked file byte-for-byte' ((Get-Content -LiteralPath (Join-Path $wt 'README.md') -Raw).Trim() -eq 'task version')

$r = Run-Dev $canon @('cleanup', 'glm', 'fix10')
Check 'cleanup real lock leftover: exit 1' ($r.Code -eq 1)
# Goal C: the deregistered-but-non-empty leftover is reported as a recovery
# state naming the real reason, never as "worktree removal failed".
Check 'cleanup real lock leftover: RECOVERY REQUIRED' ($r.Out -match 'RECOVERY REQUIRED: WORKTREE NOT REGISTERED')
Check 'cleanup real lock leftover: names the retained content' ($r.Out -match 'still holds content')
Check 'cleanup real lock leftover: not misreported as removal failure' ($r.Out -notmatch 'worktree removal failed')
Check 'cleanup real lock leftover: folder still kept' (Test-Path -LiteralPath $wt)
Check 'cleanup real lock leftover: file still intact' ((Get-Content -LiteralPath (Join-Path $wt 'README.md') -Raw).Trim() -eq 'task version')
$branchKept = $false
try { $null = GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/fix10'); $branchKept = $true } catch { }
Check 'cleanup real lock leftover: branch kept' $branchKept
Check 'cleanup real lock leftover: metadata kept' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'fix10'))
# user deletes the leftover folder manually (helper never does): the missing
# worktree now triggers the ABSOLUTE recovery policy - branch and metadata
# are preserved, never destructively cleaned from metadata/history alone.
Remove-Item -LiteralPath $wt -Recurse -Force
$r = Run-Dev $canon @('cleanup', 'glm', 'fix10')
Check 'cleanup missing worktree: exit 1' ($r.Code -eq 1)
Check 'cleanup missing worktree: RECOVERY REQUIRED' ($r.Out -match 'RECOVERY REQUIRED: WORKTREE MISSING')
$branchKept = $false
try { $null = GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/fix10'); $branchKept = $true } catch { }
Check 'cleanup missing worktree: branch preserved' $branchKept
Check 'cleanup missing worktree: metadata preserved' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'fix10'))
}

if (Test-Section 'CLEANUP') {
# ============================================================================
Write-Host '== CLEANUP =='
# ============================================================================

# T18: unintegrated task cleanup refused
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-c1'
$null = Run-Dev $canon @('new', 'glm', 'c1')
$r = Run-Dev $canon @('cleanup', 'glm', 'c1')
Check 'cleanup unintegrated: exit 1' ($r.Code -eq 1)
Check 'cleanup unintegrated: refused message' ($r.Out -match 'refused')
Check 'cleanup unintegrated: worktree kept' (Test-Path -LiteralPath $wt)

# T19: dirty integrated task cleanup deferred, content preserved
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-c2'
$null = Run-Dev $canon @('new', 'glm', 'c2')
CommitFile $wt 'task.txt' @('x') 'task work'
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$null = Run-Dev $canon @('finish', 'glm', 'c2')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
Set-Content -Path (Join-Path $wt 'junk.txt') -Value 'precious uncommitted work'
$r = Run-Dev $canon @('cleanup', 'glm', 'c2')
Check 'cleanup dirty integrated: exit 1' ($r.Code -eq 1)
Check 'cleanup dirty integrated: deferred label' ($r.Out -match 'CLEANUP DEFERRED')
Check 'cleanup dirty integrated: worktree kept' (Test-Path -LiteralPath $wt)
Check 'cleanup dirty integrated: junk kept' (Test-Path -LiteralPath (Join-Path $wt 'junk.txt'))
Check 'cleanup dirty integrated: metadata kept' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'c2'))
Remove-Item -LiteralPath (Join-Path $wt 'junk.txt') -Force
$r = Run-Dev $canon @('cleanup', 'glm', 'c2')
Check 'cleanup dirty integrated: succeeds when clean' ($r.Code -eq 0)

# T20: cleanup cannot target arbitrary / unknown worktrees
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$otherWt = Join-Path $root 'JARVIS-other'
GitRun $canon @('worktree', 'add', '-b', 'other', $otherWt)
GitRun $otherWt @('config', 'user.email', 'test@example.local')
GitRun $otherWt @('config', 'user.name', 'tester')
$r = Run-Dev $canon @('cleanup', 'glm', 'other')
Check 'cleanup arbitrary: exit 1' ($r.Code -eq 1)
Check 'cleanup arbitrary: worktree untouched' (Test-Path -LiteralPath $otherWt)
$r = Run-Dev $canon @('cleanup', 'glm', 'doesnotexist')
Check 'cleanup unknown task: exit 1' ($r.Code -eq 1)
}

if (Test-Section 'STATUS') {
# ============================================================================
Write-Host '== STATUS =='
# ============================================================================

# T21: status reports ACTIVE and INTEGRATED states, read-only
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$r = Run-Dev $canon @('status')
Check 'status empty: exit 0' ($r.Code -eq 0)
Check 'status empty: message' ($r.Out -match 'no helper tasks')
$null = Run-Dev $canon @('new', 'glm', 'st1')
$null = Run-Dev $canon @('new', 'claude', 'st2')
$r = Run-Dev $canon @('status')
Check 'status active: exit 0' ($r.Code -eq 0)
Check 'status active: shows glm/st1' ($r.Out -match 'glm/st1')
Check 'status active: shows claude/st2' ($r.Out -match 'claude/st2')
Check 'status active: ACTIVE x2' ((@($r.Out -split "`n") | Where-Object { $_ -match 'ACTIVE' }).Count -eq 2)
CommitFile (Join-Path $root 'JARVIS-GLM-st1') 't.txt' @('x') 'work'
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$null = Run-Dev $canon @('finish', 'glm', 'st1')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
$r = Run-Dev $canon @('status')
Check 'status mixed: INTEGRATED reported' ($r.Out -match 'INTEGRATED')
Check 'status mixed: still shows both tasks' (($r.Out -match 'st1') -and ($r.Out -match 'st2'))
Check 'status mixed: prints handoff tokens' ($r.Out -match 'handoff=jarvis-dev-handoff-v1:glm:st1:')
# status never mutates: the deferred task is untouched by the listing
Check 'status read-only: task still INTEGRATED after listing' (((Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'st1') -Raw | ConvertFrom-Json).state) -eq 'INTEGRATED')
}

if (Test-Section 'CLI') {
# ============================================================================
Write-Host '== CLI =='
# ============================================================================

# T22: usage / missing args
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$r = Run-Dev $canon @()
Check 'cli no args: exit 1' ($r.Code -eq 1)
Check 'cli no args: usage shown' ($r.Out -match 'usage')
$r = Run-Dev $canon @('new', 'glm')
Check 'cli missing task: exit 1' ($r.Code -eq 1)
$r = Run-Dev $canon @('bogus')
Check 'cli unknown command: exit 1' ($r.Code -eq 1)
}

if (Test-Section 'FINISH-CURRENT') {
# ============================================================================
Write-Host '== FINISH-CURRENT =='
# ============================================================================

# T23: finish-current succeeds from the correct helper-managed worktree
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-fc1'
$null = Run-Dev $canon @('new', 'glm', 'fc1')
CommitFile $wt 'task.txt' @('fc1 change') 'fc1 work'
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = Get-TaskHandoffToken -Canon $canon -Worker 'glm' -Task 'fc1'
$r = Run-Dev $wt @('finish-current')
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $null
Check 'finish-current happy: exit 0' ($r.Code -eq 0)
Check 'finish-current happy: INTEGRATED' ($r.Out -match 'INTEGRATED')
Check 'finish-current happy: not CLEANED (separate step)' ($r.Out -notmatch 'CLEANED')
Check 'finish-current happy: change on main' ((Get-Content -LiteralPath (Join-Path $canon 'task.txt') -Raw).Trim() -eq 'fc1 change')
Check 'finish-current happy: worktree kept' (Test-Path -LiteralPath $wt)
Check 'finish-current happy: metadata kept, INTEGRATED' ((Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'fc1') -Raw | ConvertFrom-Json).state -eq 'INTEGRATED')
$st = GitOut $canon @('status', '--porcelain')
Check 'finish-current happy: main clean' ([string]::IsNullOrWhiteSpace($st))

# T23b: the separate, explicit cleanup step then removes worktree + branch.
$r = Run-Dev $canon @('cleanup', 'glm', 'fc1')
Check 'cleanup after finish-current happy: exit 0' ($r.Code -eq 0)
Check 'cleanup after finish-current happy: CLEANED' ($r.Out -match 'CLEANED')
Check 'cleanup after finish-current happy: worktree removed' (-not (Test-Path -LiteralPath $wt))
Check 'cleanup after finish-current happy: metadata gone' (-not (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'fc1')))

# T24: finish-current refuses the canonical main worktree
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$null = Run-Dev $canon @('new', 'glm', 'fc2')
$r = Run-Dev $canon @('finish-current')
Check 'finish-current canonical: exit 1' ($r.Code -eq 1)
Check 'finish-current canonical: message' ($r.Out -match 'canonical main worktree')
Check 'finish-current canonical: task untouched' (Test-Path -LiteralPath (Join-Path $root 'JARVIS-GLM-fc2'))

# T25: finish-current refuses an arbitrary unmanaged worktree
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$otherWt = Join-Path $root 'JARVIS-unmanaged'
GitRun $canon @('worktree', 'add', '-b', 'unmanaged', $otherWt)
$r = Run-Dev $otherWt @('finish-current')
Check 'finish-current unmanaged: exit 1' ($r.Code -eq 1)
Check 'finish-current unmanaged: message' ($r.Out -match 'not a helper-managed task worktree')
Check 'finish-current unmanaged: worktree untouched' (Test-Path -LiteralPath $otherWt)

# T26: finish-current refuses when branch does not match metadata
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-fc3'
$null = Run-Dev $canon @('new', 'glm', 'fc3')
CommitFile $wt 'task.txt' @('x') 'fc3 work'
GitRun $wt @('checkout', '-b', 'rogue')
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = Get-TaskHandoffToken -Canon $canon -Worker 'glm' -Task 'fc3'
$r = Run-Dev $wt @('finish-current')
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $null
Check 'finish-current wrong branch: exit 1' ($r.Code -eq 1)
Check 'finish-current wrong branch: message' ($r.Out -match 'branch')
Check 'finish-current wrong branch: not integrated' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'fc3'))

# T27: finish-current refuses an already INTEGRATED (deferred) task
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-fc4'
$null = Run-Dev $canon @('new', 'glm', 'fc4')
CommitFile $wt 'task.txt' @('x') 'fc4 work'
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$null = Run-Dev $canon @('finish', 'glm', 'fc4')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
$r = Run-Dev $wt @('finish-current')
Check 'finish-current already integrated: exit 1' ($r.Code -eq 1)
Check 'finish-current already integrated: message' ($r.Out -match 'not ACTIVE')
Check 'finish-current already integrated: still INTEGRATED' ((Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'fc4') -Raw | ConvertFrom-Json).state -eq 'INTEGRATED')

# T28: finish-current from a subdirectory of the task worktree still works
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-fc5'
$null = Run-Dev $canon @('new', 'glm', 'fc5')
CommitFile $wt 'task.txt' @('x') 'fc5 work'
$null = New-Item -ItemType Directory -Force -Path (Join-Path $wt 'src\deep')
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = Get-TaskHandoffToken -Canon $canon -Worker 'glm' -Task 'fc5'
$r = Run-Dev (Join-Path $wt 'src\deep') @('finish-current')
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $null
Check 'finish-current subdir: exit 0' ($r.Code -eq 0)
Check 'finish-current subdir: INTEGRATED' ($r.Out -match 'INTEGRATED')
$r = Run-Dev $canon @('cleanup', 'glm', 'fc5')
Check 'cleanup after finish-current subdir: CLEANED' ($r.Out -match 'CLEANED')

# T29: deferred cleanup path is prompt-free (no y/n questions anywhere)
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-fc6'
$null = Run-Dev $canon @('new', 'glm', 'fc6')
CommitFile $wt 'task.txt' @('x') 'fc6 work'
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$r = Run-Dev $canon @('finish', 'glm', 'fc6')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
Check 'deferred finish: no prompt characters' (($r.Out -match '\?') -eq $false -and ($r.Out -match 'y/n') -eq $false)
Check 'deferred finish: deterministic exit 0' ($r.Code -eq 0)
$r = Run-Dev $canon @('cleanup', 'glm', 'fc6')
Check 'deferred cleanup: no prompt characters' (($r.Out -match '\?') -eq $false -and ($r.Out -match 'y/n') -eq $false)
Check 'deferred cleanup: deterministic exit 0' ($r.Code -eq 0)
}

if (Test-Section 'ADVERSARIAL') {
# ============================================================================
Write-Host '== ADVERSARIAL (destructive-safety regressions) =='
# ============================================================================

# A1: cross-task metadata substitution
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wtV = Join-Path $root 'JARVIS-GLM-victim'
$wtO = Join-Path $root 'JARVIS-GLM-other'
$null = Run-Dev $canon @('new', 'glm', 'victim')
$null = Run-Dev $canon @('new', 'glm', 'other')
CommitFile $wtV 'victim.txt' @('victim-secret') 'victim work'
CommitFile $wtO 'other.txt' @('other-secret') 'other work'
Edit-RawMeta $canon 'glm' 'victim' { param($m) $m.branch = 'glm/other'; $m.worktreePath = $wtO }
$r = Run-Dev $canon @('finish', 'glm', 'victim')
Check 'A1 finish victim: exit 1' ($r.Code -eq 1)
Check 'A1 finish victim: mismatch message' ($r.Out -match 'metadata mismatch')
Check 'A1 other task not integrated' (-not (Test-Path -LiteralPath (Join-Path $canon 'other.txt')))
Check 'A1 victim task not integrated' (-not (Test-Path -LiteralPath (Join-Path $canon 'victim.txt')))
$bothBranches = $true
foreach ($b in @('glm/victim', 'glm/other')) {
    try { $null = GitOut $canon @('rev-parse', '--verify', "refs/heads/$b") } catch { $bothBranches = $false }
}
Check 'A1 both branches survive' $bothBranches
Check 'A1 both worktrees survive' ((Test-Path -LiteralPath $wtV) -and (Test-Path -LiteralPath $wtO))
Check 'A1 victim sentinel intact' ((Get-Content -LiteralPath (Join-Path $wtV 'victim.txt') -Raw).Trim() -eq 'victim-secret')
Check 'A1 other sentinel intact' ((Get-Content -LiteralPath (Join-Path $wtO 'other.txt') -Raw).Trim() -eq 'other-secret')
$r = Run-Dev $canon @('cleanup', 'glm', 'victim')
Check 'A1 cleanup victim also refused' ($r.Code -eq 1)

# A2: arbitrary outside path in metadata
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-esc'
$null = Run-Dev $canon @('new', 'glm', 'esc')
CommitFile $wt 'task.txt' @('x') 'esc work'
$outside = Join-Path $root 'outside-dir'
$null = New-Item -ItemType Directory -Force -Path $outside
Set-Content -Path (Join-Path $outside 'sentinel.txt') -Value 'DO-NOT-DELETE-123'
Edit-RawMeta $canon 'glm' 'esc' { param($m) $m.worktreePath = $outside }
$r = Run-Dev $canon @('finish', 'glm', 'esc')
Check 'A2 finish with outside path: exit 1' ($r.Code -eq 1)
$r = Run-Dev $canon @('cleanup', 'glm', 'esc')
Check 'A2 cleanup with outside path: exit 1' ($r.Code -eq 1)
Check 'A2 outside dir still exists' (Test-Path -LiteralPath $outside)
Check 'A2 sentinel byte-for-byte' ((Get-Content -LiteralPath (Join-Path $outside 'sentinel.txt') -Raw).Trim() -eq 'DO-NOT-DELETE-123')
Check 'A2 task worktree untouched' (Test-Path -LiteralPath $wt)

# A3: forged INTEGRATED state with unintegrated commit
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-forge'
$null = Run-Dev $canon @('new', 'glm', 'forge')
CommitFile $wt 'task.txt' @('unintegrated') 'forge work'
$mainHead = GitOut $canon @('rev-parse', 'HEAD')
$branchTip = GitOut $canon @('rev-parse', 'refs/heads/glm/forge')
Edit-RawMeta $canon 'glm' 'forge' {
    param($m)
    $m.state = 'INTEGRATED'
    $m.integratedHead = $mainHead
    $m.mainHeadAtFinish = $mainHead
    $m.finishedAt = '2026-01-01T00:00:00.0000000Z'
    $m.sourceTipSha = $branchTip
    $m.sourceCommitShas = @($branchTip)
}
$r = Run-Dev $canon @('cleanup', 'glm', 'forge')
Check 'A3 forged cleanup: exit 1' ($r.Code -eq 1)
Check 'A3 forged cleanup: unintegrated message' ($r.Out -match 'not proven integrated')
Check 'A3 forged cleanup: worktree kept' (Test-Path -LiteralPath $wt)
$branchKept = $false
try { $null = GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/forge'); $branchKept = $true } catch { }
Check 'A3 forged cleanup: branch kept' $branchKept
Check 'A3 forged cleanup: commit not integrated' (-not (Test-Path -LiteralPath (Join-Path $canon 'task.txt')))
Check 'A3 forged cleanup: metadata kept' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'forge'))

# A4: new commit added to task branch after legitimate integration
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-post'
$null = Run-Dev $canon @('new', 'glm', 'post')
CommitFile $wt 'task.txt' @('first') 'post work'
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$null = Run-Dev $canon @('finish', 'glm', 'post')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
CommitFile $wt 'extra.txt' @('late work') 'late commit'
$r = Run-Dev $canon @('cleanup', 'glm', 'post')
Check 'A4 post-integration commit cleanup: exit 1' ($r.Code -eq 1)
Check 'A4 post-integration commit cleanup: REF CHANGED' ($r.Out -match 'REF CHANGED')
Check 'A4 late commit survives in worktree' ((Get-Content -LiteralPath (Join-Path $wt 'extra.txt') -Raw).Trim() -eq 'late work')
$lastSubject = GitOut $wt @('log', '--format=%s', '-1')
Check 'A4 late commit still on branch' ($lastSubject -eq 'late commit')
Check 'A4 late commit not in main' (-not (Test-Path -LiteralPath (Join-Path $canon 'extra.txt')))
Check 'A4 metadata kept' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'post'))

# A5: partial integration (main contains only a subset of task commits)
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-part'
$null = Run-Dev $canon @('new', 'glm', 'part')
CommitFile $wt 'a.txt' @('one') 'part-one'
CommitFile $wt 'b.txt' @('two') 'part-two'
$baseSha = (Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'part') -Raw | ConvertFrom-Json).baseSha
$taskShas = @((& git -C $wt rev-list --reverse "$($baseSha)..glm/part") | ForEach-Object { $_.ToString() })
GitRun $canon @('cherry-pick', $taskShas[0])
$mainHead = GitOut $canon @('rev-parse', 'HEAD')
$branchTip = GitOut $canon @('rev-parse', 'refs/heads/glm/part')
Edit-RawMeta $canon 'glm' 'part' {
    param($m)
    $m.state = 'INTEGRATED'
    $m.integratedHead = $mainHead
    $m.mainHeadAtFinish = $mainHead
    $m.finishedAt = '2026-01-01T00:00:00.0000000Z'
    $m.sourceTipSha = $branchTip
    $m.sourceCommitShas = @($taskShas[0], $taskShas[1])
}
$r = Run-Dev $canon @('cleanup', 'glm', 'part')
Check 'A5 partial integration cleanup: exit 1' ($r.Code -eq 1)
Check 'A5 partial integration cleanup: refused' ($r.Out -match 'not proven integrated')
Check 'A5 unintegrated commit survives' ((Get-Content -LiteralPath (Join-Path $wt 'b.txt') -Raw).Trim() -eq 'two')
Check 'A5 branch kept' ((GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/part')) -ne '')

# A6: metadata branch substitution (+ missing-field and unknown-field)
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$null = Run-Dev $canon @('new', 'glm', 'sub')
$null = Run-Dev $canon @('new', 'glm', 'sub2')
Edit-RawMeta $canon 'glm' 'sub' { param($m) $m.branch = 'glm/sub2' }
$r = Run-Dev $canon @('finish', 'glm', 'sub')
Check 'A6 finish branch substitution: exit 1' ($r.Code -eq 1)
$r = Run-Dev $canon @('cleanup', 'glm', 'sub')
Check 'A6 cleanup branch substitution: exit 1' ($r.Code -eq 1)
$bothBranches = $true
foreach ($b in @('glm/sub', 'glm/sub2')) {
    try { $null = GitOut $canon @('rev-parse', '--verify', "refs/heads/$b") } catch { $bothBranches = $false }
}
Check 'A6 both branches survive' $bothBranches
Edit-RawMeta $canon 'glm' 'sub' { param($m) $m.branch = 123 }
$r = Run-Dev $canon @('finish', 'glm', 'sub')
Check 'A6 wrong type refused: exit 1' ($r.Code -eq 1)
Edit-RawMeta $canon 'glm' 'sub' { param($m) $m.baseSha = 'deadbeef' }
$r = Run-Dev $canon @('finish', 'glm', 'sub')
Check 'A6 malformed SHA refused: exit 1' ($r.Code -eq 1)
Edit-RawMeta $canon 'glm' 'sub' { param($m) $m | Add-Member -NotePropertyName evil -NotePropertyValue 'x' }
$r = Run-Dev $canon @('finish', 'glm', 'sub')
Check 'A6 unknown field refused: exit 1' ($r.Code -eq 1)
Check 'A6 unknown field refused: message' ($r.Out -match "unknown field")
Edit-RawMeta $canon 'glm' 'sub' { param($m) $m.PSObject.Properties.Remove('evil'); $m.PSObject.Properties.Remove('branch') }
$r = Run-Dev $canon @('finish', 'glm', 'sub')
Check 'A6 missing field refused: exit 1' ($r.Code -eq 1)
Check 'A6 missing field refused: message' ($r.Out -match "missing field")

# A7: canonical main substitution attempt
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$null = Run-Dev $canon @('new', 'glm', 'cn')
CommitFile (Join-Path $root 'JARVIS-GLM-cn') 'task.txt' @('x') 'cn work'
Edit-RawMeta $canon 'glm' 'cn' { param($m) $m.worktreePath = $root }
$r = Run-Dev $canon @('finish', 'glm', 'cn')
Check 'A7 finish main-path substitution: exit 1' ($r.Code -eq 1)
$r = Run-Dev $canon @('cleanup', 'glm', 'cn')
Check 'A7 cleanup main-path substitution: exit 1' ($r.Code -eq 1)
$st = GitOut $canon @('status', '--porcelain')
Check 'A7 canonical main intact and clean' ([string]::IsNullOrWhiteSpace($st))
$canonBranch = GitOut $canon @('rev-parse', '--abbrev-ref', 'HEAD')
Check 'A7 canonical still on main' ($canonBranch -eq 'main')

# A8: finish-current metadata substitution
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-cur'
$wt2 = Join-Path $root 'JARVIS-GLM-cur2'
$null = Run-Dev $canon @('new', 'glm', 'cur')
$null = Run-Dev $canon @('new', 'glm', 'cur2')
CommitFile $wt 'task.txt' @('x') 'cur work'
Edit-RawMeta $canon 'glm' 'cur' { param($m) $m.branch = 'glm/cur2'; $m.worktreePath = $wt2 }
$r = Run-Dev $wt @('finish-current')
Check 'A8 finish-current substitution: exit 1' ($r.Code -eq 1)
Check 'A8 finish-current substitution: message' ($r.Out -match 'metadata mismatch')
Check 'A8 current task worktree survives' (Test-Path -LiteralPath $wt)
Check 'A8 other task worktree survives' (Test-Path -LiteralPath $wt2)
Check 'A8 nothing integrated' (-not (Test-Path -LiteralPath (Join-Path $canon 'task.txt')))

# A9: rewritten / rebased task history
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-rw'
$null = Run-Dev $canon @('new', 'glm', 'rw')
CommitFile $wt 'task.txt' @('v1') 'rw work'
CommitFile $canon 'mainline.txt' @('x') 'mainline advance'
GitRun $wt @('rebase', 'main')
$r = Run-Dev $canon @('finish', 'glm', 'rw')
Check 'A9 finish rebased history: exit 1' ($r.Code -eq 1)
Check 'A9 finish rebased history: message' ($r.Out -match 'diverge')
$r = Run-Dev $canon @('cleanup', 'glm', 'rw')
Check 'A9 cleanup rebased history: exit 1' ($r.Code -eq 1)
Check 'A9 worktree survives' (Test-Path -LiteralPath $wt)
$mainSubjects = @(& git -C $canon log --format=%s -1 | ForEach-Object { $_.ToString() })
Check 'A9 main unchanged by refusal' ($mainSubjects[0] -eq 'mainline advance')
}

if (Test-Section 'ADVERSARIAL-V2') {
# ============================================================================
Write-Host '== ADVERSARIAL V2 (linear history, final gate, ownership) =='
# ============================================================================

# B1: merge commits are refused at finish AND cleanup; content survives.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-mg'
$null = Run-Dev $canon @('new', 'glm', 'mg')
CommitFile $wt 'ordinary.txt' @('ordinary') 'ordinary task work'
GitRun $wt @('checkout', '-b', 'tmp-side')
CommitFile $wt 'merge-only.txt' @('unique merge content') 'merge side work'
GitRun $wt @('checkout', 'glm/mg')
GitRun $wt @('merge', '--no-ff', '-m', 'merge side into task', 'tmp-side')
$preFinishHead = GitOut $canon @('rev-parse', 'HEAD')
$r = Run-Dev $canon @('finish', 'glm', 'mg')
Check 'B1 finish merge: exit 1' ($r.Code -eq 1)
Check 'B1 finish merge: diagnostic' ($r.Out -match 'UNSUPPORTED TASK HISTORY: MERGE COMMIT')
Check 'B1 finish merge: main HEAD unchanged' ((GitOut $canon @('rev-parse', 'HEAD')) -eq $preFinishHead)
Check 'B1 finish merge: main has no merge-only content' (-not (Test-Path -LiteralPath (Join-Path $canon 'merge-only.txt')))
Check 'B1 finish merge: worktree kept' (Test-Path -LiteralPath $wt)
# cleanup path: forge coherent INTEGRATED metadata; the ordinary non-merge
# patch is even already present in main (as in the gate attack), but the
# merge commit refuses cleanup outright.
$baseSha = (Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'mg') -Raw | ConvertFrom-Json).baseSha
$taskShas = @((& git -C $wt rev-list --reverse "$baseSha..glm/mg") | ForEach-Object { $_.ToString() })
GitRun $canon @('cherry-pick', $taskShas[0])
$mainHead = GitOut $canon @('rev-parse', 'HEAD')
$tip = GitOut $canon @('rev-parse', 'refs/heads/glm/mg')
Edit-RawMeta $canon 'glm' 'mg' {
    param($m)
    $m.state = 'INTEGRATED'
    $m.integratedHead = $mainHead
    $m.mainHeadAtFinish = $mainHead
    $m.finishedAt = '2026-01-01T00:00:00.0000000Z'
    $m.sourceTipSha = $tip
    $m.sourceCommitShas = $taskShas
}
$r = Run-Dev $canon @('cleanup', 'glm', 'mg')
Check 'B1 cleanup merge: exit 1' ($r.Code -eq 1)
Check 'B1 cleanup merge: diagnostic' ($r.Out -match 'MERGE COMMIT')
Check 'B1 cleanup merge: worktree kept' (Test-Path -LiteralPath $wt)
Check 'B1 cleanup merge: branch kept' ((GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/mg')) -ne '')
Check 'B1 cleanup merge: merge-only content survives' ((Get-Content -LiteralPath (Join-Path $wt 'merge-only.txt') -Raw).Trim() -eq 'unique merge content')

# B2: final-gate TOCTOU - branch ref moves between the integration proof and
# the destructive removal, on BOTH the finish automatic-cleanup path and the
# explicit cleanup path (via the production no-op seam).
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-toc'
$null = Run-Dev $canon @('new', 'glm', 'toc')
CommitFile $wt 'task.txt' @('toc work') 'toc work'
# legitimately integrate first, deferring cleanup (folder still registered)
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$null = Run-Dev $canon @('finish', 'glm', 'toc')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
$driverPath = Join-Path $root 'driver-cleanup.ps1'
@'
. '__DEVPS__'
function Invoke-PreDeleteTestHook {
    param($ctx, $identity, $meta)
    Set-Content -LiteralPath (Join-Path $identity.WorktreePath 'late.txt') -Value 'late work'
    & git -C $identity.WorktreePath add late.txt
    & git -C $identity.WorktreePath commit -m 'late commit' | Out-Null
}
try {
    Invoke-CleanupTask -Worker 'glm' -Task 'toc'
    exit 0
}
catch {
    Write-Host "DRIVER-CAUGHT: $($_.Exception.Message)"
    exit 1
}
'@ -replace '__DEVPS__', $DevPs | Set-Content -LiteralPath $driverPath -Encoding UTF8
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = Get-TaskHandoffToken -Canon $canon -Worker 'glm' -Task 'toc'
$r = Run-Driver $driverPath $canon
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $null
Check 'B2 cleanup TOCTOU: exit 1' ($r.Code -eq 1)
Check 'B2 cleanup TOCTOU: gate noticed ref change' ($r.Out -match 'task branch ref changed since integration')
Check 'B2 cleanup TOCTOU: late commit survives' ((Get-Content -LiteralPath (Join-Path $wt 'late.txt') -Raw).Trim() -eq 'late work')
Check 'B2 cleanup TOCTOU: worktree kept' (Test-Path -LiteralPath $wt)
Check 'B2 cleanup TOCTOU: metadata kept' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'toc'))
# finish no longer HAS an automatic-cleanup path to race: the hard lifecycle
# rule means Invoke-FinishTask never reaches Invoke-CleanupCore, so the TOCTOU
# scenario above (which lives entirely inside the destructive gate) cannot
# recur through finish at all. Proven directly - the pre-delete hook must
# never fire from finish - rather than duplicating the cleanup-path TOCTOU
# test above with an entry point that can no longer reach it.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-tof'
$null = Run-Dev $canon @('new', 'glm', 'tof')
CommitFile $wt 'task.txt' @('tof work') 'tof work'
$driverPath = Join-Path $root 'driver-finish.ps1'
@'
. '__DEVPS__'
$script:hookFired = $false
function Invoke-PreDeleteTestHook {
    param($ctx, $identity, $meta)
    $script:hookFired = $true
}
try {
    Invoke-FinishTask -Worker 'glm' -Task 'tof'
    if ($script:hookFired) { Write-Host 'HOOK-FIRED-UNEXPECTEDLY'; exit 1 }
    exit 0
}
catch {
    Write-Host "DRIVER-CAUGHT: $($_.Exception.Message)"
    exit 1
}
'@ -replace '__DEVPS__', $DevPs | Set-Content -LiteralPath $driverPath -Encoding UTF8
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = Get-TaskHandoffToken -Canon $canon -Worker 'glm' -Task 'tof'
$r = Run-Driver $driverPath $canon
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $null
Check 'B2 finish never reaches cleanup: exit 0' ($r.Code -eq 0)
Check 'B2 finish never reaches cleanup: integration succeeded' ($r.Out -match 'INTEGRATED')
Check 'B2 finish never reaches cleanup: pre-delete hook never fired' ($r.Out -notmatch 'HOOK-FIRED-UNEXPECTEDLY')
Check 'B2 finish never reaches cleanup: worktree kept' (Test-Path -LiteralPath $wt)
$branchKept = $false
try { $null = GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/tof'); $branchKept = $true } catch { }
Check 'B2 finish never reaches cleanup: branch kept' $branchKept
$meta = Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'tof') -Raw | ConvertFrom-Json
Check 'B2 finish never reaches cleanup: metadata INTEGRATED' ($meta.state -eq 'INTEGRATED')

# B3: exact-name unmanaged worktree with otherwise-valid forged metadata and
# NO ownership marker is refused everywhere.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
GitRun $canon @('worktree', 'add', '-b', 'glm/exact', (Join-Path $root 'JARVIS-GLM-exact'))
$metaDir = Join-Path $canon '.git\dev-helper\tasks\glm'
$null = New-Item -ItemType Directory -Force -Path $metaDir
$baseSha = GitOut $canon @('rev-parse', 'HEAD')
[System.IO.File]::WriteAllText(
    (Join-Path $metaDir 'exact.json'),
    (New-ForgedMetaJson -Canon $canon -Worker 'glm' -Task 'exact' -BaseSha $baseSha),
    (New-Object System.Text.UTF8Encoding($false)))
# finish (ACTIVE forge): worktree proofs pass, then the marker refuses
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = Get-TaskHandoffToken -Canon $canon -Worker 'glm' -Task 'exact'
$r = Run-Dev $canon @('finish', 'glm', 'exact')
Check 'B3 finish exact-name: exit 1' ($r.Code -eq 1)
Check 'B3 finish exact-name: marker message' ($r.Out -match 'ownership evidence refused')
# Run-Dev clears the env after each call; re-set the handoff for finish-current
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = Get-TaskHandoffToken -Canon $canon -Worker 'glm' -Task 'exact'
$r = Run-Dev (Join-Path $root 'JARVIS-GLM-exact') @('finish-current')
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $null
Check 'B3 finish-current exact-name: exit 1' ($r.Code -eq 1)
Check 'B3 finish-current exact-name: marker message' ($r.Out -match 'ownership evidence refused')
# cleanup (INTEGRATED-coherent forge): the final gate's marker check refuses
Edit-RawMeta $canon 'glm' 'exact' {
    param($m)
    $m.state = 'INTEGRATED'
    $m.integratedHead = $baseSha
    $m.mainHeadAtFinish = $baseSha
    $m.finishedAt = '2026-01-01T00:00:00.0000000Z'
    $m.sourceTipSha = $baseSha
    $m.sourceCommitShas = @($baseSha)
}
$r = Run-Dev $canon @('cleanup', 'glm', 'exact')
Check 'B3 cleanup exact-name: exit 1' ($r.Code -eq 1)
Check 'B3 cleanup exact-name: marker message' ($r.Out -match 'ownership evidence refused')
Check 'B3 exact-name: worktree untouched' (Test-Path -LiteralPath (Join-Path $root 'JARVIS-GLM-exact'))
Check 'B3 exact-name: branch untouched' ((GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/exact')) -ne '')
Check 'B3 exact-name: nothing integrated' (-not (Test-Path -LiteralPath (Join-Path $canon 'task.txt')))

# B4: ownership marker attacks on a genuine helper-created worktree.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-own'
$null = Run-Dev $canon @('new', 'glm', 'own')
CommitFile $wt 'task.txt' @('x') 'own work'
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$null = Run-Dev $canon @('finish', 'glm', 'own')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
$markerPath = Get-MarkerPathLocal $wt
$markerRaw = Get-Content -LiteralPath $markerPath -Raw
# marker missing
Remove-Item -LiteralPath $markerPath -Force
$r = Run-Dev $canon @('cleanup', 'glm', 'own')
Check 'B4 marker missing: exit 1' ($r.Code -eq 1)
Check 'B4 marker missing: message' ($r.Out -match 'no helper ownership marker')
Set-Content -LiteralPath $markerPath -Value $markerRaw -Encoding UTF8
# marker malformed
Set-Content -LiteralPath $markerPath -Value '{ not json' -Encoding UTF8
$r = Run-Dev $canon @('cleanup', 'glm', 'own')
Check 'B4 marker malformed: exit 1' ($r.Code -eq 1)
Set-Content -LiteralPath $markerPath -Value $markerRaw -Encoding UTF8
# field-level tampers
$tampers = @(
    @{ name = 'nonce';   edit = { param($m) $m.ownershipNonce = ('f' * 64) } },
    @{ name = 'worker';  edit = { param($m) $m.worker = 'claude' } },
    @{ name = 'task';    edit = { param($m) $m.task = 'other' } },
    @{ name = 'branch';  edit = { param($m) $m.branch = 'glm/other' } },
    @{ name = 'path';    edit = { param($m) $m.worktreePath = 'C:\somewhere-else' } },
    @{ name = 'base';    edit = { param($m) $m.baseSha = ('b' * 40) } },
    @{ name = 'canon';   edit = { param($m) $m.canonicalRepositoryPath = 'C:\other-repo' } }
)
$tamperAllRefused = $true
foreach ($t in $tampers) {
    Edit-Marker $wt $t.edit
    $r = Run-Dev $canon @('cleanup', 'glm', 'own')
    if ($r.Code -ne 1) { $tamperAllRefused = $false; Write-Host "    marker tamper '$($t.name)' was NOT refused (exit $($r.Code))" }
    Set-Content -LiteralPath $markerPath -Value $markerRaw -Encoding UTF8
}
Check 'B4 marker field tampers: all refused' $tamperAllRefused
# after restoring the genuine marker, cleanup completes normally
$r = Run-Dev $canon @('cleanup', 'glm', 'own')
Check 'B4 genuine marker: cleanup succeeds' ($r.Code -eq 0)
# finish also requires the marker: an ACTIVE task with the marker deleted
$rNew = Run-Dev $canon @('new', 'glm', 'own2')
Check 'B4 new own2: succeeds' ($rNew.Code -eq 0)
CommitFile (Join-Path $root 'JARVIS-GLM-own2') 'own2.txt' @('second marker probe') 'own2 work'
Remove-Item -LiteralPath (Get-MarkerPathLocal (Join-Path $root 'JARVIS-GLM-own2')) -Force
$r = Run-Dev $canon @('finish', 'glm', 'own2')
Check 'B4 finish marker missing: exit 1' ($r.Code -eq 1)
Check 'B4 finish marker missing: message' ($r.Out -match 'ownership evidence refused')

# B5: metadata nonce tamper (marker valid).
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-nonce'
$null = Run-Dev $canon @('new', 'glm', 'nonce')
CommitFile $wt 'task.txt' @('x') 'nonce work'
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$null = Run-Dev $canon @('finish', 'glm', 'nonce')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
Edit-RawMeta $canon 'glm' 'nonce' { param($m) $m.ownershipNonce = ('c' * 64) }
$r = Run-Dev $canon @('cleanup', 'glm', 'nonce')
Check 'B5 metadata nonce tamper: exit 1' ($r.Code -eq 1)
Check 'B5 metadata nonce tamper: message' ($r.Out -match 'ownership nonce mismatch')
Check 'B5 metadata nonce tamper: worktree kept' (Test-Path -LiteralPath $wt)

# B6: sourceCommitShas as a scalar string is a schema violation.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-scalar'
$null = Run-Dev $canon @('new', 'glm', 'scalar')
CommitFile $wt 'task.txt' @('x') 'scalar work'
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$null = Run-Dev $canon @('finish', 'glm', 'scalar')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
$scalarSha = @((Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'scalar') -Raw | ConvertFrom-Json).sourceCommitShas)[0]
Edit-RawMeta $canon 'glm' 'scalar' { param($m) $m.sourceCommitShas = $scalarSha }
$r = Run-Dev $canon @('cleanup', 'glm', 'scalar')
Check 'B6 scalar sourceCommitShas: exit 1' ($r.Code -eq 1)
Check 'B6 scalar sourceCommitShas: message' ($r.Out -match 'must be an array')

# B7: sourceCommitShas array tampering refuses cleanup.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-seq'
$null = Run-Dev $canon @('new', 'glm', 'seq')
CommitFile $wt 'a.txt' @('one') 'seq one'
CommitFile $wt 'b.txt' @('two') 'seq two'
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$null = Run-Dev $canon @('finish', 'glm', 'seq')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
$original = (Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'seq') -Raw | ConvertFrom-Json).sourceCommitShas
$seqTampers = @(
    @{ name = 'remove';  edit = { param($m) $m.sourceCommitShas = @($m.sourceCommitShas[0]) } },
    @{ name = 'add';     edit = { param($m) $m.sourceCommitShas = @($m.sourceCommitShas[0], $m.sourceCommitShas[1], ('d' * 40)) } },
    @{ name = 'reorder'; edit = { param($m) $m.sourceCommitShas = @($m.sourceCommitShas[1], $m.sourceCommitShas[0]) } },
    @{ name = 'replace'; edit = { param($m) $m.sourceCommitShas = @($m.sourceCommitShas[0], ('e' * 40)) } },
    @{ name = 'empty';   edit = { param($m) $m.sourceCommitShas = @() } }
)
$seqAllRefused = $true
foreach ($t in $seqTampers) {
    Edit-RawMeta $canon 'glm' 'seq' $t.edit
    $r = Run-Dev $canon @('cleanup', 'glm', 'seq')
    if ($r.Code -ne 1) { $seqAllRefused = $false; Write-Host "    sequence tamper '$($t.name)' was NOT refused (exit $($r.Code))" }
    Edit-RawMeta $canon 'glm' 'seq' { param($m) $m.sourceCommitShas = @($original[0], $original[1]) }
}
Check 'B7 sequence tampers: all refused' $seqAllRefused
$r = Run-Dev $canon @('cleanup', 'glm', 'seq')
Check 'B7 genuine sequence: cleanup succeeds' ($r.Code -eq 0)

# B8: ACTIVE/INTEGRATED state coherence.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-coh'
$null = Run-Dev $canon @('new', 'glm', 'coh')
CommitFile $wt 'task.txt' @('x') 'coh work'
$mainHead = GitOut $canon @('rev-parse', 'HEAD')
Edit-RawMeta $canon 'glm' 'coh' { param($m) $m.integratedHead = $mainHead }
$r = Run-Dev $canon @('finish', 'glm', 'coh')
Check 'B8 ACTIVE with integratedHead: exit 1' ($r.Code -eq 1)
Edit-RawMeta $canon 'glm' 'coh' { param($m) $m.integratedHead = $null; $m.sourceCommitShas = @(('a' * 40)) }
$r = Run-Dev $canon @('finish', 'glm', 'coh')
Check 'B8 ACTIVE with sourceCommitShas: exit 1' ($r.Code -eq 1)
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$null = Run-Dev $canon @('finish', 'glm', 'coh')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
Edit-RawMeta $canon 'glm' 'coh' { param($m) $m.finishedAt = $null }
$r = Run-Dev $canon @('cleanup', 'glm', 'coh')
Check 'B8 INTEGRATED missing finishedAt: exit 1' ($r.Code -eq 1)
Edit-RawMeta $canon 'glm' 'coh' { param($m) $m.finishedAt = '2026-01-01T00:00:00.0000000Z'; $m.mainHeadAtFinish = $null }
$r = Run-Dev $canon @('cleanup', 'glm', 'coh')
Check 'B8 INTEGRATED missing mainHeadAtFinish: exit 1' ($r.Code -eq 1)
Check 'B8 coherence: worktree kept throughout' (Test-Path -LiteralPath $wt)
Check 'B8 coherence: branch kept throughout' ((GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/coh')) -ne '')
}

if (Test-Section 'ADVERSARIAL-V3') {
# ============================================================================
Write-Host '== ADVERSARIAL V3 (final architecture) =='
# ============================================================================

# H1: EXCLUSIVE HANDOFF (A.1-A.5)
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-ho1'
$null = Run-Dev $canon @('new', 'glm', 'ho1')
CommitFile $wt 'task.txt' @('x') 'ho1 work'
$preHead = GitOut $canon @('rev-parse', 'HEAD')
# 1. finish without handoff
$script:SuppressAutoHandoff = $true
$r = Run-Dev $canon @('finish', 'glm', 'ho1')
Check 'H1 finish without handoff: exit 1' ($r.Code -eq 1)
Check 'H1 finish without handoff: HANDOFF REQUIRED' ($r.Out -match 'HANDOFF REQUIRED')
Check 'H1 finish without handoff: main not mutated' ((GitOut $canon @('rev-parse', 'HEAD')) -eq $preHead)
Check 'H1 finish without handoff: worktree intact' (Test-Path -LiteralPath $wt)
# 2. finish-current without handoff
$r = Run-Dev $wt @('finish-current')
Check 'H1 finish-current without handoff: exit 1' ($r.Code -eq 1)
Check 'H1 finish-current without handoff: HANDOFF REQUIRED' ($r.Out -match 'HANDOFF REQUIRED')
Check 'H1 finish-current without handoff: main not mutated' ((GitOut $canon @('rev-parse', 'HEAD')) -eq $preHead)
# 5. wrong handoff tokens all refuse
$wrongTokens = @(
    @{ name = 'wrong nonce';  token = "jarvis-dev-handoff-v1:glm:ho1:$('0' * 64)" },
    @{ name = 'wrong task';   token = "jarvis-dev-handoff-v1:glm:other:$((Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'ho1') -Raw | ConvertFrom-Json).ownershipNonce)" },
    @{ name = 'wrong worker'; token = "jarvis-dev-handoff-v1:claude:ho1:$((Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'ho1') -Raw | ConvertFrom-Json).ownershipNonce)" },
    @{ name = 'generic true'; token = 'true' },
    @{ name = 'generic one';  token = '1' }
)
$wrongAllRefused = $true
foreach ($t in $wrongTokens) {
    $env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $t.token
    $r = Run-Dev $canon @('finish', 'glm', 'ho1')
    $env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $null
    if ($r.Code -ne 1 -or $r.Out -notmatch 'HANDOFF REQUIRED') {
        $wrongAllRefused = $false
        Write-Host "    handoff token '$($t.name)' was NOT refused (exit $($r.Code))"
    }
}
Check 'H1 wrong handoff tokens: all refused' $wrongAllRefused
Check 'H1 wrong handoff tokens: main still not mutated' ((GitOut $canon @('rev-parse', 'HEAD')) -eq $preHead)
$script:SuppressAutoHandoff = $false
# 3. cleanup without handoff (task first legitimately integrated, deferred)
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$null = Run-Dev $canon @('finish', 'glm', 'ho1')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
$script:SuppressAutoHandoff = $true
$r = Run-Dev $canon @('cleanup', 'glm', 'ho1')
Check 'H1 cleanup without handoff: exit 1' ($r.Code -eq 1)
Check 'H1 cleanup without handoff: HANDOFF REQUIRED' ($r.Out -match 'HANDOFF REQUIRED')
Check 'H1 cleanup without handoff: worktree intact' (Test-Path -LiteralPath $wt)
$script:SuppressAutoHandoff = $false
# 4. the exact valid task-generation handoff allows the normal path
$r = Run-Dev $canon @('cleanup', 'glm', 'ho1')
Check 'H1 valid handoff: cleanup succeeds' ($r.Code -eq 0)
Check 'H1 valid handoff: CLEANED' ($r.Out -match 'CLEANED')

# H5: INTERACTIVE HANDOFF CONFIRMATION. The gate is unchanged in what it
# requires; a human at a console may now satisfy it by answering one explicit
# question instead of copying a token. Every side is measured: automation
# without a token, a piped answer with no interactive console, an explicit
# refusal, a present-but-wrong token, the confirmation itself on finish and
# on cleanup, the unchanged env-token path, and the two-artifact ownership
# evidence that a confirmation must never bypass.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-h5'
$null = Run-Dev $canon @('new', 'glm', 'h5')
CommitFile $wt 'task.txt' @('h5 change') 'h5 work'
$preHead = GitOut $canon @('rev-parse', 'HEAD')
$script:SuppressAutoHandoff = $true
# a. a piped answer with NO interactive console is automation: fail closed
$r = Run-Dev $canon @('finish', 'glm', 'h5') -StdIn 'REBOUND'
Check 'H5 non-interactive piped answer: exit 1' ($r.Code -eq 1)
Check 'H5 non-interactive piped answer: HANDOFF REQUIRED' ($r.Out -match 'HANDOFF REQUIRED')
Check 'H5 non-interactive piped answer: never prompted' ($r.Out -notmatch 'HANDOFF CONFIRMATION')
Check 'H5 non-interactive piped answer: main not mutated' ((GitOut $canon @('rev-parse', 'HEAD')) -eq $preHead)
# b. interactive console, explicit refusal
$env:DEV_HELPER_TEST_ASSUME_INTERACTIVE = '1'
$r = Run-Dev $canon @('finish', 'glm', 'h5') -StdIn 'no'
Check 'H5 refusal: exit 1' ($r.Code -eq 1)
Check 'H5 refusal: HANDOFF REFUSED' ($r.Out -match 'HANDOFF REFUSED')
Check 'H5 refusal: the question was asked' ($r.Out -match 'HANDOFF CONFIRMATION')
Check 'H5 refusal: main not mutated' ((GitOut $canon @('rev-parse', 'HEAD')) -eq $preHead)
Check 'H5 refusal: worktree intact' (Test-Path -LiteralPath $wt)
Check 'H5 refusal: task still ACTIVE' (((Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'h5') -Raw | ConvertFrom-Json).state) -ceq 'ACTIVE')
# c. interactive console, but a WRONG token is present: refused, never asked
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = "jarvis-dev-handoff-v1:glm:h5:$('0' * 64)"
$r = Run-Dev $canon @('finish', 'glm', 'h5') -StdIn 'REBOUND'
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $null
Check 'H5 wrong token present: exit 1' ($r.Code -eq 1)
Check 'H5 wrong token present: HANDOFF REQUIRED' ($r.Out -match 'HANDOFF REQUIRED')
Check 'H5 wrong token present: never prompted' ($r.Out -notmatch 'HANDOFF CONFIRMATION')
Check 'H5 wrong token present: main not mutated' ((GitOut $canon @('rev-parse', 'HEAD')) -eq $preHead)
# d. interactive console, confirmed: finish integrates and keeps the worktree
$r = Run-Dev $canon @('finish', 'glm', 'h5') -StdIn 'REBOUND'
Check 'H5 confirmed finish: exit 0' ($r.Code -eq 0)
Check 'H5 confirmed finish: the question was asked' ($r.Out -match 'HANDOFF CONFIRMATION')
Check 'H5 confirmed finish: INTEGRATED, not CLEANED' (($r.Out -match 'INTEGRATED') -and ($r.Out -notmatch 'CLEANED'))
Check 'H5 confirmed finish: change on main' ((Get-Content -LiteralPath (Join-Path $canon 'task.txt') -Raw).Trim() -eq 'h5 change')
Check 'H5 confirmed finish: worktree and branch kept' ((Test-Path -LiteralPath $wt) -and (Test-BranchExists $canon 'glm/h5'))
# e. interactive console, confirmed: cleanup from canonical main
$r = Run-Dev $canon @('cleanup', 'glm', 'h5') -StdIn 'REBOUND'
Check 'H5 confirmed cleanup: exit 0' ($r.Code -eq 0)
Check 'H5 confirmed cleanup: CLEANED' ($r.Out -match 'CLEANED')
Check 'H5 confirmed cleanup: worktree, branch, metadata gone' ((-not (Test-Path -LiteralPath $wt)) -and (-not (Test-BranchExists $canon 'glm/h5')) -and (-not (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'h5'))))
# f. a valid env token never prompts, even on an interactive console
$script:SuppressAutoHandoff = $false
$wt = Join-Path $root 'JARVIS-GLM-h5b'
$null = Run-Dev $canon @('new', 'glm', 'h5b')
CommitFile $wt 'task2.txt' @('h5b change') 'h5b work'
$r = Run-Dev $canon @('finish', 'glm', 'h5b')
Check 'H5 valid token: exit 0' ($r.Code -eq 0)
Check 'H5 valid token: INTEGRATED' ($r.Out -match 'INTEGRATED')
Check 'H5 valid token: never prompted' ($r.Out -notmatch 'HANDOFF CONFIRMATION')
$r = Run-Dev $canon @('cleanup', 'glm', 'h5b')
Check 'H5 valid token cleanup: CLEANED without a prompt' (($r.Code -eq 0) -and ($r.Out -match 'CLEANED') -and ($r.Out -notmatch 'HANDOFF CONFIRMATION'))
# g. a confirmation never bypasses the two-artifact ownership evidence: with
#    metadata that disagrees with the worktree marker, REBOUND is accepted as
#    an answer and the finish is still refused before anything is mutated
$script:SuppressAutoHandoff = $true
$wt = Join-Path $root 'JARVIS-GLM-h5c'
$null = Run-Dev $canon @('new', 'glm', 'h5c')
CommitFile $wt 'task3.txt' @('h5c change') 'h5c work'
$preHead = GitOut $canon @('rev-parse', 'HEAD')
Edit-RawMeta $canon 'glm' 'h5c' { param($m) $m.ownershipNonce = ('f' * 64) }
$r = Run-Dev $canon @('finish', 'glm', 'h5c') -StdIn 'REBOUND'
Check 'H5 mismatched generation: exit 1' ($r.Code -eq 1)
Check 'H5 mismatched generation: ownership refusal' ($r.Out -match 'ownership evidence refused')
Check 'H5 mismatched generation: main not mutated' ((GitOut $canon @('rev-parse', 'HEAD')) -eq $preHead)
Check 'H5 mismatched generation: worktree intact' (Test-Path -LiteralPath $wt)
$env:DEV_HELPER_TEST_ASSUME_INTERACTIVE = $null
$script:SuppressAutoHandoff = $false

# H2: CONTENT (B.6-B.9)
# 6. tracked dirty content -> removal refused
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-hc1'
$null = Run-Dev $canon @('new', 'glm', 'hc1')
CommitFile $wt 'task.txt' @('x') 'hc1 work'
# the .gitignore is committed BEFORE integration so the branch stays identical
# to the captured T; only the ignored content varies afterwards
Set-Content -Path (Join-Path $wt '.gitignore') -Value 'cache/'
GitRun $wt @('add', '.gitignore')
GitRun $wt @('commit', '-m', 'ignore cache dir')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$null = Run-Dev $canon @('finish', 'glm', 'hc1')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
# tracked dirty content -> removal refused
Set-Content -Path (Join-Path $wt 'task.txt') -Value 'dirty tracked content'
$r = Run-Dev $canon @('cleanup', 'glm', 'hc1')
Check 'H2 tracked dirty: exit 1' ($r.Code -eq 1)
Check 'H2 tracked dirty: deferred label' ($r.Out -match 'CLEANUP DEFERRED')
Check 'H2 tracked dirty: content survives' ((Get-Content -LiteralPath (Join-Path $wt 'task.txt') -Raw).Trim() -eq 'dirty tracked content')
Check 'H2 tracked dirty: metadata kept' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'hc1'))
GitRun $wt @('checkout', '--', 'task.txt')
# ignored content -> removal refused (worker must clear it explicitly)
$null = New-Item -ItemType Directory -Force -Path (Join-Path $wt 'cache')
Set-Content -Path (Join-Path $wt 'cache\build.tmp') -Value 'ignored cache content'
$r = Run-Dev $canon @('cleanup', 'glm', 'hc1')
Check 'H2 ignored content: exit 1' ($r.Code -eq 1)
Check 'H2 ignored content: deferred label' ($r.Out -match 'CLEANUP DEFERRED')
Check 'H2 ignored content: cache survives' (Test-Path -LiteralPath (Join-Path $wt 'cache\build.tmp'))
Remove-Item -LiteralPath (Join-Path $wt 'cache') -Recurse -Force
$r = Run-Dev $canon @('cleanup', 'glm', 'hc1')
Check 'H2 ignored content: cleanup succeeds when cleared' ($r.Code -eq 0)
# 9. unfinished Git operation -> refused
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-hc2'
$null = Run-Dev $canon @('new', 'glm', 'hc2')
CommitFile $wt 'shared.txt' @('from task') 'task edits shared'
CommitFile $canon 'shared.txt' @('from main') 'main edits shared'
$conflictSha = GitOut $canon @('rev-parse', 'HEAD')
$prev = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try { $null = & git -C $wt cherry-pick $conflictSha 2>&1 } finally { $ErrorActionPreference = $prev }
$pickConflicted = $true
try { $null = GitOut $wt @('rev-parse', 'CHERRY_PICK_HEAD') } catch { $pickConflicted = $false }
$r = Run-Dev $canon @('finish', 'glm', 'hc2')
Check 'H2 unfinished git op: finish refuses' ($r.Code -eq 1)
Check 'H2 unfinished git op: pick state actually present' $pickConflicted
Check 'H2 unfinished git op: canonical untouched' (-not (Test-Path -LiteralPath (Join-Path $canon '.git\CHERRY_PICK_HEAD')))
Check 'H2 unfinished git op: worktree preserved' (Test-Path -LiteralPath $wt)
Check 'H2 unfinished git op: metadata still ACTIVE' (((Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'hc2') -Raw | ConvertFrom-Json).state) -eq 'ACTIVE')

# H3: IMMUTABLE SOURCE (C.10-C.12) - C.11/C.12 are B1/A9; here C.10:
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-imm'
$null = Run-Dev $canon @('new', 'glm', 'imm')
CommitFile $wt 'task.txt' @('captured content') 'captured work'
$driverPath = Join-Path $root 'driver-imm.ps1'
@'
. '__DEVPS__'
function Invoke-PreIntegrationTestHook {
    param($ctx, $identity, $meta, [string]$CapturedT, [string]$CapturedM)
    # move the task branch ref T -> T2 (a brand-new unrelated commit)
    $tree = & git -C $ctx.MainPath rev-parse 'HEAD^{tree}'
    $t2 = 'moved' | & git -C $ctx.MainPath commit-tree $tree
    & git -C $ctx.MainPath update-ref "refs/heads/$($identity.Branch)" $t2
}
try {
    Invoke-FinishTask -Worker 'glm' -Task 'imm'
    exit 0
}
catch {
    Write-Host "DRIVER-CAUGHT: $($_.Exception.Message)"
    exit 1
}
'@ -replace '__DEVPS__', $DevPs | Set-Content -LiteralPath $driverPath -Encoding UTF8
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = Get-TaskHandoffToken -Canon $canon -Worker 'glm' -Task 'imm'
$r = Run-Driver $driverPath $canon
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $null
Check 'H3 ref moved pre-integration: finish completes' ($r.Out -match 'INTEGRATED')
Check 'H3 ref moved pre-integration: captured content integrated' ((Get-Content -LiteralPath (Join-Path $canon 'task.txt') -Raw).Trim() -eq 'captured content')
$meta = Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'imm') -Raw | ConvertFrom-Json
$origT = $meta.sourceTipSha
Check 'H3 ref moved pre-integration: metadata records captured T' ((GitOut $canon @('rev-parse', 'refs/heads/glm/imm')) -ne $origT)
Check 'H3 ref moved pre-integration: moved T2 NOT in main' ((GitOut $canon @('branch', '--contains', (GitOut $canon @('rev-parse', 'refs/heads/glm/imm')), '-a')) -notmatch 'main')
# the final gate defers BEFORE any deletion when the source branch moved
Check 'H3 ref moved pre-integration: worktree preserved' (Test-Path -LiteralPath $wt)
$r = Run-Dev $canon @('cleanup', 'glm', 'imm')
Check 'H3 ref moved pre-integration: later cleanup REF CHANGED' ($r.Code -eq 1)
$branchKept = $false
try { $null = GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/imm'); $branchKept = $true } catch { }
Check 'H3 ref moved pre-integration: branch (T2) preserved' $branchKept
Check 'H3 ref moved pre-integration: metadata preserved' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'imm'))

# H4: POST-PROOF / CAS (D.13-D.17) - D.13 is B2; here D.14-D.17:
# 14. after successful worktree removal but before the ref transaction,
#     task ref moves T -> T2: CAS fails, T2 survives, metadata survives.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-cas1'
$null = Run-Dev $canon @('new', 'glm', 'cas1')
CommitFile $wt 'task.txt' @('x') 'cas1 work'
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$null = Run-Dev $canon @('finish', 'glm', 'cas1')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
$driverPath = Join-Path $root 'driver-cas1.ps1'
@'
. '__DEVPS__'
function Invoke-PostRemovalTestHook {
    param($ctx, $identity, [string]$CapturedT, [string]$CapturedM)
    $tree = & git -C $ctx.MainPath rev-parse 'HEAD^{tree}'
    $t2 = 'moved-after-removal' | & git -C $ctx.MainPath commit-tree $tree
    & git -C $ctx.MainPath update-ref "refs/heads/$($identity.Branch)" $t2
}
try {
    Invoke-CleanupTask -Worker 'glm' -Task 'cas1'
    exit 0
}
catch {
    Write-Host "DRIVER-CAUGHT: $($_.Exception.Message)"
    exit 1
}
'@ -replace '__DEVPS__', $DevPs | Set-Content -LiteralPath $driverPath -Encoding UTF8
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = Get-TaskHandoffToken -Canon $canon -Worker 'glm' -Task 'cas1'
$r = Run-Driver $driverPath $canon
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $null
Check 'H4 post-removal T->T2: exit 1' ($r.Code -eq 1)
Check 'H4 post-removal T->T2: deferred + REF CHANGED' (($r.Out -match 'CLEANUP DEFERRED') -and ($r.Out -match 'REF CHANGED'))
Check 'H4 post-removal T->T2: worktree truthfully reported removed' ($r.Out -match 'worktree was removed')
Check 'H4 post-removal T->T2: moved ref survives' ((GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/cas1')) -ne '')
Check 'H4 post-removal T->T2: metadata survives' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'cas1'))
# 15. after worktree removal but before the ref transaction, main changes:
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-cas2'
$null = Run-Dev $canon @('new', 'glm', 'cas2')
CommitFile $wt 'task.txt' @('x') 'cas2 work'
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$null = Run-Dev $canon @('finish', 'glm', 'cas2')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
$driverPath = Join-Path $root 'driver-cas2.ps1'
@'
. '__DEVPS__'
function Invoke-PostRemovalTestHook {
    param($ctx, $identity, [string]$CapturedT, [string]$CapturedM)
    $tree = & git -C $ctx.MainPath rev-parse 'HEAD^{tree}'
    $m2 = 'main moved after removal' | & git -C $ctx.MainPath commit-tree $tree -p $CapturedM
    & git -C $ctx.MainPath update-ref refs/heads/main $m2
}
try {
    Invoke-CleanupTask -Worker 'glm' -Task 'cas2'
    exit 0
}
catch {
    Write-Host "DRIVER-CAUGHT: $($_.Exception.Message)"
    exit 1
}
'@ -replace '__DEVPS__', $DevPs | Set-Content -LiteralPath $driverPath -Encoding UTF8
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = Get-TaskHandoffToken -Canon $canon -Worker 'glm' -Task 'cas2'
$r = Run-Driver $driverPath $canon
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $null
Check 'H4 post-removal main change: exit 1' ($r.Code -eq 1)
Check 'H4 post-removal main change: deferred + MAIN CHANGED' (($r.Out -match 'CLEANUP DEFERRED') -and ($r.Out -match 'MAIN CHANGED'))
Check 'H4 post-removal main change: task branch survives' ((GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/cas2')) -ne '')
Check 'H4 post-removal main change: metadata survives' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'cas2'))
# 16. direct task ref replaced by a symbolic ref form: deletion refused
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-sym'
$null = Run-Dev $canon @('new', 'glm', 'sym')
CommitFile $wt 'task.txt' @('x') 'sym work'
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$null = Run-Dev $canon @('finish', 'glm', 'sym')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
GitRun $canon @('symbolic-ref', 'refs/heads/glm/sym', 'refs/heads/main')
$r = Run-Dev $canon @('cleanup', 'glm', 'sym')
Check 'H4 symbolic task ref: exit 1' ($r.Code -eq 1)
Check 'H4 symbolic task ref: refused' ($r.Out -match 'refused')
Check 'H4 symbolic task ref: nothing deleted' ($r.Out -notmatch 'CLEANED')
Check 'H4 symbolic task ref: symref survives' ((GitOut $canon @('symbolic-ref', 'refs/heads/glm/sym')) -eq 'refs/heads/main')
Check 'H4 symbolic task ref: main survives' ((GitOut $canon @('rev-parse', 'refs/heads/main')) -ne '')
Check 'H4 symbolic task ref: metadata survives' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'sym'))
# 17. task ref unchanged T and main unchanged M -> atomic transaction succeeds
#     (covered by every CLEANED test above, e.g. T8; re-assert once here)
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$null = Run-Dev $canon @('new', 'glm', 'cas3')
CommitFile (Join-Path $root 'JARVIS-GLM-cas3') 'task.txt' @('x') 'cas3 work'
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$null = Run-Dev $canon @('finish', 'glm', 'cas3')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
$r = Run-Dev $canon @('cleanup', 'glm', 'cas3')
Check 'H4 unchanged T and M: transaction succeeds' (($r.Code -eq 0) -and ($r.Out -match 'CLEANED'))

# H5: MISSING WORKTREE (E.18-E.19)
# 18. cleanup starts with worktree absent + branch present -> RECOVERY REQUIRED
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-mw1'
$null = Run-Dev $canon @('new', 'glm', 'mw1')
CommitFile $wt 'task.txt' @('x') 'mw1 work'
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$null = Run-Dev $canon @('finish', 'glm', 'mw1')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
# simulate the interruption: worktree removed outside the helper
GitRun $canon @('worktree', 'remove', $wt)
Check 'H5 worktree absent precondition' (-not (Test-Path -LiteralPath $wt))
$branchPresent = $false
try { $null = GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/mw1'); $branchPresent = $true } catch { }
Check 'H5 worktree absent: branch present' $branchPresent
$r = Run-Dev $canon @('cleanup', 'glm', 'mw1')
Check 'H5 missing worktree: exit 1' ($r.Code -eq 1)
Check 'H5 missing worktree: RECOVERY REQUIRED' ($r.Out -match 'RECOVERY REQUIRED: WORKTREE MISSING')
$branchKept = $false
try { $null = GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/mw1'); $branchKept = $true } catch { }
Check 'H5 missing worktree: branch preserved' $branchKept
Check 'H5 missing worktree: metadata preserved' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'mw1'))
# 19. worktree absent + forged metadata: branch preserved regardless
Edit-RawMeta $canon 'glm' 'mw1' { param($m) $m.ownershipNonce = ('9' * 64) }
$r = Run-Dev $canon @('cleanup', 'glm', 'mw1')
Check 'H5 missing worktree + forged metadata: exit 1' ($r.Code -eq 1)
$branchKept = $false
try { $null = GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/mw1'); $branchKept = $true } catch { }
Check 'H5 missing worktree + forged metadata: branch preserved' $branchKept

# H6: INTERRUPTION (F.20-F.22)
# 20. interruption after worktree removal but before branch delete: a later
#     cleanup refuses missing-worktree destructive cleanup (H5 18 covers the
#     mechanics; assert the recovery-report shape once more explicitly)
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-ir1'
$null = Run-Dev $canon @('new', 'glm', 'ir1')
CommitFile $wt 'task.txt' @('x') 'ir1 work'
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$null = Run-Dev $canon @('finish', 'glm', 'ir1')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
GitRun $canon @('worktree', 'remove', $wt)
$r = Run-Dev $canon @('cleanup', 'glm', 'ir1')
Check 'H6 interrupted cleanup: later cleanup refuses' ($r.Code -eq 1)
Check 'H6 interrupted cleanup: recovery label' ($r.Out -match 'RECOVERY REQUIRED')
Check 'H6 interrupted cleanup: branch preserved' ((GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/ir1')) -ne '')
Check 'H6 interrupted cleanup: status reports the state' (((Run-Dev $canon @('status')).Out) -match 'ir1')
# 21. interruption during new leaves partial state that cannot be adopted
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-ir2'
# simulate an interrupted new: worktree + branch + marker, but NO metadata
GitRun $canon @('worktree', 'add', '-b', 'glm/ir2', $wt)
GitRun $wt @('config', 'user.email', 'test@example.local')
GitRun $wt @('config', 'user.name', 'tester')
$baseSha = GitOut $canon @('rev-parse', 'HEAD')
$adminDir = (GitOut $wt @('rev-parse', '--git-dir')).Replace('/', '\')
$marker = [pscustomobject]@{
    schema                  = 'jarvis-dev-helper-ownership-1'
    worker                  = 'glm'
    task                    = 'ir2'
    canonicalRepositoryPath = $canon
    branch                  = 'glm/ir2'
    worktreePath            = $wt
    baseSha                 = $baseSha
    ownershipNonce          = ('e' * 64)
}
[System.IO.File]::WriteAllText((Join-Path $adminDir 'dev-helper-ownership.json'), (ConvertTo-Json -InputObject $marker), (New-Object System.Text.UTF8Encoding($false)))
$r = Run-Dev $canon @('finish', 'glm', 'ir2')
Check 'H6 interrupted new: finish refuses' ($r.Code -eq 1)
Check 'H6 interrupted new: never adopted' ($r.Out -match 'no helper metadata')
$r = Run-Dev $canon @('cleanup', 'glm', 'ir2')
Check 'H6 interrupted new: cleanup refuses' ($r.Code -eq 1)
$r = Run-Dev $canon @('new', 'glm', 'ir2')
Check 'H6 interrupted new: new re-run refuses collision' ($r.Code -eq 1)
Check 'H6 interrupted new: worktree untouched' (Test-Path -LiteralPath $wt)
# 22. interruption during integration leaves recoverable state
#     (T15 covers the conflict rollback shape; assert the recovery wording)
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-ir3'
$null = Run-Dev $canon @('new', 'glm', 'ir3')
CommitFile $wt 'shared.txt' @('from task') 'task edits shared'
CommitFile $canon 'shared.txt' @('from main') 'main edits shared'
$r = Run-Dev $canon @('finish', 'glm', 'ir3')
Check 'H6 interrupted integration: recovery wording' ($r.Out -match 'proven restored')
Check 'H6 interrupted integration: worktree preserved' (Test-Path -LiteralPath $wt)
Check 'H6 interrupted integration: branch preserved' ((GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/ir3')) -ne '')
}

if (Test-Section 'ADVERSARIAL-P1') {
# ============================================================================
Write-Host '== ADVERSARIAL P1 (unfinished ops + conflict recovery) =='
# ============================================================================

# Helper: fabricate one Git-admin operation-state artifact in a worktree.
function Add-AdminState {
    param([string]$Wt, [string]$Name, [switch]$Directory)
    $adminDir = (GitOut $Wt @('rev-parse', '--git-dir')).Replace('/', '\')
    $p = Join-Path $adminDir $Name
    if ($Directory) {
        $null = New-Item -ItemType Directory -Force -Path $p
        Set-Content -Path (Join-Path $p 'probe') -Value 'probe'
    }
    else {
        Set-Content -Path $p -Value 'probe'
    }
    return $p
}

# A1: sequencer/ state -> cleanup refuses; everything preserved.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-uq1'
$null = Run-Dev $canon @('new', 'glm', 'uq1')
CommitFile $wt 'task.txt' @('x') 'uq1 work'
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$null = Run-Dev $canon @('finish', 'glm', 'uq1')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
$null = Add-AdminState -Wt $wt -Name 'sequencer' -Directory
$r = Run-Dev $canon @('cleanup', 'glm', 'uq1')
Check 'A1 sequencer cleanup: exit 1' ($r.Code -eq 1)
Check 'A1 sequencer cleanup: detected' ($r.Out -match 'sequencer')
Check 'A1 sequencer cleanup: worktree preserved' (Test-Path -LiteralPath $wt)
Check 'A1 sequencer cleanup: branch preserved' ((GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/uq1')) -ne '')
Check 'A1 sequencer cleanup: metadata preserved' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'uq1'))
$adminDir = (GitOut $wt @('rev-parse', '--git-dir')).Replace('/', '\')
Remove-Item -LiteralPath (Join-Path $adminDir 'sequencer') -Recurse -Force
$r = Run-Dev $canon @('cleanup', 'glm', 'uq1')
Check 'A1 sequencer cleanup: succeeds when state cleared' ($r.Code -eq 0)

# A2: sequencer/ state -> finish cannot progress into destructive cleanup.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-uq2'
$null = Run-Dev $canon @('new', 'glm', 'uq2')
CommitFile $wt 'task.txt' @('x') 'uq2 work'
$preHead = GitOut $canon @('rev-parse', 'HEAD')
$null = Add-AdminState -Wt $wt -Name 'sequencer' -Directory
$r = Run-Dev $canon @('finish', 'glm', 'uq2')
Check 'A2 sequencer finish: exit 1' ($r.Code -eq 1)
Check 'A2 sequencer finish: detected' ($r.Out -match 'sequencer')
Check 'A2 sequencer finish: main not mutated' ((GitOut $canon @('rev-parse', 'HEAD')) -eq $preHead)
Check 'A2 sequencer finish: worktree preserved' (Test-Path -LiteralPath $wt)

# A3: sequencer/ state -> finish-current cannot progress either.
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = Get-TaskHandoffToken -Canon $canon -Worker 'glm' -Task 'uq2'
$r = Run-Dev $wt @('finish-current')
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $null
Check 'A3 sequencer finish-current: exit 1' ($r.Code -eq 1)
Check 'A3 sequencer finish-current: detected' ($r.Out -match 'sequencer')
Check 'A3 sequencer finish-current: main not mutated' ((GitOut $canon @('rev-parse', 'HEAD')) -eq $preHead)

# A4-A7: REAL active operation states refused at finish.
# A4: active merge (MERGE_HEAD)
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-uq4'
$null = Run-Dev $canon @('new', 'glm', 'uq4')
CommitFile $wt 'shared.txt' @('task side') 'task edits shared'
CommitFile $canon 'shared.txt' @('main side') 'main edits shared'
$prev = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try { $null = & git -C $wt merge main 2>&1 } finally { $ErrorActionPreference = $prev }
$mergeActive = Test-Path -LiteralPath (Join-Path ((GitOut $wt @('rev-parse', '--git-dir')).Replace('/', '\')) 'MERGE_HEAD')
Check 'A4 merge state actually present' $mergeActive
$r = Run-Dev $canon @('finish', 'glm', 'uq4')
Check 'A4 active merge: finish refuses' ($r.Code -eq 1)
Check 'A4 active merge: MERGE_HEAD detected' ($r.Out -match 'MERGE_HEAD')

# A5: active cherry-pick (CHERRY_PICK_HEAD)
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-uq5'
$null = Run-Dev $canon @('new', 'glm', 'uq5')
CommitFile $wt 'shared.txt' @('task side') 'task edits shared'
CommitFile $canon 'shared.txt' @('main side') 'main edits shared'
$conflictSha = GitOut $canon @('rev-parse', 'HEAD')
$prev = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try { $null = & git -C $wt cherry-pick $conflictSha 2>&1 } finally { $ErrorActionPreference = $prev }
$pickActive = Test-Path -LiteralPath (Join-Path ((GitOut $wt @('rev-parse', '--git-dir')).Replace('/', '\')) 'CHERRY_PICK_HEAD')
Check 'A5 cherry-pick state actually present' $pickActive
$r = Run-Dev $canon @('finish', 'glm', 'uq5')
Check 'A5 active cherry-pick: finish refuses' ($r.Code -eq 1)
Check 'A5 active cherry-pick: CHERRY_PICK_HEAD detected' ($r.Out -match 'CHERRY_PICK_HEAD')

# A6: active revert (REVERT_HEAD)
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-uq6'
$null = Run-Dev $canon @('new', 'glm', 'uq6')
Set-Content -Path (Join-Path $wt 'shared.txt') -Value 'A'
GitRun $wt @('commit', '-am', 'change to A')
Set-Content -Path (Join-Path $wt 'shared.txt') -Value 'B'
GitRun $wt @('commit', '-am', 'change to B')
$baseSha = (Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'uq6') -Raw | ConvertFrom-Json).baseSha
$c1 = @((& git -C $wt rev-list --reverse "$baseSha..glm/uq6") | ForEach-Object { $_.ToString() })[0]
$prev = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try { $null = & git -C $wt revert $c1 2>&1 } finally { $ErrorActionPreference = $prev }
$revertActive = Test-Path -LiteralPath (Join-Path ((GitOut $wt @('rev-parse', '--git-dir')).Replace('/', '\')) 'REVERT_HEAD')
Check 'A6 revert state actually present' $revertActive
$r = Run-Dev $canon @('finish', 'glm', 'uq6')
Check 'A6 active revert: finish refuses' ($r.Code -eq 1)
Check 'A6 active revert: REVERT_HEAD detected' ($r.Out -match 'REVERT_HEAD')

# A7: active rebase (rebase-merge/)
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-uq7'
$null = Run-Dev $canon @('new', 'glm', 'uq7')
CommitFile $wt 'shared.txt' @('task side') 'task edits shared'
CommitFile $canon 'shared.txt' @('main side') 'main edits shared'
$prev = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try { $null = & git -C $wt rebase main 2>&1 } finally { $ErrorActionPreference = $prev }
$rebaseActive = Test-Path -LiteralPath (Join-Path ((GitOut $wt @('rev-parse', '--git-dir')).Replace('/', '\')) 'rebase-merge')
Check 'A7 rebase state actually present' $rebaseActive
$r = Run-Dev $canon @('finish', 'glm', 'uq7')
Check 'A7 active rebase: finish refuses' ($r.Code -eq 1)
Check 'A7 active rebase: rebase-merge detected' ($r.Out -match 'rebase-merge')

# A8: active bisect state (BISECT_START) -> cleanup refuses (adopted).
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-uq8'
$null = Run-Dev $canon @('new', 'glm', 'uq8')
CommitFile $wt 'task.txt' @('x') 'uq8 work'
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$null = Run-Dev $canon @('finish', 'glm', 'uq8')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
$null = Add-AdminState -Wt $wt -Name 'BISECT_START'
$r = Run-Dev $canon @('cleanup', 'glm', 'uq8')
Check 'A8 bisect state: exit 1' ($r.Code -eq 1)
Check 'A8 bisect state: BISECT_START detected' ($r.Out -match 'BISECT_START')
Check 'A8 bisect state: worktree preserved' (Test-Path -LiteralPath $wt)

# A9: stale ORIG_HEAD alone does NOT block cleanup.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-uq9'
$null = Run-Dev $canon @('new', 'glm', 'uq9')
CommitFile $wt 'task.txt' @('x') 'uq9 work'
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$null = Run-Dev $canon @('finish', 'glm', 'uq9')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
$null = Add-AdminState -Wt $wt -Name 'ORIG_HEAD'
$r = Run-Dev $canon @('cleanup', 'glm', 'uq9')
Check 'A9 ORIG_HEAD does not block: exit 0' ($r.Code -eq 0)
Check 'A9 ORIG_HEAD does not block: CLEANED' ($r.Out -match 'CLEANED')

# A10: inspection failure fails closed.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-uq10'
$null = Run-Dev $canon @('new', 'glm', 'uq10')
CommitFile $wt 'task.txt' @('x') 'uq10 work'
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$null = Run-Dev $canon @('finish', 'glm', 'uq10')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
$driverPath = Join-Path $root 'driver-uq10.ps1'
@'
. '__DEVPS__'
function Get-WorktreeAdminDir {
    param([string]$WorktreePath)
    return $null
}
try {
    Invoke-CleanupTask -Worker 'glm' -Task 'uq10'
    exit 0
}
catch {
    Write-Host "DRIVER-CAUGHT: $($_.Exception.Message)"
    exit 1
}
'@ -replace '__DEVPS__', $DevPs | Set-Content -LiteralPath $driverPath -Encoding UTF8
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = Get-TaskHandoffToken -Canon $canon -Worker 'glm' -Task 'uq10'
$r = Run-Driver $driverPath $canon
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $null
Check 'A10 inspection failure: exit 1' ($r.Code -eq 1)
Check 'A10 inspection failure: refused end-to-end' ($r.Out -match 'refused')
# the detector's own fail-closed branch, probed directly
$driverPath = Join-Path $root 'driver-uq10b.ps1'
@'
. '__DEVPS__'
function Get-WorktreeAdminDir {
    param([string]$WorktreePath)
    return $null
}
$reason = Get-UnfinishedGitOperationState -WorktreePath '$WT'
if ($reason -and $reason -match 'inspection failed') {
    Write-Host 'DETECTOR-FAIL-CLOSED: yes'
    exit 0
}
Write-Host "DETECTOR-FAIL-CLOSED: no ($reason)"
exit 1
'@ -replace '__DEVPS__', $DevPs -replace '__WT__', $wt | Set-Content -LiteralPath $driverPath -Encoding UTF8
$r = Run-Driver $driverPath $canon
Check 'A10 inspection failure: detector returns fail-closed reason' (($r.Code -eq 0) -and ($r.Out -match 'DETECTOR-FAIL-CLOSED: yes'))
Check 'A10 inspection failure: worktree preserved' (Test-Path -LiteralPath $wt)
Check 'A10 inspection failure: metadata preserved' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'uq10'))

# B2: canonical main advances M -> M2 AFTER abort and BEFORE post-abort proof.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-cr2'
$null = Run-Dev $canon @('new', 'glm', 'cr2')
CommitFile $wt 'shared.txt' @('task side') 'task edits shared'
CommitFile $canon 'shared.txt' @('main side') 'main edits shared'
$driverPath = Join-Path $root 'driver-cr2.ps1'
@'
. '__DEVPS__'
function Invoke-PostAbortTestHook {
    param($ctx, $identity, [string]$CapturedM)
    $tree = & git -C $ctx.MainPath rev-parse 'HEAD^{tree}'
    $script:m2 = 'concurrent advance' | & git -C $ctx.MainPath commit-tree $tree -p $CapturedM
    & git -C $ctx.MainPath update-ref refs/heads/main $script:m2
    Set-Content -LiteralPath (Join-Path $ctx.MainPath '..\m2.txt') -Value $script:m2
}
try {
    Invoke-FinishTask -Worker 'glm' -Task 'cr2'
    exit 0
}
catch {
    Write-Host "DRIVER-CAUGHT: $($_.Exception.Message)"
    exit 1
}
'@ -replace '__DEVPS__', $DevPs | Set-Content -LiteralPath $driverPath -Encoding UTF8
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = Get-TaskHandoffToken -Canon $canon -Worker 'glm' -Task 'cr2'
$r = Run-Driver $driverPath $canon
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $null
$m2 = (Get-Content -LiteralPath (Join-Path $root 'm2.txt') -Raw).Trim()
Check 'B2 post-abort main advance: exit 1' ($r.Code -eq 1)
Check 'B2 post-abort main advance: RECOVERY REQUIRED' ($r.Out -match 'RECOVERY REQUIRED')
Check 'B2 post-abort main advance: does not claim M restored' ($r.Out -notmatch 'proven restored')
Check 'B2 post-abort main advance: M2 is main' ((GitOut $canon @('rev-parse', 'refs/heads/main')) -eq $m2)
Check 'B2 post-abort main advance: M2 ref-reachable' ((GitOut $canon @('branch', '--contains', $m2, '-a')) -match 'main')
Check 'B2 post-abort main advance: task branch preserved' ((GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/cr2')) -ne '')
Check 'B2 post-abort main advance: worktree preserved' (Test-Path -LiteralPath $wt)
Check 'B2 post-abort main advance: metadata preserved' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'cr2'))

# B3: canonical main advances M -> M2 BEFORE recovery/abort: no abort, no
# restoration; the observed state and the sequencer state are preserved.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-cr3'
$null = Run-Dev $canon @('new', 'glm', 'cr3')
CommitFile $wt 'shared.txt' @('task side') 'task edits shared'
CommitFile $canon 'shared.txt' @('main side') 'main edits shared'
$preFinishHead = GitOut $canon @('rev-parse', 'HEAD')
$driverPath = Join-Path $root 'driver-cr3.ps1'
@'
. '__DEVPS__'
function Invoke-ConflictRecoveryTestHook {
    param($ctx, $identity, [string]$CapturedM)
    $tree = & git -C $ctx.MainPath rev-parse 'HEAD^{tree}'
    $m2 = 'concurrent advance pre-abort' | & git -C $ctx.MainPath commit-tree $tree -p $CapturedM
    & git -C $ctx.MainPath update-ref refs/heads/main $m2
}
try {
    Invoke-FinishTask -Worker 'glm' -Task 'cr3'
    exit 0
}
catch {
    Write-Host "DRIVER-CAUGHT: $($_.Exception.Message)"
    exit 1
}
'@ -replace '__DEVPS__', $DevPs | Set-Content -LiteralPath $driverPath -Encoding UTF8
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = Get-TaskHandoffToken -Canon $canon -Worker 'glm' -Task 'cr3'
$r = Run-Driver $driverPath $canon
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $null
Check 'B3 pre-abort main advance: exit 1' ($r.Code -eq 1)
Check 'B3 pre-abort main advance: RECOVERY REQUIRED' ($r.Out -match 'RECOVERY REQUIRED')
Check 'B3 pre-abort main advance: no abort performed' (Test-Path -LiteralPath (Join-Path $canon '.git\CHERRY_PICK_HEAD'))
Check 'B3 pre-abort main advance: M2 preserved (main moved off M)' ((GitOut $canon @('rev-parse', 'refs/heads/main')) -ne $preFinishHead)
Check 'B3 pre-abort main advance: task branch preserved' ((GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/cr3')) -ne '')
Check 'B3 pre-abort main advance: worktree preserved' (Test-Path -LiteralPath $wt)
Check 'B3 pre-abort main advance: metadata preserved' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'cr3'))

# B4: abort itself fails -> no reset fallback, state preserved.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-cr4'
$null = Run-Dev $canon @('new', 'glm', 'cr4')
CommitFile $wt 'shared.txt' @('task side') 'task edits shared'
CommitFile $canon 'shared.txt' @('main side') 'main edits shared'
$driverPath = Join-Path $root 'driver-cr4.ps1'
@'
. '__DEVPS__'
function Invoke-CherryPickAbort {
    param([string]$RepoPath)
    return [pscustomobject]@{ ExitCode = 1; Lines = @('simulated abort failure'); Text = 'simulated abort failure' }
}
try {
    Invoke-FinishTask -Worker 'glm' -Task 'cr4'
    exit 0
}
catch {
    Write-Host "DRIVER-CAUGHT: $($_.Exception.Message)"
    exit 1
}
'@ -replace '__DEVPS__', $DevPs | Set-Content -LiteralPath $driverPath -Encoding UTF8
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = Get-TaskHandoffToken -Canon $canon -Worker 'glm' -Task 'cr4'
$r = Run-Driver $driverPath $canon
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $null
Check 'B4 abort failure: exit 1' ($r.Code -eq 1)
Check 'B4 abort failure: RECOVERY REQUIRED' ($r.Out -match 'RECOVERY REQUIRED')
Check 'B4 abort failure: abort-failure reason' ($r.Out -match 'abort failed')
Check 'B4 abort failure: no reset (main still M)' ((GitOut $canon @('rev-parse', 'refs/heads/main')) -eq (GitOut $canon @('rev-parse', 'HEAD')))
Check 'B4 abort failure: pick state preserved' (Test-Path -LiteralPath (Join-Path $canon '.git\CHERRY_PICK_HEAD'))
Check 'B4 abort failure: task branch preserved' ((GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/cr4')) -ne '')
Check 'B4 abort failure: worktree preserved' (Test-Path -LiteralPath $wt)

# B5: post-abort proof cannot be established (operation state appears) ->
# RECOVERY REQUIRED, no destructive repair.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-cr5'
$null = Run-Dev $canon @('new', 'glm', 'cr5')
CommitFile $wt 'shared.txt' @('task side') 'task edits shared'
CommitFile $canon 'shared.txt' @('main side') 'main edits shared'
$driverPath = Join-Path $root 'driver-cr5.ps1'
@'
. '__DEVPS__'
function Invoke-PostAbortTestHook {
    param($ctx, $identity, [string]$CapturedM)
    $null = New-Item -ItemType Directory -Force -Path (Join-Path $ctx.CommonDir 'sequencer')
    Set-Content -Path (Join-Path $ctx.CommonDir 'sequencer\probe') -Value 'probe'
}
try {
    Invoke-FinishTask -Worker 'glm' -Task 'cr5'
    exit 0
}
catch {
    Write-Host "DRIVER-CAUGHT: $($_.Exception.Message)"
    exit 1
}
'@ -replace '__DEVPS__', $DevPs | Set-Content -LiteralPath $driverPath -Encoding UTF8
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = Get-TaskHandoffToken -Canon $canon -Worker 'glm' -Task 'cr5'
$r = Run-Driver $driverPath $canon
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $null
Check 'B5 post-abort proof failure: exit 1' ($r.Code -eq 1)
Check 'B5 post-abort proof failure: RECOVERY REQUIRED' ($r.Out -match 'RECOVERY REQUIRED')
Check 'B5 post-abort proof failure: observed state preserved' (Test-Path -LiteralPath (Join-Path $canon '.git\sequencer'))
Check 'B5 post-abort proof failure: main unchanged' ((GitOut $canon @('rev-parse', 'refs/heads/main')) -eq (GitOut $canon @('rev-parse', 'HEAD')))
Check 'B5 post-abort proof failure: task preserved' ((GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/cr5')) -ne '')
}

if (Test-Section 'WINDOWS-A') {
# ============================================================================
Write-Host '== WINDOWS FINISH / PARTIAL-REMOVAL RECOVERY (W1-W12) =='
# ============================================================================
# Reproduces the observed Windows failure: `git worktree remove` deregisters
# the worktree and deletes .git/worktrees/<name> (and with it the private
# ownership marker), while the final directory unlink does not happen.

# --- W1/W2/W3: cleanup, run from INSIDE the worktree, relocates first -------
# finish-current no longer reaches the destructive stage (hard lifecycle
# rule), so the relocation-before-removal proof now belongs to `cleanup`
# alone. The driver still calls Invoke-FinishCurrent first - from inside the
# worktree, exactly as a real handoff would - then calls Invoke-CleanupTask
# for the SAME task while still standing inside it, which is precisely the
# scenario the relocation logic exists to handle.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-w1'
$null = Run-Dev $canon @('new', 'glm', 'w1')
CommitFile $wt 'task.txt' @('w1 change') 'w1 work'
$driverPath = Join-Path $root 'driver-w1.ps1'
@'
. '__DEVPS__'
function Invoke-PreDeleteTestHook {
    param($ctx, $identity, $meta)
    # fires AFTER the relocation and BEFORE anything destructive
    $wt = $identity.WorktreePath
    $ps = (Get-Location).ProviderPath
    $net = [System.Environment]::CurrentDirectory
    $inside = ($ps -eq $wt) -or $ps.StartsWith("$wt\") -or ($net -eq $wt) -or $net.StartsWith("$wt\")
    Write-Host "RELOC-INSIDE=$inside"
    Write-Host "RELOC-SAFE=$([bool]($ps -eq $ctx.MainPath -and $net -eq $ctx.MainPath))"
}
Invoke-FinishCurrent
Invoke-CleanupTask -Worker 'glm' -Task 'w1'
'@ -replace '__DEVPS__', $DevPs | Set-Content -LiteralPath $driverPath -Encoding UTF8
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = Get-TaskHandoffToken -Canon $canon -Worker 'glm' -Task 'w1'
$r = Run-Driver $driverPath $wt
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $null
Check 'W1 finish-current from inside: exit 0' ($r.Code -eq 0)
Check 'W1 finish-current from inside: INTEGRATED' ($r.Out -match 'INTEGRATED')
Check 'W2 relocated out of the target before removal' ($r.Out -match 'RELOC-INSIDE=False')
Check 'W2 relocated to the canonical repository' ($r.Out -match 'RELOC-SAFE=True')
Check 'W3 windows removal path: CLEANED' ($r.Out -match 'CLEANED')
Check 'W3 windows removal path: worktree gone' (-not (Test-Path -LiteralPath $wt))
Check 'W3 windows removal path: branch gone' (-not (Test-BranchExists $canon 'glm/w1'))
Check 'W3 windows removal path: metadata gone' (-not (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'w1')))

# --- W4/W20: simulated final directory unlink failure ------------------------
# Git deregisters and empties the worktree; only the empty folder survives.
# The Git-visible cleanup completes in the SAME invocation and the outcome is
# reported as its own status - never "removal failed", never CLEANED.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IntegratedTask $canon $root 'glm' 'w4' 'JARVIS-GLM-w4'
$driverPath = Join-Path $root 'driver-w4.ps1'
@'
. '__DEVPS__'
function Remove-WorktreeDir {
    param($ctx, [string]$WorktreePath)
    # genuine git-owned removal, then the Windows final-unlink failure
    $null = Invoke-GitCapture -RepoPath $ctx.MainPath -GitArgs @('worktree', 'remove', $WorktreePath)
    if (-not (Test-Path -LiteralPath $WorktreePath)) { $null = New-Item -ItemType Directory -Path $WorktreePath }
    return 'LEFTOVER'
}
Invoke-CleanupTask -Worker 'glm' -Task 'w4'
'@ -replace '__DEVPS__', $DevPs | Set-Content -LiteralPath $driverPath -Encoding UTF8
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = Get-TaskHandoffToken -Canon $canon -Worker 'glm' -Task 'w4'
$r = Run-Driver $driverPath $canon
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $null
Check 'W4 unlink failure: exit 1' ($r.Code -eq 1)
Check 'W4 unlink failure: CLEANUP INCOMPLETE' ($r.Out -match 'CLEANUP INCOMPLETE: LEFTOVER DIRECTORY')
Check 'W4 unlink failure: not reported as removal failure' ($r.Out -notmatch 'worktree removal failed')
Check 'W20 unlink failure: CLEANED not claimed' ($r.Out -notmatch 'CLEANED')
Check 'W4 unlink failure: branch removed in the same invocation' (-not (Test-BranchExists $canon 'glm/w4'))
Check 'W4 unlink failure: metadata removed' (-not (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'w4')))
Check 'W4 unlink failure: only an empty folder remains' ((Test-Path -LiteralPath $wt) -and (@(Get-ChildItem -LiteralPath $wt -Force).Count -eq 0))

# --- W5/W8: deregistered + empty directory -> RECOVERY REQUIRED -------------
# The exact observed leftover state: no admin dir, no ownership marker, empty
# folder, INTEGRATED metadata, branch still at the recorded source tip. There
# is no automatic recovery for it: once the marker is gone the helper can no
# longer prove it created the worktree, so nothing is deleted.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IntegratedTask $canon $root 'glm' 'w5' 'JARVIS-GLM-w5'
New-PartialRemovalResidue $canon $wt
Check 'W5 residue: worktree deregistered' ((GitOut $canon @('worktree', 'list', '--porcelain')) -notmatch 'JARVIS-GLM-w5')
Check 'W5 residue: directory empty' ((Test-Path -LiteralPath $wt) -and (@(Get-ChildItem -LiteralPath $wt -Force).Count -eq 0))
Check 'W8 residue: ownership marker gone with the admin dir' (-not (Test-Path -LiteralPath (Join-Path $canon '.git\worktrees\JARVIS-GLM-w5')))
$r = Run-Dev $canon @('cleanup', 'glm', 'w5')
Check 'W8 markerless residue: exit 1' ($r.Code -eq 1)
Check 'W8 markerless residue: RECOVERY REQUIRED' ($r.Out -match 'RECOVERY REQUIRED: PARTIAL REMOVAL')
Check 'W8 markerless residue: human checkpoint demanded' ($r.Out -match 'explicit human recovery checkpoint')
Check 'W8 markerless residue: RECOVERED never claimed' ($r.Out -notmatch 'RECOVERED')
Check 'W8 markerless residue: CLEANED never claimed' ($r.Out -notmatch 'CLEANED')
Check 'W8 markerless residue: leftover directory preserved' (Test-Path -LiteralPath $wt)
Check 'W8 markerless residue: branch preserved' (Test-BranchExists $canon 'glm/w5')
Check 'W8 markerless residue: metadata preserved' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'w5'))
Check 'W8 markerless residue: integrated work still on main' ((Get-Content -LiteralPath (Join-Path $canon 'task.txt') -Raw).Trim() -eq 'w5 work')

# --- W5b: Remove-WorktreeDir classifies from observable state ---------------
# The removal outcome must never be read off git's exit code: a Windows
# removal can be complete in Git terms and still fail its final unlink.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wtOk = New-IntegratedTask $canon $root 'glm' 'wr1' 'JARVIS-GLM-wr1'
$wtEmpty = New-IntegratedTask $canon $root 'glm' 'wr2' 'JARVIS-GLM-wr2'
New-PartialRemovalResidue $canon $wtEmpty
$wtFull = New-IntegratedTask $canon $root 'glm' 'wr3' 'JARVIS-GLM-wr3'
New-PartialRemovalResidue $canon $wtFull
Set-Content -LiteralPath (Join-Path $wtFull 'keep.txt') -Value 'user content'
$driverPath = Join-Path $root 'driver-wr.ps1'
@'
. '__DEVPS__'
$ctx = Get-CanonicalContext
foreach ($n in @('wr1', 'wr2', 'wr3')) {
    $id = Get-DerivedTaskIdentity -ctx $ctx -Worker 'glm' -Task $n
    Write-Host "REMOVE-$n=$(Remove-WorktreeDir -ctx $ctx -WorktreePath $id.WorktreePath)"
}
# the relocation guard must refuse a safe directory inside the target
$id = Get-DerivedTaskIdentity -ctx $ctx -Worker 'glm' -Task 'wr3'
try { Set-SafeWorkingDirectory -SafePath $id.WorktreePath -WorktreePath $id.WorktreePath; Write-Host 'RELOC-GUARD=accepted' }
catch { Write-Host "RELOC-GUARD=$($_.Exception.Message)" }
'@ -replace '__DEVPS__', $DevPs | Set-Content -LiteralPath $driverPath -Encoding UTF8
$r = Run-Driver $driverPath $canon
Check 'W5b registered clean worktree: REMOVED' ($r.Out -match 'REMOVE-wr1=REMOVED')
Check 'W5b registered clean worktree: directory gone' (-not (Test-Path -LiteralPath $wtOk))
Check 'W5b deregistered empty leftover: REMOVED' ($r.Out -match 'REMOVE-wr2=REMOVED')
Check 'W5b deregistered empty leftover: directory unlinked' (-not (Test-Path -LiteralPath $wtEmpty))
Check 'W5b deregistered non-empty leftover: FAILED' ($r.Out -match 'REMOVE-wr3=FAILED')
Check 'W5b deregistered non-empty leftover: content intact' ((Get-Content -LiteralPath (Join-Path $wtFull 'keep.txt') -Raw).Trim() -eq 'user content')
Check 'W5b relocation guard refuses a safe dir inside the target' ($r.Out -match 'RELOC-GUARD=CLEANUP DEFERRED: the safe directory is inside the worktree being removed')

# --- W6: deregistered but directory NOT empty -> refuse ---------------------
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IntegratedTask $canon $root 'glm' 'w6' 'JARVIS-GLM-w6'
New-PartialRemovalResidue $canon $wt
Set-Content -LiteralPath (Join-Path $wt 'leftover.txt') -Value 'not helper content'
$r = Run-Dev $canon @('cleanup', 'glm', 'w6')
Check 'W6 non-empty residue: exit 1' ($r.Code -eq 1)
Check 'W6 non-empty residue: RECOVERY REQUIRED' ($r.Out -match 'RECOVERY REQUIRED: WORKTREE NOT REGISTERED')
Check 'W6 non-empty residue: names the retained content' ($r.Out -match 'still holds content')
Check 'W6 non-empty residue: RECOVERED never claimed' ($r.Out -notmatch 'RECOVERED')
Check 'W6 non-empty residue: content untouched' ((Get-Content -LiteralPath (Join-Path $wt 'leftover.txt') -Raw).Trim() -eq 'not helper content')
Check 'W6 non-empty residue: branch preserved' (Test-BranchExists $canon 'glm/w6')
Check 'W6 non-empty residue: metadata preserved' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'w6'))

# --- W7: missing marker BEFORE integration is still a hard refusal ----------
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-w7'
$null = Run-Dev $canon @('new', 'glm', 'w7')
CommitFile $wt 'task.txt' @('w7 work') 'w7 work'
Remove-Item -LiteralPath (Get-MarkerPathLocal $wt) -Force
$r = Run-Dev $canon @('finish', 'glm', 'w7')
Check 'W7 marker missing pre-integration: exit 1' ($r.Code -eq 1)
Check 'W7 marker missing pre-integration: ownership refusal' ($r.Out -match 'ownership evidence refused')
Check 'W7 marker missing pre-integration: nothing integrated' (-not (Test-Path -LiteralPath (Join-Path $canon 'task.txt')))
Check 'W7 marker missing pre-integration: worktree kept' (Test-Path -LiteralPath $wt)
Check 'W7 marker missing pre-integration: branch kept' (Test-BranchExists $canon 'glm/w7')
# a registered worktree whose marker was deleted never reaches the recovery
# path either - recovery is only for DEREGISTERED residue
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$r2 = Run-Dev $canon @('cleanup', 'glm', 'w7')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
Check 'W7 registered worktree without marker: cleanup refused' ($r2.Code -eq 1)
Check 'W7 registered worktree without marker: RECOVERED never claimed' ($r2.Out -notmatch 'RECOVERED')
Check 'W7 registered worktree without marker: branch kept' (Test-BranchExists $canon 'glm/w7')
Check 'W7 registered worktree without marker: metadata kept' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'w7'))

# --- W9: task branch moved after integration -> refuse ----------------------
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IntegratedTask $canon $root 'glm' 'w9' 'JARVIS-GLM-w9'
New-PartialRemovalResidue $canon $wt
$tree = GitOut $canon @('rev-parse', 'HEAD^{tree}')
$moved = (('moved after integration' | & git -C $canon commit-tree $tree) | Out-String).Trim()
GitRun $canon @('update-ref', 'refs/heads/glm/w9', $moved)
$r = Run-Dev $canon @('cleanup', 'glm', 'w9')
Check 'W9 branch moved: exit 1' ($r.Code -eq 1)
Check 'W9 branch moved: refused/deferred, never cleaned' ($r.Out -notmatch 'RECOVERED')
Check 'W9 branch moved: branch preserved' ((GitOut $canon @('rev-parse', 'refs/heads/glm/w9')) -eq $moved)
Check 'W9 branch moved: metadata preserved' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'w9'))
Check 'W9 branch moved: leftover folder untouched' (Test-Path -LiteralPath $wt)

# --- W10: recorded integration commit missing / unreachable -> refuse -------
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IntegratedTask $canon $root 'glm' 'w10' 'JARVIS-GLM-w10'
New-PartialRemovalResidue $canon $wt
Edit-RawMeta $canon 'glm' 'w10' { param($m) $m.integratedHead = ('b' * 40) }
$r = Run-Dev $canon @('cleanup', 'glm', 'w10')
Check 'W10 integrated commit missing: exit 1' ($r.Code -eq 1)
Check 'W10 integrated commit missing: RECOVERY REQUIRED' ($r.Out -match 'RECOVERY REQUIRED')
Check 'W10 integrated commit missing: branch preserved' (Test-BranchExists $canon 'glm/w10')
Check 'W10 integrated commit missing: metadata preserved' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'w10'))
# an integration commit that exists but is no longer reachable from main
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IntegratedTask $canon $root 'glm' 'w10b' 'JARVIS-GLM-w10b'
New-PartialRemovalResidue $canon $wt
$meta = Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'w10b') -Raw | ConvertFrom-Json
GitRun $canon @('reset', '--hard', $meta.mainHeadAtFinish)
$r = Run-Dev $canon @('cleanup', 'glm', 'w10b')
Check 'W10b integrated commit unreachable: exit 1' ($r.Code -eq 1)
Check 'W10b integrated commit unreachable: not recovered' ($r.Out -notmatch 'RECOVERED')
Check 'W10b integrated commit unreachable: branch preserved' (Test-BranchExists $canon 'glm/w10b')
Check 'W10b integrated commit unreachable: metadata preserved' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'w10b'))

# --- W11: canonical main legitimately advanced after integration ------------
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IntegratedTask $canon $root 'glm' 'w11' 'JARVIS-GLM-w11'
New-PartialRemovalResidue $canon $wt
CommitFile $canon 'other.txt' @('unrelated later work') 'unrelated main commit'
$mainAfter = GitOut $canon @('rev-parse', 'HEAD')
$r = Run-Dev $canon @('cleanup', 'glm', 'w11')
Check 'W11 main advanced: exit 1' ($r.Code -eq 1)
Check 'W11 main advanced: RECOVERY REQUIRED' ($r.Out -match 'RECOVERY REQUIRED')
Check 'W11 main advanced: RECOVERED never claimed' ($r.Out -notmatch 'RECOVERED')
Check 'W11 main advanced: branch preserved' (Test-BranchExists $canon 'glm/w11')
Check 'W11 main advanced: metadata preserved' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'w11'))
Check 'W11 main advanced: later main commit untouched' ((GitOut $canon @('rev-parse', 'HEAD')) -eq $mainAfter)

# --- W12: markerless residue never reaches the destructive seams ------------
# The post-removal seam and the ref transaction sit downstream of the final
# destructive gate; markerless residue must stop long before either, so seams
# planted there never fire. (Normal-path exact-OID CAS is covered by H4/H15.)
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IntegratedTask $canon $root 'glm' 'w12' 'JARVIS-GLM-w12'
New-PartialRemovalResidue $canon $wt
$driverPath = Join-Path $root 'driver-w12.ps1'
@'
. '__DEVPS__'
function Invoke-PostRemovalTestHook {
    param($ctx, $identity, [string]$CapturedT, [string]$CapturedM)
    Write-Host 'SEAM-REACHED=post-removal'
}
function Invoke-AtomicCleanupRefTransaction {
    param([string]$RepoPath, [string]$TaskRef, [string]$ExpectedT, [string]$ExpectedM)
    Write-Host 'SEAM-REACHED=ref-transaction'
    return [pscustomobject]@{ Ok = $true; Reason = '' }
}
Invoke-CleanupTask -Worker 'glm' -Task 'w12'
'@ -replace '__DEVPS__', $DevPs | Set-Content -LiteralPath $driverPath -Encoding UTF8
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = Get-TaskHandoffToken -Canon $canon -Worker 'glm' -Task 'w12'
$r = Run-Driver $driverPath $canon
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $null
Check 'W12 markerless residue: exit 1' ($r.Code -eq 1)
Check 'W12 markerless residue: RECOVERY REQUIRED' ($r.Out -match 'RECOVERY REQUIRED')
Check 'W12 markerless residue: no destructive seam reached' ($r.Out -notmatch 'SEAM-REACHED')
Check 'W12 markerless residue: branch survives' (Test-BranchExists $canon 'glm/w12')
Check 'W12 markerless residue: metadata survives' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'w12'))
}

if (Test-Section 'WINDOWS-B') {
# ============================================================================
Write-Host '== WINDOWS FINISH / PARTIAL-REMOVAL RECOVERY (W13-W19b) =='
# ============================================================================

# --- W13: no-force / no-recursive-delete policy preserved -------------------
# executable lines only - the prose in the header and comments explains the
# policy and must not be mistaken for a violation of it
$devCode = (@(Get-Content -LiteralPath $DevPs) | Where-Object { $_ -notmatch '^\s*#' }) -join "`n"
Check 'W13 worktree removal is never forced' ($devCode -notmatch "'worktree'\s*,\s*'remove'\s*,\s*'-")
Check 'W13 no --force anywhere in code' ($devCode -notmatch '--force')
Check 'W13 no recursive delete in code' ($devCode -notmatch '-Recurse')
Check 'W13 no recursive Directory.Delete' ($devCode -notmatch 'Directory\]::Delete\([^)]*,')

# --- W14: dirty / ignored content refusal preserved -------------------------
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IntegratedTask $canon $root 'glm' 'w14' 'JARVIS-GLM-w14'
Set-Content -LiteralPath (Join-Path $wt 'precious.txt') -Value 'uncommitted work'
$r = Run-Dev $canon @('cleanup', 'glm', 'w14')
Check 'W14 dirty worktree: exit 1' ($r.Code -eq 1)
Check 'W14 dirty worktree: deferred' ($r.Out -match 'CLEANUP DEFERRED')
Check 'W14 dirty worktree: content preserved' ((Get-Content -LiteralPath (Join-Path $wt 'precious.txt') -Raw).Trim() -eq 'uncommitted work')
Check 'W14 dirty worktree: branch preserved' (Test-BranchExists $canon 'glm/w14')
Set-Content -LiteralPath (Join-Path $wt '.gitignore') -Value 'cache/'
GitRun $wt @('add', '.gitignore')
GitRun $wt @('commit', '-m', 'ignore cache')
Remove-Item -LiteralPath (Join-Path $wt 'precious.txt') -Force
$null = New-Item -ItemType Directory -Force -Path (Join-Path $wt 'cache')
Set-Content -LiteralPath (Join-Path $wt 'cache\blob.bin') -Value 'ignored cache'
$r = Run-Dev $canon @('cleanup', 'glm', 'w14')
Check 'W14 ignored content: exit 1' ($r.Code -eq 1)
Check 'W14 ignored content: never silently deleted' ((Get-Content -LiteralPath (Join-Path $wt 'cache\blob.bin') -Raw).Trim() -eq 'ignored cache')

# --- W15: unfinished Git operation refusal preserved ------------------------
# canonical repository mid-operation: recovery must refuse too
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IntegratedTask $canon $root 'glm' 'w15' 'JARVIS-GLM-w15'
New-PartialRemovalResidue $canon $wt
Set-Content -LiteralPath (Join-Path $canon '.git\CHERRY_PICK_HEAD') -Value (GitOut $canon @('rev-parse', 'HEAD'))
$r = Run-Dev $canon @('cleanup', 'glm', 'w15')
Remove-Item -LiteralPath (Join-Path $canon '.git\CHERRY_PICK_HEAD') -Force
Check 'W15 canonical mid-operation: exit 1' ($r.Code -eq 1)
Check 'W15 canonical mid-operation: not recovered' ($r.Out -notmatch 'RECOVERED')
Check 'W15 canonical mid-operation: branch preserved' (Test-BranchExists $canon 'glm/w15')
Check 'W15 canonical mid-operation: metadata preserved' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'w15'))
# clearing the operation state does NOT unlock the markerless residue: only
# the reason changes, from the canonical-operation one to the policy one
$r = Run-Dev $canon @('cleanup', 'glm', 'w15')
Check 'W15 after clearing: still RECOVERY REQUIRED' (($r.Code -eq 1) -and ($r.Out -match 'RECOVERY REQUIRED: PARTIAL REMOVAL'))
Check 'W15 after clearing: branch preserved' (Test-BranchExists $canon 'glm/w15')

# --- W16: corrupted metadata still fails closed -----------------------------
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IntegratedTask $canon $root 'glm' 'w16' 'JARVIS-GLM-w16'
New-PartialRemovalResidue $canon $wt
Edit-RawMeta $canon 'glm' 'w16' { param($m) $m | Add-Member -NotePropertyName 'injected' -NotePropertyValue 'x' }
$r = Run-Dev $canon @('cleanup', 'glm', 'w16')
Check 'W16 unknown metadata field: exit 1' ($r.Code -eq 1)
Check 'W16 unknown metadata field: schema refusal' ($r.Out -match 'metadata schema')
Check 'W16 unknown metadata field: branch preserved' (Test-BranchExists $canon 'glm/w16')
Check 'W16 unknown metadata field: leftover folder untouched' (Test-Path -LiteralPath $wt)
# audit-sequence corruption on the same residue
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IntegratedTask $canon $root 'glm' 'w16b' 'JARVIS-GLM-w16b'
New-PartialRemovalResidue $canon $wt
Edit-RawMeta $canon 'glm' 'w16b' { param($m) $m.sourceCommitShas = @(('c' * 40)) }
$r = Run-Dev $canon @('cleanup', 'glm', 'w16b')
Check 'W16b corrupt audit sequence: exit 1' ($r.Code -eq 1)
Check 'W16b corrupt audit sequence: not recovered' ($r.Out -notmatch 'RECOVERED')
Check 'W16b corrupt audit sequence: branch preserved' (Test-BranchExists $canon 'glm/w16b')

# --- W17/W18: ordinary finish and cleanup paths remain intact ---------------
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-w17'
$null = Run-Dev $canon @('new', 'glm', 'w17')
CommitFile $wt 'task.txt' @('w17 change') 'w17 work'
$r = Run-Dev $canon @('finish', 'glm', 'w17')
Check 'W17 normal finish: exit 0' ($r.Code -eq 0)
Check 'W17 normal finish: INTEGRATED, not CLEANED (separate step)' (($r.Out -match 'INTEGRATED') -and ($r.Out -notmatch 'CLEANED'))
Check 'W17 normal finish: change on main' ((Get-Content -LiteralPath (Join-Path $canon 'task.txt') -Raw).Trim() -eq 'w17 change')
Check 'W17 normal finish: worktree, branch, metadata kept' ((Test-Path -LiteralPath $wt) -and (Test-BranchExists $canon 'glm/w17') -and (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'w17')))
$r = Run-Dev $canon @('cleanup', 'glm', 'w17')
Check 'W17b explicit cleanup: exit 0' ($r.Code -eq 0)
Check 'W17b explicit cleanup: CLEANED' ($r.Out -match 'CLEANED')
Check 'W17b explicit cleanup: worktree, branch, metadata gone' ((-not (Test-Path -LiteralPath $wt)) -and (-not (Test-BranchExists $canon 'glm/w17')) -and (-not (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'w17'))))
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IntegratedTask $canon $root 'glm' 'w18' 'JARVIS-GLM-w18'
$r = Run-Dev $canon @('cleanup', 'glm', 'w18')
Check 'W18 normal cleanup: exit 0' ($r.Code -eq 0)
Check 'W18 normal cleanup: CLEANED' ($r.Out -match 'CLEANED')
Check 'W18 normal cleanup: worktree, branch, metadata gone' ((-not (Test-Path -LiteralPath $wt)) -and (-not (Test-BranchExists $canon 'glm/w18')) -and (-not (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'w18'))))

# --- W19: status distinguishes the partial-removal state --------------------
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wtA = New-IntegratedTask $canon $root 'glm' 'w19a' 'JARVIS-GLM-w19a'
$null = Run-Dev $canon @('new', 'claude', 'w19b')
$wtB = New-IntegratedTask $canon $root 'glm' 'w19c' 'JARVIS-GLM-w19c'
New-PartialRemovalResidue $canon $wtB
$null = New-IntegratedTask $canon $root 'glm' 'w19d' 'JARVIS-GLM-w19d'
$null = Run-Dev $canon @('cleanup', 'glm', 'w19d')
$r = Run-Dev $canon @('status')
Check 'W19 status: exit 0' ($r.Code -eq 0)
Check 'W19 status: ACTIVE task listed' ($r.Out -match 'ACTIVE\s+claude/w19b')
Check 'W19 status: integrated task marked cleanup pending' (($r.Out -split "`n" | Where-Object { $_ -match 'glm/w19a' }) -match 'CLEANUP PENDING')
Check 'W19 status: residue marked recovery required' (($r.Out -split "`n" | Where-Object { $_ -match 'glm/w19c' }) -match 'PARTIAL REMOVAL - RECOVERY REQUIRED')
Check 'W19 status: read-only, residue untouched' ((Test-Path -LiteralPath $wtB) -and (Test-BranchExists $canon 'glm/w19c'))
Check 'W19 status: cleaned tasks are simply absent' ($r.Out -notmatch 'w19d')

# --- W19b: an absent directory is NOT recovered (two-evidence preserved) ----
# Without the stranded folder, retained metadata is the only witness that a
# helper worktree ever existed there - the missing-worktree policy still holds.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IntegratedTask $canon $root 'glm' 'w19e' 'JARVIS-GLM-w19e'
GitRun $canon @('worktree', 'remove', $wt)
Check 'W19b precondition: directory absent' (-not (Test-Path -LiteralPath $wt))
$r = Run-Dev $canon @('cleanup', 'glm', 'w19e')
Check 'W19b absent directory: exit 1' ($r.Code -eq 1)
Check 'W19b absent directory: RECOVERY REQUIRED: WORKTREE MISSING' ($r.Out -match 'RECOVERY REQUIRED: WORKTREE MISSING')
Check 'W19b absent directory: RECOVERED never claimed' ($r.Out -notmatch 'RECOVERED')
Check 'W19b absent directory: CLEANED never claimed' ($r.Out -notmatch 'CLEANED')
Check 'W19b absent directory: branch preserved' (Test-BranchExists $canon 'glm/w19e')
Check 'W19b absent directory: metadata preserved' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'w19e'))
}

if (Test-Section 'WINDOWS-C') {
# ============================================================================
Write-Host '== WINDOWS FINISH / PARTIAL-REMOVAL RECOVERY (W21-W23) =='
# ============================================================================

# --- W21: INCOMPLETE private worktree admin residue fails closed ------------
# `git worktree list` OMITS an admin entry whose representation is incomplete,
# and an unreadable or stale pointer proves nothing either. The admin
# inspection is kept as DIAGNOSIS only - it never authorises a deletion - and
# every uncertain shape must surface in the refusal with the task branch,
# metadata and residue all preserved.

function New-IncompleteAdminResidue {
    # The exact attack shape: the git-owned removal completes and the worktree
    # is deregistered, but an INCOMPLETE admin entry for it survives in
    # .git/worktrees. $EntryName decides whether the residue keeps the
    # target's basename; $Pointer selects the incomplete representation.
    param([string]$Canon, [string]$Wt, [string]$WtName, [string]$EntryName,
        [string]$Pointer, [string]$Elsewhere = 'C:\some\other\worktree')
    $bak = Join-Path $Canon '.git\admin-residue-backup'
    Copy-Item -LiteralPath (Join-Path $Canon ".git\worktrees\$WtName") -Destination $bak -Recurse -Force
    New-PartialRemovalResidue $Canon $Wt
    $null = New-Item -ItemType Directory -Force -Path (Join-Path $Canon '.git\worktrees')
    $dest = Join-Path $Canon ".git\worktrees\$EntryName"
    Move-Item -LiteralPath $bak -Destination $dest
    $gitdir = Join-Path $dest 'gitdir'
    switch ($Pointer) {
        'missing' { Remove-Item -LiteralPath $gitdir -Force }
        'empty' { [System.IO.File]::WriteAllText($gitdir, '') }
        'blank' { [System.IO.File]::WriteAllText($gitdir, "  `r`n `r`n") }
        'malformed' { [System.IO.File]::WriteAllText($gitdir, "not-a-path/.git`n") }
        'ambiguous' { [System.IO.File]::WriteAllText($gitdir, "$Elsewhere\.git`n$Elsewhere-2\.git`n") }
        'elsewhere' { [System.IO.File]::WriteAllText($gitdir, "$Elsewhere\.git`n") }
        'target' { }
        default { throw "unknown pointer shape $Pointer" }
    }
    return $dest
}

# Every incomplete/uncertain pointer shape must fail closed, preserving the
# branch, the metadata, the leftover folder and the admin residue itself.
$uncertain = @(
    @{ Task = 'w21a'; Pointer = 'missing'; Entry = 'resid-a'; Reason = 'has no gitdir pointer' },
    @{ Task = 'w21b'; Pointer = 'empty'; Entry = 'resid-b'; Reason = 'is empty' },
    @{ Task = 'w21c'; Pointer = 'blank'; Entry = 'resid-c'; Reason = 'is empty' },
    @{ Task = 'w21d'; Pointer = 'malformed'; Entry = 'resid-d'; Reason = 'is malformed' },
    @{ Task = 'w21e'; Pointer = 'ambiguous'; Entry = 'resid-e'; Reason = 'resolves ambiguously' },
    @{ Task = 'w21n'; Pointer = 'elsewhere'; Entry = 'resid-n'; Reason = 'resolves to a worktree that does not exist' },
    @{ Task = 'w21g'; Pointer = 'elsewhere'; Entry = 'JARVIS-GLM-w21g'; Reason = 'carries the task worktree name' }
)
foreach ($case in $uncertain) {
    $t = $case.Task
    $root = New-TempRoot
    $canon = New-CanonicalRepo $root
    $name = "JARVIS-GLM-$t"
    $wt = New-IntegratedTask $canon $root 'glm' $t $name
    $entry = New-IncompleteAdminResidue $canon $wt $name $case.Entry $case.Pointer
    Check "W21 $t ($($case.Pointer)): git worktree list omits the target" ((GitOut $canon @('worktree', 'list', '--porcelain')) -notmatch [regex]::Escape($name))
    Check "W21 $t ($($case.Pointer)): admin residue present" (Test-Path -LiteralPath $entry)
    $r = Run-Dev $canon @('cleanup', 'glm', $t)
    Check "W21 $t ($($case.Pointer)): exit 1" ($r.Code -eq 1)
    Check "W21 $t ($($case.Pointer)): RECOVERY REQUIRED" ($r.Out -match 'RECOVERY REQUIRED: PARTIAL REMOVAL')
    Check "W21 $t ($($case.Pointer)): names the uncertainty" ($r.Out -match [regex]::Escape($case.Reason))
    Check "W21 $t ($($case.Pointer)): RECOVERED never claimed" ($r.Out -notmatch 'RECOVERED')
    Check "W21 $t ($($case.Pointer)): CLEANED never claimed" ($r.Out -notmatch 'CLEANED')
    Check "W21 $t ($($case.Pointer)): task branch preserved" (Test-BranchExists $canon "glm/$t")
    Check "W21 $t ($($case.Pointer)): metadata preserved" (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' $t))
    Check "W21 $t ($($case.Pointer)): leftover folder preserved" (Test-Path -LiteralPath $wt)
    Check "W21 $t ($($case.Pointer)): admin residue untouched" (Test-Path -LiteralPath $entry)
}

# --- W21f: an admin entry that still refers to the target is never absence --
# With an intact pointer git DOES still list the worktree (as prunable), so
# this shape is refused on the registered path; the admin diagnosis reports it
# as residue regardless, so neither route can mistake it for absence.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IntegratedTask $canon $root 'glm' 'w21f' 'JARVIS-GLM-w21f'
$entry = New-IncompleteAdminResidue $canon $wt 'JARVIS-GLM-w21f' 'resid-f' 'target'
$driverPath = Join-Path $root 'driver-w21f.ps1'
@'
. '__DEVPS__'
$ctx = Get-CanonicalContext
$id = Get-DerivedTaskIdentity -ctx $ctx -Worker 'glm' -Task 'w21f'
Write-Host "RESIDUE-REASON=$(Get-WorktreeAdminResidueReason -ctx $ctx -WorktreePath $id.WorktreePath)"
'@ -replace '__DEVPS__', $DevPs | Set-Content -LiteralPath $driverPath -Encoding UTF8
$r = Run-Driver $driverPath $canon
Check 'W21f pointer at target: reported as residue, not absence' ($r.Out -match 'RESIDUE-REASON=.*still refers to the task worktree')
$r = Run-Dev $canon @('cleanup', 'glm', 'w21f')
Check 'W21f pointer at target: exit 1' ($r.Code -eq 1)
Check 'W21f pointer at target: RECOVERED never claimed' ($r.Out -notmatch 'RECOVERED')
Check 'W21f pointer at target: CLEANED never claimed' ($r.Out -notmatch 'CLEANED')
Check 'W21f pointer at target: branch preserved' (Test-BranchExists $canon 'glm/w21f')
Check 'W21f pointer at target: metadata preserved' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'w21f'))
Check 'W21f pointer at target: admin residue untouched' (Test-Path -LiteralPath $entry)

# --- W21h: an UNREADABLE gitdir pointer is uncertainty, not absence ---------
# The pointer is intact and points elsewhere, but is held open exclusively, so
# its identity cannot be established. git still omits the target from its
# listing; the helper must refuse rather than skip the entry.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IntegratedTask $canon $root 'glm' 'w21h' 'JARVIS-GLM-w21h'
$entry = New-IncompleteAdminResidue $canon $wt 'JARVIS-GLM-w21h' 'resid-h' 'elsewhere'
$lock = [System.IO.File]::Open((Join-Path $entry 'gitdir'), 'Open', 'Read', 'None')
try {
    Check 'W21h unreadable pointer: git worktree list omits the target' ((GitOut $canon @('worktree', 'list', '--porcelain')) -notmatch 'JARVIS-GLM-w21h')
    $r = Run-Dev $canon @('cleanup', 'glm', 'w21h')
}
finally { $lock.Dispose() }
Check 'W21h unreadable pointer: exit 1' ($r.Code -eq 1)
Check 'W21h unreadable pointer: RECOVERY REQUIRED' ($r.Out -match 'RECOVERY REQUIRED: PARTIAL REMOVAL')
Check 'W21h unreadable pointer: reported as unreadable' ($r.Out -match 'could not be read')
Check 'W21h unreadable pointer: not recovered' ($r.Out -notmatch 'RECOVERED')
Check 'W21h unreadable pointer: branch preserved' (Test-BranchExists $canon 'glm/w21h')
Check 'W21h unreadable pointer: metadata preserved' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'w21h'))
# a readable pointer changes only the DIAGNOSIS, never the outcome: markerless
# partial-removal state is refused either way and nothing is ever deleted
$r = Run-Dev $canon @('cleanup', 'glm', 'w21h')
Check 'W21h readable again: still RECOVERY REQUIRED' (($r.Code -eq 1) -and ($r.Out -match 'RECOVERY REQUIRED: PARTIAL REMOVAL'))
Check 'W21h readable again: RECOVERED never claimed' ($r.Out -notmatch 'RECOVERED')
Check 'W21h readable again: branch preserved' (Test-BranchExists $canon 'glm/w21h')
Check 'W21h readable again: metadata preserved' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'w21h'))
Check 'W21h readable again: residue untouched' (Test-Path -LiteralPath $entry)

# --- W21i: an unrelated HEALTHY admin entry never blocks normal cleanup -----
# A second, live, fully registered task keeps its own admin entry in the same
# store; the post-removal absence proof must dismiss it as conclusively
# unrelated so ordinary marker-backed cleanup still reaches CLEANED.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$other = New-IntegratedTask $canon $root 'claude' 'w21i-live' 'JARVIS-Claude-w21i-live'
$wt = New-IntegratedTask $canon $root 'glm' 'w21i' 'JARVIS-GLM-w21i'
Check 'W21i precondition: unrelated healthy admin entry exists' (Test-Path -LiteralPath (Join-Path $canon '.git\worktrees\JARVIS-Claude-w21i-live'))
$r = Run-Dev $canon @('cleanup', 'glm', 'w21i')
Check 'W21i unrelated healthy entry: exit 0' ($r.Code -eq 0)
Check 'W21i unrelated healthy entry: CLEANED' ($r.Out -match 'CLEANED')
Check 'W21i unrelated healthy entry: target worktree, branch, metadata gone' ((-not (Test-Path -LiteralPath $wt)) -and (-not (Test-BranchExists $canon 'glm/w21i')) -and (-not (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'w21i'))))
Check 'W21i unrelated healthy entry: other task untouched' ((Test-Path -LiteralPath $other) -and (Test-BranchExists $canon 'claude/w21i-live'))

# --- W21j: CLEANED is never emitted while admin residue remains -------------
# Normal (registered) cleanup path: git removes the worktree but incomplete
# admin residue for the same path survives. The branch and metadata must NOT
# be deleted, and CLEANED must not be claimed.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IntegratedTask $canon $root 'glm' 'w21j' 'JARVIS-GLM-w21j'
$driverPath = Join-Path $root 'driver-w21j.ps1'
@'
. '__DEVPS__'
function Remove-WorktreeDir {
    param($ctx, [string]$WorktreePath)
    # genuine git-owned removal, but the private admin entry survives with an
    # unreadable identity (the Windows partial-teardown shape)
    $admin = Get-WorktreeAdminDir -WorktreePath $WorktreePath
    $bak = Join-Path $ctx.CommonDir 'admin-residue-backup'
    Copy-Item -LiteralPath $admin -Destination $bak -Recurse -Force
    $null = Invoke-GitCapture -RepoPath $ctx.MainPath -GitArgs @('worktree', 'remove', $WorktreePath)
    $null = New-Item -ItemType Directory -Force -Path (Join-Path $ctx.CommonDir 'worktrees')
    Move-Item -LiteralPath $bak -Destination (Join-Path $ctx.CommonDir 'worktrees\resid-j')
    Remove-Item -LiteralPath (Join-Path $ctx.CommonDir 'worktrees\resid-j\gitdir') -Force
    return 'REMOVED'
}
Invoke-CleanupTask -Worker 'glm' -Task 'w21j'
'@ -replace '__DEVPS__', $DevPs | Set-Content -LiteralPath $driverPath -Encoding UTF8
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = Get-TaskHandoffToken -Canon $canon -Worker 'glm' -Task 'w21j'
$r = Run-Driver $driverPath $canon
$env:JARVIS_DEV_EXCLUSIVE_HANDOFF = $null
Check 'W21j admin residue after removal: exit 1' ($r.Code -eq 1)
Check 'W21j admin residue after removal: CLEANUP DEFERRED' ($r.Out -match 'CLEANUP DEFERRED')
Check 'W21j admin residue after removal: CLEANED never claimed' ($r.Out -notmatch 'CLEANED')
Check 'W21j admin residue after removal: branch preserved' (Test-BranchExists $canon 'glm/w21j')
Check 'W21j admin residue after removal: metadata preserved' (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'w21j'))

# --- W22: source-level guard - no automatic markerless recovery path --------
# The destructive markerless-recovery authority is REMOVED, not merely
# unreachable: no function, command or alternate path may delete a task ref or
# its metadata outside the single marker-backed lifecycle.
$devSrc = Get-Content -LiteralPath $DevPs -Raw
$devExec = (@(Get-Content -LiteralPath $DevPs) | Where-Object { $_ -notmatch '^\s*#' }) -join "`n"
Check 'W22 no partial-removal recovery function' ($devSrc -cnotmatch 'Invoke-PartialRemovalRecovery')
Check 'W22 no partial-removal classification' ($devSrc -cnotmatch 'Get-PartialRemovalClassification')
Check 'W22 helper can never emit RECOVERED' ($devSrc -cnotmatch 'RECOVERED')
Check 'W22 single task-ref deletion call site' (@([regex]::Matches($devExec, 'Invoke-AtomicCleanupRefTransaction\s+-RepoPath')).Count -eq 1)
Check 'W22 single metadata deletion call site' (@([regex]::Matches($devExec, 'Remove-Item -LiteralPath \$MetaPath')).Count -eq 1)
Check 'W22 single final destructive gate call site' (@([regex]::Matches($devExec, 'Assert-FinalDestructivePreconditions -ctx')).Count -eq 1)
Check 'W22 markerless refusal is consulted before the gate' ($devExec -match 'Get-MarkerlessCleanupRefusal')
Check 'W22 markerless diagnosis can only refuse' ($devExec -cnotmatch "Get-MarkerlessCleanupRefusal[\s\S]{0,400}?'RECOVER'")
Check 'W22 no recover/repair/prune command exposed' ($devExec -cnotmatch "(?i)'(recover|repair|prune|force-clean)'")
# the ONE metadata deletion and the ONE ref transaction must both sit inside
# Invoke-CleanupCore, after the final gate - never in any other function
$cleanupCore = [regex]::Match($devExec, 'function Invoke-CleanupCore \{[\s\S]*?\n\}').Value
Check 'W22 ref transaction lives in Invoke-CleanupCore' ($cleanupCore -match 'Invoke-AtomicCleanupRefTransaction\s+-RepoPath')
Check 'W22 metadata deletion lives in Invoke-CleanupCore' ($cleanupCore -match 'Remove-Item -LiteralPath \$MetaPath')
Check 'W22 the gate precedes both in the same function' ($cleanupCore.IndexOf('Assert-FinalDestructivePreconditions') -lt $cleanupCore.IndexOf('Invoke-AtomicCleanupRefTransaction -RepoPath'))
Check 'W22 the markerless refusal precedes the gate' ($cleanupCore.IndexOf('Get-MarkerlessCleanupRefusal') -lt $cleanupCore.IndexOf('Assert-FinalDestructivePreconditions'))

# --- W23: every markerless shape preserves branch AND metadata --------------
# One sweep over the shapes the policy enumerates, asserting the same
# invariants for each: refusal, no RECOVERED, no CLEANED, branch + metadata
# intact.
$shapes = @(
    @{ Name = 'deregistered-empty-dir'; Task = 'w23a'; Setup = { param($c, $w) New-PartialRemovalResidue $c $w } },
    @{ Name = 'deregistered-absent-dir'; Task = 'w23b'; Setup = { param($c, $w) GitRun $c @('worktree', 'remove', $w) } },
    @{ Name = 'deregistered-non-empty-dir'; Task = 'w23c'; Setup = { param($c, $w) New-PartialRemovalResidue $c $w; Set-Content -LiteralPath (Join-Path $w 'stray.txt') -Value 'stray' } },
    @{ Name = 'registered-marker-deleted'; Task = 'w23d'; Setup = { param($c, $w) Remove-Item -LiteralPath (Get-MarkerPathLocal $w) -Force } },
    @{ Name = 'registered-dir-emptied'; Task = 'w23e'; Setup = { param($c, $w) Remove-Item -LiteralPath (Join-Path $w '.git') -Force } }
)
foreach ($shape in $shapes) {
    $t = $shape.Task
    $root = New-TempRoot
    $canon = New-CanonicalRepo $root
    $wt = New-IntegratedTask $canon $root 'glm' $t "JARVIS-GLM-$t"
    & $shape.Setup $canon $wt
    $r = Run-Dev $canon @('cleanup', 'glm', $t)
    Check "W23 $($shape.Name): exit 1" ($r.Code -eq 1)
    Check "W23 $($shape.Name): RECOVERY REQUIRED" ($r.Out -match 'RECOVERY REQUIRED')
    Check "W23 $($shape.Name): RECOVERED never claimed" ($r.Out -notmatch 'RECOVERED')
    Check "W23 $($shape.Name): CLEANED never claimed" ($r.Out -notmatch 'CLEANED')
    Check "W23 $($shape.Name): task branch preserved" (Test-BranchExists $canon "glm/$t")
    Check "W23 $($shape.Name): metadata preserved" (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' $t))
}
}

if (Test-Section 'WINDOWS-D') {
# ============================================================================
Write-Host '== WINDOWS FINISH / PARTIAL-REMOVAL RECOVERY (W24) =='
# ============================================================================

# --- W24: finish preflight removes ONLY allowlisted generated artifacts -----
# The UX fix: known ignored runtime artifacts (the test database and the three
# named __pycache__ directories) no longer refuse an otherwise clean
# implementation worktree. Everything about that is bounded: the allowlist is
# exact, each entry is re-proved untracked and ignored, nothing is deleted
# before the ownership proofs pass, and every other dirty shape still refuses.

function New-GeneratedArtifacts {
    # The exact artifact set a test run leaves behind, plus the ignore rules
    # that make them ignored content in the first place.
    param([string]$Worktree)
    foreach ($rel in @('jarvis\__pycache__', 'jarvis\connectors\__pycache__', 'tests\__pycache__', 'data')) {
        $null = New-Item -ItemType Directory -Force -Path (Join-Path $Worktree $rel)
    }
    Set-Content -LiteralPath (Join-Path $Worktree 'jarvis\__pycache__\core.cpython-313.pyc') -Value 'bytecode'
    Set-Content -LiteralPath (Join-Path $Worktree 'jarvis\connectors\__pycache__\base.cpython-313.pyc') -Value 'bytecode'
    Set-Content -LiteralPath (Join-Path $Worktree 'tests\__pycache__\t.cpython-313.pyc') -Value 'bytecode'
    Set-Content -LiteralPath (Join-Path $Worktree 'data\jarvis.db') -Value 'sqlite'
    Set-Content -LiteralPath (Join-Path $Worktree 'data\jarvis.db-wal') -Value 'wal'
}

function Test-AnyArtifactPresent {
    param([string]$Worktree)
    foreach ($rel in @('jarvis\__pycache__', 'jarvis\connectors\__pycache__', 'tests\__pycache__', 'data\jarvis.db', 'data\jarvis.db-wal')) {
        if (Test-Path -LiteralPath (Join-Path $Worktree $rel)) { return $true }
    }
    return $false
}

function New-IgnoringTask {
    # An ACTIVE task worktree with one real commit and committed ignore rules.
    param([string]$Canon, [string]$Root, [string]$Task)
    $wt = Join-Path $Root "JARVIS-GLM-$Task"
    $null = Run-Dev $Canon @('new', 'glm', $Task)
    CommitFile $wt '.gitignore' @('__pycache__/', 'data/', '*.log') 'ignore generated artifacts'
    CommitFile $wt 'task.txt' @("$Task change") "$Task work"
    return $wt
}

# W24a: allowlisted artifacts only -> finish proceeds and removes exactly them
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IgnoringTask $canon $root 'w24a'
New-GeneratedArtifacts $wt
Set-Content -LiteralPath (Join-Path $wt 'jarvis\keep.py') -Value 'tracked-neighbour'
GitRun $wt @('add', 'jarvis/keep.py')
GitRun $wt @('commit', '-m', 'neighbour')
$r = Run-Dev $canon @('finish', 'glm', 'w24a')
Check 'W24a artifacts only: exit 0' ($r.Code -eq 0)
Check 'W24a artifacts only: INTEGRATED' ($r.Out -match 'INTEGRATED')
Check 'W24a artifacts only: every allowlisted artifact removed' (-not (Test-AnyArtifactPresent $wt))
Check 'W24a artifacts only: tracked neighbour untouched' (Test-Path -LiteralPath (Join-Path $wt 'jarvis\keep.py'))
Check 'W24a artifacts only: change on main' ((Get-Content -LiteralPath (Join-Path $canon 'task.txt') -Raw).Trim() -eq 'w24a change')
# The integrate-only lifecycle is unchanged: still no removal, still a
# separate explicit cleanup step.
Check 'W24a artifacts only: worktree and branch kept' ((Test-Path -LiteralPath $wt) -and (Test-BranchExists $canon 'glm/w24a'))
Check 'W24a artifacts only: not CLEANED' ($r.Out -notmatch 'CLEANED')

# W24b: one ignored file OUTSIDE the allowlist -> refuse, and delete NOTHING
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IgnoringTask $canon $root 'w24b'
New-GeneratedArtifacts $wt
Set-Content -LiteralPath (Join-Path $wt 'run.log') -Value 'ignored log'
$r = Run-Dev $canon @('finish', 'glm', 'w24b')
Check 'W24b stray ignored file: exit 1' ($r.Code -eq 1)
Check 'W24b stray ignored file: refusal reported' ($r.Out -match 'finish refused')
Check 'W24b stray ignored file: never deleted' ((Get-Content -LiteralPath (Join-Path $wt 'run.log') -Raw).Trim() -eq 'ignored log')
Check 'W24b stray ignored file: allowlisted artifacts also untouched' (Test-AnyArtifactPresent $wt)
Check 'W24b stray ignored file: nothing integrated' ((Get-Content -LiteralPath (Join-Path $canon 'shared.txt') -Raw).Trim() -eq 'base')

# W24c: a stray file INSIDE a wholly-ignored allowlisted directory refuses -
# the whole 'data/' directory is never removed on the strength of one named
# database file (the git-clean escalation trap)
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IgnoringTask $canon $root 'w24c'
New-GeneratedArtifacts $wt
Set-Content -LiteralPath (Join-Path $wt 'data\operator-notes.txt') -Value 'not mine to delete'
$r = Run-Dev $canon @('finish', 'glm', 'w24c')
Check 'W24c stray file in data/: exit 1' ($r.Code -eq 1)
Check 'W24c stray file in data/: survives' ((Get-Content -LiteralPath (Join-Path $wt 'data\operator-notes.txt') -Raw).Trim() -eq 'not mine to delete')
Check 'W24c stray file in data/: database not removed either' (Test-Path -LiteralPath (Join-Path $wt 'data\jarvis.db'))

# W24d: a __pycache__ the allowlist does not name is NOT auto-removed
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IgnoringTask $canon $root 'w24d'
$null = New-Item -ItemType Directory -Force -Path (Join-Path $wt 'dev\dashboard\__pycache__')
Set-Content -LiteralPath (Join-Path $wt 'dev\dashboard\__pycache__\r.cpython-313.pyc') -Value 'bytecode'
$r = Run-Dev $canon @('finish', 'glm', 'w24d')
Check 'W24d unnamed __pycache__: exit 1' ($r.Code -eq 1)
Check 'W24d unnamed __pycache__: survives' (Test-Path -LiteralPath (Join-Path $wt 'dev\dashboard\__pycache__\r.cpython-313.pyc'))

# W24e: uncommitted work and untracked non-ignored content still refuse
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IgnoringTask $canon $root 'w24e'
New-GeneratedArtifacts $wt
Set-Content -LiteralPath (Join-Path $wt 'precious.txt') -Value 'uncommitted work'
$r = Run-Dev $canon @('finish', 'glm', 'w24e')
Check 'W24e untracked file: exit 1' ($r.Code -eq 1)
Check 'W24e untracked file: preserved' ((Get-Content -LiteralPath (Join-Path $wt 'precious.txt') -Raw).Trim() -eq 'uncommitted work')
Remove-Item -LiteralPath (Join-Path $wt 'precious.txt') -Force
Set-Content -LiteralPath (Join-Path $wt 'task.txt') -Value 'edited in place'
$r = Run-Dev $canon @('finish', 'glm', 'w24e')
Check 'W24e tracked modification: exit 1' ($r.Code -eq 1)
Check 'W24e tracked modification: preserved' ((Get-Content -LiteralPath (Join-Path $wt 'task.txt') -Raw).Trim() -eq 'edited in place')

# W24f: a TRACKED file at an allowlisted path is never removed
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IgnoringTask $canon $root 'w24f'
$null = New-Item -ItemType Directory -Force -Path (Join-Path $wt 'data')
Set-Content -LiteralPath (Join-Path $wt 'data\jarvis.db') -Value 'deliberately committed'
GitRun $wt @('add', '-f', 'data/jarvis.db')
GitRun $wt @('commit', '-m', 'track the database on purpose')
$r = Run-Dev $canon @('finish', 'glm', 'w24f')
Check 'W24f tracked allowlisted path: exit 0' ($r.Code -eq 0)
Check 'W24f tracked allowlisted path: file kept in worktree' ((Get-Content -LiteralPath (Join-Path $wt 'data\jarvis.db') -Raw).Trim() -eq 'deliberately committed')
Check 'W24f tracked allowlisted path: integrated onto main' ((Get-Content -LiteralPath (Join-Path $canon 'data\jarvis.db') -Raw).Trim() -eq 'deliberately committed')

# W24g: ORDERING - nothing is deleted before the ownership proof passes
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = New-IgnoringTask $canon $root 'w24g'
New-GeneratedArtifacts $wt
Remove-Item -LiteralPath (Get-MarkerPathLocal $wt) -Force
$r = Run-Dev $canon @('finish', 'glm', 'w24g')
Check 'W24g missing marker: exit 1' ($r.Code -eq 1)
Check 'W24g missing marker: ownership refusal' ($r.Out -match 'ownership evidence refused')
Check 'W24g missing marker: artifacts untouched' (Test-AnyArtifactPresent $wt)
Check 'W24g missing marker: nothing integrated' (-not (Test-Path -LiteralPath (Join-Path $canon 'task.txt')))
}

# ---- summary ---------------------------------------------------------------

foreach ($t in $script:tempRoots) {
    try { Remove-Item -LiteralPath $t -Recurse -Force -ErrorAction Stop } catch { Write-Host "  (could not remove temp root: $t)" }
}

if ($ResultFile) {
    $report = [pscustomobject]@{ sections = @($script:selected); pass = $script:pass; fail = $script:fail; failures = @($script:failures) }
    [System.IO.File]::WriteAllText($ResultFile, (ConvertTo-Json -InputObject $report), (New-Object System.Text.UTF8Encoding($false)))
}

Write-Host ''
Write-Host "RESULT: $script:pass passed, $script:fail failed"
if ($script:fail -gt 0) {
    $script:failures | ForEach-Object { Write-Host "  FAILED: $_" -ForegroundColor Red }
    exit 1
}
exit 0
