import { expect, test } from 'claude-code/testing'
import { classifyCommand, isGated, lockState, newLock, parseTestOutput, plain } from '../hooks/lib/suite.ts'
import { header, put, startSession, userPrompt, world } from './world.ts'

const LOCK = '/repo/build/locks/suite.json'
const FULL = 'python scripts\\test_parallel.py'
const MIN = 60 * 1000
const iso = (ms: number) => new Date(ms).toISOString()
// another session's lock record, heartbeat at `beat`
const theirs = (beat: number, extra: Record<string, string> = {}) =>
  JSON.stringify({ lane: 'OTHER', session: 'sess-other', started_at: iso(0), command: FULL, heartbeat: iso(beat), ...extra })
const bash = (command: string, extra: Record<string, unknown> = {}) => ({ tool: 'Bash', command, ...extra }) as any
const flakes = (w: any) =>
  String(w.files.get('/repo/build/flakes/ledger.jsonl') ?? '')
    .split('\n')
    .filter(Boolean)
    .map((l) => JSON.parse(l))

test('pure: which commands are test runs', { timeoutMs: 60000 }, async () => {
  const k = (c: string) => classifyCommand(c)
  expect(k('python scripts\\test_parallel.py')).toMatchObject({ kind: 'full', lock: true })
  expect(k('python scripts/test_parallel.py --workers 4 2>&1 | tail -20')).toMatchObject({ kind: 'full', lock: true })
  expect(k('cd /r && uv run python scripts/test_parallel.py tests.test_ui')).toMatchObject({ kind: 'full', lock: true })
  expect(k('powershell -File scripts\\test.ps1 -Parallel')).toMatchObject({ kind: 'full', lock: true })
  expect(k('python -m unittest tests.test_intent')).toMatchObject({ kind: 'targeted', lock: false, ui: false })
  expect(k('python -m unittest tests.test_ui.Foo.test_bar')).toMatchObject({ kind: 'alone', ui: true, targets: ['tests.test_ui.Foo.test_bar'] })
  expect(k('python -m unittest -v tests.test_intent.IntentTests.test_a')).toMatchObject({ kind: 'alone', ui: false })
  expect(k('python -m unittest discover -s tests')).toMatchObject({ kind: 'full', lock: false, ui: true })
  expect(k('python -m unittest tests.test_motion_6g tests.test_intent')).toMatchObject({ kind: 'targeted', ui: true })
  // not runs
  // ordinary spellings of the same run
  for (const c of [
    'PYTHONUTF8=1 python scripts/test_parallel.py',
    'QT_QPA_PLATFORM=offscreen python scripts/test_parallel.py tests.test_ui',
    'timeout 3600 python scripts/test_parallel.py',
    'Start-Process python -ArgumentList scripts\\test_parallel.py -Wait',
    'Start-Job { python scripts\\test_parallel.py }',
    '(python scripts/test_parallel.py)',
    '& "C:\\Python312\\python.exe" scripts\\test_parallel.py',
    'cmd /c "python scripts\\test_parallel.py"',
  ]) expect(k(c), c).toMatchObject({ kind: 'full', lock: true })
  expect(k('QT_QPA_PLATFORM=offscreen python -m unittest tests.test_ui_shell')).toMatchObject({ ui: true })
  expect(k('git add scripts/test_parallel.py')).toBeNull()
  expect(k('grep -n unittest JARVIS_RUNBOOK.md')).toBeNull()
  expect(k('powershell -Command "Get-Content scripts\\test_parallel.py"')).toBeNull()
  expect(k('cat scripts/test_parallel.py')).toBeNull()
  expect(k('ls')).toBeNull()
})

