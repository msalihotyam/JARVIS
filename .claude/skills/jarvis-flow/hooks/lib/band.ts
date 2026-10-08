import type { Power } from './power.ts'

// The element constructors $.ui.resolve(e) hands a ui.render hook.
export type Elements = { Box: (props: any) => any; Text: (props: any) => any }

export type BandState = {
  family: string // opus | sonnet | haiku | unknown
  lane: string | null
  // 'ok' | 'bad' | 'main' | null (no lane yet)
  baseState: 'ok' | 'bad' | 'main' | null
  head: string | null
  worktree: string | null
  branch: string | null
  contextPct: number | null
  planPct: number | null
  power: Power
  paused: boolean
  lowBattery: boolean
  // the oldest pending prompt, if any
  pendingLane: string | null
  // this session's test command is held behind another session's full suite
  suiteWait?: { lane: string; minutes: number } | null
}

const COLORS: Record<string, string> = { opus: 'magenta', sonnet: 'cyan', haiku: 'yellow' }

export function identOf(family: string): string {
  return family === 'unknown' ? 'CLAUDE' : family.toUpperCase()
}

type Piece = { text: string; props?: Record<string, unknown> }

// Priority order: what must survive a narrow window comes first.
export function pieces(state: BandState): Piece[] {
  const out: Piece[] = []
  out.push({
    text: '  ' + identOf(state.family) + '  ',
    props: { bold: true, inverse: true, color: COLORS[state.family] ?? 'white' },
  })
  if (state.pendingLane !== null) {
    out.push({ text: 'Yeni istem hazır: ' + state.pendingLane + ' · /basla', props: { bold: true, color: 'green' } })
  }
  if (state.suiteWait) {
    out.push({ text: 'Test kuyruğu: ' + state.suiteWait.lane + ' koşuyor, ' + state.suiteWait.minutes + ' dk', props: { bold: true, color: 'yellow' } })
  }
  if (state.paused) out.push({ text: 'MOLA', props: { bold: true, color: 'yellow' } })
  if (state.baseState === 'ok') out.push({ text: 'taban ✓', props: { color: 'green' } })
  else if (state.baseState === 'bad') out.push({ text: 'taban ✗ ' + (state.head ?? '?'), props: { bold: true, color: 'red' } })
  else if (state.baseState === 'main') out.push({ text: 'taban main', props: { dimColor: true } })
  if (state.lane !== null) out.push({ text: state.lane })
  if (!state.power.ac) {
    const pct = state.power.pct === null ? '?' : String(state.power.pct)
    out.push({
      text: 'PİL %' + pct,
      props: { bold: state.lowBattery, color: state.lowBattery ? 'red' : state.power.pct !== null && state.power.pct < 40 ? 'yellow' : undefined },
    })
  }
  if (state.contextPct !== null) {
    out.push({ text: 'bağlam %' + state.contextPct, props: { dimColor: true } })
  }
  if (state.planPct !== null) out.push({ text: 'plan %' + state.planPct, props: { dimColor: true } })
  if (state.worktree !== null || state.branch !== null) {
    const where = (state.worktree !== null ? state.worktree : '') + (state.branch !== null ? ' @' + state.branch : '')
    out.push({ text: where, props: { dimColor: true } })
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
