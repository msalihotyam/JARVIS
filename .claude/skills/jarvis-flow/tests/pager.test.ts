import { expect, test } from 'claude-code/testing'
import { curlArgv, curlConfig, isBadStatus, nextWaitPush, pushText, validTopic } from '../hooks/lib/pager.ts'
import { header, startSession, userPrompt, world } from './world.ts'

const TOPIC = 'jarvis-Secret_topic42'
const MIN = 60 * 1000
const done = (turnId: string, answer: string) => ({ turnId, answer, durationMs: 5, isAborted: false, reason: 'answer', usage: null }) as any
const bat = (pct: number, status = 1) => JSON.stringify({ EstimatedChargeRemaining: pct, BatteryStatus: status })

// one header turn that ends with the given final text
async function headerTurn($: any, answer: string, id = 't1') {
  await userPrompt($, header('MA1') + '\nbody')
  await $.turn.start({ text: 'x', turnId: id })
  await $.turn.complete(done(id, answer))
}

// nothing the mod wrote, showed or logged may hold the topic
function expectNoLeak(w: any, topic = TOPIC) {
  for (const [path, text] of w.files) expect(path + '\n' + text, path).not.toContain(topic)
  for (const toast of w.toasts) expect(toast).not.toContain(topic)
  for (const run of w.runs) expect(run.join(' ')).not.toContain(topic)
}

