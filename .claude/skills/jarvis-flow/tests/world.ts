// A simulated machine for the jarvis-flow tests: an in-memory file system, git
// answers, a battery, and a session. Register every stub through `world(...)`
// before the test's first call on `$`.
import { mock } from 'claude-code/testing'

export const ROOT = '/repo'
export const OPUS = 'claude-opus-5-5'
export const SONNET = 'claude-sonnet-5-5'

export type WorldOptions = {
  model?: string
  cwd?: string
  mainHead?: string
  head?: string
  branch?: string
  baseIsAncestor?: boolean
  battery?: string
  // path -> real path after resolving links (default: the path itself)
  realPaths?: Record<string, string>
  files?: Record<string, string>
  isJarvis?: boolean
  usagePercent?: number
}

export type World = {
  files: Map<string, string>
  mtimes: Map<string, number>
  runs: string[][]
  writes: string[]
  toasts: string[]
  submitted: string[]
  filled: string[]
  battery: string
  mainHead: string
  head: string
  baseIsAncestor: boolean
  model: string
  // make session.model fail, to prove the lint is fail-open
  breakModel: boolean
  // make every file write fail
  failWrites: boolean
  clock: ReturnType<typeof mock.clock>
  calls: string[]
}

export function header(lane: string, model = 'sonnet', effort = 'high', base = 'main'): string {
  return '@jarvis lane=' + lane + ' model=' + model + ' effort=' + effort + ' base=' + base
}

