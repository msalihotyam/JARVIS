<#
Registers the conveyor's Windows scheduled tasks on a new machine (SETUPTASKS1).
PowerShell 5.1, ASCII-only source. Repo root = this script's parent folder;
user = the current user. No secrets, topics or credentials: JARVIS_NTFY_TOPIC,
JARVIS_NTFY_CMD_TOPIC, pager.json and ~/.jarvis/orch_wake.topic are set by hand.

Tasks (reproduced from the live definitions, 2026-11-17):
  \JARVIS Konveyor          every 5 min + at logon; JARVIS-Konveyor.exe (a copy
                            of pythonw.exe) scripts\conveyor.py; no time limit,
                            3 restarts 1 min apart, wake to run, runs on battery
  \Konveyor Yeniden Baslat  no trigger (conveyor.py runs it with schtasks /Run);
                            conhost --headless powershell -File scripts\konveyor_restart.ps1; 5 min
  \JARVIS Watchdog          every 5 min; conhost --headless powershell -File scripts\watchdog.ps1
                            (named in watchdog.ps1; not registered on the old box)

All run as the current user, interactive logon, least privilege.
A NEW task is created DISABLED: the new box must not start a second conveyor
before migration. -Enable turns them on. A re-run updates the definitions and
keeps an existing task's enabled/disabled state unless -Enable is given.
-WhatIf prints what would be registered and changes nothing.

  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup_tasks.ps1 -WhatIf
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup_tasks.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup_tasks.ps1 -Enable
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Enable,
    [string]$PythonW = ''
)
Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ($Root -cmatch '[^\x20-\x7E]') { Write-Warning "repo root is not ASCII: $Root (use e.g. C:\dev\JARVIS)" }
$User = "$env:USERDOMAIN\$env:USERNAME"

if (-not $PythonW) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $PythonW = $cmd.Source }
}
if (-not $PythonW -or $PythonW -like '*\WindowsApps\*') {
    throw 'pythonw.exe not found (or only the Store stub): pass -PythonW <path to pythonw.exe>'
}
$KonvExe = Join-Path (Split-Path -Parent $PythonW) 'JARVIS-Konveyor.exe'

# conhost --headless: a hidden window style still flashes a window under Windows Terminal (Win11).
$ps = "$env:SystemRoot\System32\conhost.exe"
$psArgs = '--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File'
$every5 = { New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 5) }

$tasks = @(
    @{
        Name     = 'JARVIS Konveyor'
        Trigger  = { @((& $every5), (New-ScheduledTaskTrigger -AtLogOn -User $User)) }
        Exe      = $KonvExe
        Args     = "`"$Root\scripts\conveyor.py`""
        Settings = { New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
                        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -WakeToRun }
        Describe = 'every 5 min + at logon'
    },
    @{
        Name     = 'Konveyor Yeniden Baslat'
        Trigger  = { @() }
        Exe      = $ps
        Args     = "$psArgs `"$Root\scripts\konveyor_restart.ps1`""
        Settings = { New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew }
        Describe = 'none (on demand: schtasks /Run)'
    },
    @{
        Name     = 'JARVIS Watchdog'
        Trigger  = { @(& $every5) }
        Exe      = $ps
        Args     = "$psArgs `"$Root\scripts\watchdog.ps1`""
        Settings = { New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                        -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -MultipleInstances IgnoreNew -StartWhenAvailable }
        Describe = 'every 5 min'
    }
)

if (-not (Test-Path $KonvExe) -and $PSCmdlet.ShouldProcess($KonvExe, "copy $PythonW")) {
    Copy-Item -LiteralPath $PythonW -Destination $KonvExe
}

$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
foreach ($t in $tasks) {
    $existing = Get-ScheduledTask -TaskName $t.Name -TaskPath '\' -ErrorAction SilentlyContinue
    if ($Enable) { $on = $true }
    elseif ($existing) { $on = [string]$existing.State -ne 'Disabled' }
    else { $on = $false }
    $state = if ($on) { 'enabled' } else { 'disabled' }
    $verb = if ($existing) { 'update' } else { 'create' }
    Write-Host ("\{0} | {1} | trigger: {2} | action: {3} {4} | workdir: {5} | user: {6} interactive, least privilege | {7}" -f `
        $t.Name, $verb, $t.Describe, $t.Exe, $t.Args, $Root, $User, $state)

    $settings = & $t.Settings
    if (-not $on) { $settings.Enabled = $false }
    $action = New-ScheduledTaskAction -Execute $t.Exe -Argument $t.Args -WorkingDirectory $Root
    $trig = @(& $t.Trigger)
    $p = @{ TaskName = $t.Name; TaskPath = '\'; Action = $action; Settings = $settings; Principal = $principal; Force = $true }
    if ($trig.Count) { $p.Trigger = $trig }
    if ($PSCmdlet.ShouldProcess("\$($t.Name)", "Register-ScheduledTask ($state)")) {
        Register-ScheduledTask @p | Out-Null
    }
}
