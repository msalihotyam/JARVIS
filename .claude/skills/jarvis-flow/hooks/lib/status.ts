import type { Power } from './power.ts'

export type LaneState = 'working' | 'idle' | 'waiting' | 'low_battery'

export type LastAuto = { file: string; sha256: string; at: string }

export type Status = {
  family: string
  model: string | null
  effort: string | null
  lane: string | null
  base: string | null
  cwd: string | null
  worktree: string | null
  branch: string | null
  head: string | null
  state: LaneState
  busy: boolean
  updated_at: string
  context_pct: number | null
  power: Power
  paused: boolean
  last_auto: LastAuto | null
}

export function stateOf(input: { busy: boolean; waiting: boolean; lowBattery: boolean }): LaneState {
  if (input.lowBattery) return 'low_battery'
  if (input.waiting) return 'waiting'
  if (input.busy) return 'working'
  return 'idle'
}

// The comparable part of a status: everything except the timestamp.
export function fingerprint(status: Status): string {
  const { updated_at: _ignored, ...rest } = status
  return JSON.stringify(rest)
}

// The worktree name when the cwd is inside .claude/worktrees/<name>, else null.
export function worktreeOf(cwd: string): string | null {
  const flat = cwd.replace(/\\/g, '/')
  const marker = '/.claude/worktrees/'
  const at = flat.indexOf(marker)
  if (at === -1) return null
  const rest = flat.slice(at + marker.length)
  const name = rest.split('/')[0]
  return name ? name : null
}

// The canonical checkout: the cwd with any /.claude/worktrees/<name>... cut off.
export function canonicalRootOf(cwd: string): string {
  const flat = cwd.replace(/\\/g, '/').replace(/\/+$/, '')
  const at = flat.indexOf('/.claude/worktrees/')
  return at === -1 ? flat : flat.slice(0, at)
}
