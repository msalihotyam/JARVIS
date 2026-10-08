// Wave 2: the flake ledger (build/flakes/ledger.jsonl) and the slice ledger
// (build/ledger/slices.jsonl): row shapes, /flaky and /defter text, the append command.
import type { Kind } from './suite.ts'

export type FlakeRow = {
  time: string
  lane: string
  kind: Kind
  targets: string[]
  ran: number | null
  failures: string[]
  ok: boolean | null
  wall_s: number | null
  python_procs: number | null
  // another lane held the suite lock when this run started (or this run had to wait for it)
  other_lane_held: boolean
}

export type TestRun = { kind: Kind; ran: number | null; failures: number; python_procs: number | null; wall_s: number | null }

export type Usage = { input: number; output: number; cache_read: number; cache_creation: number }

export type SliceRow = {
  start: string
  end: string
  wall_s: number
  lane: string
  model: string | null
  effort: string | null
  tokens: Usage | null
  five_hour: { start: number | null; end: number | null }
  seven_day: { start: number | null; end: number | null }
  tool_calls: number
  test_runs: TestRun[]
  status: string | null
}

export const FLAKE_PATH = '/build/flakes/ledger.jsonl'
export const SLICE_PATH = '/build/ledger/slices.jsonl'

// Appends one line without rewriting the file (two sessions may append at once).
export const APPEND_ARGV = [
  'powershell',
  '-NoProfile',
  '-NonInteractive',
  '-Command',
  [
    '# JF_APPEND',
    "$ErrorActionPreference = 'Stop'",
    'try {',
    '  New-Item -ItemType Directory -Force -Path (Split-Path -LiteralPath $env:JF_PATH) | Out-Null',
    '  [System.IO.File]::AppendAllText($env:JF_PATH, [string]$env:JF_LINE + "`n", (New-Object System.Text.UTF8Encoding($false)))',
    '  exit 0',
    '} catch { exit 4 }',
  ].join('\n'),
] as const

export function usageOf(raw: any): Usage | null {
  if (!raw || typeof raw !== 'object') return null
  const n = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : 0)
  return {
    input: n(raw.input_tokens),
    output: n(raw.output_tokens),
    cache_read: n(raw.cache_read_input_tokens),
    cache_creation: n(raw.cache_creation_input_tokens),
  }
}

// The STATUS line of a final report, or null.
export function statusLineOf(answer: string): string | null {
  const m = /^STATUS:[^\r\n]*/m.exec(String(answer ?? ''))
  return m === null ? null : m[0].slice(0, 300)
}

export function statusWord(answer: string): string | null {
  const m = /^STATUS:\s*([A-Za-z_-]+)/m.exec(String(answer ?? ''))
  return m === null ? null : (m[1] as string).toUpperCase()
}

// Rows whose JSON parses and passes `shape`; a torn, foreign or hand-edited line is skipped.
export function parseRows<T>(text: string, shape: (row: any) => boolean): T[] {
  const rows: T[] = []
  for (const line of String(text ?? '').split('\n')) {
    if (line.trim() === '') continue
    try {
      const row = JSON.parse(line)
      if (row !== null && typeof row === 'object' && shape(row)) rows.push(row as T)
    } catch {
      // skipped
    }
  }
  return rows
}

export const isFlakeRow = (r: any): boolean =>
  typeof r.time === 'string' && typeof r.kind === 'string' && Array.isArray(r.failures) && r.failures.every((f: unknown) => typeof f === 'string') && (r.targets === undefined || Array.isArray(r.targets))

export const isSliceRow = (r: any): boolean =>
  typeof r.start === 'string' &&
  typeof r.lane === 'string' &&
  typeof r.wall_s === 'number' &&
  r.five_hour && r.seven_day && Array.isArray(r.test_runs) && typeof r.tool_calls === 'number'

type FlakyStat = { failLoad: number; failAlone: number; passAlone: number; last: string }

// Per failing test id: fails in full / targeted runs (under load), and how an
// "alone" run of it went. Ids that never failed are not listed.
export function flakyStats(rows: readonly FlakeRow[]): Map<string, FlakyStat> {
  const stats = new Map<string, FlakyStat>()
  const get = (id: string): FlakyStat => {
    let s = stats.get(id)
    if (s === undefined) {
      s = { failLoad: 0, failAlone: 0, passAlone: 0, last: '' }
      stats.set(id, s)
    }
    return s
  }
  for (const row of rows) {
    for (const id of row.failures ?? []) {
      const s = get(id)
      if (row.kind === 'alone') s.failAlone++
      else s.failLoad++
      if (row.time > s.last) s.last = row.time
    }
  }
  for (const row of rows) {
    if (row.kind !== 'alone' || row.ok !== true) continue
    for (const id of row.targets ?? []) {
      const s = stats.get(id)
      if (s !== undefined) s.passAlone++
    }
  }
  return stats
}

export function flakyText(rows: readonly FlakeRow[]): string {
  const stats = flakyStats(rows)
  if (stats.size === 0) return 'Kayıtlı test hatası yok (build/flakes/ledger.jsonl).'
  const lines = [...stats.entries()]
    .sort((a, b) => (a[1].last < b[1].last ? 1 : a[1].last > b[1].last ? -1 : 0))
    .map(([id, s]) => id + '\n    yükte ' + s.failLoad + ' kez düştü · tek başına ' + s.passAlone + ' geçti, ' + s.failAlone + ' düştü · son: ' + s.last.slice(0, 16).replace('T', ' '))
  return 'Test hataları (yükte düşen / tek başına geçen):\n' + lines.join('\n')
}

// Local calendar day of a time in ms, as YYYY-MM-DD.
export function dayOf(ms: number): string {
  const d = new Date(ms)
  const two = (n: number) => String(n).padStart(2, '0')
  return d.getFullYear() + '-' + two(d.getMonth() + 1) + '-' + two(d.getDate())
}

function hhmm(iso: string): string {
  const d = new Date(Date.parse(iso))
  const two = (n: number) => String(n).padStart(2, '0')
  return Number.isFinite(d.getTime()) ? two(d.getHours()) + ':' + two(d.getMinutes()) : '??:??'
}

function pct(a: number | null, b: number | null): string {
  return (a === null ? '?' : String(a)) + '→' + (b === null ? '?' : String(b))
}

function tokens(u: Usage | null): string {
  if (u === null) return '?'
  const total = u.input + u.output + u.cache_read + u.cache_creation
  return total >= 1000 ? Math.round(total / 1000) + 'k' : String(total)
}

export function defterText(rows: readonly SliceRow[], today: string): string {
  const mine = rows.filter((r) => typeof r.start === 'string' && dayOf(Date.parse(r.start)) === today)
  if (mine.length === 0) return 'Bugün kayıt yok (build/ledger/slices.jsonl).'
  const lines = mine.map(
    (r) =>
      hhmm(r.start) + ' ' + r.lane + ' ' + (r.model ?? '?') + '/' + (r.effort ?? '?') + ' · ' + Math.max(1, Math.round(r.wall_s / 60)) + ' dk · ' + tokens(r.tokens) +
      ' tok · plan %' + pct(r.five_hour.start, r.five_hour.end) + ' · 7g %' + pct(r.seven_day.start, r.seven_day.end) +
      ' · araç ' + r.tool_calls + ' · test ' + r.test_runs.length + ' · ' + (r.status ?? 'STATUS yok'),
  )
  return 'Bugün ' + mine.length + ' dilim:\n' + lines.join('\n')
}
