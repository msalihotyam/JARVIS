#Requires -Version 5.1
<#
.SYNOPSIS
  JARVIS local release manager: inspect, roll back, activate.

.USAGE
  .\scripts\release.ps1 -Status                 what is installed and active (read-only)
  .\scripts\release.ps1 -List                   every installed release (read-only)
  .\scripts\release.ps1 -Rollback               current -> the previously active release
  .\scripts\release.ps1 -Activate <release-id>  current -> that installed release

  Installing a new release is `.\scripts\build_release.ps1 -Install`.
  -InstallRoot overrides %USERPROFILE%\Apps\JARVIS (tests / experiments only).
  All logic lives in scripts\JarvisRelease.psm1; this is only the command line.

  Exit code: 0 done (for -Status: manager consistent), 1 refused / failed /
  inconsistent, 2 usage.
#>
param(
    [switch]$Status,
    [switch]$List,
    [switch]$Rollback,
    [string]$Activate = '',
    [string]$InstallRoot = ''
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
Import-Module (Join-Path $PSScriptRoot 'JarvisRelease.psm1') -Force

$chosen = @()
if ($Status) { $chosen += 'Status' }
if ($List) { $chosen += 'List' }
if ($Rollback) { $chosen += 'Rollback' }
if ($Activate) { $chosen += 'Activate' }
if ($chosen.Count -ne 1) {
    Write-Output 'usage: release.ps1 -Status | -List | -Rollback | -Activate <release-id>   [-InstallRoot <dir>]'
    exit 2
}

$root = Get-JarvisReleaseRoot -InstallRoot $InstallRoot
try {
    switch ($chosen[0]) {
        'Status' {
            $s = Get-JarvisReleaseStatus -Root $root -IncludeShortcut
            Format-JarvisReleaseStatus $s | Write-Output
            if ($s.Consistent) { exit 0 } else { exit 1 }
        }
        'List' {
            $s = Get-JarvisReleaseStatus -Root $root
            Format-JarvisReleaseList $s | Write-Output
            exit 0
        }
        'Rollback' {
            $r = Invoke-JarvisRollback -Root $root
            Write-Output "Rolled back: current -> $($r.ReleaseId) (was $($r.From))"
            Format-JarvisReleaseStatus (Get-JarvisReleaseStatus -Root $root -IncludeShortcut) | Write-Output
            exit 0
        }
        'Activate' {
            $r = Invoke-JarvisActivate -Root $root -ReleaseId $Activate
            if ($r.Changed) { Write-Output "Activated: current -> $($r.ReleaseId) (was $($r.From))" }
            else { Write-Output "Already active: $($r.ReleaseId)" }
            Format-JarvisReleaseStatus (Get-JarvisReleaseStatus -Root $root -IncludeShortcut) | Write-Output
            exit 0
        }
    }
}
catch {
    Write-Output "ERROR: $($_.Exception.Message)"
    exit 1
}
