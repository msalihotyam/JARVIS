import { expect, test } from 'claude-code/testing'
import { gate, isFreshWorking, parseBattery, STALE_MS } from '../hooks/lib/power.ts'
import { header, put, startSession, statusOf, world } from './world.ts'

const AUTO = '/repo/build/queue/sonnet/auto'
const PAUSE = '/repo/build/queue/PAUSE'
const bat = (pct: number, status = 1) => JSON.stringify({ EstimatedChargeRemaining: pct, BatteryStatus: status })
const workingOpus = JSON.stringify({ state: 'working', updated_at: new Date(0).toISOString() })

test('pure: parseBattery', async () => {
  expect(parseBattery('')).toEqual({ ac: true, pct: null })
  expect(parseBattery('not json')).toEqual({ ac: true, pct: null })
  expect(parseBattery(bat(62, 2))).toEqual({ ac: true, pct: 62 })
  expect(parseBattery(bat(62, 1))).toEqual({ ac: false, pct: 62 })
  expect(parseBattery(bat(18, 4))).toEqual({ ac: false, pct: 18 })
  expect(parseBattery(bat(80, 6))).toEqual({ ac: true, pct: 80 })
  expect(parseBattery('[' + bat(55, 1) + ']')).toEqual({ ac: false, pct: 55 })
  expect(parseBattery(bat(140, 2))).toEqual({ ac: true, pct: 100 })
})

test('pure: gate rules', async () => {
  const mains = { ac: true, pct: 100 }
  expect(gate({ power: mains, paused: false, otherLaneWorking: true }).allow).toBe(true)
  expect(gate({ power: mains, paused: true, otherLaneWorking: false })).toMatchObject({ allow: false, reason: 'MOLA açık (/devam yaz)' })
  expect(gate({ power: { ac: false, pct: 40 }, paused: false, otherLaneWorking: false }).allow).toBe(true)
  expect(gate({ power: { ac: false, pct: 40 }, paused: false, otherLaneWorking: true })).toMatchObject({
    allow: false,
    reason: 'pilde tek istem: başka hat çalışıyor',
  })
  expect(gate({ power: { ac: false, pct: 39 }, paused: false, otherLaneWorking: false })).toMatchObject({
    allow: false,
    batteryPaused: true,
    lowBattery: false,
  })
  expect(gate({ power: { ac: false, pct: 19 }, paused: false, otherLaneWorking: false })).toMatchObject({ allow: false, lowBattery: true })
  expect(gate({ power: { ac: true, pct: 5 }, paused: false, otherLaneWorking: false }).allow).toBe(true)
  expect(gate({ power: { ac: false, pct: null }, paused: false, otherLaneWorking: false }).allow).toBe(true)
})

test('pure: a stale working status is not believed', async () => {
  const at = new Date(0).toISOString()
  expect(isFreshWorking({ state: 'working', updated_at: at }, 1000)).toBe(true)
  expect(isFreshWorking({ state: 'working', updated_at: at }, STALE_MS + 1)).toBe(false)
  expect(isFreshWorking({ state: 'idle', updated_at: at }, 1000)).toBe(false)
  expect(isFreshWorking(null, 1000)).toBe(false)
})

test('auto: a file in auto/ starts by itself when idle and records last_auto', async ($, on) => {
  const w = world(on)
  const body = header('AUTO1') + '\nrun it'
  put(w, AUTO + '/a1.md', body)
  await startSession($)
  await w.clock.advance(100)
  expect(w.submitted).toEqual([body])
  expect(w.files.has(AUTO + '/a1.md')).toBe(false)
  expect(w.files.has(AUTO + '/done/19700101-000000-a1.md')).toBe(true)
  expect(w.toasts.some((t) => t.startsWith('Otomatik başladı: AUTO1'))).toBe(true)
  const status = statusOf(w, 'sonnet')
  expect(status.last_auto.file).toBe('a1.md')
  expect(status.last_auto.sha256).toMatch(/^[0-9a-f]{64}$/)
})

test('auto: does not start while PAUSE exists, and starts after /devam', async ($, on) => {
  const w = world(on)
  put(w, PAUSE, 'x')
  put(w, AUTO + '/a1.md', header('AUTO1'))
  await startSession($)
  await w.clock.advance(6000)
  expect(w.submitted).toEqual([])
  expect(w.files.has(AUTO + '/a1.md')).toBe(true)
  const back = await $.command.run({ command: 'devam', args: '' })
  expect(back.text).toBe('Devam: kuyruk yeniden başlatabilir.')
  expect(w.files.has(PAUSE)).toBe(false)
  await w.clock.advance(6000)
  expect(w.submitted).toEqual([header('AUTO1')])
})

test('/mola creates the flag and stops the inbox, also for /basla', async ($, on) => {
  const w = world(on)
  await startSession($)
  const out = await $.command.run({ command: 'mola', args: '' })
  expect(out.text).toContain('MOLA')
  expect(w.files.has(PAUSE)).toBe(true)
  expect(statusOf(w, 'sonnet').paused).toBe(true)
  put(w, '/repo/build/queue/sonnet/m.md', header('M1'))
  put(w, AUTO + '/a1.md', header('AUTO1'))
  await w.clock.advance(6000)
  const manual = await $.command.run({ command: 'basla', args: '' })
  expect(manual.text).toBe('Başlatılmadı: MOLA açık (/devam yaz)')
  expect(w.submitted).toEqual([])
})

