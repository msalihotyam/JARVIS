<#
JARVIS watchdog: Claude-independent alarms for the laptop, run every 5 min by
the Task Scheduler task "JARVIS Watchdog". PowerShell 5.1, ASCII-only source
(non-ASCII text is built from [char] codes so 5.1 reads the file correctly).

It never kills processes, approves anything, edits settings, deletes
conveyor.lock or touches the repo. Its only side effects: Start-ScheduledTask
"JARVIS Konveyor", an ntfy page, and build\conveyor\watchdog_state.json.
The ntfy topic is never printed, logged or written. -DryRun prints what it
would do and changes nothing (no task start, no page, no state write, no topic
read). -Now (epoch seconds) and -ProcessName exist for tests.
#>
param(
    [switch]$DryRun,
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [double]$Now = 0,
    [string]$ProcessName = 'claude'
)
Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'

function Epoch([datetime]$t) { ($t.ToUniversalTime() - [datetime]'1970-01-01').TotalSeconds }
if ($Now -le 0) { $Now = Epoch ([datetime]::UtcNow) }

$THROTTLE_S = 7200
$conv = Join-Path $Root 'build\conveyor'
$statusPath = Join-Path $conv 'status.json'
$statePath = Join-Path $conv 'watchdog_state.json'
$o = [char]0x00F6; $i = [char]0x0131; $s = [char]0x015F

function Say([string]$m) { Write-Output "watchdog: $m" }
function Age([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return [double]::PositiveInfinity }
    $Now - (Epoch (Get-Item -LiteralPath $path).LastWriteTimeUtc)
}
function Mins([double]$sec) { if ([double]::IsInfinity($sec)) { '?' } else { [int][math]::Floor($sec / 60) } }
function ReadJson([string]$path) {
    try { Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $null }
}
function Prop($obj, [string]$name) {
    if ($null -ne $obj -and $obj -isnot [array] -and $obj.PSObject.Properties[$name]) { $obj.$name } else { $null }
}
function Num($v) { try { [double]$v } catch { 0 } }
function Get-Topic {
    $t = Prop (ReadJson (Join-Path $conv 'pager.json')) 'topic'
    if (-not $t) { $t = $env:JARVIS_NTFY_TOPIC }
    if (-not $t) {
        try { $t = (Get-ItemProperty -Path 'HKCU:\Environment' -Name JARVIS_NTFY_TOPIC).JARVIS_NTFY_TOPIC } catch { $t = $null }
    }
    if ($t -is [string] -and $t -match '^[A-Za-z0-9_-]{8,64}$') { return $t }
    $null
}

$state = @{}
$raw = ReadJson $statePath
if ($raw) { foreach ($p in $raw.PSObject.Properties) { $state[$p.Name] = Num $p.Value } }
$pages = New-Object System.Collections.ArrayList
function Want([string]$cond, [string]$msg) { [void]$pages.Add(@($cond, $msg)) }

# Stale conveyor.lock: reported only, never deleted (the conveyor handles it).
$lockNote = ''
$lock = ReadJson (Join-Path $conv 'conveyor.lock')
$hb = Num (Prop $lock 'heartbeat')
if ($hb -gt 0 -and ($Now - $hb) -gt 300) {
    $lockPid = [int](Num (Prop $lock 'pid'))
    $alive = $lockPid -gt 0 -and $null -ne (Get-Process -Id $lockPid -ErrorAction SilentlyContinue)
    $boot = 0
    try { $boot = Epoch (Get-CimInstance Win32_OperatingSystem).LastBootUpTime } catch { $boot = 0 }
    if (-not $alive -or $hb -lt $boot) {
        $lockNote = " Kilit bayat (pid $lockPid, $(Mins ($Now - $hb)) dk); silinmedi."
        Say "stale conveyor.lock (pid $lockPid)"
    }
}

