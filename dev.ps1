#Requires -Version 5.1
<#
.SYNOPSIS
  JARVIS isolated dev-task worktree helper (developer tooling only).

.USAGE
  .\dev.ps1 new <worker> <task>       create task worktree + branch from current canonical main HEAD
  .\dev.ps1 finish <worker> <task>    cherry-pick committed task into canonical main, then cleanup
  .\dev.ps1 finish-current            finish from inside the current helper-managed task worktree
  .\dev.ps1 cleanup <worker> <task>   remove an already INTEGRATED task worktree + branch
  .\dev.ps1 status                    list helper-managed tasks (read-only)

  workers: glm, claude.  task: lowercase slug such as gmail or scheduler-core.

.THREAT MODEL (adjudicated; do not reopen)
  IN SCOPE: stale/corrupt helper state, corruption of one helper artifact,
  wrong worker/task/path/branch selection, ordinary concurrent development,
  branch/ref movement, process interruption, partial Git operations, conflicts,
  dirty or locked worktrees, missing worktrees, unexpected canonical-main
  advancement, accidental invocation on manually-created worktrees.
  OUT OF SCOPE: deliberate same-user forgery of ALL helper-private state by an
  actor with unrestricted repository/.git write authority; malicious arbitrary
  ref/file writes by a fully compromised same-user process; admin/root.
  The random ownership nonce is NOT authentication. It is task-generation /
  consistency evidence between the two helper-created artifacts (task metadata
  and the linked-worktree private marker); it detects stale metadata,
  single-artifact corruption, cross-task mismatch, and normal manually-created
  exact-name worktrees that lack matching helper state.

.EXCLUSIVE HANDOFF (workflow precondition, not a security proof)
  finish / finish-current / cleanup require the environment variable
  JARVIS_DEV_EXCLUSIVE_HANDOFF to exactly equal the task-generation handoff
  token printed by `.\dev.ps1 status`:
      jarvis-dev-handoff-v1:<worker>:<task>:<ownershipNonce>
  Its meaning: "the external worker/controller asserts that task writers have
  been stopped and drained and that canonical mutation is serialized for this
  operation." Without it nothing canonical is mutated and nothing is removed.
  The ref transaction below is the second, final fence; CAS plus handoff do
  not eliminate every possible same-user concurrent history mutation (ABA) -
  they are the adjudicated fences for the accepted threat model.

.PRIMARY DATA-LOSS INVARIANT
  The helper never makes the last durable Git-ref-reachable copy of
  unintegrated committed task work disappear, and never force-removes
  uncommitted/dirty worktree content. Worktree removal and branch deletion are
  separate safety events; integration success and cleanup success are separate
  outcomes; CLEANED is printed only when the ENTIRE cleanup completed safely.

  Metadata lives under the shared git dir (dev-helper/tasks/<worker>/<task>.json)
  and is never a tracked repository file.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$Command = '',
    [Parameter(Position = 1)][string]$Worker = '',
    [Parameter(Position = 2)][string]$Task = ''
)

$Script:CanonicalBranch = 'main'
$Script:WorkerDisplay = @{ glm = 'GLM'; claude = 'Claude' }
$Script:MaxSlugLength = 40
$Script:OwnershipMarkerSchema = 'jarvis-dev-helper-ownership-1'
$Script:OwnershipMarkerName = 'dev-helper-ownership.json'
$Script:HandoffEnvVar = 'JARVIS_DEV_EXCLUSIVE_HANDOFF'
$Script:HandoffPrefix = 'jarvis-dev-handoff-v1'

# Decode native git output as UTF-8 so non-ASCII paths (e.g. "Masaüstü")
# survive; harmless no-op when no console is attached.
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) }
catch { }

# ---------- git plumbing ---------------------------------------------------

function Invoke-GitCapture {
    param([string]$RepoPath, [Parameter(Mandatory = $true)][string[]]$GitArgs)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $raw = & git -C $RepoPath @GitArgs 2>&1
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $prev
    }
    $lines = @(@($raw) | ForEach-Object { $_.ToString() })
    return [pscustomobject]@{ ExitCode = $code; Lines = $lines; Text = ($lines -join "`n") }
}

function Invoke-Git {
    param([string]$RepoPath, [Parameter(Mandatory = $true)][string[]]$GitArgs)
    $r = Invoke-GitCapture -RepoPath $RepoPath -GitArgs $GitArgs
    if ($r.ExitCode -ne 0) { throw "git $($GitArgs -join ' ') failed: $($r.Text)" }
    return $r
}

function ConvertFrom-GitQuotedPath {
    # git quotes paths containing non-ASCII (e.g. "Masaüstü") as C-style octal
    # escapes; decode them back to a real string via UTF-8 bytes.
    param([string]$Value)
    if ($Value.Length -lt 2 -or -not $Value.StartsWith('"') -or -not $Value.EndsWith('"')) { return $Value }
    $inner = $Value.Substring(1, $Value.Length - 2)
    $bytes = New-Object 'System.Collections.Generic.List[byte]'
    $i = 0
    while ($i -lt $inner.Length) {
        $c = $inner[$i]
        if ($c -eq '\') {
            $i++
            if ($i -ge $inner.Length) { break }
            $n = $inner[$i]
            if ($n -ge '0' -and $n -le '7') {
                $len = [Math]::Min(3, $inner.Length - $i)
                $oct = $inner.Substring($i, $len)
                $bytes.Add([Convert]::ToByte($oct, 8))
                $i += $len
                continue
            }
            switch ($n) {
                'n' { $bytes.Add(10) } 't' { $bytes.Add(9) } 'r' { $bytes.Add(13) }
                'a' { $bytes.Add(7) } 'b' { $bytes.Add(8) } 'f' { $bytes.Add(12) }
                'v' { $bytes.Add(11) } '"' { $bytes.Add(34) } '\' { $bytes.Add(92) }
                default { $bytes.Add([byte][char]$n) }
            }
            $i++
            continue
        }
        $code = [int][char]$c
        if ($code -lt 128) { $bytes.Add([byte]$code) }
        else { $bytes.AddRange([System.Text.Encoding]::UTF8.GetBytes($c)) }
        $i++
    }
    return [System.Text.Encoding]::UTF8.GetString($bytes.ToArray())
}