test('pure: the test output parser reads totals, FAILED (errors=1) and subTest ids', { timeoutMs: 60000 }, async () => {
  const text = [
    'test_a (tests.test_m.C.test_a) ... ok',
    '======================================================================',
    'ERROR: test_boom (tests.test_m.C.test_boom)',
    'Traceback ...',
    '======================================================================',
    'FAIL: test_sub (tests.test_m.C.test_sub) (i=1)',
    'Traceback ...',
    'FAIL: test_sub (tests.test_m.C.test_sub) (i=2)',
    'ERROR: setUpClass (tests.test_m.D)',
    '----------------------------------------------------------------------',
    'Ran 12 tests in 3.456s',
    '',
    'FAILED (failures=2, errors=2)',
  ].join('\r\n')
  expect(parseTestOutput(text)).toEqual({
    ran: 12,
    wallS: 3.456,
    ok: false,
    failures: ['tests.test_m.C.test_boom', 'tests.test_m.C.test_sub (i=1)', 'tests.test_m.C.test_sub (i=2)', 'tests.test_m.D.setUpClass'],
  })
  expect(parseTestOutput('Ran 1 test in 0.001s\n\nFAILED (errors=1)')).toMatchObject({ ran: 1, ok: false, failures: [] })
  expect(parseTestOutput('Ran 3 tests in 0.5s\n\nOK')).toEqual({ ran: 3, wallS: 0.5, ok: true, failures: [] })
  expect(parseTestOutput('Ran 4750 tests in 445.1s wall (skipped=3, failures=0, errors=0)\nOK')).toMatchObject({ ran: 4750, ok: true })
  expect(parseTestOutput('nothing to see')).toBeNull()
  expect(parseTestOutput('')).toBeNull()
})

test('pure: lock states - free, mine, held, stale by age, future and corrupt', { timeoutMs: 60000 }, async () => {
  const now = 100 * MIN
  expect(lockState(null, now, 's').state).toBe('free')
  expect(lockState('  \n', now, 's').state).toBe('free')
  expect(lockState(theirs(now - MIN), now, 's').state).toBe('held')
  expect(lockState(theirs(now - 44 * MIN), now, 's').state).toBe('held')
  expect(lockState(theirs(now - 46 * MIN), now, 's').state).toBe('stale')
  expect(lockState(theirs(now + 10 * MIN), now, 's').state).toBe('stale')
  expect(lockState('not json', now, 's').state).toBe('stale')
  expect(lockState('{"lane":"x"}', now, 's').state).toBe('stale')
  expect(lockState(theirs(now - 90 * MIN, { session: 's' }), now, 's').state).toBe('mine')
  expect(plain('a b/../c\n')).toBe('a_b_.._c_')
  const mine = newLock('SB1', 'sess-me', 'python x', now)
  expect(JSON.parse(mine)).toMatchObject({ lane: 'SB1', session: 'sess-me', command: 'python x' })
})

test('A1: a free lock is taken for a full suite and released on the tool result', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  const res = await $.tool.call(bash(FULL))
  expect(res.deny).toBeUndefined()
  expect(w.toolRuns).toEqual([FULL])
  expect(w.sleeps).toBe(0)
  expect(w.files.get(LOCK)).toBe('')
  // the lock was written with the five fields while the command ran
  const wrote = w.writes.filter((p) => p === LOCK).length
  expect(wrote).toBeGreaterThanOrEqual(2)
})

test('A1: while the suite runs the lock names this lane, session, command and a heartbeat', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  await userPrompt($, header('MA1') + '\nbody')
  let release!: () => void
  w.toolHold = new Promise<void>((r) => (release = r))
  const call = $.tool.call(bash(FULL))
  await w.clock.settle()
  while (!w.toolRuns.length) await w.clock.advance(100)
  const rec = JSON.parse(w.files.get(LOCK) as string)
  expect(rec).toMatchObject({ lane: 'MA1', session: 'sess-me', command: FULL })
  expect(Object.keys(rec).sort()).toEqual(['command', 'heartbeat', 'lane', 'session', 'started_at'])
  release()
  await call
  expect(w.files.get(LOCK)).toBe('')
})

test('A1: a held lock holds the call, the band says who runs and for how long, then it goes on', { timeoutMs: 60000 }, async ($, on) => {
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  const w = world(on)
  put(w, LOCK, theirs(0))
  await startSession($)
  let go!: () => void
  w.sleepHold = new Promise<void>((r) => (go = r))
  w.onSleep = (n) => {
    if (n === 2) w.files.set(LOCK, '')
  }
  const call = $.tool.call(bash(FULL))
  while (w.sleeps < 1) await w.clock.advance(100)
  const ui = await $.ui.mount({
    plugin: 'jarvis-flow',
    component: 'AbovePrompt',
    requestId: 'band',
    surface: 'terminal',
    viewport: { columns: 140, rows: 30 },
    props: { hasSurvey: false, isWorking: false, maxRows: 3, bodyColumns: 140, scroll: { offset: 0, bodyRows: 3 }, view: {} },
  } as any)
  expect(await ui.find({ type: 'Text', text: 'Test kuyruğu: OTHER koşuyor, 0 dk' })).toBeDefined()
  await ui.unmount()
  expect(w.toolRuns).toEqual([])
  go()
  const res = await call
  expect(res.deny).toBeUndefined()
  expect(w.toolRuns).toEqual([FULL])
  expect(w.sleeps).toBeGreaterThanOrEqual(2)
  expect(w.files.get(LOCK)).toBe('')
})