export function world(on: any, opts: WorldOptions = {}): World {
  const clock = mock.clock(on)
  const w: World = {
    files: new Map(Object.entries(opts.files ?? {})),
    mtimes: new Map(),
    runs: [],
    writes: [],
    toasts: [],
    submitted: [],
    filled: [],
    battery: opts.battery ?? '',
    mainHead: opts.mainHead ?? 'aaaaaaaaaaaa',
    head: opts.head ?? 'aaaaaaaaaaaa',
    baseIsAncestor: opts.baseIsAncestor ?? true,
    model: opts.model ?? SONNET,
    breakModel: false,
    failWrites: false,
    clock,
    calls: [],
  }
  const cwd = opts.cwd ?? ROOT
  if (opts.isJarvis !== false) w.files.set(ROOT + '/JARVIS_RUNBOOK.md', '# runbook')
  const calls = w.calls
  const reg = (name: string, fn: any) => on(name, (_$: any, e: any) => { calls.push(name); return fn(_$, e) })
  let tick = 1
  const touch = (path: string) => w.mtimes.set(path, tick++)

  reg('session.start', () => ({ cwd }))
  reg('command.register', () => ({ value: undefined }))
  reg('ui.toast', (_$: any, e: any) => {
    w.toasts.push(e.text)
    return { value: undefined }
  })
  reg('ui.log', () => ({ value: undefined }))
  reg('ui.status', () => ({ value: undefined }))

  reg('session.cwd', () => ({ value: cwd }))
  reg('session.model', () => (w.breakModel ? { deny: 'model unavailable' } : { value: w.model }))
  reg('session.usage', () => ({
    value: {
      startedAt: 0,
      context: { window: 1000000, tokens: 1000, percent: opts.usagePercent ?? 12 },
      rateLimits: [{ kind: 'five_hour', percentUsed: 31 }],
    },
  }))

  reg('fs.exists', (_$: any, e: any) => ({ value: w.files.has(norm(e.path)) }))
  reg('fs.read', (_$: any, e: any) => {
    const path = norm(e.path)
    // the compact template ships with the plugin, outside the simulated tree
    if (path.endsWith('/compact-template.md')) return { value: 'Keep: rules. lane={{lane}} base={{base}} head={{head}} wt={{worktree}} br={{branch}} cwd={{cwd}}' }
    return w.files.has(path) ? { value: w.files.get(path) } : { deny: 'ENOENT ' + path }
  })
  reg('fs.write', (_$: any, e: any) => {
    if (w.failWrites) return { deny: 'disk full' }
    const path = norm(e.path)
    w.files.set(path, e.text)
    w.writes.push(path)
    touch(path)
    return { value: undefined }
  })
  reg('fs.list', (_$: any, e: any) => {
    const dir = norm(e.path).replace(/\/$/, '')
    const out: any[] = []
    for (const [path, text] of w.files) {
      if (!path.startsWith(dir + '/')) continue
      const rest = path.slice(dir.length + 1)
      if (rest.includes('/')) continue
      out.push({ name: rest, kind: 'file', size: sizeOf(text), mtimeMs: w.mtimes.get(path) ?? 0, isLink: false })
    }
    return out.length > 0 || hasDir(w.files, dir) ? { value: out } : { deny: 'ENOENT ' + dir }
  })
  reg('fs.stat', (_$: any, e: any) => {
    const path = norm(e.path)
    if (!w.files.has(path)) return { deny: 'ENOENT ' + path }
    return {
      value: {
        kind: 'file',
        size: sizeOf(w.files.get(path) as string),
        mtimeMs: w.mtimes.get(path) ?? 0,
        isLink: false,
        realPath: opts.realPaths?.[path] ?? path,
      },
    }
  })

  reg('process.run', (_$: any, e: any) => {
    const argv: string[] = [...e.argv]
    w.runs.push(argv)
    const ok = (stdout = '') => ({ value: { exitCode: 0, stdout, stderr: '' } })
    const no = () => ({ value: { exitCode: 1, stdout: '', stderr: '' } })
    if (argv[0] === 'git') {
      if (argv.join(' ') === 'git rev-parse --short=12 main') return ok(w.mainHead + '\n')
      if (argv.join(' ') === 'git rev-parse --abbrev-ref HEAD') return ok('worktree-demo\n')
      if (argv.join(' ') === 'git rev-parse --short=12 HEAD') return ok(w.head + '\n')
      if (argv[1] === 'merge-base') return w.baseIsAncestor ? ok() : no()
      return no()
    }
    if (argv[0] === 'powershell') {
      const command = String(argv[argv.length - 1])
      if (command.includes('Win32_Battery')) return ok(w.battery)
      const env = (e.init && e.init.env) || {}
      if (command.includes('Move-Item')) {
        const from = norm(env.JF_SRC)
        const to = norm(env.JF_DST)
        if (!w.files.has(from)) return no()
        w.files.set(to, w.files.get(from) as string)
        w.files.delete(from)
        touch(to)
        return ok()
      }
      if (command.includes('Remove-Item')) {
        w.files.delete(norm(env.JF_SRC))
        return ok()
      }
    }
    return no()
  })

  // Claude Code's own answers for a prompt and for filling the prompt box
  reg('prompt.submit', (_$: any, e: any) => {
    w.submitted.push(e.text)
    return { text: e.text }
  })
  reg('prompt.fill', (_$: any, e: any) => {
    w.filled.push(e.text)
    return { isFilled: true }
  })
  reg('turn.start', (_$: any, e: any) => ({ turnId: e.turnId }))
  reg('turn.complete', () => ({ text: '' }))
  reg('classic.PermissionRequest', () => ({}))
  reg('tool.call', () => ({ result: 'ok' }))
  reg('command.run', () => ({ text: '' }))
  return w
}

// The kit hands paths over as absolute ones, with a drive letter on Windows.
function norm(path: string): string {
  return String(path).replace(/\\/g, '/').replace(/^[A-Za-z]:/, '')
}

function sizeOf(text: string): number {
  return new TextEncoder().encode(text).length
}

function hasDir(files: Map<string, string>, dir: string): boolean {
  for (const path of files.keys()) {
    if (path.startsWith(dir + '/')) return true
  }
  return false
}

// What Claude Code does at the start of a session in this world
export async function startSession($: any, cwd = ROOT): Promise<void> {
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd })
}

export async function userPrompt($: any, text: string): Promise<any> {
  return $.prompt.submit({ text, wait: false, origin: { kind: 'composer' } })
}

export function statusOf(w: World, family: string): any {
  const text = w.files.get(ROOT + '/build/lanes/' + family + '.json')
  return text === undefined ? null : JSON.parse(text)
}

export function put(w: World, path: string, text: string): void {
  w.files.set(path, text)
  w.mtimes.set(path, w.files.size + 1000)
}