function ConvertTo-NormalPath {
    param([string]$Value, [string]$Base)
    if ([System.IO.Path]::IsPathRooted($Value)) { $p = $Value } else { $p = Join-Path $Base $Value }
    return ([System.IO.Path]::GetFullPath($p)).Replace('/', '\')
}

function Test-SamePath {
    param([string]$A, [string]$B)
    return [string]::Equals($A, $B, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-WorktreeList {
    param([string]$RepoPath)
    $r = Invoke-Git -RepoPath $RepoPath -GitArgs @('worktree', 'list', '--porcelain')
    $list = @()
    $current = $null
    foreach ($line in $r.Lines) {
        if ($line -like 'worktree *') {
            if ($current) { $list += $current }
            $path = ConvertTo-NormalPath -Value (ConvertFrom-GitQuotedPath $line.Substring(9)) -Base $RepoPath
            $current = [pscustomobject]@{ Path = $path; Branch = $null }
        }
        elseif ($line -like 'branch *' -and $current) {
            $current.Branch = ($line.Substring(7) -replace '^refs/heads/', '')
        }
    }
    if ($current) { $list += $current }
    return @($list)
}

# ---------- repository context ---------------------------------------------

function Get-CanonicalContext {
    $cwd = (Get-Location).ProviderPath
    $r = Invoke-GitCapture -RepoPath $cwd -GitArgs @('rev-parse', '--git-common-dir')
    if ($r.ExitCode -ne 0) { throw 'not inside a git repository - run from the JARVIS repo or one of its worktrees' }
    $commonDir = ConvertTo-NormalPath -Value $r.Lines[0] -Base $cwd

    $wl = Invoke-GitCapture -RepoPath $cwd -GitArgs @('worktree', 'list', '--porcelain')
    if ($wl.ExitCode -ne 0 -or @($wl.Lines).Count -eq 0) { throw 'unable to list git worktrees' }
    $mainPath = $null
    foreach ($line in $wl.Lines) {
        if ($line -like 'worktree *') {
            $mainPath = ConvertTo-NormalPath -Value (ConvertFrom-GitQuotedPath $line.Substring(9)) -Base $cwd
            break
        }
    }
    if (-not $mainPath -or -not (Test-Path -LiteralPath $mainPath)) { throw 'unable to identify the main worktree of this repository' }

    $expectedCommon = ConvertTo-NormalPath -Value (Join-Path $mainPath '.git') -Base $mainPath
    if (-not (Test-SamePath $expectedCommon $commonDir)) { throw 'cannot safely identify the canonical repository' }

    return [pscustomobject]@{
        MainPath  = $mainPath
        CommonDir = $commonDir
        TasksDir  = (Join-Path $commonDir 'dev-helper\tasks')
    }
}

function Get-CurrentBranch {
    param([string]$RepoPath)
    return (Invoke-Git -RepoPath $RepoPath -GitArgs @('rev-parse', '--abbrev-ref', 'HEAD')).Lines[0]
}

function Test-TreeClean {
    # Canonical-main cleanliness (ordinary porcelain; ignored content is a
    # normal, non-destructive condition for the canonical checkout).
    param([string]$RepoPath)
    $st = Invoke-GitCapture -RepoPath $RepoPath -GitArgs @('status', '--porcelain')
    if ($st.ExitCode -ne 0) { throw "cannot read git status in $RepoPath" }
    return (@($st.Lines | Where-Object { $_ -ne '' }).Count -eq 0)
}

function Assert-CanonicalReady {
    param($ctx)
    $branch = Get-CurrentBranch -RepoPath $ctx.MainPath
    if ($branch -cne $Script:CanonicalBranch) { throw "canonical repository is on '$branch' - must be '$($Script:CanonicalBranch)'" }
    if (-not (Test-TreeClean -RepoPath $ctx.MainPath)) { throw "canonical $Script:CanonicalBranch working tree is not clean (commit or stash first)" }
}

function Get-UnfinishedGitOperationState {
    # THE canonical unfinished-Git-operation detector, shared by finish,
    # finish-current, cleanup and the final destructive gate. It resolves the
    # worktree's PRIVATE Git-admin directory through Git itself (never via
    # '<worktree>/.git' string concatenation - a linked worktree's .git is a
    # file, not a directory) and refuses on any ACTIVE operation state that
    # worktree removal could destroy: merge, cherry-pick, revert, rebase
    # (both layouts), cherry-pick/revert sequencer state, and active bisect.
    # Ordinary historical bookkeeping such as ORIG_HEAD is NOT an unfinished
    # operation. Deterministic and fail-closed: inspection uncertainty
    # returns a refusal reason, never 'clean'.
    param([string]$WorktreePath)
    $admin = Get-WorktreeAdminDir -WorktreePath $WorktreePath
    if ($null -eq $admin -or -not (Test-Path -LiteralPath $admin)) {
        return 'unfinished-operation inspection failed: the worktree git-admin dir could not be resolved through git'
    }
    foreach ($state in @('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'BISECT_START', 'rebase-merge', 'rebase-apply', 'sequencer')) {
        if (Test-Path -LiteralPath (Join-Path $admin $state)) {
            return "active Git operation state: $state"
        }
    }
    return $null
}

function Get-TaskWorktreeCleanlinessReason {
    # Strict managed-worktree cleanliness as ONE reason string ($null = clean):
    # no active unfinished Git operation, index clean, tracked files clean,
    # untracked files absent, ignored content absent (ignored caches are never
    # silently deleted - the worker/controller removes them explicitly before
    # handoff). Fail-closed in every direction: uncertainty is a reason, not
    # cleanliness.
    param([string]$WorktreePath)
    $op = Get-UnfinishedGitOperationState -WorktreePath $WorktreePath
    if ($op) { return $op }
    $st = Invoke-GitCapture -RepoPath $WorktreePath -GitArgs @('status', '--porcelain', '--ignored')
    if ($st.ExitCode -ne 0) { return 'worktree status could not be read (fail closed)' }
    $dirty = @($st.Lines | Where-Object { $_ -ne '' })
    if ($dirty.Count -gt 0) { return "worktree content changed: $($dirty[0])" }
    return $null
}

# ---------- input validation ------------------------------------------------

function Assert-Worker {
    param([string]$Worker)
    # -cne / -cmatch on purpose: PowerShell's default operators are case-insensitive.
    if ($Worker -cne 'glm' -and $Worker -cne 'claude') { throw "unknown worker '$Worker' (supported: glm, claude)" }
}

function Assert-TaskSlug {
    param([string]$Task)
    if ([string]::IsNullOrEmpty($Task)) { throw 'task name is required' }
    if ($Task.Length -gt $Script:MaxSlugLength) { throw "task name too long (max $Script:MaxSlugLength chars)" }
    if ($Task -cnotmatch '^[a-z0-9]+(-[a-z0-9]+)*$') { throw "invalid task '$Task' - use lowercase letters/digits with single dashes (e.g. gmail, scheduler-core)" }
    if ($Task -cin @('new', 'finish', 'cleanup', 'status', 'help')) { throw "reserved task name '$Task'" }
}

function Test-Sha1Hex {
    param($Value)
    return ($Value -is [string] -and $Value -cmatch '^[0-9a-f]{40}$')
}

function Test-NonceHex {
    param($Value)
    return ($Value -is [string] -and $Value -cmatch '^[0-9a-f]{64}$')
}

# ---------- exclusive handoff (workflow precondition) ------------------------

function Get-HandoffToken {
    # Deterministic closed serialization of the CURRENT managed task
    # generation. Exact-match only; no truthy parsing, no generic value, no
    # fallback. Not authentication: anyone who can read the helper metadata
    # can construct it - by design. It is the external worker/controller's
    # assertion that task writers are stopped and canonical mutation is
    # serialized for this operation.
    param([string]$Worker, [string]$Task, [string]$Nonce)
    return "$($Script:HandoffPrefix):$Worker`:$Task`:$Nonce"
}

function Assert-ExclusiveHandoff {
    param($identity, $meta)
    $expected = Get-HandoffToken -Worker $identity.Worker -Task $identity.Task -Nonce $meta.ownershipNonce
    $provided = [System.Environment]::GetEnvironmentVariable($Script:HandoffEnvVar)
    if (-not ($provided -is [string]) -or $provided -cne $expected) {
        throw "HANDOFF REQUIRED: set $Script:HandoffEnvVar to the exact task-generation handoff token for $($identity.Worker) $($identity.Task) (printed by .\dev.ps1 status)"
    }
}

# ---------- derived task identity -------------------------------------------

function Get-DerivedTaskIdentity {
    # The ONLY source of branch/worktree/metadata paths: validated worker +
    # validated task + canonical repository path. Metadata content can never
    # redirect a command to a different branch, task or directory.
    param($ctx, [string]$Worker, [string]$Task)
    $branch = "$Worker/$Task"
    $wtPath = Join-Path (Split-Path -Parent $ctx.MainPath) ("JARVIS-{0}-{1}" -f $Script:WorkerDisplay[$Worker], $Task)
    $metaPath = Join-Path (Join-Path $ctx.TasksDir $Worker) "$Task.json"
    return [pscustomobject]@{
        Worker       = $Worker
        Task         = $Task
        Branch       = $branch
        WorktreePath = $wtPath
        MetaPath     = $metaPath
    }
}

# ---------- metadata (closed schema, assertion-only) -------------------------

$Script:MetaSchemaFields = @(
    'worker', 'task', 'branch', 'worktreePath', 'baseSha', 'state',
    'createdAt', 'integratedHead', 'mainHeadAtFinish', 'finishedAt',
    'sourceTipSha', 'sourceCommitShas', 'ownershipNonce'
)

function Read-TaskMeta {
    # Loads the metadata file at the DERIVED location and enforces the closed
    # schema plus exact equality with the derived identity. Stored values are
    # consistency evidence, never deletion authority.
    param($ctx, $identity)
    $Worker = $identity.Worker
    $Task = $identity.Task
    $Path = $identity.MetaPath
    if (-not (Test-Path -LiteralPath $Path)) { throw "no helper metadata for task '$Worker $Task' - was it created with '.\dev.ps1 new'? (an interrupted creation is never adopted)" }
    try { $meta = [System.IO.File]::ReadAllText($Path) | ConvertFrom-Json }
    catch { throw "metadata schema: unreadable JSON for '$Worker $Task'" }
    if ($null -eq $meta -or $null -eq $meta.PSObject) { throw "metadata schema: metadata for '$Worker $Task' is not an object" }

    $props = @($meta.PSObject.Properties.Name)
    foreach ($p in $props) {
        if ($Script:MetaSchemaFields -cnotcontains $p) { throw "metadata schema: unknown field '$p'" }
    }
    foreach ($p in $Script:MetaSchemaFields) {
        if ($props -cnotcontains $p) { throw "metadata schema: missing field '$p'" }
    }
    foreach ($p in @('worker', 'task', 'branch', 'worktreePath', 'baseSha', 'state', 'createdAt')) {
        if (-not ($meta.$p -is [string])) { throw "metadata schema: field '$p' must be a string" }
    }
    if (-not (Test-Sha1Hex $meta.baseSha)) { throw "metadata schema: baseSha is not a valid commit SHA" }
    if ($meta.state -cne 'ACTIVE' -and $meta.state -cne 'INTEGRATED') { throw "metadata schema: invalid state '$($meta.state)'" }
    foreach ($p in @('integratedHead', 'mainHeadAtFinish', 'sourceTipSha')) {
        if ($null -ne $meta.$p -and -not (Test-Sha1Hex $meta.$p)) { throw "metadata schema: field '$p' is not a valid commit SHA" }
    }
    if ($null -ne $meta.finishedAt -and -not ($meta.finishedAt -is [string])) { throw "metadata schema: field 'finishedAt' must be a string" }
    if (-not (Test-NonceHex $meta.ownershipNonce)) { throw "metadata schema: field 'ownershipNonce' is missing or malformed" }
    if ($null -ne $meta.sourceCommitShas) {
        # EXACT array, full SHAs, chronological order (order is verified
        # against independently recomputed git history before any destructive
        # step), no duplicates. A scalar string is a schema violation.
        if ($meta.sourceCommitShas -isnot [System.Array]) { throw "metadata schema: field 'sourceCommitShas' must be an array of commit SHAs" }
        $seen = @{}
        foreach ($s in @($meta.sourceCommitShas)) {
            if (-not (Test-Sha1Hex $s)) { throw "metadata schema: field 'sourceCommitShas' contains an invalid commit SHA" }
            if ($seen.ContainsKey($s)) { throw "metadata schema: field 'sourceCommitShas' contains a duplicate commit SHA" }
            $seen[$s] = $true
        }
    }

    # stored values must equal the independently derived identity
    if ($meta.worker -cne $Worker) { throw "metadata mismatch: worker '$($meta.worker)' does not match '$Worker'" }
    if ($meta.task -cne $Task) { throw "metadata mismatch: task '$($meta.task)' does not match '$Task'" }
    if ($meta.branch -cne $identity.Branch) { throw "metadata mismatch: branch '$($meta.branch)' does not match derived branch '$($identity.Branch)'" }
    if (-not (Test-SamePath (ConvertTo-NormalPath -Value $meta.worktreePath -Base $ctx.MainPath) $identity.WorktreePath)) {
        throw "metadata mismatch: worktreePath does not match derived worktree '$($identity.WorktreePath)'"
    }
    # state coherence: finish/integration audit fields belong to exactly one
    # state; impossible cross-state combinations are refused, never repaired.
    # integratedHead/mainHeadAtFinish/finishedAt are historical audit data and
    # are never destructive authority.
    if ($meta.state -ceq 'ACTIVE') {
        foreach ($p in @('integratedHead', 'mainHeadAtFinish', 'sourceTipSha')) {
            if ($null -ne $meta.$p) { throw "metadata schema: ACTIVE state must not record '$p'" }
        }
        if ($null -ne $meta.sourceCommitShas) { throw "metadata schema: ACTIVE state must not record 'sourceCommitShas'" }
        if ($null -ne $meta.finishedAt) { throw "metadata schema: ACTIVE state must not record 'finishedAt'" }
    }
    if ($meta.state -ceq 'INTEGRATED') {
        if ($null -eq $meta.integratedHead) { throw "metadata schema: INTEGRATED state requires 'integratedHead'" }
        if ($null -eq $meta.mainHeadAtFinish) { throw "metadata schema: INTEGRATED state requires 'mainHeadAtFinish'" }
        if (-not ($meta.finishedAt -is [string]) -or $meta.finishedAt -eq '') { throw "metadata schema: INTEGRATED state requires 'finishedAt'" }
        if ($null -eq $meta.sourceTipSha) { throw "metadata schema: INTEGRATED state requires 'sourceTipSha'" }
        if ($null -eq $meta.sourceCommitShas) { throw "metadata schema: INTEGRATED state requires 'sourceCommitShas'" }
        if (@($meta.sourceCommitShas).Count -lt 1) { throw "metadata schema: INTEGRATED state requires a non-empty 'sourceCommitShas'" }
    }
    return $meta
}

function Write-Meta {
    param($ctx, $meta, [string]$Path)
    $dir = Split-Path -Parent $Path
    $null = New-Item -ItemType Directory -Force -Path $dir
    $json = ConvertTo-Json -InputObject $meta
    [System.IO.File]::WriteAllText($Path, $json, (New-Object System.Text.UTF8Encoding($false)))
}

# ---------- ownership artifacts (generation evidence, NOT authentication) ----

function New-OwnershipNonce {
    $bytes = New-Object 'byte[]' 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return ([System.BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
}

function Get-WorktreeAdminDir {
    # The linked worktree's PRIVATE git administrative directory, resolved
    # through Git itself - never guessed from folder names.
    param([string]$WorktreePath)
    $r = Invoke-GitCapture -RepoPath $WorktreePath -GitArgs @('rev-parse', '--git-dir')
    if ($r.ExitCode -ne 0) { return $null }
    return (ConvertTo-NormalPath -Value $r.Lines[0] -Base $WorktreePath)
}

function Assert-OwnershipMarker {
    # Metadata alone MUST NOT establish that dev.ps1 new created a worktree.
    # Only a successful `dev.ps1 new` writes the marker, into the linked
    # worktree's private admin directory; both artifacts must agree on the
    # task generation (worker/task/paths/base/nonce). This detects stale
    # metadata, single-artifact corruption, cross-task mismatch and normal
    # manually-created exact-name worktrees. Deliberate same-user forgery of
    # BOTH artifacts is outside the accepted threat model. A missing marker is
    # never reconstructed here.
    param($ctx, $identity, $meta, [string]$WorktreePath)
    $gitDir = Get-WorktreeAdminDir -WorktreePath $WorktreePath
    if ($null -eq $gitDir) { throw 'ownership evidence refused: the worktree git dir could not be resolved through git' }
    if (-not $gitDir.StartsWith("$($ctx.CommonDir)\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'ownership evidence refused: the worktree is not a linked worktree of this repository'
    }
    $markerPath = Join-Path $gitDir $Script:OwnershipMarkerName
    if (-not (Test-Path -LiteralPath $markerPath)) {
        throw 'ownership evidence refused: no helper ownership marker in the linked worktree - it was not created by dev.ps1 new'
    }
    try { $marker = [System.IO.File]::ReadAllText($markerPath) | ConvertFrom-Json }
    catch { throw 'ownership evidence refused: marker is not readable JSON' }
    if ($null -eq $marker -or $null -eq $marker.PSObject) { throw 'ownership evidence refused: marker is not an object' }
    $expected = @('schema', 'worker', 'task', 'canonicalRepositoryPath', 'branch', 'worktreePath', 'baseSha', 'ownershipNonce')
    $props = @($marker.PSObject.Properties.Name)
    foreach ($p in $props) {
        if ($expected -cnotcontains $p) { throw "ownership evidence refused: unknown marker field '$p'" }
    }
    foreach ($p in $expected) {
        if ($props -cnotcontains $p) { throw "ownership evidence refused: missing marker field '$p'" }
    }
    foreach ($p in $expected) {
        if (-not ($marker.$p -is [string])) { throw "ownership evidence refused: marker field '$p' must be a string" }
    }
    if ($marker.schema -cne $Script:OwnershipMarkerSchema) { throw 'ownership evidence refused: unknown marker schema' }
    if ($marker.worker -cne $identity.Worker -or $marker.task -cne $identity.Task) { throw 'ownership evidence refused: worker/task mismatch' }
    if ($marker.branch -cne $identity.Branch) { throw 'ownership evidence refused: branch mismatch' }
    if (-not (Test-SamePath (ConvertTo-NormalPath -Value $marker.canonicalRepositoryPath -Base $ctx.MainPath) $ctx.MainPath)) {
        throw 'ownership evidence refused: canonical repository mismatch'
    }
    if (-not (Test-SamePath (ConvertTo-NormalPath -Value $marker.worktreePath -Base $ctx.MainPath) $identity.WorktreePath)) {
        throw 'ownership evidence refused: worktree path mismatch'
    }
    if (-not (Test-Sha1Hex $marker.baseSha) -or $marker.baseSha -cne $meta.baseSha) {
        throw 'ownership evidence refused: base SHA mismatch'
    }
    if (-not (Test-NonceHex $marker.ownershipNonce) -or $marker.ownershipNonce -cne $meta.ownershipNonce) {
        throw 'ownership evidence refused: ownership nonce mismatch'
    }
}

# ---------- linear-only task history ----------------------------------------

function Assert-LinearTaskHistory {
    # Helper task branches are LINEAR-ONLY. Every task-owned commit after the
    # base must have exactly one parent; any merge commit refuses integration
    # AND cleanup deterministically. Operates on captured immutable OIDs only.
    param([string]$RepoPath, [string]$BaseSha, [string]$Branch, [string]$TipSha)
    $lines = @((Invoke-Git -RepoPath $RepoPath -GitArgs @('rev-list', '--parents', "$($BaseSha)..$($TipSha)")).Lines | Where-Object { $_ -ne '' })
    foreach ($line in $lines) {
        $fields = @($line -split '\s+')
        if ($fields.Count -ne 2) {
            throw "UNSUPPORTED TASK HISTORY: MERGE COMMIT ($($fields[0]) on $Branch)"
        }
    }
}

# ---------- deletion stage (git-owned only) ----------------------------------

function Remove-WorktreeDir {
    # Narrow deterministic seam: forcing this to $false only ever makes the
    # helper MORE conservative (defers cleanup); it can never force a deletion.
    # The ONLY mechanism is `git worktree remove` WITHOUT force - never manual
    # recursion, never -f/--force.
    param($ctx, [string]$WorktreePath)
    if ($env:DEV_HELPER_TEST_FORCE_CLEANUP_DEFERRED -eq '1') { return $false }
    $r = Invoke-GitCapture -RepoPath $ctx.MainPath -GitArgs @('worktree', 'remove', $WorktreePath)
    return ($r.ExitCode -eq 0)
}

function Invoke-AtomicCleanupRefTransaction {
    # All-or-nothing ref transaction via `git update-ref --no-deref --stdin`
    # (verified against the installed Git's documented start/verify/delete/
    # commit format): verify the full canonical main ref still equals the
    # exact Mclean snapshot the integration/cleanup proof was accepted
    # against, AND delete the full direct task ref ONLY IF it still equals the
    # exact captured T. No symbolic-ref redirection; the task ref must be a
    # direct local branch ref (checked here). Failure changes nothing.
    #
    # CAS plus the exclusive handoff are the two fences for ordinary
    # concurrent writers; CAS alone does not prove absence of an ABA
    # T->T2->T history, and this is documented rather than claimed away.
    param([string]$RepoPath, [string]$TaskRef, [string]$ExpectedT, [string]$ExpectedM)
    $mainRef = "refs/heads/$Script:CanonicalBranch"
    $sym = Invoke-GitCapture -RepoPath $RepoPath -GitArgs @('symbolic-ref', '-q', $TaskRef)
    if ($sym.ExitCode -eq 0) {
        return [pscustomobject]@{ Ok = $false; Reason = 'REF CHANGED (task ref is symbolic, not a direct branch ref)' }
    }
    $lines = @(
        'start',
        "verify $mainRef $ExpectedM",
        "delete $TaskRef $ExpectedT",
        'commit'
    )
    # Drive git via .NET with exact LF-terminated stdin: the PowerShell
    # pipeline would append CRLF, which git's --stdin parser rejects.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'git'
    $psi.Arguments = 'update-ref --no-deref --stdin'
    $psi.WorkingDirectory = $RepoPath
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $process = [System.Diagnostics.Process]::Start($psi)
    $process.StandardInput.Write((($lines -join "`n") + "`n"))
    $process.StandardInput.Close()
    $stdoutText = $process.StandardOutput.ReadToEnd()
    $stderrText = $process.StandardError.ReadToEnd()
    $null = $process.WaitForExit()
    if ($process.ExitCode -eq 0) { return [pscustomobject]@{ Ok = $true; Reason = '' } }
    # Diagnostics only - never substituted for the captured OIDs.
    $reasons = @()
    $mainNow = (Invoke-GitCapture -RepoPath $RepoPath -GitArgs @('rev-parse', '--verify', '--quiet', $mainRef))
    if ($mainNow.ExitCode -ne 0 -or $mainNow.Lines[0] -cne $ExpectedM) { $reasons += 'MAIN CHANGED' }
    $taskNow = (Invoke-GitCapture -RepoPath $RepoPath -GitArgs @('rev-parse', '--verify', '--quiet', $TaskRef))
    if ($taskNow.ExitCode -ne 0 -or $taskNow.Lines[0] -cne $ExpectedT) { $reasons += 'REF CHANGED' }
    if ($reasons.Count -eq 0) { $reasons += 'TRANSACTION FAILED' }
    return [pscustomobject]@{ Ok = $false; Reason = ($reasons -join ' + '); Detail = ($stderrText + ' ' + $stdoutText).Trim() }
}

function Invoke-PreIntegrationTestHook {
    # Deterministic test seam ONLY: production behavior is an empty no-op, so
    # it can never weaken a production path. Fires in finish after the
    # immutable T/M capture and before the first cherry-pick.
    param($ctx, $identity, $meta, [string]$CapturedT, [string]$CapturedM)
}

function Invoke-CherryPickAbort {
    # Production: abort THIS invocation's own cherry-pick sequencer state.
    # Wrapping the call gives the regression suite a deterministic seam to
    # simulate abort failure without weakening production behavior.
    param([string]$RepoPath)
    return (Invoke-GitCapture -RepoPath $RepoPath -GitArgs @('cherry-pick', '--abort'))
}

function Invoke-ConflictRecoveryTestHook {
    # Deterministic test seam ONLY: production no-op. Fires at the start of
    # conflict recovery, before the pre-abort canonical re-read.
    param($ctx, $identity, [string]$CapturedM)
}

function Invoke-PostAbortTestHook {
    # Deterministic test seam ONLY: production no-op. Fires after a successful
    # cherry-pick abort and before the post-abort state proof.
    param($ctx, $identity, [string]$CapturedM)
}

function Invoke-PreDeleteTestHook {
    # Deterministic test seam ONLY: production no-op. Fires in cleanup after
    # the earlier integration proof and before the final destructive gate.
    param($ctx, $identity, $meta)
}

function Invoke-PostRemovalTestHook {
    # Deterministic test seam ONLY: production no-op. Fires in cleanup after
    # successful git-owned worktree removal and before the atomic ref
    # transaction (post-proof CAS regressions).
    param($ctx, $identity, [string]$CapturedT, [string]$CapturedM)
}

function Assert-FinalDestructivePreconditions {
    # THE one authoritative final destructive precondition gate. Runs
    # immediately before `git worktree remove` and independently re-proves:
    # canonical repository identity, expected main branch and state, derived
    # worker/task identity, valid exclusive handoff, ownership marker,
    # exact registered worktree path and branch attachment, shared repository,
    # strict worktree cleanliness (tracked/untracked/ignored/unfinished ops),
    # worktree HEAD == captured T, task branch ref == captured T, base and
    # topology validity, linear-only history, chronological commit sequence
    # equal to the audit record, and the complete captured task patch set
    # integrated in the canonical main snapshot (Mclean) captured here. Both
    # finish's automatic cleanup and explicit cleanup reach deletion ONLY
    # through this gate. Source branch moved from T or worktree content
    # changes are DEFERRALS that preserve everything; ownership/identity/
    # history/patch failures are hard refusals. Nothing is deleted unless
    # this gate returns PROCEED with the captured Mclean.
    param($ctx, $identity)

    # (1)(2)(3) canonical repository identity, main branch, canonical state.
    if (-not (Test-Path -LiteralPath $ctx.MainPath)) { throw 'final gate refused: canonical repository is missing' }
    $expectedCommon = ConvertTo-NormalPath -Value (Join-Path $ctx.MainPath '.git') -Base $ctx.MainPath
    if (-not (Test-SamePath $expectedCommon $ctx.CommonDir)) { throw 'final gate refused: canonical repository identity changed' }
    Assert-CanonicalReady -ctx $ctx

    # (4) independently derived worker/task identity; the caller's object is
    # never authority.
    $derived = Get-DerivedTaskIdentity -ctx $ctx -Worker $identity.Worker -Task $identity.Task
    if (-not (Test-SamePath $derived.WorktreePath $identity.WorktreePath) -or $derived.Branch -cne $identity.Branch) {
        throw 'final gate refused: derived task identity mismatch'
    }
    if (Test-SamePath $derived.WorktreePath $ctx.MainPath) { throw 'final gate refused: derived worktree path is the canonical repository' }

    # Metadata is re-read from the derived location and fully re-validated
    # (schema, coherence, identity equality); the caller's copy is never
    # authority. This also catches metadata tampering after the earlier proof.
    $metaNow = Read-TaskMeta -ctx $ctx -identity $derived
    if ($metaNow.state -cne 'INTEGRATED') { throw 'final gate refused: metadata state is not INTEGRATED' }

    # exclusive handoff must still hold at the destructive step.
    Assert-ExclusiveHandoff -identity $derived -meta $metaNow

    # The captured immutable source tip T is the ONLY source tip this cleanup
    # may act on; a newly observed OID never silently replaces it.
    $capturedT = $metaNow.sourceTipSha
    $mainHead = (Invoke-Git -RepoPath $ctx.MainPath -GitArgs @('rev-parse', 'HEAD')).Lines[0]

    # (7) exact expected task branch exists.
    $br = Invoke-GitCapture -RepoPath $ctx.MainPath -GitArgs @('rev-parse', '--verify', '--quiet', "refs/heads/$($derived.Branch)")
    if ($br.ExitCode -ne 0) { throw "final gate refused: task branch does not exist: $($derived.Branch)" }
    $branchTip = (Invoke-Git -RepoPath $ctx.MainPath -GitArgs @('rev-parse', "refs/heads/$($derived.Branch)")).Lines[0]

    # (11) branch ref unchanged from the integration proof (captured T).
    if ($branchTip -cne $capturedT) {
        throw "CLEANUP DEFERRED: task branch ref changed since integration - captured T is preserved, no deletion (REF CHANGED: $($metaNow.sourceTipSha) -> $branchTip)"
    }

    # (13) recorded base/topology still valid.
    $bv = Invoke-GitCapture -RepoPath $ctx.MainPath -GitArgs @('rev-parse', '--verify', '--quiet', "$($metaNow.baseSha)^{commit}")
    if ($bv.ExitCode -ne 0) { throw "final gate refused: recorded base $($metaNow.baseSha) is not a valid commit" }
    $ancMain = Invoke-GitCapture -RepoPath $ctx.MainPath -GitArgs @('merge-base', '--is-ancestor', $metaNow.baseSha, $mainHead)
    if ($ancMain.ExitCode -ne 0) { throw 'final gate refused: task base is not part of canonical main history' }
    $ancTip = Invoke-GitCapture -RepoPath $ctx.MainPath -GitArgs @('merge-base', '--is-ancestor', $metaNow.baseSha, $capturedT)
    if ($ancTip.ExitCode -ne 0) { throw 'final gate refused: task branch no longer descends from its recorded base' }

    # (12) linear-only task history after base (captured T).
    Assert-LinearTaskHistory -RepoPath $ctx.MainPath -BaseSha $metaNow.baseSha -Branch $derived.Branch -TipSha $capturedT

    # (5) genuine helper ownership marker, resolved through Git.
    Assert-OwnershipMarker -ctx $ctx -identity $derived -meta $metaNow -WorktreePath $derived.WorktreePath
    # (6) exact expected registered worktree path.
    $registered = @(Get-WorktreeList -RepoPath $ctx.MainPath | Where-Object { Test-SamePath $_.Path $derived.WorktreePath })[0]
    if (-not $registered) {
        throw "final gate refused: '$($derived.WorktreePath)' exists but is not registered as a worktree of this repository (a stale leftover folder must be removed manually - the helper never deletes unregistered directories)"
    }
    if ($registered.Branch -cne $derived.Branch) { throw "final gate refused: worktree is on branch '$($registered.Branch)', expected '$($derived.Branch)'" }
    # (8) same shared Git repository.
    $wtdir = Invoke-GitCapture -RepoPath $derived.WorktreePath -GitArgs @('rev-parse', '--git-common-dir')
    if ($wtdir.ExitCode -ne 0 -or -not (Test-SamePath (ConvertTo-NormalPath -Value $wtdir.Lines[0] -Base $derived.WorktreePath) $ctx.CommonDir)) {
        throw 'final gate refused: task worktree does not belong to this repository'
    }
    # (9) strict worktree cleanliness (tracked/untracked/ignored/unfinished
    # Git operation state, including sequencer state) via the ONE canonical
    # detector, re-proved here immediately before removal.
    $cleanReason = Get-TaskWorktreeCleanlinessReason -WorktreePath $derived.WorktreePath
    if ($cleanReason) {
        throw "CLEANUP DEFERRED: $cleanReason; nothing will be deleted"
    }
    # (10) worktree HEAD == captured T.
    $wtHead = (Invoke-Git -RepoPath $derived.WorktreePath -GitArgs @('rev-parse', 'HEAD')).Lines[0]
    if ($wtHead -cne $capturedT) {
        throw "CLEANUP DEFERRED: task worktree HEAD no longer equals the captured source tip T; nothing will be deleted"
    }

    # (15) audit equality: the recomputed chronological commit sequence after
    # base must equal the recorded sequence exactly (ref == T makes the
    # sequence fully determined, so a mismatch is metadata corruption).
    $sequence = @((Invoke-Git -RepoPath $ctx.MainPath -GitArgs @('rev-list', '--reverse', "$($metaNow.baseSha)..$capturedT")).Lines | Where-Object { $_ -ne '' })
    $recorded = @($metaNow.sourceCommitShas)
    if ($sequence.Count -ne $recorded.Count) {
        throw 'final gate refused: task commit sequence does not match the recorded audit sequence'
    }
    for ($i = 0; $i -lt $sequence.Count; $i++) {
        if ($sequence[$i] -cne $recorded[$i]) {
            throw 'final gate refused: task commit sequence does not match the recorded audit sequence'
        }
    }

    # (14) complete captured task patch set integrated in the canonical main
    # snapshot (Mclean) captured here. `git cherry` marks '-' = patch-
    # equivalent integrated, '+' = NOT integrated; commits already ancestral
    # to Mclean are excluded (identical-SHA collapse is independently safe for
    # a linear branch whose sequence was just proven equal to the audit
    # record). Empty output means fully integrated.
    $cherry = Invoke-Git -RepoPath $ctx.MainPath -GitArgs @('cherry', $mainHead, $capturedT)
    $unintegrated = @(@($cherry.Lines) | Where-Object { $_ -ne '' } | Where-Object { $_ -cnotmatch '^- ' })
    if ($unintegrated.Count -gt 0) {
        throw "final gate refused: task commit(s) not proven integrated in canonical main: $($unintegrated[0])"
    }

    return [pscustomobject]@{
        Action    = 'PROCEED'
        Mclean    = $mainHead
        ExpectedT = $capturedT
        MetaNow   = $metaNow
    }
}

function Invoke-CleanupCore {
    # Deletion stage; both finish's automatic cleanup and explicit cleanup
    # reach it. Throws 'CLEANUP DEFERRED: ...' / 'RECOVERY REQUIRED: ...' /
    # plain refusals; returns only on full success. There is intentionally NO
    # manual recursive-delete path, no force removal, and no unconditional
    # branch deletion anywhere.
    param($ctx, $identity, [string]$MetaPath)
    # Move our own process working directory out of the worktree first, or a
    # finish-current invoked from inside it would hold the folder open and
    # always defer its own cleanup.
    try {
        [System.Environment]::CurrentDirectory = $ctx.MainPath
        Set-Location -LiteralPath $ctx.MainPath
    }
    catch { }
    $wt = $identity.WorktreePath
    if (Test-SamePath $wt $ctx.MainPath) { throw 'cleanup refused: derived worktree path is the canonical repository' }
    # F2 seam: fires after every earlier proof and before the final gate.
    Invoke-PreDeleteTestHook -ctx $ctx -identity $identity -meta $null
    # ABSOLUTE missing-worktree policy: a cleanup that STARTS without the
    # managed worktree never deletes the task branch or metadata, no matter
    # what metadata or history says. This also covers an interruption between
    # a previous worktree removal and its branch deletion: only the SAME
    # uninterrupted invocation that removed the worktree may continue.
    if (-not (Test-Path -LiteralPath $wt)) {
        throw 'RECOVERY REQUIRED: WORKTREE MISSING - the managed worktree is absent; the helper will not delete the task branch or metadata reconstructed from metadata/history alone'
    }
    # THE final destructive precondition gate - nothing is deleted unless it
    # returns PROCEED with the captured Mclean snapshot.
    $proof = Assert-FinalDestructivePreconditions -ctx $ctx -identity $identity
    # git-owned removal, WITHOUT force; the exclusive handoff is held.
    if (-not (Remove-WorktreeDir -ctx $ctx -WorktreePath $wt)) {
        throw 'CLEANUP DEFERRED: git worktree removal failed (locked or partial); branch and metadata preserved'
    }
    # Seam for post-removal CAS regressions (production no-op).
    Invoke-PostRemovalTestHook -ctx $ctx -identity $identity -CapturedT $proof.ExpectedT -CapturedM $proof.Mclean
    # Atomic all-or-nothing ref transaction: verify main == Mclean AND delete
    # the task ref iff it still equals the captured T. Branch deletion happens
    # ONLY here, only after successful git-owned worktree removal, only in
    # this same uninterrupted invocation, and only with expected-OID CAS.
    $txn = Invoke-AtomicCleanupRefTransaction -RepoPath $ctx.MainPath `
        -TaskRef "refs/heads/$($identity.Branch)" `
        -ExpectedT $proof.ExpectedT -ExpectedM $proof.Mclean
    if (-not $txn.Ok) {
        throw "CLEANUP DEFERRED: atomic ref transaction failed ($($txn.Reason)) - the task branch remains ref-reachable and its metadata is preserved (worktree was removed)"
    }
    # Only after the confirmed conditional ref deletion: remove the single
    # derived metadata file (non-recursive, derived path).
    Remove-Item -LiteralPath $MetaPath -Force
}

# ---------- commands --------------------------------------------------------

function Invoke-NewTask {
    param([string]$Worker, [string]$Task)
    Assert-Worker -Worker $Worker
    Assert-TaskSlug -Task $Task
    $ctx = Get-CanonicalContext
    Assert-CanonicalReady -ctx $ctx
    $identity = Get-DerivedTaskIdentity -ctx $ctx -Worker $Worker -Task $Task
    if (Test-SamePath $identity.WorktreePath $ctx.MainPath) { throw 'new refused: derived worktree path is the canonical repository' }

    $baseSha = (Invoke-Git -RepoPath $ctx.MainPath -GitArgs @('rev-parse', 'HEAD')).Lines[0]

    if (Test-Path -LiteralPath $identity.MetaPath) { throw "task already exists: $Worker $Task" }
    $br = Invoke-GitCapture -RepoPath $ctx.MainPath -GitArgs @('rev-parse', '--verify', '--quiet', "refs/heads/$($identity.Branch)")
    if ($br.ExitCode -eq 0) { throw "branch already exists: $($identity.Branch)" }
    if (Test-Path -LiteralPath $identity.WorktreePath) { throw "worktree path already exists: $($identity.WorktreePath)" }

    # Creation is serialized by Git's own repository locks (worktree add takes
    # them); they are cooperative serialization, never filesystem ownership.
    $null = Invoke-Git -RepoPath $ctx.MainPath -GitArgs @('worktree', 'add', '-b', $identity.Branch, $identity.WorktreePath, $baseSha)

    # The ownership marker lives in the linked worktree's PRIVATE git
    # administrative directory (resolved through Git) and carries a strong
    # random nonce - task-generation evidence, not authentication. Metadata is
    # written only after the marker, never overwriting existing state, so an
    # interrupted creation leaves state that later commands refuse to adopt.
    $nonce = New-OwnershipNonce
    $wtGitDir = Get-WorktreeAdminDir -WorktreePath $identity.WorktreePath
    if ($null -eq $wtGitDir) { throw 'new failed: could not resolve the new worktree git dir through git' }
    $markerPath = Join-Path $wtGitDir $Script:OwnershipMarkerName
    if (Test-Path -LiteralPath $markerPath) { throw 'new refused: ownership marker already exists (possible interrupted creation)' }
    $marker = [pscustomobject]@{
        schema                  = $Script:OwnershipMarkerSchema
        worker                  = $Worker
        task                    = $Task
        canonicalRepositoryPath = $ctx.MainPath
        branch                  = $identity.Branch
        worktreePath            = $identity.WorktreePath
        baseSha                 = $baseSha
        ownershipNonce          = $nonce
    }
    [System.IO.File]::WriteAllText($markerPath, (ConvertTo-Json -InputObject $marker), (New-Object System.Text.UTF8Encoding($false)))

    $meta = [pscustomobject]@{
        worker           = $Worker
        task             = $Task
        branch           = $identity.Branch
        worktreePath     = $identity.WorktreePath
        baseSha          = $baseSha
        state            = 'ACTIVE'
        createdAt        = (Get-Date).ToString('o')
        integratedHead   = $null
        mainHeadAtFinish = $null
        finishedAt       = $null
        sourceTipSha     = $null
        sourceCommitShas = $null
        ownershipNonce   = $nonce
    }
    Write-Meta -ctx $ctx -meta $meta -Path $identity.MetaPath

    Write-Host 'WORKTREE READY'
    Write-Host "  path  : $($identity.WorktreePath)"
    Write-Host "  branch: $($identity.Branch)"
    Write-Host "  base  : $baseSha"
}

function Invoke-FinishTask {
    param([string]$Worker, [string]$Task)
    Assert-Worker -Worker $Worker
    Assert-TaskSlug -Task $Task
    $ctx = Get-CanonicalContext
    $invocationCwd = ConvertTo-NormalPath -Value (Get-Location).ProviderPath -Base (Get-Location).ProviderPath
    $identity = Get-DerivedTaskIdentity -ctx $ctx -Worker $Worker -Task $Task
    if (Test-SamePath $identity.WorktreePath $ctx.MainPath) { throw 'finish refused: derived worktree path is the canonical repository' }
    $meta = Read-TaskMeta -ctx $ctx -identity $identity
    if ($meta.state -cne 'ACTIVE') { throw "finish refused: task state is '$($meta.state)', not ACTIVE" }

    # EXCLUSIVE HANDOFF: without it, canonical main is never mutated and
    # nothing is ever removed. This is the external worker/controller's
    # assertion that task writers are stopped and canonical mutation is
    # serialized - a workflow precondition, not a security proof.
    Assert-ExclusiveHandoff -identity $identity -meta $meta

    Assert-CanonicalReady -ctx $ctx
    # Capture the immutable canonical snapshot M this integration is validated
    # against and rolled back to.
    $capturedM = (Invoke-Git -RepoPath $ctx.MainPath -GitArgs @('rev-parse', 'HEAD')).Lines[0]

    # base validity + topology proof (fail closed on rewritten/rebased history)
    $bv = Invoke-GitCapture -RepoPath $ctx.MainPath -GitArgs @('rev-parse', '--verify', '--quiet', "$($meta.baseSha)^{commit}")
    if ($bv.ExitCode -ne 0) { throw "finish refused: recorded base $($meta.baseSha) is not a valid commit" }
    $anc = Invoke-GitCapture -RepoPath $ctx.MainPath -GitArgs @('merge-base', '--is-ancestor', $meta.baseSha, $capturedM)
    if ($anc.ExitCode -ne 0) { throw 'finish refused: recorded base is not part of canonical main history' }
    $br = Invoke-GitCapture -RepoPath $ctx.MainPath -GitArgs @('rev-parse', '--verify', '--quiet', "refs/heads/$($identity.Branch)")
    if ($br.ExitCode -ne 0) { throw "finish refused: task branch does not exist: $($identity.Branch)" }
    # Capture the immutable task source tip T ONCE; integration never
    # re-resolves the moving branch.
    $capturedT = (Invoke-Git -RepoPath $ctx.MainPath -GitArgs @('rev-parse', "refs/heads/$($identity.Branch)")).Lines[0]
    $mb = Invoke-GitCapture -RepoPath $ctx.MainPath -GitArgs @('merge-base', $capturedT, $capturedM)
    if ($mb.ExitCode -ne 0 -or @($mb.Lines)[0] -cne $meta.baseSha) {
        throw 'finish refused: task branch does not diverge from its recorded base (history rewritten or rebased) - refusing to guess task commits'
    }

    # worktree proofs against the DERIVED path/branch
    $wt = $identity.WorktreePath
    if (-not (Test-Path -LiteralPath $wt)) { throw "finish refused: task worktree is missing: $wt" }
    # Strict cleanliness FIRST: an unfinished Git operation (merge,
    # cherry-pick, revert, rebase, sequencer, bisect) is the most precise
    # diagnostic and must be reported before attachment/ownership details.
    $cleanReason = Get-TaskWorktreeCleanlinessReason -WorktreePath $wt
    if ($cleanReason) { throw "finish refused: $cleanReason (commit your work; clear untracked/ignored content and unfinished Git operations first)" }
    $registered = @(Get-WorktreeList -RepoPath $ctx.MainPath | Where-Object { Test-SamePath $_.Path $wt })[0]
    if (-not $registered) { throw "finish refused: '$wt' is not registered as a worktree of this repository" }
    if ($registered.Branch -cne $identity.Branch) { throw "finish refused: worktree is on branch '$($registered.Branch)', expected '$($identity.Branch)'" }
    $wtdir = Invoke-GitCapture -RepoPath $wt -GitArgs @('rev-parse', '--git-common-dir')
    if ($wtdir.ExitCode -ne 0 -or -not (Test-SamePath (ConvertTo-NormalPath -Value $wtdir.Lines[0] -Base $wt) $ctx.CommonDir)) {
        throw 'finish refused: task worktree does not belong to this repository'
    }
    # Ownership evidence: marker + metadata must agree on the task generation.
    Assert-OwnershipMarker -ctx $ctx -identity $identity -meta $meta -WorktreePath $wt

    $wtHead = (Invoke-Git -RepoPath $wt -GitArgs @('rev-parse', 'HEAD')).Lines[0]
    if ($capturedT -cne $wtHead) { throw 'finish refused: task branch tip does not match worktree HEAD' }
    if ($capturedT -ceq $meta.baseSha) { throw 'finish refused: task branch has no commits after its recorded base (nothing to integrate)' }

    # linear-only task history + the chronological captured commit sequence
    Assert-LinearTaskHistory -RepoPath $ctx.MainPath -BaseSha $meta.baseSha -Branch $identity.Branch -TipSha $capturedT
    $commits = @((Invoke-Git -RepoPath $ctx.MainPath -GitArgs @('rev-list', '--reverse', "$($meta.baseSha)..$capturedT")).Lines | Where-Object { $_ -ne '' })
    if ($commits.Count -lt 1) { throw 'finish refused: no task commits found to integrate' }

    # Seam: fires after the immutable capture, before the first pick (test
    # only; production no-op).
    Invoke-PreIntegrationTestHook -ctx $ctx -identity $identity -meta $meta -CapturedT $capturedT -CapturedM $capturedM

    # integrate ONLY the captured immutable commit sequence, in order, onto
    # the captured canonical snapshot M (main may have advanced earlier; it is
    # serialized by the exclusive handoff from here on).
    $pickError = ''
    $failed = $false
    foreach ($c in $commits) {
        $r = Invoke-GitCapture -RepoPath $ctx.MainPath -GitArgs @('cherry-pick', $c)
        if ($r.ExitCode -ne 0) {
            $failed = $true
            $pickError = (@($r.Lines) | Select-Object -First 5) -join ' | '
            break
        }
    }
    if ($failed) {
        # CONFLICT RECOVERY - observe and preserve, never rewrite canonical
        # state. The exclusive handoff is workflow evidence, NOT proof that
        # concurrent main movement is impossible; unexpected canonical ref
        # movement is always observed and preserved, never overwritten to
        # restore the captured M. There is deliberately NO reset/checkout/ref
        # fallback in this path.
        # Seam (production no-op): fires at recovery start for regression probes.
        Invoke-ConflictRecoveryTestHook -ctx $ctx -identity $identity -CapturedM $capturedM
        # 2. Re-read the authoritative canonical main ref BEFORE recovery. If
        # it has already moved away from the captured M, this invocation cannot
        # safely attribute or abort - preserve the observed state and stop.
        $mainNow = Invoke-GitCapture -RepoPath $ctx.MainPath -GitArgs @('rev-parse', '--verify', '--quiet', "refs/heads/$Script:CanonicalBranch")
        if ($mainNow.ExitCode -ne 0 -or $mainNow.Lines[0] -cne $capturedM) {
            throw "RECOVERY REQUIRED: canonical main changed during a failed integration (observed: $($mainNow.Lines[0]), captured M: $capturedM); the observed state is preserved - no abort, no reset, no repair. Task branch, worktree and metadata preserved."
        }
        # 4. Abort ONLY this invocation's own sequencer state.
        $abort = Invoke-CherryPickAbort -RepoPath $ctx.MainPath
        if ($abort.ExitCode -ne 0) {
            throw "RECOVERY REQUIRED: cherry-pick abort failed; the observed canonical state is preserved - no reset, no forced ref movement. Task branch, worktree and metadata preserved."
        }
        # Seam (production no-op): fires after the abort, before the proof.
        Invoke-PostAbortTestHook -ctx $ctx -identity $identity -CapturedM $capturedM
        # 5/6. Successful recovery may be reported ONLY when the canonical
        # state is proven to be exactly the captured pre-integration state M
        # and the helper's own sequencer state is gone.
        # 7. Anything else is RECOVERY REQUIRED - never repaired destructively.
        $headNow = (Invoke-GitCapture -RepoPath $ctx.MainPath -GitArgs @('rev-parse', 'HEAD')).Lines[0]
        $opState = Get-UnfinishedGitOperationState -WorktreePath $ctx.MainPath
        if (($headNow -cne $capturedM) -or $opState -or -not (Test-TreeClean -RepoPath $ctx.MainPath)) {
            throw "RECOVERY REQUIRED: post-abort canonical state is not the exact pre-integration state M ($capturedM); the observed state is preserved - no destructive repair. Task branch, worktree and metadata preserved."
        }
        throw "FINISH FAILED: cherry-pick conflict - canonical main proven restored to its exact pre-finish state; task worktree and branch kept ($pickError)"
    }

    $newHead = (Invoke-Git -RepoPath $ctx.MainPath -GitArgs @('rev-parse', 'HEAD')).Lines[0]
    if (-not (Test-TreeClean -RepoPath $ctx.MainPath)) { throw 'FINISH FAILED: canonical main is not clean after integration' }

    # coherent INTEGRATED audit state: sourceTipSha/sourceCommitShas are
    # consistency evidence for the captured source; integratedHead/
    # mainHeadAtFinish/finishedAt are historical audit data, never authority.
    $meta.state = 'INTEGRATED'
    $meta.integratedHead = $newHead
    $meta.mainHeadAtFinish = $capturedM
    $meta.finishedAt = (Get-Date).ToString('o')
    $meta.sourceTipSha = $capturedT
    $meta.sourceCommitShas = @($commits)
    Write-Meta -ctx $ctx -meta $meta -Path $identity.MetaPath

    Write-Host 'INTEGRATED'
    Write-Host "  main  : $newHead"

    # Integration success and cleanup success are separate outcomes.
    $cleanupOutcome = ''
    try {
        Invoke-CleanupCore -ctx $ctx -identity $identity -MetaPath $identity.MetaPath
        $cleanupOutcome = 'CLEANED'
    }
    catch {
        $cleanupOutcome = $_.Exception.Message
    }
    if ($cleanupOutcome -eq 'CLEANED') {
        Write-Host 'CLEANED'
        if ($invocationCwd -and ($invocationCwd -eq $wt -or $invocationCwd.StartsWith("$wt\", [System.StringComparison]::OrdinalIgnoreCase))) {
            Write-Host '  note  : your previous working directory was inside the removed worktree - move your session to the canonical repo or a new task worktree'
        }
    }
    elseif ($cleanupOutcome -like 'CLEANUP DEFERRED*' -or $cleanupOutcome -like 'RECOVERY REQUIRED*') {
        Write-Host $cleanupOutcome
        Write-Host "  later : .\dev.ps1 cleanup $Worker $Task"
    }
    else {
        Write-Host "CLEANUP DEFERRED: $cleanupOutcome"
        Write-Host "  later : .\dev.ps1 cleanup $Worker $Task"
    }
}

function Find-TaskMetaByDerivedWorktree {
    # Locate the ONE derived/validated helper task identity whose derived
    # worktree path matches the given path. A metadata path-match alone is not
    # sufficient: the match is computed from the derived identity, the
    # metadata at the derived location must fully validate, and ownership is
    # then proven against the worktree marker inside finish itself.
    param($ctx, [string]$WorktreePath)
    if (-not (Test-Path -LiteralPath $ctx.TasksDir)) { return $null }
    foreach ($w in @(Get-ChildItem -LiteralPath $ctx.TasksDir -Directory -ErrorAction SilentlyContinue)) {
        $workerName = $w.Name
        try { Assert-Worker -Worker $workerName } catch { continue }
        foreach ($f in @(Get-ChildItem -LiteralPath $w.FullName -Filter '*.json' -ErrorAction SilentlyContinue)) {
            $taskName = [System.IO.Path]::GetFileNameWithoutExtension($f.Name)
            try { Assert-TaskSlug -Task $taskName } catch { continue }
            $identity = Get-DerivedTaskIdentity -ctx $ctx -Worker $workerName -Task $taskName
            if (-not (Test-SamePath $identity.WorktreePath $WorktreePath)) { continue }
            $null = Read-TaskMeta -ctx $ctx -identity $identity
            return $identity
        }
    }
    return $null
}

function Invoke-FinishCurrent {
    # For coding agents inside their own helper-managed task worktree: derive
    # the task identity from the current git worktree and run the full finish
    # flow. Refuses unless the current worktree is provably helper-managed.
    $cwd = (Get-Location).ProviderPath
    $r = Invoke-GitCapture -RepoPath $cwd -GitArgs @('rev-parse', '--show-toplevel')
    if ($r.ExitCode -ne 0) { throw 'finish-current refused: current directory is not inside a git repository' }
    $currentWt = ConvertTo-NormalPath -Value $r.Lines[0] -Base $cwd
    $ctx = Get-CanonicalContext
    if (Test-SamePath $currentWt $ctx.MainPath) { throw 'finish-current refused: current directory is the canonical main worktree - run inside a helper task worktree' }
    $identity = Find-TaskMetaByDerivedWorktree -ctx $ctx -WorktreePath $currentWt
    if (-not $identity) { throw "finish-current refused: current worktree is not a helper-managed task worktree ($currentWt)" }
    Invoke-FinishTask -Worker $identity.Worker -Task $identity.Task
}

function Invoke-CleanupTask {
    param([string]$Worker, [string]$Task)
    Assert-Worker -Worker $Worker
    Assert-TaskSlug -Task $Task
    $ctx = Get-CanonicalContext
    $identity = Get-DerivedTaskIdentity -ctx $ctx -Worker $Worker -Task $Task
    if (Test-SamePath $identity.WorktreePath $ctx.MainPath) { throw 'cleanup refused: derived worktree path is the canonical repository' }
    $meta = Read-TaskMeta -ctx $ctx -identity $identity
    if ($meta.state -cne 'INTEGRATED') { throw "cleanup refused: task state is '$($meta.state)' - only INTEGRATED tasks can be cleaned up" }

    # EXCLUSIVE HANDOFF: without it the worktree and branch are never removed.
    Assert-ExclusiveHandoff -identity $identity -meta $meta
    Assert-CanonicalReady -ctx $ctx

    # Independent git proofs (fast fail on captured immutable OIDs; the final
    # gate re-proves everything immediately before any deletion).
    $br = Invoke-GitCapture -RepoPath $ctx.MainPath -GitArgs @('rev-parse', '--verify', '--quiet', "refs/heads/$($identity.Branch)")
    if ($br.ExitCode -ne 0) { throw "cleanup refused: task branch does not exist: $($identity.Branch)" }
    $branchTip = (Invoke-Git -RepoPath $ctx.MainPath -GitArgs @('rev-parse', "refs/heads/$($identity.Branch)")).Lines[0]

    $bv = Invoke-GitCapture -RepoPath $ctx.MainPath -GitArgs @('rev-parse', '--verify', '--quiet', "$($meta.baseSha)^{commit}")
    if ($bv.ExitCode -ne 0) { throw "cleanup refused: recorded base $($meta.baseSha) is not a valid commit" }
    $anc = Invoke-GitCapture -RepoPath $ctx.MainPath -GitArgs @('merge-base', '--is-ancestor', $meta.baseSha, $branchTip)
    if ($anc.ExitCode -ne 0) { throw 'cleanup refused: task branch no longer descends from its recorded base' }
    if ($branchTip -cne $meta.sourceTipSha) {
        throw "CLEANUP DEFERRED: task branch ref changed since integration ($($meta.sourceTipSha) -> $branchTip); the new OID requires a fresh recovery/integration workflow"
    }
    Assert-LinearTaskHistory -RepoPath $ctx.MainPath -BaseSha $meta.baseSha -Branch $identity.Branch -TipSha $branchTip

    # patch-equivalence proof over the captured source tip: every commit the
    # task branch still carries that is not already in main by ancestry must
    # have an equivalent patch in canonical main. Empty output means the
    # branch is fully contained in main by ancestry (identical-SHA collapse).
    $cherry = Invoke-Git -RepoPath $ctx.MainPath -GitArgs @('cherry', 'HEAD', $branchTip)
    $unintegrated = @(@($cherry.Lines) | Where-Object { $_ -ne '' } | Where-Object { $_ -cnotmatch '^- ' })
    if ($unintegrated.Count -gt 0) {
        throw "cleanup refused: task commit(s) not proven integrated in canonical main: $($unintegrated[0])"
    }

    try {
        Invoke-CleanupCore -ctx $ctx -identity $identity -MetaPath $identity.MetaPath
        Write-Host 'CLEANED'
    }
    catch {
        $msg = $_.Exception.Message
        if ($msg -like 'CLEANUP DEFERRED*' -or $msg -like 'RECOVERY REQUIRED*') {
            Write-Host $msg
            # deterministic non-success: only CLEANED exits 0
            exit 1
        }
        else {
            throw
        }
    }
}

function Invoke-Status {
    # Read-only. Reports state and read-only diagnostics; it never
    # reconstructs markers, prunes worktrees, updates refs, repairs ownership
    # state or claims review approval. The handoff token is printed so the
    # worker/controller can set the exclusive-handoff environment value
    # without reading helper internals (it is generation evidence, not auth).
    $ctx = Get-CanonicalContext
    if (-not (Test-Path -LiteralPath $ctx.TasksDir)) { Write-Host 'no helper tasks'; return }
    $rows = @()
    foreach ($w in @(Get-ChildItem -LiteralPath $ctx.TasksDir -Directory -ErrorAction SilentlyContinue)) {
        foreach ($f in @(Get-ChildItem -LiteralPath $w.FullName -Filter '*.json' -ErrorAction SilentlyContinue)) {
            try {
                $m = [System.IO.File]::ReadAllText($f.FullName) | ConvertFrom-Json
                $note = ''
                if (-not (Test-Path -LiteralPath $m.worktreePath)) { $note += ' [WORKTREE MISSING]' }
                if ($m.state -ceq 'INTEGRATED' -and (Test-Sha1Hex $m.sourceTipSha)) {
                    $tip = Invoke-GitCapture -RepoPath $ctx.MainPath -GitArgs @('rev-parse', '--verify', '--quiet', "refs/heads/$($m.branch)")
                    if ($tip.ExitCode -ne 0 -or $tip.Lines[0] -cne $m.sourceTipSha) { $note += ' [REF CHANGED]' }
                }
                $rows += ('{0,-10} {1}/{2}  {3}  handoff={4}{5}' -f $m.state, $m.worker, $m.task, $m.branch, (Get-HandoffToken -Worker $m.worker -Task $m.task -Nonce $m.ownershipNonce), $note)
            }
            catch { $rows += ("UNREADABLE {0}" -f $f.FullName) }
        }
    }
    if ($rows.Count -eq 0) { Write-Host 'no helper tasks' }
    else { $rows | ForEach-Object { Write-Host $_ } }
}

# ---------- entry point -----------------------------------------------------

function Show-Usage {
    Write-Host 'usage:'
    Write-Host '  .\dev.ps1 new <worker> <task>       create task worktree from current main HEAD'
    Write-Host '  .\dev.ps1 finish <worker> <task>    integrate committed task into main, then cleanup'
    Write-Host '  .\dev.ps1 finish-current            finish from inside the current helper task worktree'
    Write-Host '  .\dev.ps1 cleanup <worker> <task>   remove an already integrated worktree + branch'
    Write-Host '  .\dev.ps1 status                    list helper-managed tasks (read-only)'
    Write-Host 'workers: glm, claude'
    Write-Host "finish/finish-current/cleanup require $Script:HandoffEnvVar to equal the"
    Write-Host 'task handoff token printed by status (exclusive handoff precondition).'
}

function Invoke-DevMain {
    param([string]$Command, [string]$Worker, [string]$Task)
    try {
        switch ($Command) {
            'new' {
                if (-not $Worker -or -not $Task) { throw 'usage: .\dev.ps1 new <worker> <task>' }
                Invoke-NewTask -Worker $Worker -Task $Task
            }
            'finish' {
                if (-not $Worker -or -not $Task) { throw 'usage: .\dev.ps1 finish <worker> <task>' }
                Invoke-FinishTask -Worker $Worker -Task $Task
            }
            'finish-current' {
                if ($Worker -or $Task) { throw 'usage: .\dev.ps1 finish-current (takes no arguments - run inside the task worktree)' }
                Invoke-FinishCurrent
            }
            'cleanup' {
                if (-not $Worker -or -not $Task) { throw 'usage: .\dev.ps1 cleanup <worker> <task>' }
                Invoke-CleanupTask -Worker $Worker -Task $Task
            }
            'status' { Invoke-Status }
            default { Show-Usage; exit 1 }
        }
    }
    catch {
        Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    Invoke-DevMain -Command $Command -Worker $Worker -Task $Task
}