test('A1: after 40 minutes the call is refused with a reason that says wait, then retry', { timeoutMs: 180000 }, async ($, on) => {
  const w = world(on)
  put(w, LOCK, theirs(0))
  await startSession($)
  // the other lane keeps its heartbeat fresh the whole time
  w.onSleep = () => {
    const rec = JSON.parse(w.files.get(LOCK) as string)
    w.files.set(LOCK, JSON.stringify({ ...rec, heartbeat: iso(w.clock.now()) }))
  }
  const res = await $.tool.call(bash(FULL))
  expect(w.toolRuns).toEqual([])
  expect(res.deny).toContain("lane 'OTHER'")
  expect(res.deny).toContain('wait')
  expect(res.deny).toContain('retry')
  expect(w.clock.now()).toBeGreaterThanOrEqual(40 * MIN)
  // the other lane's lock was not touched
  expect(JSON.parse(w.files.get(LOCK) as string).session).toBe('sess-other')
})

const staleCase = async ($: any, on: any, text: string, nowMs: number) => {
  const w = world(on)
  await startSession($)
  await w.clock.set(nowMs)
  put(w, LOCK, text)
  const res = await $.tool.call(bash(FULL))
  expect(res.deny).toBeUndefined()
  expect(w.sleeps).toBe(0)
  expect(w.toolRuns).toEqual([FULL])
  expect(w.files.get(LOCK)).toBe('')
}

test('A1: a lock with no heartbeat for 45 min is stale and taken over at once', { timeoutMs: 60000 }, async ($, on) => {
  await staleCase($, on, theirs(0), 60 * MIN)
})

test('A1: a lock with a forged future heartbeat is stale too', { timeoutMs: 60000 }, async ($, on) => {
  await staleCase($, on, theirs(10 * 3600 * 1000), 60 * MIN)
})

test('A1: a garbage lock file is stale', { timeoutMs: 60000 }, async ($, on) => {
  await staleCase($, on, 'not json at all', 60 * MIN)
})

test('A1: a lock whose holder session is mine is never held (the holder runs proofs alongside)', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  let release!: () => void
  w.toolHold = new Promise<void>((r) => (release = r))
  const first = $.tool.call(bash(FULL))
  while (!w.toolRuns.length) await w.clock.advance(100)
  w.toolHold = null
  // the same session starts a UI unittest and a second full suite: neither waits
  const ui = await $.tool.call(bash('python -m unittest tests.test_ui'))
  expect(ui.deny).toBeUndefined()
  // a second full suite of the same session is refused (it would load the machine twice), nothing waits
  const second = await $.tool.call(bash(FULL))
  expect(second.deny).toContain('own full suite is still running')
  expect(w.sleeps).toBe(0)
  expect(w.toolRuns).toEqual([FULL, 'python -m unittest tests.test_ui'])
  // the lock stays until the holder's suite ends
  expect(JSON.parse(w.files.get(LOCK) as string).session).toBe('sess-me')
  release()
  await first
  expect(w.files.get(LOCK)).toBe('')
})

test('A1: while another session holds a full suite, a unittest that loads UI tests waits, one that does not does not', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  put(w, LOCK, theirs(0))
  await startSession($)
  const plainRun = await $.tool.call(bash('python -m unittest tests.test_intent'))
  expect(plainRun.deny).toBeUndefined()
  expect(w.sleeps).toBe(0)
  w.onSleep = (n) => {
    if (n === 3) w.files.set(LOCK, '')
  }
  const uiRun = await $.tool.call(bash('python -m unittest tests.test_ui'))
  expect(uiRun.deny).toBeUndefined()
  expect(w.sleeps).toBe(3)
  // a UI unittest never takes the lock itself
  expect(w.files.get(LOCK)).toBe('')
  expect(w.toolRuns).toEqual(['python -m unittest tests.test_intent', 'python -m unittest tests.test_ui'])
})

