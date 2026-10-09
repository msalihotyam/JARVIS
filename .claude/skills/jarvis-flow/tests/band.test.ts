import { expect, test } from 'claude-code/testing'
import { conveyorOf, fit, identOf, pieces, renderBand, type BandState } from '../hooks/lib/band.ts'
import { header, put, startSession, userPrompt, world } from './world.ts'

const bat = (pct: number, status = 1) => JSON.stringify({ EstimatedChargeRemaining: pct, BatteryStatus: status })
const STATUS = '/repo/build/conveyor/status.json'
// conveyor.py iso(): local time, no offset
const localIso = (ms: number) => {
  const d = new Date(ms)
  const p = (n: number) => String(n).padStart(2, '0')
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds())
}
const lanesJson = (at: number, ...lanes: Array<{ state: string; status: string | null }>) =>
  JSON.stringify({ updated_at: localIso(at), paused: false, lanes: lanes.map((ln, i) => ({ name: 'L' + i, id: 'id' + i, ...ln })) })
const NOW = Date.parse('2026-10-09T16:00:00')

const el = {
  Box: (props: any) => ({ type: 'Box', props: { ...props, children: undefined }, children: props.children }),
  Text: (props: any) => ({ type: 'Text', props: { ...props, children: undefined }, children: props.children }),
}
const base: BandState = {
  family: 'opus', baseState: 'ok', head: 'abc1234',
  contextPct: 41, planPct: 7, power: { ac: true, pct: null }, paused: false, lowBattery: false, pendingLane: null,
}
const texts = (tree: any): string[] => (tree.children as any[]).map((c) => c.children[0])

const PANE = (surface: 'terminal' | 'desktop', columns = 120) =>
  ({
    plugin: 'jarvis-flow',
    component: 'AbovePrompt',
    requestId: 'band',
    surface,
    viewport: { columns, rows: 30 },
    props: { hasSurvey: false, isWorking: false, maxRows: 3, bodyColumns: columns, scroll: { offset: 0, bodyRows: 3 }, view: {} },
  }) as any

test('pure: identity, colour per family, and nothing else when all is well', { timeoutMs: 60000 }, async () => {
  expect(identOf('opus')).toBe('OPUS')
  expect(identOf('sonnet')).toBe('SONNET')
  expect(identOf('unknown')).toBe('CLAUDE')
  const tree = renderBand(el, base, 200)
  expect(texts(tree)).toEqual(['  OPUS  '])
  expect(tree.children[0].props).toMatchObject({ bold: true, inverse: true, color: 'magenta' })
  expect(renderBand(el, { ...base, family: 'sonnet' }, 200).children[0].props.color).toBe('cyan')
  expect(texts(renderBand(el, { ...base, baseState: 'main' }, 200))).toEqual(['  OPUS  '])
})

test('pure: a bad base shows the head in red, no lane shows no base', { timeoutMs: 60000 }, async () => {
  const bad = renderBand(el, { ...base, baseState: 'bad' }, 200)
  expect(texts(bad)).toContain('taban ✗ abc1234')
  expect(bad.children[1].props.color).toBe('red')
  const none = renderBand(el, { ...base, baseState: null }, 200)
  expect(texts(none).some((t) => t.startsWith('taban'))).toBe(false)
})

test('pure: pending prompt, MOLA and a low battery', { timeoutMs: 60000 }, async () => {
  const all = texts(renderBand(el, { ...base, pendingLane: 'SB2', paused: true, power: { ac: false, pct: 35 }, lowBattery: false }, 300))
  expect(all).toContain('Yeni istem hazır: SB2 · /basla')
  expect(all).toContain('MOLA')
  expect(all.some((t) => t.startsWith('PİL'))).toBe(false)
  const lowest = renderBand(el, { ...base, power: { ac: false, pct: 12 }, lowBattery: true }, 300)
  const battery = (lowest.children as any[]).find((c) => c.children[0] === 'PİL %12')
  expect(battery.props).toMatchObject({ bold: true, color: 'red' })
  expect(texts(renderBand(el, base, 300)).some((t) => t.startsWith('PİL'))).toBe(false)
})

test('pure: battery only on battery and below 25% or flagged low', { timeoutMs: 60000 }, async () => {
  const shown = (power: BandState['power'], lowBattery = false) =>
    texts(renderBand(el, { ...base, power, lowBattery }, 300)).filter((t) => t.startsWith('PİL'))
  expect(shown({ ac: false, pct: 24 })).toEqual(['PİL %24'])
  expect(shown({ ac: false, pct: 25 })).toEqual([])
  expect(shown({ ac: false, pct: null }, true)).toEqual(['PİL %?'])
  expect(shown({ ac: false, pct: null })).toEqual([])
  expect(shown({ ac: true, pct: 10 })).toEqual([])
})

