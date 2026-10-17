import { expect, test } from 'claude-code/testing'
import { extractReport, safeLane } from '../hooks/lib/report.ts'
import { canonicalRootOf, fingerprint, stateOf, worktreeOf } from '../hooks/lib/status.ts'
import { header, put, startSession, statusOf, userPrompt, world } from './world.ts'

const LANES = '/repo/build/lanes/sonnet.json'
const done = (turnId: string, answer = 'ok') => ({ turnId, answer, durationMs: 5, isAborted: false, reason: 'answer', usage: null }) as any
const written = (w: any, path: string) => w.writes.filter((p: string) => p === path).length

test('pure: state precedence', { timeoutMs: 60000 }, async () => {
  expect(stateOf({ busy: false, waiting: false, lowBattery: false })).toBe('idle')
  expect(stateOf({ busy: true, waiting: false, lowBattery: false })).toBe('working')
  expect(stateOf({ busy: true, waiting: true, lowBattery: false })).toBe('waiting')
  expect(stateOf({ busy: true, waiting: true, lowBattery: true })).toBe('low_battery')
})

test('pure: worktree and canonical root come from the cwd', { timeoutMs: 60000 }, async () => {
  expect(worktreeOf('C:\\Users\\x\\JARVIS\\.claude\\worktrees\\mods-flow1\\sub')).toBe('mods-flow1')
  expect(worktreeOf('C:/Users/x/JARVIS')).toBeNull()
  expect(canonicalRootOf('C:\\Users\\x\\JARVIS\\.claude\\worktrees\\mods-flow1\\sub')).toBe('C:/Users/x/JARVIS')
  expect(canonicalRootOf('C:/Users/x/JARVIS/')).toBe('C:/Users/x/JARVIS')
})

test('pure: the fingerprint ignores only the timestamp', { timeoutMs: 60000 }, async () => {
  const base: any = {
    family: 'sonnet', model: null, effort: null, lane: null, base: null, cwd: '/r', worktree: null, branch: null, head: null,
    state: 'idle', busy: false, updated_at: 'a', context_pct: null, power: { ac: true, pct: null }, paused: false, last_auto: null,
  }
  expect(fingerprint(base)).toBe(fingerprint({ ...base, updated_at: 'b' }))
  expect(fingerprint(base)).not.toBe(fingerprint({ ...base, state: 'working' }))
})

test('pure: report extraction and lane safety', { timeoutMs: 60000 }, async () => {
  expect(extractReport('text\nSTATUS: PASS\nmore')).toBe('STATUS: PASS\nmore')
  expect(extractReport('STATUS: PASS')).toBe('STATUS: PASS')
  expect(extractReport('no status here\n  STATUS: indented')).toBeNull()
  expect(extractReport('')).toBeNull()
  expect(safeLane('SB1')).toBe('SB1')
  expect(safeLane('../x')).toBeNull()
  expect(safeLane(null)).toBeNull()
})

test('the status file is written at start with every field', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  const status = statusOf(w, 'sonnet')
  expect(Object.keys(status).sort()).toEqual(
    ['base', 'branch', 'busy', 'context_pct', 'cwd', 'effort', 'family', 'head', 'lane', 'last_auto', 'model', 'paused', 'plan_pct', 'power', 'seven_pct', 'state', 'updated_at', 'worktree'].sort(),
  )
  expect(status).toMatchObject({
    family: 'sonnet',
    model: 'claude-sonnet-5-5',
    state: 'idle',
    cwd: '/repo',
    worktree: null,
    branch: 'worktree-demo',
    head: 'aaaaaaaaaaaa',
    context_pct: 12,
    plan_pct: 31,
    seven_pct: 10,
    power: { ac: true, pct: null },
    lane: null,
  })
})

test('the status file follows a turn: working, waiting on a permission, working, idle', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  await userPrompt($, header('SB1') + '\nbody')
  expect(statusOf(w, 'sonnet')).toMatchObject({ lane: 'SB1', base: 'main', state: 'idle' })
  await $.turn.start({ text: 'x', turnId: 't1' })
  expect(statusOf(w, 'sonnet').state).toBe('working')
  await $.classic.PermissionRequest({ hook_event_name: 'PermissionRequest', tool_name: 'Bash', tool_input: {} } as any)
  expect(statusOf(w, 'sonnet').state).toBe('waiting')
  await $.tool.call({ tool: 'Bash', command: 'ls' })
  expect(statusOf(w, 'sonnet').state).toBe('working')
  await $.turn.complete(done('t1'))
  expect(statusOf(w, 'sonnet')).toMatchObject({ state: 'idle', busy: false })
})

test('the status file is rewritten only on a real change', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  const first = written(w, LANES)
  expect(first).toBeGreaterThan(0)
  // many timer ticks later nothing changed, so nothing is written
  await w.clock.advance(5000)
  await w.clock.advance(5000)
  await w.clock.advance(120000)
  expect(written(w, LANES)).toBe(first)
  await $.turn.start({ text: 'x', turnId: 't1' })
  expect(written(w, LANES)).toBe(first + 1)
})

test('inside a worktree the status names it and the root stays canonical', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on, { cwd: '/repo/.claude/worktrees/mods-flow1' })
  await startSession($, '/repo/.claude/worktrees/mods-flow1')
  expect(statusOf(w, 'sonnet')).toMatchObject({ worktree: 'mods-flow1', cwd: '/repo/.claude/worktrees/mods-flow1' })
  expect(w.files.has(LANES)).toBe(true)
})

