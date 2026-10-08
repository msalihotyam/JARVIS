import { expect, test } from 'claude-code/testing'
import { fit, identOf, pieces, renderBand, type BandState } from '../hooks/lib/band.ts'
import { header, put, startSession, userPrompt, world } from './world.ts'

const bat = (pct: number, status = 1) => JSON.stringify({ EstimatedChargeRemaining: pct, BatteryStatus: status })

const el = {
  Box: (props: any) => ({ type: 'Box', props: { ...props, children: undefined }, children: props.children }),
  Text: (props: any) => ({ type: 'Text', props: { ...props, children: undefined }, children: props.children }),
}
const base: BandState = {
  family: 'opus', lane: 'F-ee', baseState: 'ok', head: 'abc1234', worktree: '6g-fee', branch: 'worktree-6g-fee',
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

test('pure: identity, colour per family and the lane pieces', async () => {
  expect(identOf('opus')).toBe('OPUS')
  expect(identOf('sonnet')).toBe('SONNET')
  expect(identOf('unknown')).toBe('CLAUDE')
  const tree = renderBand(el, base, 200)
  expect(texts(tree)).toEqual(['  OPUS  ', 'taban ✓', 'F-ee', 'bağlam %41', 'plan %7', '6g-fee @worktree-6g-fee'])
  expect(tree.children[0].props).toMatchObject({ bold: true, inverse: true, color: 'magenta' })
  expect(renderBand(el, { ...base, family: 'sonnet' }, 200).children[0].props.color).toBe('cyan')
})

test('pure: a bad base shows the head in red, no lane shows no base', async () => {
  const bad = renderBand(el, { ...base, baseState: 'bad' }, 200)
  expect(texts(bad)).toContain('taban ✗ abc1234')
  expect(bad.children[1].props.color).toBe('red')
  const none = renderBand(el, { ...base, lane: null, baseState: null }, 200)
  expect(texts(none).some((t) => t.startsWith('taban'))).toBe(false)
  expect(texts(renderBand(el, { ...base, baseState: 'main' }, 200))).toContain('taban main')
})

test('pure: pending prompt, MOLA and battery pieces', async () => {
  const all = texts(renderBand(el, { ...base, pendingLane: 'SB2', paused: true, power: { ac: false, pct: 35 }, lowBattery: false }, 300))
  expect(all).toContain('Yeni istem hazır: SB2 · /basla')
  expect(all).toContain('MOLA')
  expect(all).toContain('PİL %35')
  const lowest = renderBand(el, { ...base, power: { ac: false, pct: 12 }, lowBattery: true }, 300)
  const battery = (lowest.children as any[]).find((c) => c.children[0] === 'PİL %12')
  expect(battery.props).toMatchObject({ bold: true, color: 'red' })
  expect(texts(renderBand(el, base, 300)).some((t) => t.startsWith('PİL'))).toBe(false)
})

test('pure: a narrow band keeps the identity and the highest priorities only', async () => {
  const state = { ...base, pendingLane: 'SB2', paused: true }
  const narrow = texts(renderBand(el, state, 30))
  expect(narrow[0]).toBe('  OPUS  ')
  expect(narrow.length).toBeLessThan(pieces(state).length)
  expect(fit(pieces(state), 1).map((p) => p.text)).toEqual(['  OPUS  '])
  // priority: the inbox hint outranks the worktree
  const mid = texts(renderBand(el, state, 60))
  expect(mid).toContain('Yeni istem hazır: SB2 · /basla')
  expect(mid).not.toContain('6g-fee @worktree-6g-fee')
})

test('pure: the other mods band is kept under ours', async () => {
  const theirs = { type: 'Text', props: {}, children: ['theirs'] }
  const tree = renderBand(el, base, 120, theirs)
  expect(tree.type).toBe('Box')
  expect(tree.children[1]).toBe(theirs)
})

for (const surface of ['terminal', 'desktop'] as const) {
  test('band on ' + surface + ': identity, lane, base and the other bands text', async ($, on) => {
    on('ui.render', () => ({ type: 'Text', props: {}, children: ['drawn by Claude Code'] }))
    world(on, { model: 'claude-opus-5-5' })
    await startSession($)
    await userPrompt($, header('F-ee', 'opus', 'high', 'main') + '\nbody')
    const ui = await $.ui.mount(PANE(surface))
    expect(await ui.find({ type: 'Text', text: /OPUS/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'F-ee' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'taban main' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'drawn by Claude Code' })).toBeDefined()
    await ui.unmount()
  })

  test('band on ' + surface + ': a stale base shows taban ✗ with the head', async ($, on) => {
    on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
    world(on, { baseIsAncestor: false, head: 'bbbbbbbbbbbb' })
    await startSession($)
    await userPrompt($, header('SB1', 'sonnet', 'high', 'aaaaaaa') + '\nbody')
    const ui = await $.ui.mount(PANE(surface))
    expect(await ui.find({ type: 'Text', text: 'taban ✗ bbbbbbbbbbbb' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /SONNET/ })).toBeDefined()
    await ui.unmount()
  })

  test('band on ' + surface + ': a pending prompt, MOLA and the battery', async ($, on) => {
    on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
    const w = world(on, { battery: bat(55) })
    put(w, '/repo/build/queue/sonnet/sb2.md', header('SB2'))
    put(w, '/repo/build/queue/PAUSE', 'x')
    await startSession($)
    const ui = await $.ui.mount(PANE(surface))
    expect(await ui.find({ type: 'Text', text: 'Yeni istem hazır: SB2 · /basla' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'MOLA' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'PİL %55' })).toBeDefined()
    await ui.unmount()
  })

  test('band on ' + surface + ': a narrow window drops the details but keeps the identity', async ($, on) => {
    on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
    world(on)
    await startSession($)
    await userPrompt($, header('SB1') + '\nbody')
    const ui = await $.ui.mount(PANE(surface, 14))
    expect(await ui.find({ type: 'Text', text: /SONNET/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'SB1' })).toBeUndefined()
    await ui.unmount()
  })
}

test('outside the JARVIS repo the band is left to Claude Code', async ($, on) => {
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['drawn by Claude Code'] }))
  world(on, { isJarvis: false })
  await startSession($)
  const ui = await $.ui.mount(PANE('terminal'))
  expect(await ui.find({ type: 'Text', text: /SONNET|OPUS/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: 'drawn by Claude Code' })).toBeDefined()
  await ui.unmount()
})
