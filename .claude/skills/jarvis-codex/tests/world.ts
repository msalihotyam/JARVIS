// A simulated machine for the jarvis-codex tests: files, git answers and a Codex CLI.
import { mock } from 'claude-code/testing'

export const ROOT = '/repo'
export const HELP = [
  'Usage: codex exec [OPTIONS] [PROMPT]',
  '  -s, --sandbox <SANDBOX_MODE>   [possible values: read-only, workspace-write, danger-full-access]',
  '      --skip-git-repo-check',
  '      --ephemeral',
  '      --ignore-user-config',
  '      --ignore-rules',
  '      --color <COLOR>',
  '  -o, --output-last-message <FILE>',
  '      --disable <FEATURE>',
].join('\n')

export const FEATURES = ['shell_tool', 'unified_exec', 'apps', 'browser_use', 'browser_use_external', 'computer_use', 'plugins', 'hooks', 'memories', 'goals']
  .map((n) => n.padEnd(40) + 'stable             true')
  .join('\n')

// the commit id the simulated git gives a revision
export const shaOf = (rev: string): string =>
  [...rev.replace(/\^\{commit\}$/, '')].map((c) => c.charCodeAt(0).toString(16)).join('').padEnd(40, '0').slice(0, 40)

export type WorldOptions = { isJarvis?: boolean; cwd?: string }

export type World = {
  files: Map<string, string>
  runs: { argv: string[]; env: Record<string, string>; stdin: string; cwd: string }[]
  toasts: string[]
  submitted: string[]
  codexVersion: string | null
  help: string
  features: string
  paths: string[]
  diff: string
  gitFails: boolean
  status: string
  statusAfter: string | null
  codexExit: number
  codexEnv: Record<string, string>
  codexMessage: string
  // hold the Codex run until the test lets go
  codexHold: Promise<void> | null
  clock: ReturnType<typeof mock.clock>
}

export function world(on: any, opts: WorldOptions = {}): World {
  const clock = mock.clock(on)
  const w: World = {
    files: new Map(),
    runs: [],
    toasts: [],
    submitted: [],
    codexVersion: 'codex-cli 0.159.2',
    help: HELP,
    features: FEATURES,
    paths: ['jarvis/app.py', 'tests/test_app.py'],
    diff: 'diff --git a/jarvis/app.py b/jarvis/app.py\n@@ -1 +1 @@\n-old\n+new\n',
    gitFails: false,
    status: '',
    statusAfter: null,
    codexExit: 0,
    codexEnv: {},
    codexMessage: 'BLOCKER: none\nMAJOR: none\nMINOR: jarvis/app.py:1 nit\nVERDICT: READY-WITH-NITS\n',
    codexHold: null,
    clock,
  }
  const cwd = opts.cwd ?? ROOT
  if (opts.isJarvis !== false) w.files.set(ROOT + '/JARVIS_RUNBOOK.md', '# runbook')
  let statusCalls = 0
  const reg = (name: string, fn: any) => on(name, (_$: any, e: any) => fn(_$, e))
  reg('session.start', () => ({ cwd }))
  reg('session.cwd', () => ({ value: cwd }))
  reg('command.register', () => ({ value: undefined }))
  reg('ui.toast', (_$: any, e: any) => {
    w.toasts.push(e.text)
    return { value: undefined }
  })
  reg('prompt.submit', (_$: any, e: any) => {
    w.submitted.push(e.text)
    return { text: e.text }
  })
  reg('fs.exists', (_$: any, e: any) => ({ value: w.files.has(norm(e.path)) }))
  reg('fs.read', (_$: any, e: any) => (w.files.has(norm(e.path)) ? { value: w.files.get(norm(e.path)) } : { deny: 'ENOENT' }))
  reg('fs.write', (_$: any, e: any) => {
    w.files.set(norm(e.path), e.text)
    return { value: undefined }
  })
  reg('command.run', () => ({ text: '' }))
  reg('process.run', async (_$: any, e: any) => {
    const argv: string[] = [...e.argv]
    const init = e.init ?? {}
    w.runs.push({ argv, env: init.env ?? {}, stdin: String(init.stdin ?? ''), cwd: String(init.cwd ?? '') })
    const ok = (stdout = '') => ({ value: { exitCode: 0, stdout, stderr: '' } })
    const no = (stderr = '') => ({ value: { exitCode: 1, stdout: '', stderr } })
    if (argv[0] === 'git') {
      if (w.gitFails) return no('fatal: bad revision')
      if (argv[1] === 'rev-parse') return ok(shaOf(String(argv[argv.length - 1])) + '\n')
      if (argv.includes('--name-only')) return ok(w.paths.join('\0') + (w.paths.length ? '\0' : ''))
      if (argv[1] === 'diff') return ok(w.diff)
      if (argv[1] === 'status') {
        statusCalls++
        return ok(statusCalls === 1 || w.statusAfter === null ? w.status : w.statusAfter)
      }
      return no()
    }
    if (argv[0] === 'powershell') {
      const command = String(argv[argv.length - 1])
      if (command.includes('JF_CODEX_VERSION')) return w.codexVersion === null ? no('not recognized') : ok(w.codexVersion + '\n')
      if (command.includes('JF_CODEX_HELP')) return ok(w.help)
      if (command.includes('JF_CODEX_FEATURES')) return ok(w.features)
      if (command.includes('JF_CODEX')) {
        if (w.codexHold) await w.codexHold
        if (w.codexExit === 0) w.files.set(norm(init.env.JF_OUT), w.codexMessage)
        w.codexEnv = init.env
        return { value: { exitCode: w.codexExit, stdout: '', stderr: w.codexExit === 0 ? '' : 'boom\nlast line' } }
      }
      if (command.includes('Remove-Item')) {
        w.files.delete(norm(init.env.JF_SRC))
        return ok()
      }
    }
    return no()
  })
  return w
}

export async function startSession($: any): Promise<void> {
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: ROOT })
}

function norm(path: string): string {
  return String(path).replace(/\\/g, '/').replace(/^[A-Za-z]:/, '')
}