test('A1: the lock is released when the tool errors or throws', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  w.toolError = true
  w.toolText = 'Traceback ...'
  await $.tool.call(bash(FULL))
  expect(w.files.get(LOCK)).toBe('')
  w.toolError = false
  w.toolThrow = true
  let threw = false
  try {
    await $.tool.call(bash(FULL))
  } catch {
    threw = true
  }
  expect(threw || w.files.get(LOCK) === '').toBe(true)
  expect(w.files.get(LOCK)).toBe('')
})

test('A1: session end frees a lock that is still held', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  let release!: () => void
  w.toolHold = new Promise<void>((r) => (release = r))
  const call = $.tool.call(bash(FULL))
  while (!w.toolRuns.length) await w.clock.advance(100)
  expect(JSON.parse(w.files.get(LOCK) as string).session).toBe('sess-me')
  await $.session.end({ reason: 'other', sessionId: 'sess-me', resume: { sessionId: 'sess-me' } } as any)
  expect(w.files.get(LOCK)).toBe('')
  release()
  await call
})

test('A1: the heartbeat moves while the suite runs', { timeoutMs: 120000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  let release!: () => void
  w.toolHold = new Promise<void>((r) => (release = r))
  const call = $.tool.call(bash(FULL))
  while (!w.toolRuns.length) await w.clock.advance(100)
  const first = JSON.parse(w.files.get(LOCK) as string).heartbeat
  await w.clock.advance(3 * MIN)
  const later = JSON.parse(w.files.get(LOCK) as string)
  expect(Date.parse(later.heartbeat)).toBeGreaterThan(Date.parse(first))
  expect(later.started_at).toBe(iso(0))
  release()
  await call
})

test('A1: a background full suite keeps the lock until its processes are gone', { timeoutMs: 120000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  w.suiteProcs = 2
  const res = await $.tool.call(bash(FULL, { run_in_background: true }))
  expect(res.deny).toBeUndefined()
  expect(JSON.parse(w.files.get(LOCK) as string).session).toBe('sess-me')
  await w.clock.advance(5 * MIN)
  expect(JSON.parse(w.files.get(LOCK) as string).session).toBe('sess-me')
  w.suiteProcs = 0
  await w.clock.advance(31 * 1000)
  expect(w.files.get(LOCK)).not.toBe('')
  await w.clock.advance(31 * 1000)
  expect(w.files.get(LOCK)).toBe('')
})

test('A1: the fail-closed handler covers full suites and UI unittest runs, and nothing else', { timeoutMs: 60000 }, async () => {
  expect(isGated(classifyCommand(FULL))).toBe(true)
  expect(isGated(classifyCommand('python -m unittest tests.test_ui'))).toBe(true)
  expect(isGated(classifyCommand('python -m unittest tests.test_intent'))).toBe(false)
  expect(isGated(classifyCommand('git status'))).toBe(false)
})

test('A2: a full suite and a targeted run are written to the flake ledger', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  await userPrompt($, header('MA1') + '\nbody')
  w.toolText = 'Ran 4750 tests in 445.1s wall (skipped=3, failures=1, errors=0)\nERROR: test_x (tests.test_ui.Ui.test_x)\nFAILED (errors=1)\n'
  await $.tool.call(bash(FULL))
  w.toolText = 'test_a ... ok\nFAIL: test_sub (tests.test_m.C.test_sub) (i=1)\n----\nRan 5 tests in 1.2s\n\nFAILED (failures=1)\n'
  await $.tool.call(bash('python -m unittest tests.test_m'))
  w.toolText = 'Ran 1 test in 0.1s\n\nOK\n'
  await $.tool.call(bash('python -m unittest tests.test_ui.Ui.test_x'))
  const rows = flakes(w)
  expect(rows.length).toBe(3)
  expect(rows[0]).toMatchObject({ lane: 'MA1', kind: 'full', ran: 4750, ok: false, failures: ['tests.test_ui.Ui.test_x'], wall_s: 445.1, python_procs: 3, other_lane_held: false })
  expect(rows[1]).toMatchObject({ kind: 'targeted', targets: ['tests.test_m'], failures: ['tests.test_m.C.test_sub (i=1)'], ran: 5 })
  expect(rows[2]).toMatchObject({ kind: 'alone', targets: ['tests.test_ui.Ui.test_x'], ok: true, failures: [] })
  expect(typeof rows[0].time).toBe('string')
})