# Conveyor: status.json > 5 min -> start its task; > 10 min -> page.
$statusAge = Age $statusPath
if ($statusAge -gt 300) {
    if ($DryRun) { Say "would Start-ScheduledTask 'JARVIS Konveyor'" }
    else {
        try { Start-ScheduledTask -TaskName 'JARVIS Konveyor'; Say 'started JARVIS Konveyor' }
        catch { Say "Start-ScheduledTask failed: $($_.Exception.GetType().Name)" }
    }
}
if ($statusAge -gt 600) { Want 'conveyor' ("Konvey${o}r durdu: status.json $(Mins $statusAge) dk eski." + $lockNote) }

# Claude desktop app. A process whose path is unreadable counts as running
# (no false page); the CLI under .local\bin or node does not count.
$desktop = @(Get-Process -Name $ProcessName -ErrorAction SilentlyContinue | Where-Object {
    -not $_.Path -or $_.Path -match '\\(AnthropicClaude|WindowsApps|Claude)\\' })
if ($desktop.Count -eq 0) { Want 'claude' "Claude uygulamas${i} kapal${i}." }

# Outage > 30 min: pager.json outage.since, else status.json hold first seen here.
$status = ReadJson $statusPath
$since = Num (Prop (Prop (ReadJson (Join-Path $conv 'pager.json')) 'outage') 'since')
$attn = (@(Prop $status 'attention') | ForEach-Object { "$_" }) -join ' '
if ($since -gt 0 -or (Prop $status 'outage_hold') -eq $true -or $attn -match 'outage') {
    if ($since -le 0) {
        if (-not $state.ContainsKey('seen:outage')) { $state['seen:outage'] = $Now }
        $since = $state['seen:outage']
    }
    if (($Now - $since) -gt 1800) { Want 'outage' "Konvey${o}r kesintide: $(Mins ($Now - $since)) dk." }
} else { $state.Remove('seen:outage') }

# Orchestrator heartbeat mtime > 90 min (same rule as conveyor.py ORCH_STALE_S).
$orchAge = Age (Join-Path $Root 'build\orch\heartbeat')
if ($orchAge -gt 5400) { Want 'orch' "Orkestrat${o}r tur atm${i}yor: heartbeat $(Mins $orchAge) dk eski." }

# Free disk on C:.
try {
    $freeGb = (Get-PSDrive -Name C).Free / 1GB
    if ($freeGb -lt 20) { Want 'disk' ("C: bo${s} alan {0:N1} GB." -f $freeGb) }
} catch { Say "disk check failed: $($_.Exception.GetType().Name)" }

# One page per condition per 2 h.
$topic = $null
foreach ($pg in $pages) {
    $cond = $pg[0]; $msg = $pg[1]; $key = "page:$cond"
    if ($state.ContainsKey($key) -and ($Now - $state[$key]) -lt $THROTTLE_S) { Say "throttled $cond"; continue }
    if ($DryRun) { Say "would page [$cond] $msg"; continue }
    if (-not $topic) { $topic = Get-Topic }
    if (-not $topic) { Say "no ntfy topic; [$cond] not sent"; continue }
    try {
        Invoke-RestMethod -Method Post -Uri ('https://ntfy.sh/' + $topic) -UseBasicParsing -TimeoutSec 15 `
            -Headers @{ Title = 'JARVIS Watchdog'; Priority = 'high' } `
            -ContentType 'text/plain; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($msg)) | Out-Null
        $state[$key] = $Now
        Say "paged [$cond]"
    } catch { Say "page [$cond] failed: $($_.Exception.GetType().Name)" }
}

if (-not $DryRun) {
    try {
        $tmp = "$statePath.tmp"
        $state | ConvertTo-Json | Set-Content -LiteralPath $tmp -Encoding UTF8
        Move-Item -LiteralPath $tmp -Destination $statePath -Force
    } catch { Say "state write failed: $($_.Exception.GetType().Name)" }
}
