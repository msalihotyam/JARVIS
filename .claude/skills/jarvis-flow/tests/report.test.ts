import { expect, test } from 'claude-code/testing'
import { extractReport, isFreshReport, reportDirs } from '../hooks/lib/report.ts'
import { header, put, startSession, userPrompt, world } from './world.ts'

const REPORT = '/repo/build/reports/SB1.md'
const done = (turnId: string, answer: string) => ({ turnId, answer, durationMs: 5, isAborted: false, reason: 'answer', usage: null }) as any
const reports = (w: any) => [...w.files.keys()].filter((k: string) => k.startsWith('/repo/build/reports/'))

// one header turn, started at `at` ms, that ends with the given final text
async function laneTurn($: any, w: any, answer: string, at = 60000) {
  await w.clock.set(at)
  await userPrompt($, header('SB1') + '\nbody')
  await $.turn.start({ text: 'x', turnId: 't1' })
  return $.turn.complete(done('t1', answer))
}

test('pure: only the block from the first STATUS line is the report, fences and preamble dropped', { timeoutMs: 60000 }, async () => {
  const fenced = 'Here is the report:\n\n```\nSTATUS: PASS\nCOMMIT: abc fix\nPUSHED: no\n```\n\nLet me know.'
  expect(extractReport(fenced)).toBe('STATUS: PASS\nCOMMIT: abc fix\nPUSHED: no')
  expect(extractReport('```text\r\nSTATUS: BLOCKED\r\nNEEDS FILES: a.py\r\n```')).toBe('STATUS: BLOCKED\nNEEDS FILES: a.py')
  expect(extractReport('pre\nSTATUS: CI_PENDING\nCI: run 1\n\n')).toBe('STATUS: CI_PENDING\nCI: run 1')
  expect(extractReport('STATUS: BLOCKED-with-patch (x)\nA: 1')).toBe('STATUS: BLOCKED-with-patch (x)\nA: 1')
  // the first STATUS line decides: a quoted dependency report after it is part of the block, not a second report
  expect(extractReport('STATUS: PASS\nX: 1\n```\nSTATUS: BLOCKED\n```')).toBe('STATUS: PASS\nX: 1')
})

test('pure: a STATUS word other than PASS, BLOCKED or CI_PENDING is no report', { timeoutMs: 60000 }, async () => {
  for (const bad of ['STATUS: FAIL', 'STATUS: STOP', 'STATUS:', 'STATUS: pass', 'STATUS: PASSED', 'STATUS: (interim) PASS', '**STATUS:** PASS', 'Done - STATUS: PASS'])
    expect(extractReport(bad), bad).toBeNull()
  // only the first STATUS line counts, even when a later one has a good word
  expect(extractReport('STATUS: FAIL\nSTATUS: PASS')).toBeNull()
})

test('pure: a fresh report is one modified since the turn start whose first non-empty line starts with STATUS:', { timeoutMs: 60000 }, async () => {
  expect(isFreshReport('\n\nSTATUS: PASS\nx', 100, 100)).toBe(true)
  expect(isFreshReport('STATUS: PASS', 99, 100)).toBe(false)
  expect(isFreshReport('```\nSTATUS: PASS\n```', 200, 100)).toBe(false)
  expect(isFreshReport('', 200, 100)).toBe(false)
  expect(isFreshReport('# Report\nSTATUS: PASS', 200, 100)).toBe(false)
})

test('pure: report folders are the canonical one and the session checkout\'s', { timeoutMs: 60000 }, async () => {
  expect(reportDirs('/repo', '/repo')).toEqual(['/repo/build/reports'])
  expect(reportDirs('/repo', '/repo/.claude/worktrees/SB1/.claude/skills/x')).toEqual(['/repo/build/reports', '/repo/.claude/worktrees/SB1/build/reports'])
  expect(reportDirs('C:/r', 'C:\\r\\.claude\\worktrees\\W\\')).toEqual(['C:/r/build/reports', 'C:/r/.claude/worktrees/W/build/reports'])
})

test('a chat answer with a fenced report is captured without the fence or the prose around it', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  await laneTurn($, w, 'Bitti.\n```\nSTATUS: PASS\nCOMMIT: abc\n```\nRapor yukarıda.')
  expect(w.files.get(REPORT)).toBe('STATUS: PASS\nCOMMIT: abc')
})

test('a report the lane wrote during the turn is never overwritten by the chat answer', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  await w.clock.set(60000)
  await userPrompt($, header('SB1') + '\nbody')
  await $.turn.start({ text: 'x', turnId: 't1' })
  const own = 'STATUS: PASS\nCOMMIT: abc full\nNEW MAIN SHA: def'
  w.files.set(REPORT, own)
  w.mtimes.set(REPORT, 70000)
  await $.turn.complete(done('t1', 'STATUS: PASS, see build/reports/SB1.md'))
  expect(w.files.get(REPORT)).toBe(own)
  expect(reports(w)).toEqual([REPORT])
})

test("a report the lane wrote in its worktree's build/reports blocks the capture too", { timeoutMs: 60000 }, async ($, on) => {
  const wt = '/repo/.claude/worktrees/SB1'
  const w = world(on, { cwd: wt })
  await startSession($, wt)
  await w.clock.set(60000)
  await userPrompt($, header('SB1') + '\nbody')
  await $.turn.start({ text: 'x', turnId: 't1' })
  w.files.set(wt + '/build/reports/SB1.md', 'STATUS: BLOCKED\nwhy')
  w.mtimes.set(wt + '/build/reports/SB1.md', 70000)
  await $.turn.complete(done('t1', 'STATUS: BLOCKED'))
  expect(w.files.has(REPORT)).toBe(false)
})

test('a report file from before the turn, or one that does not start with STATUS:, is replaced (old copy kept)', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  put(w, REPORT, 'STATUS: BLOCKED\nlast run')
  await startSession($)
  await laneTurn($, w, 'STATUS: PASS\nnew')
  expect(w.files.get(REPORT)).toBe('STATUS: PASS\nnew')
  expect(w.files.get('/repo/build/reports/SB1.19700101-000100.md')).toBe('STATUS: BLOCKED\nlast run')
})

test('a fresh file that is not a report (a draft) does not block the capture', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  await w.clock.set(60000)
  await userPrompt($, header('SB1') + '\nbody')
  await $.turn.start({ text: 'x', turnId: 't1' })
  w.files.set(REPORT, '# draft')
  w.mtimes.set(REPORT, 70000)
  await $.turn.complete(done('t1', 'STATUS: PASS\nok'))
  expect(w.files.get(REPORT)).toBe('STATUS: PASS\nok')
})

test('a final answer whose STATUS word is not final writes nothing', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  await laneTurn($, w, 'STATUS: FAIL\nboom')
  expect(reports(w)).toEqual([])
})