test('A2: a run that had to wait for another lane says so, and a non-test command writes nothing', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  put(w, LOCK, theirs(0))
  await startSession($)
  w.onSleep = (n) => {
    if (n === 1) w.files.set(LOCK, '')
  }
  w.toolText = 'Ran 2 tests in 0.2s\n\nOK\n'
  await $.tool.call(bash(FULL))
  await $.tool.call(bash('git status'))
  const rows = flakes(w)
  expect(rows.length).toBe(1)
  expect(rows[0].other_lane_held).toBe(true)
})

test('A2: the result of a background run is read from the later tool result', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  w.suiteProcs = 0
  await $.tool.call(bash(FULL, { run_in_background: true }))
  expect(flakes(w).length).toBe(0)
  w.toolText = 'Ran 4750 tests in 400.0s wall (skipped=0, failures=0, errors=0)\nOK\n'
  await $.tool.call({ tool: 'Read', file_path: '/tmp/out.txt' } as any)
  const rows = flakes(w)
  expect(rows.length).toBe(1)
  expect(rows[0]).toMatchObject({ kind: 'full', ran: 4750, ok: true })
})

test('A2: /flaky counts fails under load against passes alone, with the last time seen', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  const row = (time: string, kind: string, failures: string[], targets: string[] = [], ok = failures.length === 0) =>
    JSON.stringify({ time, lane: 'L', kind, targets, ran: 1, failures, ok, wall_s: 1, python_procs: 1, other_lane_held: false })
  put(
    w,
    '/repo/build/flakes/ledger.jsonl',
    [
      row('2026-10-07T10:00:00.000Z', 'full', ['tests.test_ui.Ui.test_x', 'tests.test_a.A.test_y']),
      row('2026-10-08T11:30:00.000Z', 'full', ['tests.test_ui.Ui.test_x']),
      row('2026-10-08T11:40:00.000Z', 'alone', [], ['tests.test_ui.Ui.test_x']),
      row('2026-10-08T11:45:00.000Z', 'alone', [], ['tests.test_ui.Ui.test_x']),
      'torn line {',
    ].join('\n') + '\n',
  )
  await startSession($)
  const out = await $.command.run({ command: 'flaky', args: '' })
  expect(out.text).toContain('tests.test_ui.Ui.test_x')
  expect(out.text).toContain('yükte 2 kez düştü · tek başına 2 geçti, 0 düştü · son: 2026-10-08 11:30')
  expect(out.text).toContain('tests.test_a.A.test_y')
  expect(out.text).toContain('yükte 1 kez düştü · tek başına 0 geçti, 0 düştü · son: 2026-10-07 10:00')
})

test('A1: a lock file that cannot be read for a minute refuses the call at once, naming the file', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  put(w, LOCK, 'x')
  w.unreadable.add(LOCK)
  await startSession($)
  const res = await $.tool.call(bash(FULL))
  expect(res.deny).toContain('build/locks/suite.json cannot be read')
  expect(w.clock.now()).toBeLessThan(5 * MIN)
  expect(w.toolRuns).toEqual([])
})

test('A1: a release that meets a busy lock file retries, and the tick finishes it if the retries fail', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  w.casFail = 2
  await $.tool.call(bash(FULL))
  // two failed frees, then it went through
  expect(w.files.get(LOCK)).toBe('')
  // every free fails: the record stays, then the next ticks free it
  w.casFail = 50
  await $.tool.call(bash(FULL))
  expect(JSON.parse(w.files.get(LOCK) as string).session).toBe('sess-me')
  w.casFail = 0
  await w.clock.advance(11 * 1000)
  expect(w.files.get(LOCK)).toBe('')
})

test('A1: session end frees a lock record of this session even when nothing is held in memory', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  put(w, LOCK, theirs(0, { session: 'sess-me' }))
  await $.session.end({ reason: 'other', sessionId: 'sess-me', resume: { sessionId: 'sess-me' } } as any)
  expect(w.files.get(LOCK)).toBe('')
  // another session's record is never freed by this session
  put(w, LOCK, theirs(0))
  await $.session.end({ reason: 'other', sessionId: 'sess-me', resume: { sessionId: 'sess-me' } } as any)
  expect(JSON.parse(w.files.get(LOCK) as string).session).toBe('sess-other')
})