test('auto: waits while a turn is running and starts when it ends', async ($, on) => {
  const w = world(on)
  await startSession($)
  await $.turn.start({ text: 'working', turnId: 't1' })
  put(w, AUTO + '/a1.md', header('AUTO1'))
  await w.clock.advance(6000)
  expect(w.submitted).toEqual([])
  await $.turn.complete({ turnId: 't1', answer: 'done', durationMs: 5, isAborted: false, reason: 'answer', usage: null })
  await w.clock.advance(6000)
  expect(w.submitted).toEqual([header('AUTO1')])
})

test('auto: a pending manual file goes first, nothing else is auto-started', async ($, on) => {
  const w = world(on)
  put(w, '/repo/build/queue/sonnet/manual.md', header('MAN1'))
  put(w, AUTO + '/a1.md', header('AUTO1'))
  await startSession($)
  await w.clock.advance(6000)
  expect(w.submitted).toEqual([])
  expect(w.files.has(AUTO + '/a1.md')).toBe(true)
})

test('auto: two auto files go one at a time', async ($, on) => {
  const w = world(on)
  put(w, AUTO + '/a1.md', header('AUTO1'))
  put(w, AUTO + '/a2.md', header('AUTO2'))
  w.mtimes.set(AUTO + '/a1.md', 1)
  w.mtimes.set(AUTO + '/a2.md', 2)
  await startSession($)
  await w.clock.advance(100)
  expect(w.submitted).toEqual([header('AUTO1')])
  await w.clock.advance(6000)
  // the first prompt has not become a turn yet, so the second waits
  expect(w.submitted).toEqual([header('AUTO1')])
  await $.turn.start({ text: 'x', turnId: 't1' })
  await $.turn.complete({ turnId: 't1', answer: 'done', durationMs: 5, isAborted: false, reason: 'answer', usage: null })
  await w.clock.advance(6000)
  expect(w.submitted).toEqual([header('AUTO1'), header('AUTO2')])
})

test('battery >= 40%: one prompt at a time across lanes', async ($, on) => {
  const w = world(on, { battery: bat(60) })
  put(w, '/repo/build/lanes/opus.json', workingOpus)
  put(w, AUTO + '/a1.md', header('AUTO1'))
  await startSession($)
  await w.clock.advance(6000)
  expect(w.submitted).toEqual([])
  expect(statusOf(w, 'sonnet').power).toEqual({ ac: false, pct: 60 })
  // the other lane finishes
  put(w, '/repo/build/lanes/opus.json', JSON.stringify({ state: 'idle', updated_at: new Date(0).toISOString() }))
  await w.clock.advance(6000)
  expect(w.submitted).toEqual([header('AUTO1')])
})

test('battery >= 40% and nobody else working: starts', async ($, on) => {
  const w = world(on, { battery: bat(60) })
  put(w, AUTO + '/a1.md', header('AUTO1'))
  await startSession($)
  await w.clock.advance(100)
  expect(w.submitted).toEqual([header('AUTO1')])
})

test('battery < 40%: nothing new starts, one toast, running work is untouched', async ($, on) => {
  const w = world(on, { battery: bat(35) })
  await startSession($)
  await $.turn.start({ text: 'working', turnId: 't1' })
  put(w, AUTO + '/a1.md', header('AUTO1'))
  await w.clock.advance(125000)
  await w.clock.advance(125000)
  expect(w.submitted).toEqual([])
  expect(w.toasts.filter((t) => t === 'Pil %35: yeni istem başlamaz').length).toBe(1)
  // still working: the mod never interrupts a turn
  expect(statusOf(w, 'sonnet').state).toBe('working')
})

test('battery < 20%: the status file says low_battery', async ($, on) => {
  const w = world(on, { battery: bat(15) })
  await startSession($)
  expect(statusOf(w, 'sonnet').state).toBe('low_battery')
  expect(statusOf(w, 'sonnet').busy).toBe(false)
})

test('on mains power a low charge does not stop anything', async ($, on) => {
  const w = world(on, { battery: bat(15, 2) })
  put(w, AUTO + '/a1.md', header('AUTO1'))
  await startSession($)
  await w.clock.advance(100)
  expect(w.submitted).toEqual([header('AUTO1')])
  expect(statusOf(w, 'sonnet').state).not.toBe('low_battery')
})

test('/basla also honours the battery rule', async ($, on) => {
  const w = world(on, { battery: bat(30) })
  put(w, '/repo/build/queue/sonnet/m.md', header('M1'))
  await startSession($)
  const out = await $.command.run({ command: 'basla', args: '' })
  expect(out.text).toBe('Başlatılmadı: pil %30 (< %40), yeni istem başlamaz')
  expect(w.submitted).toEqual([])
})
