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

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$DevPs = Join-Path $RepoRoot 'dev.ps1'

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
    $root = Join-Path ([System.IO.Path]::GetTempPath()) ("devhelper-test-" + [guid]::NewGuid().ToString('N'))
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

function New-CanonicalRepo {
    param([string]$Root)
    $canon = Join-Path $Root 'JARVIS'
    $null = New-Item -ItemType Directory -Path $canon
    GitRun $canon @('init')
    GitRun $canon @('symbolic-ref', 'HEAD', 'refs/heads/main')
    GitRun $canon @('config', 'user.email', 'test@example.local')
    GitRun $canon @('config', 'user.name', 'tester')
    GitRun $canon @('config', 'commit.gpgsign', 'false')
    Set-Content -Path (Join-Path $canon 'README.md') -Value 'base'
    Set-Content -Path (Join-Path $canon 'shared.txt') -Value 'base'
    GitRun $canon @('add', 'README.md', 'shared.txt')
    GitRun $canon @('commit', '-m', 'init')
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
    param([string]$Cwd, [string[]]$DevArgs)
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
        $raw = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $DevPs @DevArgs 2>&1
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

# ============================================================================
Write-Host '== FINISH =='
# ============================================================================

# T8: clean committed task cherry-picks into main, cleanup succeeds
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-fix1'
$null = Run-Dev $canon @('new', 'glm', 'fix1')
CommitFile $wt 'task.txt' @('hello') 'add task file'
$r = Run-Dev $canon @('finish', 'glm', 'fix1')
Check 'finish happy: exit 0' ($r.Code -eq 0)
Check 'finish happy: INTEGRATED' ($r.Out -match 'INTEGRATED')
Check 'finish happy: CLEANED' ($r.Out -match 'CLEANED')
Check 'finish happy: not deferred' ($r.Out -notmatch 'CLEANUP DEFERRED')
Check 'finish happy: change on main' ((Get-Content -LiteralPath (Join-Path $canon 'task.txt') -Raw).Trim() -eq 'hello')
Check 'finish happy: worktree removed' (-not (Test-Path -LiteralPath $wt))
$branchGone = $true
try { $null = GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/fix1'); $branchGone = $false } catch { $branchGone = $true }
Check 'finish happy: branch gone from refs' $branchGone
Check 'finish happy: metadata removed' (-not (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'fix1')))
$st = GitOut $canon @('status', '--porcelain')
Check 'finish happy: main clean' ([string]::IsNullOrWhiteSpace($st))

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

# T16: deterministic lock seam - integration succeeds, cleanup deferred
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-fix9'
$null = Run-Dev $canon @('new', 'glm', 'fix9')
CommitFile $wt 'task.txt' @('deferred') 'deferred task'
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = '1'
$r = Run-Dev $canon @('finish', 'glm', 'fix9')
$env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED = $null
Check 'finish deferred: exit 0' ($r.Code -eq 0)
Check 'finish deferred: INTEGRATED' ($r.Out -match 'INTEGRATED')
Check 'finish deferred: CLEANUP DEFERRED' ($r.Out -match 'CLEANUP DEFERRED')
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
$r = Run-Dev $canon @('cleanup', 'glm', 'fix9')
Check 'cleanup after deferred: exit 0' ($r.Code -eq 0)
Check 'cleanup after deferred: CLEANED' ($r.Out -match 'CLEANED')
Check 'cleanup after deferred: worktree gone' (-not (Test-Path -LiteralPath $wt))
$branchGone = $true
try { $null = GitOut $canon @('rev-parse', '--verify', 'refs/heads/glm/fix9'); $branchGone = $false } catch { }
Check 'cleanup after deferred: branch gone' $branchGone
Check 'cleanup after deferred: metadata gone' (-not (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'fix9')))

