import { expect, test } from 'claude-code/testing'
import { dayOf, defterText, flakyText, isFlakeRow, isSliceRow, parseRows, statusLineOf, statusWord, usageOf } from '../hooks/lib/ledger.ts'
import { OPUS, SONNET, header, put, startSession, statusOf, userPrompt, world } from './world.ts'

const SLICES = '/repo/build/ledger/slices.jsonl'
const rowsOf = (w: any) =>
  String(w.files.get(SLICES) ?? '')
    .split('\n')
    .filter(Boolean)
    .map((l) => JSON.parse(l))
const usage = { input_tokens: 1000, output_tokens: 200, cache_read_input_tokens: 5000, cache_creation_input_tokens: 300, model: SONNET }
const done = (turnId: string, answer: string, extra: Record<string, unknown> = {}) =>
  ({ turnId, answer, durationMs: 90000, isAborted: false, reason: 'answer', usage, ...extra }) as any

test('pure: usage, status lines and the jsonl reader', { timeoutMs: 60000 }, async () => {
  expect(usageOf(usage)).toEqual({ input: 1000, output: 200, cache_read: 5000, cache_creation: 300 })
  expect(usageOf(undefined)).toBeNull()
  expect(statusLineOf('a\nSTATUS: PASS - all green\nb')).toBe('STATUS: PASS - all green')
  expect(statusLineOf('no status')).toBeNull()
  expect(statusWord('x\nSTATUS: blocked - why')).toBe('BLOCKED')
  expect(parseRows('{"a":1}\n\nnot json\nnull\n{"a":2}\n', () => true)).toEqual([{ a: 1 }, { a: 2 }])
  expect(parseRows('null\n{"time":"t","kind":"full","failures":["a"]}\n{"time":"t","kind":"full","failures":[1]}\n{"start":"x"}\n', isFlakeRow).length).toBe(1)
  expect(isSliceRow({ start: 'x' })).toBe(false)
  expect(flakyText([])).toContain('yok')
  expect(defterText([], '2026-10-08')).toContain('kayıt yok')
})

test('A4: a header turn writes one ledger row with times, usage, plan percentages, tool calls, tests and STATUS', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  await w.clock.set(1000 * 1000)
  await userPrompt($, header('MA1', 'sonnet', 'high') + '\nbody')
  w.five = 31
  w.seven = 10
  await $.turn.start({ text: 'x', turnId: 't1' })
  await $.tool.call({ tool: 'Read', file_path: '/repo/a.txt' } as any)
  await $.tool.call({ tool: 'Bash', command: 'git status' })
  w.toolText = 'Ran 2 tests in 0.1s\n\nOK\n'
  await $.tool.call({ tool: 'Bash', command: 'python -m unittest tests.test_m' })
  w.five = 35
  w.seven = 11
  await w.clock.advance(90 * 1000)
  await $.turn.complete(done('t1', 'özet\nSTATUS: PASS - tamam'))
  const rows = rowsOf(w)
  expect(rows.length).toBe(1)
  expect(rows[0]).toMatchObject({
    start: new Date(1000 * 1000).toISOString(),
    end: new Date(1090 * 1000).toISOString(),
    wall_s: 90,
    lane: 'MA1',
    model: SONNET,
    tokens: { input: 1000, output: 200, cache_read: 5000, cache_creation: 300 },
    five_hour: { start: 31, end: 35 },
    seven_day: { start: 10, end: 11 },
    tool_calls: 3,
    status: 'STATUS: PASS - tamam',
  })
  expect(rows[0].test_runs).toEqual([{ kind: 'targeted', ran: 2, failures: 0, python_procs: 3, wall_s: 0.1 }])
  expect(Object.keys(rows[0]).sort()).toEqual(
    ['effort', 'end', 'five_hour', 'lane', 'model', 'seven_day', 'start', 'status', 'test_runs', 'tokens', 'tool_calls', 'wall_s'].sort(),
  )
})

test('A4: the python process count at the start of a full suite is in the row', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  await userPrompt($, header('MA1') + '\nbody')
  await $.turn.start({ text: 'x', turnId: 't1' })
  w.pyProcs = 7
  w.toolText = 'Ran 4750 tests in 445.1s wall (skipped=0, failures=0, errors=0)\nOK\n'
  await $.tool.call({ tool: 'Bash', command: 'python scripts\\test_parallel.py' })
  await $.turn.complete(done('t1', 'STATUS: PASS'))
  expect(rowsOf(w)[0].test_runs[0]).toMatchObject({ kind: 'full', ran: 4750, python_procs: 7 })
})

