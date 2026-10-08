// Wave 2, pure parts: which shell commands are test runs, what a test run's output
// says, and the suite lock record (build/locks/suite.json).

export type Kind = 'full' | 'targeted' | 'alone'

export type TestCommand = {
  kind: Kind
  // a full suite: takes the one suite lock (python scripts\test_parallel.py, scripts\test.ps1)
  lock: boolean
  // loads UI tests: waits while another session's full suite holds the lock
  ui: boolean
  targets: string[]
  background: boolean
}

// Programs that may carry a test run (the run itself or a shell that starts it).
const RUNNERS = /^(python[\d.]*|pythonw|py|uv|uvx|powershell|pwsh|cmd|bash|sh|wsl|time|start|nohup|call|env|exec|command|nice|start-process|saps|start-job|measure-command|invoke-expression|iex|invoke-command|timeout)$/i
const SHELLS = /^(powershell|pwsh|cmd|bash|sh|wsl|start-process|saps|start-job|measure-command|invoke-expression|iex|invoke-command|timeout)$/i
const UI_TARGET = /test_ui|_ui_|_6g|bridge_shutdown|conversation_attach|conversation_lifecycle|model_list_newest|projects_(gate7|ui)|windows_scripts|(^|[.\\/])ui([.\\/]|$)/i
const METHOD_ID = /(^|\.)[A-Z]\w*\.test\w*$/
// unittest options that take a value, so the value is not a target
const VALUE_OPTS = new Set(['-s', '-t', '-p', '-k', '--start-directory', '--top-level-directory', '--pattern', '--durations'])

// Splits a command line into simple commands. Quotes are not parsed: a split
// inside a quoted string only makes a segment that is not a runner, which is skipped.
export function segmentsOf(command: string): string[] {
  return String(command ?? '')
    .split(/&&|\|\||[;|\n&{}]/)
    .map((s) => s.trim())
    .filter(Boolean)
}

