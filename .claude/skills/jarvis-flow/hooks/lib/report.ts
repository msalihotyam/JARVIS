const FINAL = /^STATUS:\s*(PASS|BLOCKED|CI_PENDING)(?![A-Za-z0-9_])/
const FENCE = /^\s*```/

// A final report is the block that starts at the first line beginning "STATUS:" and
// runs to the closing ``` of its fence, or to the end. Only PASS, BLOCKED and
// CI_PENDING are final words; anything else (a quoted line, STATUS: FAIL) is no report.
export function extractReport(answer: string): string | null {
  const lines = String(answer ?? '').split(/\r?\n/)
  const at = lines.findIndex((l) => l.startsWith('STATUS:'))
  if (at === -1 || !FINAL.test(lines[at] as string)) return null
  let end = lines.findIndex((l, i) => i > at && FENCE.test(l))
  if (end === -1) end = lines.length
  return lines.slice(at, end).join('\n').trimEnd()
}

// A report file the lane wrote itself during this turn: it was modified at or after
// the turn started and its first non-empty line starts with "STATUS:".
export function isFreshReport(text: string, mtimeMs: number, sinceMs: number): boolean {
  const first = String(text ?? '').split(/\r?\n/).find((l) => l.trim() !== '')
  return mtimeMs >= sinceMs && first !== undefined && first.startsWith('STATUS:')
}

// The folders a lane's report may sit in: the canonical root's build/reports and the
// session's own checkout's (a worktree root, or the cwd outside one).
export function reportDirs(root: string, cwd: string): string[] {
  const flat = cwd.replace(/\\/g, '/').replace(/\/+$/, '')
  const m = /^(.*\/\.claude\/worktrees\/[^/]+)/.exec(flat)
  const own = m ? (m[1] as string) : flat
  return [...new Set([root, own].filter(Boolean))].map((d) => d + '/build/reports')
}

// The lane id becomes a file name, so it must be the safe kind (the header
// parser already guarantees this; the check is repeated where the path is built).
export function safeLane(lane: string | null): string | null {
  return lane !== null && /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(lane) ? lane : null
}
