// A simulated machine for the jarvis-flow tests: an in-memory file system, git
// answers, a battery, and a session. Register every stub through `world(...)`
// before the test's first call on `$`.
import { mock } from 'claude-code/testing'
import { lockState } from '../hooks/lib/suite.ts'

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
  sessionId?: string
  // environment variables the mod may read
  env?: Record<string, string>
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
  // this session's Claude Code pid as the holder-pid lookup reports it (null = not found)
  holderPid: number | null
  // pids the alive check reports dead
  deadPids: Set<number>
  // JF_PID of each runner-process count ('' = none passed)
  suitePidArgs: string[]
  model: string
  // make session.model fail, to prove the lint is fail-open
  breakModel: boolean
  // make every file write fail
  failWrites: boolean
  // wave 2: what the simulated shell tool answers, and what it was asked to run
  toolText: string
  toolError: boolean
  toolRuns: string[]
  // python processes and suite-runner processes the simulated machine reports
  pyProcs: number
  suiteProcs: number
  // Start-Sleep calls (the suite queue waits inside these); the hook can free the lock mid-wait
  sleeps: number
  onSleep: ((n: number) => void) | null
  // curl.exe calls: argv and stdin
  curls: { argv: string[]; stdin: string }[]
  curlExit: number
  // plan percentages the usage noun reports
  five: number
  seven: number
  // the usage noun is not available on this surface
  breakUsage: boolean
  // hold the simulated shell tool / a Start-Sleep until the test lets go
  toolHold: Promise<void> | null
  toolThrow: boolean
  sleepHold: Promise<void> | null
  // paths whose read fails (a directory, a locked file), and how many lock swaps fail with exit 4 first
  unreadable: Set<string>
  casFail: number
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
    holderPid: null,
    deadPids: new Set(),
    suitePidArgs: [],
    model: opts.model ?? SONNET,
    breakModel: false,
    failWrites: false,
    toolText: '',
    toolError: false,
    toolRuns: [],
    pyProcs: 3,
    suiteProcs: 1,
    sleeps: 0,
    onSleep: null,
    curls: [],
    curlExit: 0,
    five: 31,
    seven: 10,
    breakUsage: false,
    toolHold: null,
    toolThrow: false,
    sleepHold: null,
    unreadable: new Set(),
    casFail: 0,
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
  mock.env(on, opts.env ?? {})
  reg('session.id', () => ({ value: opts.sessionId ?? 'sess-me' }))

  reg('session.cwd', () => ({ value: cwd }))
  reg('session.model', () => (w.breakModel ? { deny: 'model unavailable' } : { value: w.model }))
  reg('session.usage', () => (w.breakUsage ? { deny: 'usage unavailable' } : {
    value: {
      startedAt: 0,
      context: { window: 1000000, tokens: 1000, percent: opts.usagePercent ?? 12 },
      rateLimits: [
        { kind: 'five_hour', percentUsed: w.five },
        { kind: 'seven_day', percentUsed: w.seven },
      ],
    },
  }))

  reg('fs.exists', (_$: any, e: any) => ({ value: w.files.has(norm(e.path)) }))
  reg('fs.read', (_$: any, e: any) => {
    const path = norm(e.path)
    if (w.unreadable.has(path)) return { deny: 'EISDIR ' + path }
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
    if (argv[0].toLowerCase().endsWith('curl.exe')) {
      w.curls.push({ argv, stdin: String((e.init && e.init.stdin) ?? '') })
      return { value: { exitCode: w.curlExit, stdout: '', stderr: '' } }
    }
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
      if (command.includes('JF_CAS')) {
        const path = norm(env.JF_PATH)
        if (w.casFail > 0 && env.JF_MODE === 'swap' && String(env.JF_NEW ?? '') === '') {
          w.casFail--
          return { value: { exitCode: 4, stdout: '', stderr: '' } }
        }
        const have = w.files.has(path)
        const fresh = String(env.JF_NEW ?? '')
        if (env.JF_MODE === 'create') {
          if (have) return { value: { exitCode: 4, stdout: '', stderr: '' } }
        } else if (env.JF_MODE === 'stale') {
          if (!have) return { value: { exitCode: 4, stdout: '', stderr: '' } }
          // same rule as the PowerShell: replace unless it is a valid, fresh record
          const cur = String(w.files.get(path))
          const pid = (() => {
            try {
              return Number(JSON.parse(cur).pid)
            } catch {
              return NaN
            }
          })()
          if (lockState(cur, Number(env.JF_NOW), '', w.deadPids.has(pid)).state === 'held') return { value: { exitCode: 3, stdout: '', stderr: '' } }
        } else {
          if (!have || String(w.files.get(path)).trim() !== String(env.JF_EXPECT ?? '').trim()) return { value: { exitCode: 3, stdout: '', stderr: '' } }
        }
        w.files.set(path, fresh)
        w.writes.push(path)
        touch(path)
        return ok()
      }
      if (command.includes('JF_APPEND')) {
        const path = norm(env.JF_PATH)
        w.files.set(path, (w.files.get(path) ?? '') + String(env.JF_LINE) + '\n')
        touch(path)
        return ok()
      }
      if (command.includes('Start-Sleep')) {
        w.sleeps++
        const seconds = Number(env.JF_SECONDS ?? 0)
        return (async () => {
          if (w.sleepHold) await w.sleepHold
          if (w.onSleep) w.onSleep(w.sleeps)
          await w.clock.advance(seconds * 1000)
          return ok()
        })()
      }
      if (command.includes('JF_PYCOUNT')) return ok(String(w.pyProcs) + '\n')
      if (command.includes('JF_SUITE_RUNNING')) {
        w.suitePidArgs.push(String(env.JF_PID ?? ''))
        return ok(String(w.suiteProcs) + '\n')
      }
      if (command.includes('JF_HOLDER_PID')) return w.holderPid === null ? no() : ok(String(w.holderPid) + '\n')
      if (command.includes('JF_PID_ALIVE')) return ok(w.deadPids.has(Number(env.JF_PID)) ? 'dead\n' : 'alive\n')
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
  reg('session.end', (_$: any, e: any) => ({ sessionId: e.sessionId }))
  reg('classic.PostModelSwitch', () => ({}))
  reg('tool.call', async (_$: any, e: any) => {
    w.toolRuns.push(String(e.command ?? e.tool))
    if (w.toolHold) await w.toolHold
    if (w.toolThrow) throw new Error('tool exploded')
    return w.toolError ? { result: 'err', text: w.toolText, isError: true } : { result: 'ok', text: w.toolText }
  })
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