// The program a simple command starts: leading ( { & . and NAME=value words are skipped.
function progOf(segment: string): string {
  let rest = segment
  for (;;) {
    const next = rest.replace(/^(?:[({&]\s*|\.\s+|[A-Za-z_][A-Za-z0-9_]*=\S*\s+|\$[\w:]+\s*=\s*)/, '')
    if (next === rest) break
    rest = next
  }
  const first = (rest.match(/^("[^"]*"|'[^']*'|\S+)/) ?? ['', ''])[1] as string
  const bare = first.replace(/^["']|["']$/g, '')
  const base = bare.split(/[\\/]/).pop() as string
  return base.replace(/\.(exe|cmd|bat)$/i, '')
}

// Commands that only read, check or format the runner, or ask it for help.
const NOT_A_RUN = /\b(py_compile|ruff|black|mypy|pyflakes|flake8|pylint|isort|bandit|pip|pytest-cov|Get-Content|Select-String|gc|cat)\b|--help\b|(^|\s)-h(\s|$)/i

// A run that goes on after the tool call returns: a trailing &, Start-Job, nohup, or Start-Process without -Wait.
function backgroundSpelling(command: string): boolean {
  const text = String(command ?? '')
  if (/(^|[^&])&\s*$/.test(text.trim()) || /\b(Start-Job|nohup)\b/i.test(text)) return true
  return /\b(Start-Process|saps)\b/i.test(text) && !/\s-Wait\b/i.test(text)
}

export function classifyCommand(command: string, background = false): TestCommand | null {
  let best: TestCommand | null = null
  const bg = background || backgroundSpelling(command)
  for (const seg of segmentsOf(command)) {
    background = bg
    if (NOT_A_RUN.test(seg)) continue
    const prog = progOf(seg)
    const direct = /^test\.ps1$/i.test(prog)
    const viaPs1 = /^(powershell|pwsh)$/i.test(prog) && /(^|[\\/\s"'])test\.ps1\b/i.test(seg)
    // a program held in a variable (& $py ...) is a runner too
    const runner = RUNNERS.test(prog) || prog.startsWith('$')
    const shellOk = !SHELLS.test(prog) || /(^|[\s"'\\/])(python[\d.]*|py|pythonw)(\.exe)?(?=[\s"'])|test\.ps1/i.test(seg)
    let found: TestCommand | null = null
    if (direct || viaPs1 || (runner && shellOk && /test_parallel/i.test(seg))) {
      found = { kind: 'full', lock: true, ui: true, targets: [], background }
    } else if (runner && shellOk && /(^|\s)-m\s+unittest\b/.test(seg)) {
      found = unittestOf(seg, background)
    }
    if (found !== null && (best === null || (found.lock && !best.lock))) best = found
  }
  return best
}

function unittestOf(seg: string, background: boolean): TestCommand {
  const words = seg.split(/\s+/)
  const at = words.findIndex((w) => w === 'unittest')
  const args = at === -1 ? [] : words.slice(at + 1)
  const targets: string[] = []
  let discover = false
  for (let i = 0; i < args.length; i++) {
    const a = (args[i] as string).replace(/^["']|["']$/g, '')
    if (VALUE_OPTS.has(a)) {
      i++
      continue
    }
    if (a === 'discover') discover = true
    else if (/^\d?\*?>>?$/.test(a)) i++
    else if (a !== '' && !a.startsWith('-') && !/^\d?\*?>/.test(a)) targets.push(a)
  }
  const wholeSuite = discover || targets.length === 0
  const kind: Kind = wholeSuite ? 'full' : targets.length === 1 && METHOD_ID.test(targets[0] as string) ? 'alone' : 'targeted'
  // a whole-suite unittest run is heavy and loads UI tests, but it is not "python scripts\test_parallel.py"
  return { kind, lock: false, ui: wholeSuite || targets.some((t) => UI_TARGET.test(t) || t.includes('$')), targets, background }
}

// A test command the suite queue gates: if the queue check breaks, this one is refused.
export function isGated(tc: TestCommand | null): boolean {
  return tc !== null && (tc.lock || tc.ui)
}

export type Parsed = { ran: number | null; wallS: number | null; ok: boolean | null; failures: string[] }

// The result of a unittest, a unittest -v or scripts\test_parallel.py run. FAIL/ERROR
// lines give the ids (a subTest keeps its parameters), "Ran N tests in Xs" and the
// last OK / FAILED line give the totals. Returns null when no summary is there.
export function parseTestOutput(text: string): Parsed | null {
  const out = String(text ?? '')
  const ranAll = [...out.matchAll(/^Ran (\d+) tests? in ([\d.]+)s/gm)]
  const ran = ranAll.length > 0 ? (ranAll[ranAll.length - 1] as RegExpMatchArray) : null
  const verdicts = [...out.matchAll(/^(OK|FAILED)(?: \(([^)]*)\))?\s*$/gm)]
  const verdict = verdicts.length > 0 ? (verdicts[verdicts.length - 1] as RegExpMatchArray) : null
  const failures = new Set<string>()
  for (const m of out.matchAll(/^(?:FAIL|ERROR): (\S+) \(([^)\s]+)\)([^\r\n]*)$/gm)) {
    const name = m[1] as string
    const dotted = m[2] as string
    const rest = (m[3] as string).trim()
    const base = /^(setUpClass|tearDownClass|setUpModule|tearDownModule)$/.test(name) ? dotted + '.' + name : dotted
    failures.add(rest === '' ? base : base + ' ' + rest)
  }
  if (ran === null && verdict === null && failures.size === 0) return null
  return {
    ran: ran === null ? null : Number(ran[1]),
    wallS: ran === null ? null : Number(ran[2]),
    ok: verdict === null ? (failures.size > 0 ? false : null) : verdict[1] === 'OK' && failures.size === 0,
    failures: [...failures],
  }
}

// ---- the lock record

export const STALE_LOCK_MS = 45 * 60 * 1000
export const HOLD_MAX_MS = 40 * 60 * 1000
export const BEAT_MS = 60 * 1000
export const POLL_SECONDS = 15
// a heartbeat further ahead than this is a broken clock or a forged file
export const FUTURE_SKEW_MS = 5 * 60 * 1000

export type LockRec = { lane: string; session: string; started_at: string; command: string; heartbeat: string }

export type LockState =
  | { state: 'free' }
  | { state: 'mine'; rec: LockRec }
  | { state: 'stale'; rec: LockRec | null }
  | { state: 'held'; rec: LockRec }

// Text from a lock file is shown to the model and in the band: keep it plain.
export function plain(text: unknown, max = 40): string {
  return String(text ?? '').replace(/[^A-Za-z0-9._-]/g, '_').slice(0, max)
}

export function parseLock(raw: string): LockRec | null {
  try {
    const v = JSON.parse(raw) as Record<string, unknown>
    if (!v || typeof v !== 'object') return null
    for (const k of ['lane', 'session', 'started_at', 'command', 'heartbeat']) if (typeof v[k] !== 'string') return null
    return {
      lane: plain(v.lane),
      session: plain(v.session, 80),
      started_at: v.started_at as string,
      command: String(v.command).slice(0, 200),
      heartbeat: v.heartbeat as string,
    }
  } catch {
    return null
  }
}

// Absent or empty file = free. Unreadable JSON, a missing field, a bad or future
// heartbeat, or one older than 45 minutes = stale (anyone may take it over).
export function lockState(raw: string | null, nowMs: number, session: string): LockState {
  if (raw === null || raw.trim() === '') return { state: 'free' }
  const rec = parseLock(raw)
  if (rec === null) return { state: 'stale', rec: null }
  if (rec.session === plain(session, 80)) return { state: 'mine', rec }
  const beat = Date.parse(rec.heartbeat)
  if (!Number.isFinite(beat) || beat > nowMs + FUTURE_SKEW_MS || nowMs - beat > STALE_LOCK_MS) return { state: 'stale', rec }
  return { state: 'held', rec }
}

export function minutesSince(iso: string, nowMs: number): number {
  const at = Date.parse(iso)
  return Number.isFinite(at) ? Math.max(0, Math.floor((nowMs - at) / 60000)) : 0
}

export function newLock(lane: string, session: string, command: string, nowMs: number, startedAt?: string): string {
  const stamp = new Date(nowMs).toISOString()
  const rec: LockRec = {
    lane: plain(lane),
    session: plain(session, 80),
    started_at: startedAt ?? stamp,
    command: String(command).slice(0, 200),
    heartbeat: stamp,
  }
  return JSON.stringify(rec)
}

// Compare-and-swap on the lock file, done inside one exclusive open so two sessions
// cannot both win. JF_MODE 'create' makes the file (it must not exist yet); 'swap'
// needs JF_EXPECT to be the text last read (unset = an empty file; compared exactly,
// case included); 'stale' replaces the file unless it holds a valid lock record whose
// heartbeat is fresh by JF_NOW (the same rule as lockState: 45 min, 5 min of future),
// so a garbage, UTF-16 or differently decoded file can still be taken over. JF_NEW is
// the text to leave ('' releases). Exit 0 = swapped, 3 = the file is not what was
// expected / is still fresh, 4 = could not open (busy, already there, or an error).
export const CAS_ARGV = [
  'powershell',
  '-NoProfile',
  '-NonInteractive',
  '-Command',
  [
    '# JF_CAS',
    "$ErrorActionPreference = 'Stop'",
    'try {',
    '  $enc = New-Object System.Text.UTF8Encoding($false)',
    "  if ($env:JF_MODE -eq 'create') {",
    '    New-Item -ItemType Directory -Force -Path (Split-Path -LiteralPath $env:JF_PATH) | Out-Null',
    '    $fs = New-Object System.IO.FileStream($env:JF_PATH, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)',
    '  } else {',
    '    $fs = New-Object System.IO.FileStream($env:JF_PATH, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)',
    '    $sr = New-Object System.IO.StreamReader($fs, $enc, $true, 1024, $true)',
    '    $cur = $sr.ReadToEnd(); $sr.Dispose()',
    "    if ($env:JF_MODE -eq 'stale') {",
    '      $fresh = $false',
    '      try {',
    '        $ok = $true',
    "        foreach ($k in 'lane','session','started_at','command','heartbeat') { if ($cur -notmatch ('\"' + $k + '\"\\s*:\\s*\"')) { $ok = $false } }",
    "        if ($ok -and ($cur -match '\"heartbeat\"\\s*:\\s*\"([^\"]+)\"')) {",
    '          $hb = [System.DateTimeOffset]::Parse($Matches[1]).ToUnixTimeMilliseconds(); $now = [long]$env:JF_NOW',
    '          if (($hb -le ($now + 300000)) -and (($now - $hb) -le 2700000)) { $fresh = $true }',
    '        }',
    '      } catch { }',
    '      if ($fresh) { $fs.Dispose(); exit 3 }',
    '    } else {',
    '      $want = [string]$env:JF_EXPECT',
    '      if ($cur.Trim() -cne $want.Trim()) { $fs.Dispose(); exit 3 }',
    '    }',
    '    $fs.SetLength(0)',
    '  }',
    '  $b = $enc.GetBytes([string]$env:JF_NEW); $fs.Write($b, 0, $b.Length); $fs.Dispose(); exit 0',
    '} catch { exit 4 }',
  ].join('\n'),
] as const

export const SLEEP_ARGV = ['powershell', '-NoProfile', '-NonInteractive', '-Command', 'Start-Sleep -Seconds ([int]$env:JF_SECONDS)'] as const

// python processes on this PC (the count at the start of a test run)
export const PY_COUNT_ARGV = [
  'powershell',
  '-NoProfile',
  '-NonInteractive',
  '-Command',
  '# JF_PYCOUNT\n(Get-Process -Name python,pythonw -ErrorAction SilentlyContinue | Measure-Object).Count',
] as const

// processes whose command line runs the parallel runner (the [e] keeps this very command out of the match)
export const SUITE_RUNNING_ARGV = [
  'powershell',
  '-NoProfile',
  '-NonInteractive',
  '-Command',
  "# JF_SUITE_RUNNING\ntry { (Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" -ErrorAction Stop | Where-Object { $_.CommandLine -match 'test_parall[e]l|unittes[t] discover' } | Measure-Object).Count } catch { exit 1 }",
] as const

export function toCount(stdout: string): number | null {
  const n = Number(String(stdout ?? '').trim())
  return Number.isInteger(n) && n >= 0 ? n : null
}