test('pure: context and plan only at 80% or more', { timeoutMs: 60000 }, async () => {
  expect(texts(renderBand(el, { ...base, contextPct: 79, planPct: 79 }, 300))).toEqual(['  OPUS  '])
  expect(texts(renderBand(el, { ...base, contextPct: 80, planPct: 80 }, 300))).toEqual(['  OPUS  ', 'bağlam %80', 'plan %80'])
  expect(texts(renderBand(el, { ...base, contextPct: null, planPct: 97 }, 300))).toEqual(['  OPUS  ', 'plan %97'])
})

test('pure: the conveyor counts lanes not finished, MOLA when paused, nothing when unreadable or stale', { timeoutMs: 60000 }, async () => {
  expect(conveyorOf(null, false, NOW)).toBeNull()
  expect(conveyorOf('{not json', false, NOW)).toBeNull()
  expect(conveyorOf('{"paused": false}', false, NOW)).toBeNull()
  expect(conveyorOf('{"lanes": []}', false, NOW)).toBeNull()
  expect(conveyorOf(lanesJson(NOW - 300000), false, NOW)).toEqual({ running: 0, paused: false })
  expect(conveyorOf(lanesJson(NOW - 301000), false, NOW)).toBeNull()
  const text = lanesJson(
    NOW,
    { state: 'working', status: null },
    { state: 'stopped', status: null },
    { state: 'done', status: 'PASS' },
    { state: 'stopped', status: 'BLOCKED' },
    { state: 'idle', status: null },
    { state: 'missing', status: null },
  )
  expect(conveyorOf(text, false, NOW)).toEqual({ running: 2, paused: false })
  expect(conveyorOf(text, true, NOW)).toEqual({ running: 2, paused: true })
  expect(texts(renderBand(el, { ...base, conveyor: { running: 2, paused: false } }, 300))).toEqual(['  OPUS  ', 'Konveyör 2/3'])
  expect(texts(renderBand(el, { ...base, conveyor: { running: 2, paused: true } }, 300))).toEqual(['  OPUS  ', 'Konveyör MOLA'])
  expect(texts(renderBand(el, { ...base, conveyor: null }, 300))).toEqual(['  OPUS  '])
})

test('pure: a narrow band keeps the identity and the highest priorities only', { timeoutMs: 60000 }, async () => {
  const state = { ...base, pendingLane: 'SB2', paused: true, contextPct: 91 }
  const narrow = texts(renderBand(el, state, 30))
  expect(narrow[0]).toBe('  OPUS  ')
  expect(narrow.length).toBeLessThan(pieces(state).length)
  expect(fit(pieces(state), 1).map((p) => p.text)).toEqual(['  OPUS  '])
  // priority: the inbox hint outranks the context
  const mid = texts(renderBand(el, state, 50))
  expect(mid).toContain('Yeni istem hazır: SB2 · /basla')
  expect(mid).not.toContain('bağlam %91')
})

test('pure: the other mods band is kept under ours', { timeoutMs: 60000 }, async () => {
  const theirs = { type: 'Text', props: {}, children: ['theirs'] }
  const tree = renderBand(el, base, 120, theirs)
  expect(tree.type).toBe('Box')
  expect(tree.children[1]).toBe(theirs)
})