# T17: real Windows file lock held by another process
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-fix10'
$null = Run-Dev $canon @('new', 'glm', 'fix10')
CommitFile $wt 'README.md' @('task version') 'task edits readme'
# Realistic lock: another process holds README.md open so it cannot be
# DELETED, but git can still read it (e.g. an editor or ZCode session).
$lock = [System.IO.File]::Open((Join-Path $wt 'README.md'), 'Open', 'Read', 'Read')
try {
    $r = Run-Dev $canon @('finish', 'glm', 'fix10')
} finally {
    $lock.Close()
}
Check 'finish real lock: exit 0' ($r.Code -eq 0)
Check 'finish real lock: INTEGRATED' ($r.Out -match 'INTEGRATED')
Check 'finish real lock: CLEANUP DEFERRED' ($r.Out -match 'CLEANUP DEFERRED')
Check 'finish real lock: change on main' ((Get-Content -LiteralPath (Join-Path $canon 'README.md') -Raw).Trim() -eq 'task version')
$meta = Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'fix10') -Raw | ConvertFrom-Json
Check 'finish real lock: state INTEGRATED' ($meta.state -eq 'INTEGRATED')
# git partially removed the worktree: dir remains but git deregistered it.
# The helper never deletes that unregistered leftover itself.
Check 'finish real lock: leftover folder kept' (Test-Path -LiteralPath $wt)
Check 'finish real lock: locked file byte-for-byte' ((Get-Content -LiteralPath (Join-Path $wt 'README.md') -Raw).Trim() -eq 'task version')
$r = Run-Dev $canon @('cleanup', 'glm', 'fix10')
Check 'cleanup real lock leftover: exit 1' ($r.Code -eq 1)
Check 'cleanup real lock leftover: refused' ($r.Out -match 'refused')
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
Check 'finish-current happy: CLEANED' ($r.Out -match 'CLEANED')
Check 'finish-current happy: change on main' ((Get-Content -LiteralPath (Join-Path $canon 'task.txt') -Raw).Trim() -eq 'fc1 change')
Check 'finish-current happy: worktree removed' (-not (Test-Path -LiteralPath $wt))
Check 'finish-current happy: metadata gone' (-not (Test-Path -LiteralPath (Get-MetaPathLocal $canon 'glm' 'fc1')))
$st = GitOut $canon @('status', '--porcelain')
Check 'finish-current happy: main clean' ([string]::IsNullOrWhiteSpace($st))

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
Check 'finish-current subdir: CLEANED' ($r.Out -match 'CLEANED')

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
# finish automatic-cleanup path: integration succeeds, the gate refuses, so
# finish reports the ref change truthfully and the late commit survives.
$root = New-TempRoot
$canon = New-CanonicalRepo $root
$wt = Join-Path $root 'JARVIS-GLM-tof'
$null = Run-Dev $canon @('new', 'glm', 'tof')
CommitFile $wt 'task.txt' @('tof work') 'tof work'
$driverPath = Join-Path $root 'driver-finish.ps1'
@'
. '__DEVPS__'
function Invoke-PreDeleteTestHook {
    param($ctx, $identity, $meta)
    Set-Content -LiteralPath (Join-Path $identity.WorktreePath 'late.txt') -Value 'late work'
    & git -C $identity.WorktreePath add late.txt
    & git -C $identity.WorktreePath commit -m 'late commit' | Out-Null
}
try {
    Invoke-FinishTask -Worker 'glm' -Task 'tof'
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
Check 'B2 finish TOCTOU: integration still succeeded' ($r.Out -match 'INTEGRATED')
Check 'B2 finish TOCTOU: deferred, not deleted' ($r.Out -match 'CLEANUP DEFERRED')
Check 'B2 finish TOCTOU: gate noticed ref change' ($r.Out -match 'task branch ref changed since integration')
Check 'B2 finish TOCTOU: late commit survives' ((Get-Content -LiteralPath (Join-Path $wt 'late.txt') -Raw).Trim() -eq 'late work')
Check 'B2 finish TOCTOU: worktree kept' (Test-Path -LiteralPath $wt)
$meta = Get-Content -LiteralPath (Get-MetaPathLocal $canon 'glm' 'tof') -Raw | ConvertFrom-Json
Check 'B2 finish TOCTOU: metadata INTEGRATED' ($meta.state -eq 'INTEGRATED')

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

# ---- summary ---------------------------------------------------------------

foreach ($t in $script:tempRoots) {
    try { Remove-Item -LiteralPath $t -Recurse -Force -ErrorAction Stop } catch { Write-Host "  (could not remove temp root: $t)" }
}

Write-Host ''
Write-Host "RESULT: $script:pass passed, $script:fail failed"
if ($script:fail -gt 0) {
    $script:failures | ForEach-Object { Write-Host "  FAILED: $_" -ForegroundColor Red }
    exit 1
}
exit 0
