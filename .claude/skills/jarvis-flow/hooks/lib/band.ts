import type { Power } from './power.ts'

// The element constructors $.ui.resolve(e) hands a ui.render hook.
export type Elements = { Box: (props: any) => any; Text: (props: any) => any }

// Salih, 2026-10-09: only what must be seen at a glance; everything else is noise.
export const LOW_BAND_BATTERY_PCT = 25
export const HIGH_USE_PCT = 80
export const CONVEYOR_MAX_LANES = 3
// conveyor.py STALE_LOCK_S: five missed passes and the conveyor counts as stopped
export const CONVEYOR_STALE_MS = 300000

export type Conveyor = { running: number; paused: boolean }

export type BandState = {
  family: string // opus | sonnet | haiku | unknown
  // 'ok' | 'bad' | 'main' | null (no lane yet); only 'bad' is shown
  baseState: 'ok' | 'bad' | 'main' | null
  head: string | null
  contextPct: number | null
  planPct: number | null
  power: Power
  paused: boolean
  lowBattery: boolean
  // the oldest pending prompt, if any
  pendingLane: string | null
  // this session's test command is held behind another session's full suite
  suiteWait?: { lane: string; minutes: number } | null
  // build/conveyor/status.json; null when missing or unreadable
  conveyor?: Conveyor | null
}

const COLORS: Record<string, string> = { opus: 'magenta', sonnet: 'cyan', haiku: 'yellow' }
// lanes scripts/conveyor.py no longer counts as running: a report (status), a finished or a missing session
const FINISHED_STATES = new Set(['done', 'idle', 'missing'])

export function identOf(family: string): string {
  return family === 'unknown' ? 'CLAUDE' : family.toUpperCase()
}

// The conveyor piece from the status.json text; null when it is missing, unreadable or stale
// (the conveyor rewrites it every pass and leaves it behind when it stops).
export function conveyorOf(statusText: string | null, paused: boolean, nowMs: number): Conveyor | null {
  if (statusText === null) return null
  try {
    const status = JSON.parse(statusText)
    if (!status || !Array.isArray(status.lanes)) return null
    // updated_at is local time without an offset (conveyor.py iso()); Date.parse reads it as local too
    const updated = Date.parse(status.updated_at)
    if (!(nowMs - updated <= CONVEYOR_STALE_MS)) return null
    const running = status.lanes.filter((ln: any) => ln && !ln.status && !FINISHED_STATES.has(ln.state)).length
    return { running, paused }
  } catch {
    return null
  }
}

type Piece = { text: string; props?: Record<string, unknown> }

// Priority order: what must survive a narrow window comes first.
export function pieces(state: BandState): Piece[] {
  const out: Piece[] = []
  out.push({
    text: '  ' + identOf(state.family) + '  ',
    props: { bold: true, inverse: true, color: COLORS[state.family] ?? 'white' },
  })
  if (state.conveyor) {
    out.push(
      state.conveyor.paused
        ? { text: 'Konveyör MOLA', props: { bold: true, color: 'yellow' } }
        : { text: 'Konveyör ' + state.conveyor.running + '/' + CONVEYOR_MAX_LANES, props: { dimColor: true } },
    )
  }
  if (state.pendingLane !== null) {
    out.push({ text: 'Yeni istem hazır: ' + state.pendingLane + ' · /basla', props: { bold: true, color: 'green' } })
  }
  if (state.suiteWait) {
    out.push({ text: 'Test kuyruğu: ' + state.suiteWait.lane + ' koşuyor, ' + state.suiteWait.minutes + ' dk', props: { bold: true, color: 'yellow' } })
  }
  if (state.paused) out.push({ text: 'MOLA', props: { bold: true, color: 'yellow' } })
  if (state.baseState === 'bad') out.push({ text: 'taban ✗ ' + (state.head ?? '?'), props: { bold: true, color: 'red' } })
  const pct = state.power.pct
  if (!state.power.ac && (state.lowBattery || (pct !== null && pct < LOW_BAND_BATTERY_PCT))) {
    out.push({ text: 'PİL %' + (pct === null ? '?' : String(pct)), props: { bold: true, color: 'red' } })
  }
  if (state.contextPct !== null && state.contextPct >= HIGH_USE_PCT) {
    out.push({ text: 'bağlam %' + state.contextPct, props: { bold: true, color: 'yellow' } })
  }
  if (state.planPct !== null && state.planPct >= HIGH_USE_PCT) {
    out.push({ text: 'plan %' + state.planPct, props: { bold: true, color: 'yellow' } })
  }
  return out
}

// The pieces that fit in `columns` cells (two cells between pieces).
export function fit(all: readonly Piece[], columns: number): Piece[] {
  const kept: Piece[] = []
  let used = 0
  for (const piece of all) {
    const width = piece.text.length + (kept.length > 0 ? 2 : 0)
    if (kept.length > 0 && used + width > columns) continue
    kept.push(piece)
    used += width
  }
  return kept
}

export function renderBand(el: Elements, state: BandState, columns: number, theirs?: unknown) {
  const children = fit(pieces(state), columns).map((piece) => el.Text({ ...(piece.props ?? {}), children: [piece.text] }))
  const row = el.Box({ flexDirection: 'row', columnGap: 2, children })
  if (theirs === undefined || theirs === null) return row
  return el.Box({ flexDirection: 'column', children: [row, theirs] })
}