for (const surface of ['terminal', 'desktop'] as const) {
  test('band on ' + surface + ': identity and the other bands text, no lane or base when all is well', { timeoutMs: 60000 }, async ($, on) => {
    on('ui.render', () => ({ type: 'Text', props: {}, children: ['drawn by Claude Code'] }))
    world(on, { model: 'claude-opus-5-5' })
    await startSession($)
    await userPrompt($, header('F-ee', 'opus', 'high', 'main') + '\nbody')
    const ui = await $.ui.mount(PANE(surface))
    expect(await ui.find({ type: 'Text', text: /OPUS/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'F-ee' })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: 'taban main' })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: 'drawn by Claude Code' })).toBeDefined()
    await ui.unmount()
  })

  test('band on ' + surface + ': a stale base shows taban ✗ with the head', { timeoutMs: 60000 }, async ($, on) => {
    on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
    world(on, { baseIsAncestor: false, head: 'bbbbbbbbbbbb' })
    await startSession($)
    await userPrompt($, header('SB1', 'sonnet', 'high', 'aaaaaaa') + '\nbody')
    const ui = await $.ui.mount(PANE(surface))
    expect(await ui.find({ type: 'Text', text: 'taban ✗ bbbbbbbbbbbb' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /SONNET/ })).toBeDefined()
    await ui.unmount()
  })

  test('band on ' + surface + ': a pending prompt and MOLA; a battery not low is hidden', { timeoutMs: 60000 }, async ($, on) => {
    on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
    const w = world(on, { battery: bat(55) })
    put(w, '/repo/build/queue/sonnet/sb2.md', header('SB2'))
    put(w, '/repo/build/queue/PAUSE', 'x')
    await startSession($)
    const ui = await $.ui.mount(PANE(surface))
    expect(await ui.find({ type: 'Text', text: 'Yeni istem hazır: SB2 · /basla' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'MOLA' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'PİL %55' })).toBeUndefined()
    await ui.unmount()
  })

  test('band on ' + surface + ': a narrow window drops the details but keeps the identity', { timeoutMs: 60000 }, async ($, on) => {
    on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
    world(on)
    await startSession($)
    await userPrompt($, header('SB1') + '\nbody')
    const ui = await $.ui.mount(PANE(surface, 14))
    expect(await ui.find({ type: 'Text', text: /SONNET/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'SB1' })).toBeUndefined()
    await ui.unmount()
  })

  test('band on ' + surface + ': the conveyor shows n/3, MOLA under PAUSE, nothing without status.json', { timeoutMs: 60000 }, async ($, on) => {
    on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
    const w = world(on)
    await w.clock.set(NOW)
    put(w, STATUS, lanesJson(NOW, { state: 'working', status: null }, { state: 'done', status: 'PASS' }))
    await startSession($)
    const ui = await $.ui.mount(PANE(surface))
    expect(await ui.find({ type: 'Text', text: 'Konveyör 1/3' })).toBeDefined()
    await ui.unmount()
  })
}

test('band: Konveyör MOLA when build/conveyor/PAUSE exists', { timeoutMs: 60000 }, async ($, on) => {
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  const w = world(on)
  await w.clock.set(NOW)
  put(w, STATUS, lanesJson(NOW, { state: 'working', status: null }))
  put(w, '/repo/build/conveyor/PAUSE', 'x')
  await startSession($)
  const ui = await $.ui.mount(PANE('terminal'))
  expect(await ui.find({ type: 'Text', text: 'Konveyör MOLA' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /Konveyör \d/ })).toBeUndefined()
  await ui.unmount()
})

test('band: no conveyor piece when status.json is missing, unreadable or stale', { timeoutMs: 60000 }, async ($, on) => {
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  const w = world(on)
  await w.clock.set(NOW)
  put(w, '/repo/build/conveyor/PAUSE', 'x')
  await startSession($)
  const missing = await $.ui.mount(PANE('terminal'))
  expect(await missing.find({ type: 'Text', text: /Konveyör/ })).toBeUndefined()
  await missing.unmount()
  put(w, STATUS, lanesJson(NOW, { state: 'working', status: null }))
  w.unreadable.add(STATUS)
  await w.clock.advance(60000)
  const unreadable = await $.ui.mount(PANE('terminal'))
  expect(await unreadable.find({ type: 'Text', text: /Konveyör/ })).toBeUndefined()
  await unreadable.unmount()
  // a conveyor that stopped leaves its last status.json behind
  w.unreadable.delete(STATUS)
  put(w, STATUS, lanesJson(NOW + 120000 - 400000, { state: 'working', status: null }))
  await w.clock.advance(60000)
  const stale = await $.ui.mount(PANE('terminal'))
  expect(await stale.find({ type: 'Text', text: /Konveyör/ })).toBeUndefined()
  await stale.unmount()
})

test('band: status.json is read at most once a minute', { timeoutMs: 60000 }, async ($, on) => {
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  const w = world(on)
  await w.clock.set(NOW)
  put(w, STATUS, lanesJson(NOW, { state: 'working', status: null }))
  await startSession($)
  put(w, STATUS, lanesJson(NOW,{ state: 'working', status: null }, { state: 'working', status: null }))
  await w.clock.advance(5000)
  const early = await $.ui.mount(PANE('terminal'))
  expect(await early.find({ type: 'Text', text: 'Konveyör 1/3' })).toBeDefined()
  await early.unmount()
  await w.clock.advance(55000)
  const later = await $.ui.mount(PANE('terminal'))
  expect(await later.find({ type: 'Text', text: 'Konveyör 2/3' })).toBeDefined()
  await later.unmount()
})

test('outside the JARVIS repo the band is left to Claude Code', { timeoutMs: 60000 }, async ($, on) => {
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['drawn by Claude Code'] }))
  world(on, { isJarvis: false })
  await startSession($)
  const ui = await $.ui.mount(PANE('terminal'))
  expect(await ui.find({ type: 'Text', text: /SONNET|OPUS/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: 'drawn by Claude Code' })).toBeDefined()
  await ui.unmount()
})