test('a final report is captured to build/reports/<lane>.md', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  await userPrompt($, header('SB1') + '\nbody')
  const report = 'work done\nSTATUS: PASS\nCOMMIT: abc'
  await $.turn.start({ text: 'x', turnId: 't1' })
  await $.turn.complete(done('t1', report))
  expect(w.files.get('/repo/build/reports/SB1.md')).toBe('STATUS: PASS\nCOMMIT: abc')
})

test('a turn without a STATUS line, or without a lane, writes no report', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  await $.turn.complete(done('t0', 'STATUS: PASS (but no lane is known)'))
  expect([...w.files.keys()].filter((k) => k.startsWith('/repo/build/reports/'))).toEqual([])
  await userPrompt($, header('SB1') + '\nbody')
  await $.turn.complete(done('t1', 'just chatting'))
  expect([...w.files.keys()].filter((k) => k.startsWith('/repo/build/reports/'))).toEqual([])
})

test('an existing report is kept as a timestamped copy before it is replaced', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  put(w, '/repo/build/reports/SB1.md', 'STATUS: FAIL\nold')
  await startSession($)
  // the old report was written before this turn started
  await w.clock.set(60000)
  await userPrompt($, header('SB1') + '\nbody')
  await $.turn.start({ text: 'x', turnId: 't1' })
  await $.turn.complete(done('t1', 'STATUS: PASS\nnew'))
  expect(w.files.get('/repo/build/reports/SB1.md')).toBe('STATUS: PASS\nnew')
  expect(w.files.get('/repo/build/reports/SB1.19700101-000100.md')).toBe('STATUS: FAIL\nold')
})

test('a report identical to the one on disk changes nothing', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  put(w, '/repo/build/reports/SB1.md', 'STATUS: PASS\nsame')
  await startSession($)
  await userPrompt($, header('SB1') + '\nbody')
  await $.turn.complete(done('t1', 'STATUS: PASS\nsame'))
  expect([...w.files.keys()].filter((k) => k.startsWith('/repo/build/reports/'))).toEqual(['/repo/build/reports/SB1.md'])
})

test('a failing status or report write never breaks the turn', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  await userPrompt($, header('SB1') + '\nbody')
  w.failWrites = true
  const out = await $.turn.complete(done('t1', 'STATUS: PASS'))
  expect(out).toBeDefined()
  expect(w.toasts.some((t) => t.startsWith('jarvis-flow: rapor/durum yazılamadı'))).toBe(true)
})

test('a STATUS line in a turn that no header prompt started is not captured under the old lane', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  await userPrompt($, header('SB1') + '\nbody')
  await $.turn.complete(done('t1', 'STATUS: PASS\nfirst'))
  await userPrompt($, 'a plain chat question')
  await $.turn.complete(done('t2', 'STATUS: PASS\nfrom a chat turn'))
  expect(w.files.get('/repo/build/reports/SB1.md')).toBe('STATUS: PASS\nfirst')
})

test('a fallback model on a request never moves the queue family', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  on('turn.step', async function* (_$: any, e: any) {
    return { turnId: e.turnId, index: e.index, answer: 'ok', toolUses: [], stopReason: 'end_turn', usage: null }
  })
  put(w, '/repo/build/queue/sonnet/sb1.md', header('SB1'))
  await startSession($)
  const stream = $.turn.step({ turnId: 't1', index: 0, model: 'claude-opus-5-5', effort: 'high', messageCount: 1 })
  let step = await stream.next()
  while (step.done !== true) step = await stream.next()
  await w.clock.advance(6000)
  expect(statusOf(w, 'sonnet')).toMatchObject({ family: 'sonnet' })
  expect(w.files.has('/repo/build/lanes/opus.json')).toBe(false)
  const list = await $.command.run({ command: 'kuyruk', args: '' })
  expect(list.text).toContain('SB1 · sb1.md')
})

const usageReads = (w: any) => w.calls.filter((c: string) => c === 'session.usage').length

test('the tick re-reads plan usage at most once every 5 minutes', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  const first = usageReads(w)
  expect(first).toBeGreaterThan(0)
  for (let i = 0; i < 10; i++) await w.clock.advance(5000)
  expect(usageReads(w)).toBe(first)
  await w.clock.advance(250000)
  expect(usageReads(w)).toBe(first + 1)
  for (let i = 0; i < 10; i++) await w.clock.advance(5000)
  expect(usageReads(w)).toBe(first + 1)
})

test('a changed plan_pct or seven_pct is written even when nothing else changed', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  const first = written(w, LANES)
  expect(statusOf(w, 'sonnet')).toMatchObject({ plan_pct: 31, seven_pct: 10 })
  w.five = 47.6
  await w.clock.advance(300000)
  expect(written(w, LANES)).toBe(first + 1)
  expect(statusOf(w, 'sonnet')).toMatchObject({ plan_pct: 48, seven_pct: 10 })
  w.seven = 12.2
  await w.clock.advance(300000)
  expect(written(w, LANES)).toBe(first + 2)
  expect(statusOf(w, 'sonnet')).toMatchObject({ plan_pct: 48, seven_pct: 12 })
  // same numbers next time: no rewrite
  await w.clock.advance(300000)
  expect(written(w, LANES)).toBe(first + 2)
})

test('usage unavailable on this surface keeps the last known values', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  const first = written(w, LANES)
  w.breakUsage = true
  w.five = 90
  await w.clock.advance(300000)
  expect(statusOf(w, 'sonnet')).toMatchObject({ plan_pct: 31, seven_pct: 10 })
  expect(written(w, LANES)).toBe(first)
  expect(w.toasts.some((t) => t.includes('hatası'))).toBe(false)
})

test('usage never available leaves plan_pct and seven_pct null', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  w.breakUsage = true
  await startSession($)
  await w.clock.advance(300000)
  expect(statusOf(w, 'sonnet')).toMatchObject({ plan_pct: null, seven_pct: null })
})