test('pure: topic names, the fixed curl argument list and the quoted config', { timeoutMs: 60000 }, async () => {
  expect(validTopic('jarvis-abc_123')).toBe(true)
  for (const bad of ['', 'a/b', 'a b', 'a?x=1', 'a@b', '../x', 'x'.repeat(65), undefined, 5]) expect(validTopic(bad as any)).toBe(false)
  expect(curlArgv(undefined)).toEqual(['C:\\Windows\\System32\\curl.exe', '-q', '-sS', '--max-time', '10', '-K', '-'])
  expect(curlArgv('D:\\WINNT\\')[0]).toBe('D:\\WINNT\\System32\\curl.exe')
  expect(curlArgv('x; evil')[0]).toBe('C:\\Windows\\System32\\curl.exe')
  const cfg = curlConfig('abc', 'say "hi"\\ now\nline2')
  expect(cfg).toContain('url = "https://ntfy.sh/abc"')
  expect(cfg).toContain('data-binary = "say \\"hi\\"\\\\ now\\nline2"')
  expect(cfg).not.toContain('Priority')
  expect(curlConfig('abc', 'x', true)).toContain('header = "Priority: 2"')
  expect(isBadStatus('STOP')).toBe(true)
  expect(isBadStatus('BLOCKED')).toBe(true)
  expect(isBadStatus('FAIL')).toBe(true)
  expect(isBadStatus('PASS')).toBe(false)
  expect(isBadStatus(null)).toBe(false)
  expect(nextWaitPush(null, null)).toBeNull()
  expect(nextWaitPush(1000, null)).toBe(1000 + 5 * MIN)
  expect(nextWaitPush(1000, 9000)).toBe(9000 + 30 * MIN)
  expect(pushText('wait', 'MA1', 0, '')).toMatch(/^JARVIS MA1: izin bekliyor, \d\d:\d\d'den beri$/)
})

test('A3: a header turn that ends STATUS: BLOCKED pushes once; the topic is only in the stdin config', { timeoutMs: 120000 }, async ($, on) => {
  const w = world(on, { env: { JARVIS_NTFY_TOPIC: TOPIC } })
  await startSession($)
  await headerTurn($, 'raporu yazdım\nSTATUS: BLOCKED - test düştü')
  expect(w.curls.length).toBe(1)
  const call = w.curls[0] as { argv: string[]; stdin: string }
  expect(call.argv).toEqual(curlArgv(undefined))
  expect(call.argv.join(' ')).not.toContain(TOPIC)
  expect(call.stdin).toContain('url = "https://ntfy.sh/' + TOPIC + '"')
  expect(call.stdin).toContain('JARVIS MA1: STATUS: BLOCKED')
  expectNoLeak(w)
})

test('A3: STOP and FAIL push too, PASS does not, and a turn without a header does not', { timeoutMs: 120000 }, async ($, on) => {
  const w = world(on, { env: { JARVIS_NTFY_TOPIC: TOPIC } })
  await startSession($)
  await headerTurn($, 'x\nSTATUS: PASS', 't1')
  expect(w.curls.length).toBe(0)
  await $.turn.start({ text: 'plain chat', turnId: 't2' })
  await $.turn.complete(done('t2', 'STATUS: FAIL'))
  expect(w.curls.length).toBe(0)
  await headerTurn($, 'x\nSTATUS: FAIL', 't3')
  expect(w.curls.length).toBe(1)
  await w.clock.advance(11 * MIN)
  await headerTurn($, 'x\nSTATUS: STOP', 't4')
  expect(w.curls.length).toBe(2)
  expect(w.curls[1]?.stdin).toContain('STATUS: STOP')
  expectNoLeak(w)
})

test('A3: at most one push per 10 minutes per session', { timeoutMs: 120000 }, async ($, on) => {
  const w = world(on, { env: { JARVIS_NTFY_TOPIC: TOPIC } })
  await startSession($)
  await headerTurn($, 'STATUS: FAIL', 't1')
  await w.clock.advance(9 * MIN)
  await headerTurn($, 'STATUS: FAIL', 't2')
  expect(w.curls.length).toBe(1)
  await w.clock.advance(2 * MIN)
  await headerTurn($, 'STATUS: FAIL', 't3')
  expect(w.curls.length).toBe(2)
})

test('A3: no topic means no network and exactly one toast, however often it would push', { timeoutMs: 120000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  await headerTurn($, 'STATUS: FAIL', 't1')
  await w.clock.advance(11 * MIN)
  await headerTurn($, 'STATUS: BLOCKED', 't2')
  await w.clock.advance(11 * MIN)
  await headerTurn($, 'STATUS: STOP', 't3')
  expect(w.curls.length).toBe(0)
  expect(w.toasts.filter((t) => t.includes('JARVIS_NTFY_TOPIC')).length).toBe(1)
})

test('A3: a topic that could change the URL is refused like a missing one', { timeoutMs: 120000 }, async ($, on) => {
  const w = world(on, { env: { JARVIS_NTFY_TOPIC: 'evil/../topic?x=1' } })
  await startSession($)
  await headerTurn($, 'STATUS: FAIL', 't1')
  expect(w.curls.length).toBe(0)
  expect(w.toasts.filter((t) => t.includes('JARVIS_NTFY_TOPIC')).length).toBe(1)
  expectNoLeak(w, 'evil')
})

test('A3: a failing curl toasts once and never shows the topic', { timeoutMs: 120000 }, async ($, on) => {
  const w = world(on, { env: { JARVIS_NTFY_TOPIC: TOPIC } })
  w.curlExit = 6
  await startSession($)
  await headerTurn($, 'STATUS: FAIL', 't1')
  await w.clock.advance(11 * MIN)
  await headerTurn($, 'STATUS: FAIL', 't2')
  expect(w.toasts.filter((t) => t.startsWith('Pager gönderemedi')).length).toBe(1)
  expectNoLeak(w)
})

test('A3: a permission prompt pushes after 5 minutes, then every 30 minutes while it still waits', { timeoutMs: 120000 }, async ($, on) => {
  const w = world(on, { env: { JARVIS_NTFY_TOPIC: TOPIC } })
  await startSession($)
  await userPrompt($, header('MA1') + '\nbody')
  await $.turn.start({ text: 'x', turnId: 't1' })
  await $.classic.PermissionRequest({ hook_event_name: 'PermissionRequest', tool_name: 'Bash', tool_input: {} } as any)
  await w.clock.advance(4 * MIN + 50 * 1000)
  expect(w.curls.length).toBe(0)
  await w.clock.advance(20 * 1000)
  expect(w.curls.length).toBe(1)
  expect(w.curls[0]?.stdin).toContain('izin bekliyor')
  expect(w.curls[0]?.stdin).toContain('JARVIS MA1')
  await w.clock.advance(28 * MIN)
  expect(w.curls.length).toBe(1)
  await w.clock.advance(3 * MIN)
  expect(w.curls.length).toBe(2)
  // the prompt is answered: the next tool call ends the wait and the pushes stop
  await $.tool.call({ tool: 'Bash', command: 'ls' })
  await w.clock.advance(70 * MIN)
  expect(w.curls.length).toBe(2)
  expectNoLeak(w)
})

test('A3: below 20% on battery pushes once; mains power or 20% and above does not', { timeoutMs: 120000 }, async ($, on) => {
  const w = world(on, { env: { JARVIS_NTFY_TOPIC: TOPIC }, battery: bat(15) })
  await startSession($)
  expect(w.curls.length).toBe(1)
  expect(w.curls[0]?.stdin).toContain('pil %15')
  // still low: no second push, even after the 10 minute gap
  await w.clock.advance(12 * MIN)
  expect(w.curls.length).toBe(1)
  // charging (status 6) then low again later
  w.battery = bat(30, 6)
  await w.clock.advance(3 * MIN)
  w.battery = bat(12)
  await w.clock.advance(3 * MIN)
  expect(w.curls.length).toBe(2)
  expectNoLeak(w)
})

test('A3: 20% and above on battery, or mains power, never pushes', { timeoutMs: 120000 }, async ($, on) => {
  const w = world(on, { env: { JARVIS_NTFY_TOPIC: TOPIC }, battery: bat(25) })
  await startSession($)
  await w.clock.advance(30 * MIN)
  expect(w.curls.length).toBe(0)
  w.battery = bat(5, 2)
  await w.clock.advance(5 * MIN)
  expect(w.curls.length).toBe(0)
})