test('A4: a turn that no header prompt started writes no row, and a missing STATUS line is null', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  await $.turn.start({ text: 'plain', turnId: 't1' })
  await $.turn.complete(done('t1', 'hello'))
  expect(rowsOf(w).length).toBe(0)
  await userPrompt($, header('MA1') + '\nbody')
  await $.turn.start({ text: 'x', turnId: 't2' })
  await $.turn.complete(done('t2', 'no status line here', { usage: undefined }))
  const rows = rowsOf(w)
  expect(rows.length).toBe(1)
  expect(rows[0].status).toBeNull()
  expect(rows[0].tokens).toBeNull()
  // the next plain turn is not mistaken for the header turn
  await $.turn.start({ text: 'again', turnId: 't3' })
  await $.turn.complete(done('t3', 'STATUS: PASS'))
  expect(rowsOf(w).length).toBe(1)
})

test('A4: a failing ledger append never breaks the turn', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  await userPrompt($, header('MA1') + '\nbody')
  await $.turn.start({ text: 'x', turnId: 't1' })
  w.failWrites = true
  const out = await $.turn.complete(done('t1', 'STATUS: PASS'))
  expect(out).toBeDefined()
})

test("A4: /defter prints today's rows and leaves other days out", async ($, on) => {
  const w = world(on)
  const row = (start: string, lane: string) =>
    JSON.stringify({
      start,
      end: start,
      wall_s: 600,
      lane,
      model: SONNET,
      effort: 'high',
      tokens: { input: 1000, output: 200, cache_read: 5000, cache_creation: 300 },
      five_hour: { start: 31, end: 35 },
      seven_day: { start: 10, end: 11 },
      tool_calls: 42,
      test_runs: [{ kind: 'full', ran: 1, failures: 0, python_procs: 3, wall_s: 1 }],
      status: 'STATUS: PASS',
    })
  const today = new Date(0).toISOString()
  put(w, SLICES, [row('1969-12-20T10:00:00.000Z', 'OLD'), row(today, 'TODAY')].join('\n') + '\n')
  await startSession($)
  expect(dayOf(0)).toBe(dayOf(Date.parse(today)))
  const out = await $.command.run({ command: 'defter', args: '' })
  expect(out.text).toContain('Bugün 1 dilim')
  expect(out.text).toContain('TODAY')
  expect(out.text).not.toContain('OLD')
  expect(out.text).toContain('10 dk')
  expect(out.text).toContain('7k tok')
  expect(out.text).toContain('plan %31→35')
  expect(out.text).toContain('7g %10→11')
  expect(out.text).toContain('araç 42')
  expect(out.text).toContain('test 1')
  expect(out.text).toContain('STATUS: PASS')
})

test('band fix: a typed /model changes the chat family, the status file and the queue folder', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  put(w, '/repo/build/queue/opus/op1.md', header('OP1', 'opus', 'high'))
  await startSession($)
  expect(statusOf(w, 'sonnet')).toMatchObject({ family: 'sonnet' })
  w.model = OPUS
  await $.command.run({ command: 'model', args: 'opus' })
  expect(statusOf(w, 'opus')).toMatchObject({ family: 'opus', model: OPUS })
  expect(w.toasts).toContain('Sohbet artık OPUS')
  const list = await $.command.run({ command: 'kuyruk', args: '' })
  expect(list.text).toContain('OP1')
})

test('band fix: the model picker and the SDK move the family, the automatic fallback does not', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  const switchTo = (to: string, source: string) =>
    $.classic.PostModelSwitch({
      hook_event_name: 'PostModelSwitch',
      from_model: SONNET,
      to_model: to,
      requested_model: null,
      source,
      context_tokens: 0,
      prompt_cache_warm: false,
      cache_ttl: '5m',
      estimated_cache_write_usd: 0,
    } as any)
  w.model = OPUS
  await switchTo(OPUS, 'auto')
  expect(statusOf(w, 'opus')).toBeNull()
  await switchTo(OPUS, 'picker')
  expect(statusOf(w, 'opus')).toMatchObject({ family: 'opus' })
})

test('band fix: a user prompt after an automatic model change keeps the start family', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  w.model = OPUS
  await userPrompt($, 'just chatting')
  expect(statusOf(w, 'opus')).toBeNull()
  put(w, '/repo/build/queue/sonnet/s1.md', header('S1', 'sonnet', 'high'))
  await w.clock.advance(6000)
  const list = await $.command.run({ command: 'kuyruk', args: '' })
  // the sonnet folder is still the one scanned (the lint then judges the file against the chat's model)
  expect(list.text).toContain('s1.md')
})
