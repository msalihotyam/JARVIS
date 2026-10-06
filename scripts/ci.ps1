# Run the full test suite on GitHub Actions for the current HEAD.
#
#   scripts\ci.ps1 [-Name setup]
#
# Pushes HEAD to the disposable branch ci/<Name> (force-push is allowed ONLY
# for ci/* - Salih decision 2026-10-02; main and every other branch still need
# his explicit authorization every time), waits for the .github/workflows/
# tests.yml run for that exact commit and prints the result, the run URL and
# the combined summary of all shards (the aggregate job's). Exit code: 0
# green, 1 red, 2 setup problem.
#
# This script never logs in. If `gh` is missing or not authenticated it stops
# and says what to run.
param(
    [string]$Name = "setup"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if ($Name -notmatch '^[A-Za-z0-9._-]+$') {
    Write-Output "ERROR: -Name must be letters, digits, '.', '_' or '-' (the branch is ci/<Name>)."
    exit 2
}
$Branch = "ci/$Name"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Output "STOP: the GitHub CLI (gh) is not installed. Install it (winget install GitHub.cli), then run: gh auth login"
    exit 2
}
gh auth status *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Output "STOP: gh is not authenticated. Run this yourself (this script never logs in): gh auth login"
    exit 2
}

$Sha = (git rev-parse HEAD).Trim()
$Dirty = git status --porcelain --untracked-files=no | Where-Object { $_ -notmatch 'dev/dashboard/' }
if ($Dirty) {
    Write-Output "NOTE: uncommitted tracked changes exist; CI tests the committed HEAD $($Sha.Substring(0, 7)) only."
}

Write-Output "Pushing $($Sha.Substring(0, 7)) to origin/$Branch (force; ci/* only)"
git push --force origin "HEAD:refs/heads/$Branch"
if ($LASTEXITCODE -ne 0) { Write-Output "ERROR: the push failed."; exit 2 }

# A transient gh failure (a network reset on the watch, a failed lookup) is never
# a run result: the only result is the run's own completed conclusion.
function Get-RunInfo {
    param([string]$Id, [string]$Fields)
    try {
        $Text = gh run view $Id --json $Fields 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $Text) { return $null }
        return ($Text | ConvertFrom-Json)
    } catch { return $null }
}

# The run for exactly this commit.
$Run = $null
for ($i = 0; $i -lt 30 -and -not $Run; $i++) {
    Start-Sleep -Seconds 4
    try {
        $Text = gh run list --workflow tests.yml --branch $Branch --limit 10 --json "databaseId,headSha,url" 2>$null
        if ($LASTEXITCODE -eq 0 -and $Text) {
            $Run = ($Text | ConvertFrom-Json) | Where-Object { $_.headSha -eq $Sha } | Select-Object -First 1
        }
    } catch { }
}
if (-not $Run) { Write-Output "ERROR: no workflow run appeared for $Sha on $Branch."; exit 2 }
Write-Output "Run: $($Run.url)"

# Wait for completion. `gh run watch` is only a convenience (live progress);
# whether it returns, fails or is cut by a network reset, the decision is taken
# from re-polling `gh run view` for THIS run until it is completed - bounded
# (the workflow's own timeout is 90 min) with a short, growing backoff.
$Started = Get-Date
$Deadline = $Started.AddMinutes(100)
gh run watch $Run.databaseId --interval 20 *> $null
$Backoff = 3
$Info = $null
while ($true) {
    $Info = Get-RunInfo -Id $Run.databaseId -Fields "status,conclusion,url"
    if ($Info -and $Info.status -eq "completed") { break }
    if ((Get-Date) -gt $Deadline) {
        Write-Output "ERROR: run $($Run.url) did not complete within 100 minutes (last seen status: $(if ($Info) { $Info.status } else { 'unreadable' }))."
        exit 2
    }
    Start-Sleep -Seconds $Backoff
    $Backoff = [Math]::Min($Backoff * 2, 30)
}
$Wall = [int]((Get-Date) - $Started).TotalSeconds
Write-Output "Result: $($Info.conclusion) (waited $Wall s)  $($Info.url)"

# Only now (the run is completed) is the artifact downloaded.
$Download = Join-Path ([IO.Path]::GetTempPath()) "jarvis-ci-$($Run.databaseId)"
gh run download $Run.databaseId --name ci-summary --dir $Download 2>$null
$Summary = Join-Path $Download "ci-summary.txt"
if (Test-Path $Summary) {
    Write-Output "--- combined summary of all shards ($Summary) ---"
    Get-Content $Summary
} else {
    Write-Output "(no ci-summary artifact was uploaded)"
}
if ($Info.conclusion -eq "success") { exit 0 } else { exit 1 }