test('A1: parallel calls of one session cannot take the lock twice', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  let release!: () => void
  w.toolHold = new Promise<void>((r) => (release = r))
  const a = $.tool.call(bash(FULL))
  const b = $.tool.call(bash(FULL))
  while (w.toolRuns.length < 1) await w.clock.advance(100)
  await w.clock.advance(1000)
  release()
  const results = await Promise.all([a, b])
  expect(results.filter((r: any) => r.deny !== undefined).length).toBe(1)
  expect(w.toolRuns.length).toBe(1)
  expect(w.files.get(LOCK)).toBe('')
})

test('pure: PowerShell and shell spellings of a run are still runs, and readers of the runner are not', { timeoutMs: 60000 }, async () => {
  const k = (c: string) => classifyCommand(c)
  for (const c of [
    '$out = python scripts\\test_parallel.py 2>&1',
    'Push-Location C:\\x; try { python scripts\\test_parallel.py } finally { Pop-Location }',
    'if ($ok) { python scripts\\test_parallel.py }',
    '$py = ".venv\\Scripts\\python.exe"; & $py scripts\\test_parallel.py',
    '$p = Start-Process python "scripts\\test_parallel.py" -PassThru -Wait',
  ]) expect(k(c), c).toMatchObject({ kind: 'full', lock: true })
  expect(k('foreach ($m in $mods) { python -m unittest $m }')).toMatchObject({ ui: true })
  expect(k('$res = python -m unittest tests.test_ui 2>&1')).toMatchObject({ ui: true, kind: 'targeted' })
  // not runs: checkers, help, readers, other uses of the word unittest
  for (const c of [
    'python -m py_compile scripts/test_parallel.py',
    'ruff check scripts/test_parallel.py',
    'python scripts/test_parallel.py --help',
    'powershell -Command "Get-Content scripts\\test.ps1"',
    'py -m pip show unittest-xml',
    'python -c "import unittest; print(1)"',
    'python -m unittest --help',
  ]) expect(k(c), c).toBeNull()
  // redirects are not test targets
  expect(k('python -m unittest tests.test_x.C.test_y > build/out.log')).toMatchObject({ kind: 'alone', targets: ['tests.test_x.C.test_y'] })
  expect(k('python -m unittest tests.test_x 2>&1')).toMatchObject({ kind: 'targeted', targets: ['tests.test_x'] })
  // a run that goes on after the tool call returns
  expect(k('python scripts/test_parallel.py &')).toMatchObject({ background: true })
  expect(k('nohup python scripts/test_parallel.py > o.log 2>&1 &')).toMatchObject({ background: true })
  expect(k('Start-Process python "scripts\\test_parallel.py"')).toMatchObject({ background: true })
  expect(k('Start-Job { python scripts\\test_parallel.py }')).toMatchObject({ background: true })
  expect(k('python scripts/test_parallel.py')).toMatchObject({ background: false })
})

test('A1: a /clear keeps the lock of a suite that is still running; a real exit frees it', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  w.suiteProcs = 2
  await $.tool.call(bash(FULL, { run_in_background: true }))
  await $.session.end({ reason: 'clear', sessionId: 'sess-me', resume: { sessionId: 'sess-me' } } as any)
  expect(JSON.parse(w.files.get(LOCK) as string).session).toBe('sess-me')
  await $.session.end({ reason: 'prompt_input_exit', sessionId: 'sess-me', resume: { sessionId: 'sess-me' } } as any)
  expect(w.files.get(LOCK)).toBe('')
})

test('A1: after a reload of the mod a lock of this session is kept while its suite runs and freed when it does not', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  put(w, LOCK, theirs(0, { session: 'sess-me' }))
  w.suiteProcs = 1
  await startSession($)
  expect(JSON.parse(w.files.get(LOCK) as string).session).toBe('sess-me')
})

test('A1: after a reload of the mod a lock of this session whose suite is gone is freed', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  put(w, LOCK, theirs(0, { session: 'sess-me' }))
  w.suiteProcs = 0
  await startSession($)
  expect(w.files.get(LOCK)).toBe('')
})

test('A1: a suite lock that is ours again after a release gets a fresh heartbeat at once', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  await w.clock.set(50 * MIN)
  put(w, LOCK, theirs(0, { session: 'sess-me' }))
  await $.tool.call(bash(FULL))
  expect(w.toolRuns).toEqual([FULL])
  expect(w.files.get(LOCK)).toBe('')
})
