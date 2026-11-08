# Phone restart of the conveyor (PHONECMDFIX4).
#
# Runs as its own on-demand scheduled task '\Konveyor Yeniden Baslat' (no trigger,
# 5 min limit), started by scripts/conveyor.py with `schtasks /Run`. Task Scheduler
# starts it outside the conveyor's job object, so stopping the conveyor cannot kill it.
#
# Steps: stop '\JARVIS Konveyor', wait (max 30 s) until it is not Running, then start
# it and check State = Running; up to 3 tries over ~60 s. On failure it posts ONE ntfy
# message; the topic is read only from the USER environment, never from argv or a file.
#
# Exit codes:
#   0  the conveyor task is Running again
#   1  the task did not reach Running after 3 starts (failure push attempted)
#   2  the task was still Running 30 s after Stop (failure push attempted)

$ErrorActionPreference = 'Continue'
$Name = 'JARVIS Konveyor'
$Path = '\'

function Get-State {
    $t = Get-ScheduledTask -TaskName $Name -TaskPath $Path -ErrorAction SilentlyContinue
    if ($t) { return [string]$t.State }
    return 'Missing'
}

function Send-Failure {
    $topic = [Environment]::GetEnvironmentVariable('JARVIS_NTFY_TOPIC','User')
    if ($topic -cmatch '^[A-Za-z0-9_-]{1,64}$') {
        try {
            Invoke-RestMethod -Method Post -Uri ('https://ntfy.sh/' + $topic) `
                -Body 'Konveyor telefondan yeniden baslatilamadi; panelden baslat' `
                -Headers @{ Title = 'Yeniden baslat' } -TimeoutSec 15 | Out-Null
        } catch { }
    }
}

Stop-ScheduledTask -TaskName $Name -TaskPath $Path -ErrorAction SilentlyContinue
$deadline = (Get-Date).AddSeconds(30)
while ((Get-State) -eq 'Running' -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 1 }
if ((Get-State) -eq 'Running') { Send-Failure; exit 2 }

for ($i = 1; $i -le 3; $i++) {
    try { Start-ScheduledTask -TaskName $Name -TaskPath $Path -ErrorAction Stop } catch { }
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        if ((Get-State) -eq 'Running') { exit 0 }
        Start-Sleep -Seconds 2
    }
}
Send-Failure
exit 1
