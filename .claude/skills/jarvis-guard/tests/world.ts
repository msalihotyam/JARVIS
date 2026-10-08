// A simulated machine for the jarvis-guard hook tests: files, links, git's
// answers, the profile folder and the tools beneath the guard. Register it with
// `world(on, ...)` before the test's first call on `$`.
export const ROOT = 'C:/repo'
export const HOME = 'C:/Users/u'
export const LANE = ROOT + '/.claude/worktrees/lane'

export type Git = (args: string[]) => { exitCode: number; stdout: string } | null

export type WorldOptions = {
  cwd?: string
  isJarvis?: boolean
  files?: Record<string, string>
  dirs?: string[]
  links?: Record<string, string>
  git?: Git
  breakCwd?: boolean
}

export type World = { ran: string[]; gitCalls: string[][]; files: Map<string, string> }

export function key(path: string): string {
  return String(path).replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase()
}

export function world(on: any, opts: WorldOptions = {}): World {
  const files = new Map<string, string>()
  for (const [p, t] of Object.entries(opts.files ?? {})) files.set(key(p), t)
  if (opts.isJarvis !== false) files.set(key(ROOT + '/JARVIS_RUNBOOK.md'), '# runbook')
  const dirs = new Set((opts.dirs ?? []).concat([ROOT, LANE, ROOT + '/build', ROOT + '/data', HOME]).map(key))
  const links = new Map(Object.entries(opts.links ?? {}).map(([a, b]) => [key(a), b]))
  const w: World = { ran: [], gitCalls: [], files }
  const cwd = opts.cwd ?? ROOT

  const linked = (p: string): string | null => {
    for (const [from, to] of links) {
      if (p === from) return to
      if (p.startsWith(from + '/')) return to + p.slice(from.length)
    }
    return null
  }

  on('session.start', () => ({ cwd }))
  on('session.cwd', () => (opts.breakCwd ? { deny: 'no cwd' } : { value: cwd }))
  on('env.get', (_$: any, e: any) => ({ value: e.name === 'USERPROFILE' ? HOME : undefined }))
  on('fs.exists', (_$: any, e: any) => ({ value: files.has(key(e.path)) || dirs.has(key(e.path)) }))
  on('fs.read', (_$: any, e: any) => (files.has(key(e.path)) ? { value: files.get(key(e.path)) } : { deny: 'ENOENT ' + e.path }))
  on('fs.stat', (_$: any, e: any) => {
    const k = key(e.path)
    const to = linked(k)
    if (to !== null) return { value: { kind: 'dir', size: 0, mtimeMs: 0, isLink: true, realPath: to } }
    if (files.has(k)) return { value: { kind: 'file', size: 1, mtimeMs: 0, isLink: false, realPath: e.path } }
    if (dirs.has(k)) return { value: { kind: 'dir', size: 0, mtimeMs: 0, isLink: false, realPath: e.path } }
    return { deny: 'ENOENT ' + e.path }
  })
  on('process.run', (_$: any, e: any) => {
    const argv: string[] = [...e.argv]
    if (argv[0] !== 'git') return { value: { exitCode: 1, stdout: '', stderr: '' } }
    const args = argv[1] === '-C' ? argv.slice(3) : argv.slice(1)
    w.gitCalls.push(args)
    const out = opts.git ? opts.git(args) : null
    return { value: out ? { exitCode: out.exitCode, stdout: out.stdout, stderr: '' } : { exitCode: 1, stdout: '', stderr: '' } }
  })
  on('tool.call', (_$: any, e: any) => {
    w.ran.push(String(e.tool))
    if (e.tool === 'EnterWorktree') return { result: { worktreePath: LANE, worktreeBranch: 'worktree-lane', message: '' } }
    return { result: 'ok' }
  })
  on('prompt.submit', (_$: any, e: any) => ({ text: e.text }))
  return w
}

export async function start($: any, cwd = ROOT): Promise<void> {
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd })
}

export async function prompt($: any, text: string): Promise<void> {
  await $.prompt.submit({ text, wait: false, origin: { kind: 'composer' } })
}

// What the model's call came back as: the refusal text, or null when the tool ran.
export async function call($: any, input: any): Promise<string | null> {
  const out = await $.tool.call(input)
  if (out && typeof out.deny === 'string') return out.deny
  if (out && out.isError) return String(out.text ?? 'error')
  return null
}
