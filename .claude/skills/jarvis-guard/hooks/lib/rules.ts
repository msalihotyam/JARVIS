// The JARVIS hard rules as decisions on one shell command line or one file
// path. Pure but for the few reads `Ctx` gives (real paths, file kinds, the
// assert guard). Every unclear case refuses: a guard that cannot read what a
// command does says so instead of letting it run.
import { parse, programName, type Kind, type Simple, type Word } from './shell.ts'
import { expandHome, hasGlob, inside, isAbsolute, join, normalize, protectedKind, strictlyInside, worktreeOf } from './paths.ts'

export type Ctx = {
  root: string // the canonical checkout, normal form
  home: string | null // the user's profile folder, normal form
  cwd: string // the tool's working folder, normal form
  // where an absolute path really lands (links and junctions resolved; for a
  // path not there yet its nearest existing folder resolved plus the rest)
  realPath: (abs: string) => Promise<string | null>
  kindOf: (abs: string) => Promise<'file' | 'dir' | null>
  // the assert guard for `git commit` run in `dir`; a refusal text or null
  onCommit: (dir: string, amend: boolean) => Promise<string | null>
}

export const RULES: Record<string, string> = {
  R1: 'git push yalnız refs/heads/ci/* (scripts\\ci.ps1)',
  R2: 'git add yalnız açık yollarla, uv.lock asla',
  R3: 'git commit -a / --all yok',
  R4: 'git reset / restore / stash / clean / checkout -- <yol> yok',
  R5: 'git switch --discard-changes yok',
  R6: 'git rebase yalnız .claude/worktrees/<ad> içinde',
  R7: 'git worktree prune / remove --force yok',
  R8: 'git branch -D yalnız worktree-* dalları',
  R9: 'özyinelemeli silme yalnız build/ ya da .claude/worktrees/<ad>/ altında',
  R10: 'korunan yola yazma / taşıma / silme yok',
  R12: 'assert sayısı düşemez',
  R13: 'worktree tabanı istem başlığıyla uyuşmuyor',
  X1: 'git takma adı ya da bilinmeyen git alt komutu',
  X2: 'denetlenemeyen komut',
  X3: 'satır içi betikte yasak işlem',
  X4: 'git geçmişini / dalı yeniden yazma yok',
  X5: 'ExitWorktree discard_changes yok',
}

export function refusal(rule: string, detail: string): string {
  return 'jarvis-guard reddetti [' + rule + ' ' + (RULES[rule] ?? '') + ']: ' + detail
}

const PS_ALIASES: Record<string, string> = {
  rm: 'remove-item', del: 'remove-item', erase: 'remove-item', rd: 'remove-item', rmdir: 'remove-item', ri: 'remove-item',
  mv: 'move-item', move: 'move-item', mi: 'move-item', cp: 'copy-item', copy: 'copy-item', cpi: 'copy-item',
  ren: 'rename-item', rni: 'rename-item', sc: 'set-content', ac: 'add-content', ni: 'new-item', clc: 'clear-content',
  iex: 'invoke-expression', saps: 'start-process', start: 'start-process', cd: 'set-location', chdir: 'set-location',
  sl: 'set-location', pushd: 'push-location', popd: 'pop-location', tee: 'tee-object',
}

const DELETERS = new Set(['rm', 'unlink', 'shred', 'remove-item', 'del', 'erase', 'rd', 'rmdir', 'rimraf'])
const MOVERS = new Set(['mv', 'move', 'move-item', 'ren', 'rename', 'rename-item'])
const WRITERS = new Set([
  'touch', 'truncate', 'tee', 'tee-object', 'set-content', 'add-content', 'out-file', 'new-item', 'clear-content',
  'sqlite3', 'ln', 'mklink', 'chmod', 'chown', 'attrib', 'icacls', 'takeown', 'install', 'dd',
])
const COPIERS = new Set(['cp', 'copy', 'copy-item', 'xcopy', 'rsync', 'scp'])
const SHELLS = new Set(['bash', 'sh', 'zsh', 'dash', 'ksh', 'wsl'])
const POWERSHELLS = new Set(['powershell', 'pwsh', 'powershell_ise'])
const INTERPRETERS: Record<string, string[]> = {
  python: ['-c'], python3: ['-c'], py: ['-c'], pythonw: ['-c'], node: ['-e', '--eval', '-p', '--print'],
  perl: ['-e', '-E'], ruby: ['-e'], php: ['-r'], deno: ['eval'], bun: ['-e', '--eval'],
}
const CD = new Set(['cd', 'chdir', 'pushd', 'set-location', 'push-location', 'popd', 'pop-location'])
const WRAPPERS = new Set(['sudo', 'nohup', 'time', 'command', 'builtin', 'exec', 'nice', 'stdbuf', 'timeout', 'winpty', 'unbuffer', 'chronic'])

// Every git subcommand the guard knows. Anything else may be an alias.
const GIT_KNOWN = new Set(('add am annotate apply archive bisect blame branch bundle cat-file check-attr check-ignore ' +
  'check-mailmap check-ref-format checkout cherry cherry-pick citool clean clone commit commit-graph commit-tree config ' +
  'count-objects credential describe diff diff-files diff-index diff-tree difftool fetch for-each-ref format-patch fsck gc ' +
  'get-tar-commit-id grep hash-object help init interpret-trailers lfs log ls-files ls-remote ls-tree maintenance merge ' +
  'merge-base merge-file merge-tree mktag mktree mv name-rev notes pack-refs prune pull push range-diff read-tree rebase ' +
  'reflog remote repack replace rerere reset restore rev-list rev-parse revert rm shortlog show show-branch show-ref ' +
  'sparse-checkout stash status stripspace submodule switch symbolic-ref tag update-index update-ref var verify-commit ' +
  'verify-pack verify-tag version whatchanged worktree write-tree filter-branch filter-repo').split(' '))

const OPAQUE: [RegExp, string][] = [
  [/\bgit\b[^\n]{0,120}?\b(push|reset|restore|stash|clean|rebase|prune|filter-branch|update-ref|checkout|switch|add)\b/i, 'git yazma komutu'],
  [/\bgit\b[^\n]{0,120}?\bbranch\b[^\n]{0,60}?\s-[a-zA-Z]*D\b/, 'git branch -D'],
  [/\bgit\b[^\n]{0,120}?\bcommit\b[^\n]{0,80}?(\s-[a-zA-Z]*a[a-zA-Z]*\b|--all\b)/, 'git commit -a'],
  [/uv\.lock|jarvis\.db|jarvis\.pre-v2\.db|browser-profile|chrome-profile|jarvis-backups|\.jarvis[\\/]+conversation\.json|(^|[\\/'"\s])\.git([\\/'"\s]|$)/im, 'korunan yol'],
  [/rmtree|remove-item[^\n]*\s-r|\brm\s+-[a-z]*r|\b(rmdir|rd|del)\s+\/s|directory\]::delete|\.rmdir\(|unlinksync|rmsync|rmdirsync|\.rm\(/i, 'özyinelemeli silme'],
]

export function opaqueFinding(text: string): string | null {
  for (const [re, what] of OPAQUE) if (re.test(text)) return what
  return null
}

// What a command line carries from one simple command to the next: the folder
// (`cd`) and whether GIT_DIR / GIT_WORK_TREE now point git elsewhere.
type Box = { dir: string | null; altGit: boolean }
const MAX_DEPTH = 6

export async function judgeShell(command: string, kind: Kind, ctx: Ctx): Promise<string | null> {
  return judgeText(command, kind, ctx, { dir: ctx.cwd, altGit: false }, 0)
}

async function judgeText(text: string, kind: Kind, ctx: Ctx, box: Box, depth: number): Promise<string | null> {
  if (depth > MAX_DEPTH) return refusal('X2', 'iç içe komut çok derin')
  const parsed = parse(text, kind)
  if (parsed.problems.length > 0) return refusal('X2', 'komut okunamadı (' + parsed.problems[0] + ')')
  const outer: (string | null)[] = [] // the folder each open subshell started in
  for (const cmd of parsed.commands) {
    while (outer.length < cmd.depth) outer.push(box.dir)
    while (outer.length > cmd.depth) box.dir = outer.pop() ?? null
    for (const inner of cmd.nested) {
      const r = await judgeText(inner, kind, ctx, { dir: box.dir, altGit: box.altGit }, depth + 1)
      if (r) return r
    }
    const r = await judgeSimple(cmd, kind, ctx, box, depth, text)
    if (r) return r
  }
  return null
}

// An absolute normal-form path for a word, or null when the guard cannot know
// it (a variable it cannot expand, or a relative path in an unknown folder).
function resolve(word: Word, dir: string | null, ctx: Ctx): string | null {
  const ex = expandHome(word.text, ctx.home)
  if (ex.dynamic) return null
  const n = normalize(ex.text)
  if (isAbsolute(n)) return n
  if (dir === null) return null
  return join(dir, ex.text)
}

function isOpt(word: Word, kind: Kind): boolean {
  if (word.quoted && !word.text.startsWith('-')) return false
  if (word.text.startsWith('-') && word.text.length > 1) return true
  // cmd-style switches: /s /q /mir (one or a few letters, never a path segment)
  return (kind === 'cmd' || kind === 'powershell') && /^(\/[A-Za-z]{1,6})+(:\S*)?$/.test(word.text)
}

function joinWords(words: Word[]): string {
  return words.map(w => (/[\s;&|<>()]/.test(w.text) ? "'" + w.text.replace(/'/g, "''") + "'" : w.text)).join(' ')
}

const KEYWORDS = new Set(['if', 'then', 'elif', 'else', 'do', 'while', 'until', '!', 'coproc', 'fi', 'done', 'esac'])
// launchers that run the command after their own subcommand and options
const LAUNCHERS: Record<string, string[]> = { uv: ['run'], pipx: ['run'], poetry: ['run'], pdm: ['run'], hatch: ['run'], conda: ['run'], pnpm: ['exec', 'dlx'], yarn: ['exec', 'dlx'], npm: ['exec'] }
const BARE_LAUNCHERS = new Set(['uvx', 'npx', 'bunx'])
const LAUNCHER_VALUE = /^(--with|--with-requirements|--python|-p|--project|--directory|--package|--from|--env-file|--extra|--group|-n|--name|--prefix|-e|--env|--spec)$/

// Strips what only runs the real command: assignments (bash `X=1 cmd`,
// PowerShell `$x = cmd`), shell keywords (`if`, `then`, `do`, `!`), env,
// sudo, nohup, time, launchers (`uv run`, `npx`), xargs (whose arguments then
// come from stdin), PowerShell's `.`, cmd's `if [not] exist X`. The names of
// the variables assigned on the way are returned too.
function unwrap(words: Word[], kind: Kind): { argv: Word[]; fromStdin: boolean; env: string[] } {
  let argv = words.slice()
  let fromStdin = false
  const env: string[] = []
  for (let guard = 0; guard < 40 && argv.length > 0; guard++) {
    const first = argv[0]
    // (a quoted value still makes an assignment: GIT_TRACE="1" git push)
    if (kind !== 'powershell' && /^[A-Za-z_][A-Za-z0-9_]*=/.test(first.text)) {
      env.push(first.text.slice(0, first.text.indexOf('=')).toUpperCase())
      argv = argv.slice(1)
      continue
    }
    if (kind === 'powershell' && !first.quoted && /^\$[\w:]+$/.test(first.text) && argv[1] && /^[-+*\/]?=$/.test(argv[1].text)) {
      env.push(first.text.replace(/^\$(env:)?/i, '').toUpperCase())
      argv = argv.slice(2)
      continue
    }
    if (kind === 'powershell' && /^\$[\w:]+=/.test(first.text)) {
      env.push(first.text.slice(1, first.text.indexOf('=')).replace(/^env:/i, '').toUpperCase())
      argv = argv.slice(1)
      continue
    }
    if (!first.quoted && (kind === 'bash' ? KEYWORDS.has(first.text) : kind === 'cmd' && /^(do|else)$/i.test(first.text))) {
      argv = argv.slice(1)
      continue
    }
    if (kind === 'cmd' && /^if$/i.test(first.text)) {
      let i = 1
      if (/^\/i$/i.test(argv[i]?.text ?? '')) i++
      if (/^not$/i.test(argv[i]?.text ?? '')) i++
      const test = (argv[i]?.text ?? '').toLowerCase()
      if (test === 'exist' || test === 'defined' || test === 'errorlevel') i += 2
      else if (argv[i + 1] && /^(==|equ|neq|lss|leq|gtr|geq)$/i.test(argv[i + 1].text)) i += 3
      else i += 1
      argv = argv.slice(i)
      continue
    }
    const name = programName(first.text)
    if (LAUNCHERS[name] && argv[1] && LAUNCHERS[name].includes(argv[1].text)) {
      let i = 2
      while (i < argv.length && argv[i].text.startsWith('-')) i += LAUNCHER_VALUE.test(argv[i].text) ? 2 : 1
      argv = argv.slice(i)
      continue
    }
    if (BARE_LAUNCHERS.has(name)) {
      let i = 1
      while (i < argv.length && argv[i].text.startsWith('-')) i += LAUNCHER_VALUE.test(argv[i].text) ? 2 : 1
      argv = argv.slice(i)
      continue
    }
    if (first.text === '.' || first.text === '&') {
      argv = argv.slice(1)
      continue
    }
    if (name === 'env') {
      let i = 1
      while (i < argv.length) {
        const t = argv[i].text
        if (t === '-u' || t === '--unset' || t === '-C' || t === '--chdir') i += 2
        else if (t === '-S' || t === '--split-string') {
          // env -S 'cmd args': the string is the command line
          const s = argv[i + 1]
          argv = s ? [s] : []
          i = -1
          break
        } else if (/^[A-Za-z_][A-Za-z0-9_]*=/.test(t)) {
          env.push(t.slice(0, t.indexOf('=')).toUpperCase())
          i++
        } else if (t.startsWith('-')) i++
        else break
      }
      if (i === -1) return { argv: [{ text: '__env_split__', dynamic: true, glob: false, quoted: false }, ...argv], fromStdin, env }
      argv = argv.slice(i)
      continue
    }
    if (name === 'xargs') {
      let i = 1
      while (i < argv.length && argv[i].text.startsWith('-')) {
        const t = argv[i].text
        i += /^-([nLIdPsaEe]|-max-args|-replace|-delimiter|-max-procs|-arg-file|-max-chars)$/.test(t) ? 2 : 1
      }
      argv = argv.slice(i)
      fromStdin = true
      continue
    }
    if (WRAPPERS.has(name)) {
      let i = 1
      while (i < argv.length && argv[i].text.startsWith('-')) {
        const t = argv[i].text
        i += /^-(u|g|h|p|C|D|n|i|o|e|k|s|-signal|-kill-after|-user|-group)$/.test(t) ? 2 : 1
      }
      // timeout DURATION cmd
      if (name === 'timeout' && i < argv.length && /^\d/.test(argv[i].text)) i++
      argv = argv.slice(i)
      continue
    }
    break
  }
  return { argv, fromStdin, env }
}

const GIT_ENV = /^GIT_(DIR|WORK_TREE|INDEX_FILE|COMMON_DIR|OBJECT_DIRECTORY|NAMESPACE)$/

async function judgeSimple(cmd: Simple, kind: Kind, ctx: Ctx, box: Box, depth: number, fullText: string): Promise<string | null> {
  // output redirections are writes
  for (const target of cmd.redirects) {
    const r = await protectedWrite(target, box.dir, ctx, 'yönlendirme')
    if (r) return r
  }
  const { argv, fromStdin, env } = unwrap(cmd.words, kind)
  const gitEnv = env.some(v => GIT_ENV.test(v))
  // PowerShell's $env:X = ... lasts for the rest of the line, like bash's X=1 alone
  if (gitEnv && kind === 'powershell') box.altGit = true
  if (argv.length === 0) {
    if (gitEnv) box.altGit = true // GIT_DIR=x on its own line points every later git elsewhere
    return null
  }
  if (argv[0].text === '__env_split__') return argv[1] ? judgeText(argv[1].text, 'bash', ctx, { dir: box.dir, altGit: box.altGit }, depth + 1) : null
  let name = programName(argv[0].text)
  if (name.includes('$') || name.includes('%')) return refusal('X2', 'program adı değişkenden geliyor: ' + argv[0].text)
  if (kind === 'powershell' && PS_ALIASES[name]) name = PS_ALIASES[name]
  if (/^python[\d.]*w?$/.test(name)) name = 'python'
  const args = argv.slice(1)
  if (name === 'export' || name === 'set' || name === 'setx' || name === 'declare' || name === 'typeset' || name === 'set-item') {
    if (args.some(a => GIT_ENV.test(a.text.replace(/^env:/i, '').split('=')[0].toUpperCase()))) box.altGit = true
    return null
  }
  const guarded = name === 'git' || DELETERS.has(name) || MOVERS.has(name) || WRITERS.has(name) || COPIERS.has(name) || name === 'find'
  if (fromStdin && guarded) return refusal('X2', 'xargs ile hedefleri stdin\'den alan ' + name)
  const stdinCode = cmd.piped || cmd.heredocs.length > 0

  if (CD.has(name)) {
    if (kind === 'bash' && (cmd.piped || cmd.pipeOut)) return null // a pipeline stage is a subshell
    if (name === 'popd' || name === 'pop-location') box.dir = null
    else {
      const target = args.find(a => !isOpt(a, kind) || a.text === '-')
      box.dir = target === undefined ? ctx.home : target.text === '-' ? null : resolve(target, box.dir, ctx)
    }
    return null
  }

  if (name === 'git') return judgeGit(args, kind, ctx, box, gitEnv)

  if (SHELLS.has(name)) {
    const c = args.findIndex(a => /^-[a-z]*c[a-z]*$/.test(a.text) && !a.text.startsWith('--'))
    if (c >= 0) {
      const script = args[c + 1]
      if (!script) return null
      if (script.dynamic) return refusal('X2', name + ' -c ile değişkenli komut')
      return judgeText(script.text, 'bash', ctx, { dir: box.dir, altGit: box.altGit }, depth + 1)
    }
    if (args.some(a => !isOpt(a, kind))) return null // runs a script file (opaque; see the known limits)
    for (const doc of cmd.heredocs) {
      const r = await judgeText(doc.body, 'bash', ctx, { dir: box.dir, altGit: box.altGit }, depth + 1)
      if (r) return r
    }
    if (cmd.piped) {
      const hit = opaqueFinding(fullText)
      if (hit) return refusal('X3', name + ' stdin\'den komut okuyor: ' + hit)
    }
    return null
  }

  if (POWERSHELLS.has(name)) return judgePowerShellExe(args, cmd, ctx, box, depth, fullText)

  if (name === 'cmd') {
    const c = args.findIndex(a => /^\/[ck]$/i.test(a.text))
    if (c < 0) return null
    const rest = args.slice(c + 1)
    if (rest.some(w => w.dynamic)) return refusal('X2', 'cmd /c ile değişkenli komut')
    return judgeText(rest.map(w => w.text).join(' '), 'cmd', ctx, { dir: box.dir, altGit: box.altGit }, depth + 1)
  }

  if (name === 'invoke-expression' || name === 'eval') {
    const rest = args.filter(a => !/^-command$/i.test(a.text))
    if (rest.some(w => w.dynamic)) return refusal('X2', name + ' ile değişkenli komut')
    return judgeText(rest.map(w => w.text).join(' '), name === 'eval' ? 'bash' : 'powershell', ctx, { dir: box.dir, altGit: box.altGit }, depth + 1)
  }

  if (name === 'start-process') {
    let file: Word | null = null
    const rest: Word[] = []
    for (let i = 0; i < args.length; i++) {
      const t = args[i].text.toLowerCase()
      if (/^-(filepath|fi|fil|file)/.test(t) || t === '-path') file = args[++i] ?? null
      else if (/^-(argumentlist|args|ar|arg)/.test(t)) {
        i++
        while (i < args.length && !isOpt(args[i], 'powershell')) rest.push(args[i++])
        i--
      } else if (/^-(workingdirectory|wo)/.test(t) || /^-(verb|windowstyle|redirect)/.test(t)) i++
      else if (isOpt(args[i], 'powershell')) continue
      else if (file === null) file = args[i]
      else rest.push(args[i])
    }
    if (file === null) return null
    if (file.dynamic || rest.some(w => w.dynamic)) return refusal('X2', 'Start-Process ile değişkenli komut')
    return judgeText(joinWords([file]) + ' ' + rest.map(w => w.text).join(' '), 'powershell', ctx, { dir: box.dir, altGit: box.altGit }, depth + 1)
  }

  if (INTERPRETERS[name]) {
    if (name === 'perl' && args.some(a => /^-[a-zA-Z]*i/.test(a.text))) {
      const r = await protectedArgs(args.filter(a => a.text !== args[args.findIndex(b => b.text === '-e') + 1]?.text), kind, box.dir, ctx, 'perl -i')
      if (r) return r
    }
    const flags = INTERPRETERS[name]
    const at = args.findIndex(a => flags.includes(a.text) || (name === 'python' && /^-[a-zA-Z]*c$/.test(a.text)))
    if (at >= 0) {
      const code = args[at + 1]
      const hit = code ? opaqueFinding(code.text) : null
      return hit ? refusal('X3', name + ' ' + args[at].text + ': ' + hit) : null
    }
    const script = args.find(a => !isOpt(a, kind) && a.text !== '-')
    if (script === undefined || args.some(a => a.text === '-')) {
      if (stdinCode) {
        for (const doc of cmd.heredocs) {
          const hit = opaqueFinding(doc.body)
          if (hit) return refusal('X3', name + ' stdin betiği: ' + hit)
        }
        if (cmd.piped) {
          const hit = opaqueFinding(fullText)
          if (hit) return refusal('X3', name + ' stdin\'den betik okuyor: ' + hit)
        }
      }
    }
    return null
  }

  // .NET calls in PowerShell ([IO.Directory]::Delete(...), [IO.File]::WriteAllText(...))
  if (kind === 'powershell' && argv.some(w => /\]::/.test(w.text))) {
    const raw = argv.map(w => w.text).join(' ')
    if (/directory\]::delete/i.test(raw)) return refusal('R9', '.NET Directory.Delete denetlenemez')
    if (/\]::(delete|move|copy|replace|write|append|create|open)/i.test(raw)) {
      const hit = opaqueFinding(raw)
      if (hit) return refusal('R10', '.NET dosya çağrısı: ' + hit)
    }
  }

  if (name === 'find') return judgeFind(args, kind, ctx, box, depth)
  if (name === 'robocopy') return judgeRobocopy(args, kind, ctx, box)
  if (name === 'sed' || name === 'perl') {
    if (args.some(a => /^-[a-zA-Z]*i/.test(a.text) || a.text.startsWith('--in-place'))) return protectedArgs(args, kind, box.dir, ctx, name + ' -i')
    return null
  }

  if (DELETERS.has(name)) {
    const targets = pathArgs(args, kind)
    if (targets.length === 0) return cmd.piped ? refusal('R9', name + ' hedefleri boru hattından geliyor') : null
    const r = await protectedMoveDelete(targets, box.dir, ctx, 'silme')
    if (r) return r
    return isRecursiveDelete(name, args, kind) ? recursiveTargets(targets, box.dir, ctx, name) : null
  }
  if (MOVERS.has(name)) {
    const paths = pathArgs(args, kind)
    const r = await protectedMoveDelete(paths, box.dir, ctx, 'taşıma')
    if (r) return r
    return intoFolder(args, paths, kind, box.dir, ctx, 'taşıma hedefi')
  }
  if (WRITERS.has(name)) {
    if (name === 'sqlite3' && args.some(a => /^--?readonly$/i.test(a.text))) {
      if (stdinCode || args.some(a => /(^|\n|;)\s*\.(shell|system|output|once|backup|save|import|load|open|excel|cd)\b/i.test(a.text))) {
        return refusal('X3', 'sqlite3 nokta komutu ya da stdin betiği')
      }
      return null
    }
    if (name === 'dd') {
      for (const a of args) {
        if (/^of=/i.test(a.text)) {
          const r = await protectedWrite({ ...a, text: a.text.slice(3) }, box.dir, ctx, 'dd of=')
          if (r) return r
        }
      }
      return null
    }
    return protectedArgs(args, kind, box.dir, ctx, 'yazma')
  }
  if (COPIERS.has(name)) {
    const paths = pathArgs(args, kind)
    const r = await intoFolder(args, paths, kind, box.dir, ctx, 'kopyalama hedefi')
    if (r) return r
    if (name === 'rsync' && args.some(a => a.text.startsWith('--del'))) {
      const dest = paths[paths.length - 1]
      if (dest) return recursiveTargets([dest], box.dir, ctx, 'rsync --delete')
    }
    return null
  }
  return null
}

// The destination of a copy or move, and when it is a folder, each source's
// name inside it (`cp x/uv.lock .` writes ./uv.lock).
async function intoFolder(args: Word[], paths: Word[], kind: Kind, dir: string | null, ctx: Ctx, what: string): Promise<string | null> {
  const flag = args.findIndex(a => /^-(destination|de|des|dest|target|t)$/i.test(a.text) || a.text === '--target-directory')
  const glued = args.find(a => a.text.startsWith('--target-directory='))
  const dest = glued ? { ...glued, text: glued.text.slice('--target-directory='.length) }
    : flag >= 0 ? args[flag + 1] : paths.length >= 2 ? paths[paths.length - 1] : undefined
  if (dest === undefined) return null
  const r = await protectedWrite(dest, dir, ctx, what)
  if (r) return r
  const sources = paths.filter(p => p !== dest && p.text !== dest.text)
  const destAbs = resolve(dest, dir, ctx)
  if (destAbs === null) return null
  const named = glued !== undefined || (flag >= 0 && /^(-t|--target-directory)$/.test(args[flag].text)) // -t names a folder
  const isFolder = named || /[\\/]$/.test(dest.text) || dest.text === '.' || dest.text === '..' || (await ctx.kindOf(destAbs)) === 'dir'
  if (!isFolder) return null
  for (const src of sources) {
    const base = normalize(expandHome(src.text, ctx.home).text).split('/').pop() ?? ''
    if (base === '') continue
    const r2 = await protectedWrite({ ...src, text: destAbs + '/' + base, quoted: true }, dir, ctx, what)
    if (r2) return r2
  }
  return null
}

// Everything a protected path lives in: moving or deleting one of these
// moves or deletes the protected path with it.
function protectedItems(ctx: Ctx): string[] {
  const r = ctx.root
  const items = [r + '/uv.lock', r + '/.git', r + '/data/jarvis.db', r + '/data/jarvis.db-wal', r + '/data/jarvis.db-shm',
    r + '/data/jarvis.pre-v2.db', r + '/data/browser-profile', r + '/data/chrome-profile']
  if (ctx.home !== null) items.push(ctx.home + '/jarvis-backups', ctx.home + '/.jarvis/conversation.json')
  return items
}

function globRegex(absGlob: string): RegExp {
  let re = ''
  for (let i = 0; i < absGlob.length; i++) {
    const c = absGlob[i]
    if (c === '*') re += '[^/]*'
    else if (c === '?') re += '[^/]'
    else if (c === '[') {
      const end = absGlob.indexOf(']', i + 1)
      if (end < 0) re += '\\['
      else {
        re += '[' + absGlob.slice(i + 1, end).replace(/^!/, '^').replace(/\\/g, '\\\\') + ']'
        i = end
      }
    } else re += c.replace(/[.+^${}()|\\]/g, '\\$&')
  }
  return new RegExp('^' + re + '$', 'i')
}

// A glob that could name a protected path, or a folder holding one.
function globHitsProtected(absGlob: string, ctx: Ctx): string | null {
  const re = globRegex(absGlob)
  for (const item of protectedItems(ctx)) {
    let q = item
    while (q.length > 3) {
      if (re.test(q)) return item
      const cut = q.lastIndexOf('/')
      if (cut <= 2) break
      q = q.slice(0, cut)
    }
  }
  return null
}

async function protectedMoveDelete(targets: Word[], dir: string | null, ctx: Ctx, what: string): Promise<string | null> {
  for (const t of targets) {
    const r = await protectedWrite(t, dir, ctx, what)
    if (r) return r
    const abs = resolve(t, dir, ctx)
    if (abs === null) {
      if (/^\$null$/i.test(t.text)) continue
      return refusal('R10', what + ': hedef okunamıyor (değişken ya da bilinmeyen klasör): ' + t.text)
    }
    if (hasGlob(abs)) continue
    const real = (await ctx.realPath(abs)) ?? abs
    for (const p of [abs, real]) {
      const holds = protectedItems(ctx).find(item => item.startsWith(p + '/'))
      if (holds) return refusal('R10', what + ': ' + t.text + ' (' + holds + ' bunun içinde)')
    }
  }
  return null
}

async function judgePowerShellExe(args: Word[], cmd: Simple, ctx: Ctx, box: Box, depth: number, fullText: string): Promise<string | null> {
  for (let i = 0; i < args.length; i++) {
    const t = args[i].text
    if (!/^[-/]/.test(t) || args[i].quoted) {
      // powershell.exe reads the rest as the command
      const rest = args.slice(i)
      if (rest.some(w => w.dynamic)) return refusal('X2', 'powershell ile değişkenli komut')
      return judgeText(rest.map(w => w.text).join(' '), 'powershell', ctx, { dir: box.dir, altGit: box.altGit }, depth + 1)
    }
    const p = t.replace(/^[-/]+/, '').toLowerCase().replace(/:.*$/, '')
    if (p === 'e' || p === 'ec' || p.startsWith('enc')) return refusal('X2', 'kodlanmış PowerShell komutu (-EncodedCommand) denetlenemez')
    if (p === 'c' || (p.length >= 2 && 'command'.startsWith(p))) {
      const rest = args.slice(i + 1)
      if (rest.length === 1 && rest[0].text === '-') return stdinPowerShell(cmd, ctx, box, depth, fullText)
      if (rest.some(w => w.dynamic)) return refusal('X2', 'powershell -Command ile değişkenli komut')
      return judgeText(rest.map(w => w.text).join(' '), 'powershell', ctx, { dir: box.dir, altGit: box.altGit }, depth + 1)
    }
    if (p === 'f' || (p.length >= 2 && 'file'.startsWith(p))) return null // a script file (opaque; see the known limits)
    if (/^(ex|ep|exe|exec|executionpolicy|w|wi|win|windowstyle|o|of|outputformat|if|inputformat|config|configurationname|v|ver|version|wd|wo|workingdirectory|psconsolefile|settingsfile)$/.test(p) ||
        'executionpolicy'.startsWith(p) && p.length >= 2) {
      i++
      continue
    }
  }
  return stdinPowerShell(cmd, ctx, box, depth, fullText)
}

async function stdinPowerShell(cmd: Simple, ctx: Ctx, box: Box, depth: number, fullText: string): Promise<string | null> {
  for (const doc of cmd.heredocs) {
    const r = await judgeText(doc.body, 'powershell', ctx, { dir: box.dir, altGit: box.altGit }, depth + 1)
    if (r) return r
  }
  if (cmd.piped) {
    const hit = opaqueFinding(fullText)
    if (hit) return refusal('X3', 'powershell stdin\'den komut okuyor: ' + hit)
  }
  return null
}

// ---- paths in commands

function pathArgs(args: Word[], kind: Kind): Word[] {
  const out: Word[] = []
  let endOfOptions = false
  for (let i = 0; i < args.length; i++) {
    const a = args[i]
    if (!endOfOptions && a.text === '--') {
      endOfOptions = true
      continue
    }
    if (!endOfOptions && isOpt(a, kind)) {
      // PowerShell -Path / -LiteralPath / -Destination take the next word
      if (kind === 'powershell' && /^-(path|literalpath|lp|pspath|destination|filepath|target|value)$/i.test(a.text.replace(/:.*$/, ''))) {
        const inline = a.text.includes(':') ? a.text.slice(a.text.indexOf(':') + 1) : ''
        if (inline) out.push({ ...a, text: inline })
        else if (args[i + 1] && /^-(path|literalpath|lp|pspath|destination|filepath|target)$/i.test(a.text)) out.push(args[++i])
      }
      continue
    }
    out.push(a)
  }
  return out
}

async function protectedArgs(args: Word[], kind: Kind, dir: string | null, ctx: Ctx, what: string): Promise<string | null> {
  for (const a of pathArgs(args, kind)) {
    const r = await protectedWrite(a, dir, ctx, what)
    if (r) return r
  }
  return null
}

async function protectedWrite(word: Word, dir: string | null, ctx: Ctx, what: string): Promise<string | null> {
  const literal = protectedKind(expandHome(word.text, ctx.home).text)
  if (literal) return refusal('R10', what + ': ' + word.text + ' (' + literal + ')')
  const abs = resolve(word, dir, ctx)
  if (abs === null) return null
  const kindAbs = protectedKind(abs)
  if (kindAbs) return refusal('R10', what + ': ' + word.text + ' (' + kindAbs + ')')
  if (hasGlob(abs)) {
    const prefix = abs.slice(0, abs.search(/[*?[]/)).replace(/\/[^/]*$/, '')
    const realPrefix = await ctx.realPath(prefix)
    const tail = abs.slice(prefix.length)
    for (const candidate of [abs, realPrefix === null ? abs : realPrefix + tail]) {
      const hit = globHitsProtected(candidate, ctx)
      if (hit) return refusal('R10', what + ': ' + word.text + ' (' + hit + ' ile eşleşebilir)')
    }
    return null
  }
  const real = await ctx.realPath(abs)
  const kindReal = real ? protectedKind(real) : null
  if (kindReal) return refusal('R10', what + ': ' + word.text + ' -> ' + real + ' (' + kindReal + ')')
  return null
}

function isRecursiveDelete(name: string, args: Word[], kind: Kind): boolean {
  if (name === 'rimraf') return true
  for (const a of args) {
    const t = a.text
    if (t === '--') break
    if (name === 'remove-item' && kind === 'powershell') {
      const p = t.toLowerCase().replace(/:.*$/, '')
      if (/^-r/.test(p) && 'recurse'.startsWith(p.slice(1))) return true
      continue
    }
    if (/^\/(.*\/)?s(\/|$)/i.test(t) && (name === 'del' || name === 'erase' || name === 'rd' || name === 'rmdir' || name === 'remove-item')) return true
    if (name === 'rm' || name === 'remove-item') {
      if (/^-[a-zA-Z]*[rR]/.test(t) && !t.startsWith('--')) return true
      if (/^--rec/.test(t)) return true
    }
  }
  return false
}

async function recursiveTargets(targets: Word[], dir: string | null, ctx: Ctx, name: string): Promise<string | null> {
  const build = ctx.root + '/build'
  const trees = ctx.root + '/.claude/worktrees'
  const allowed = (p: string, isGlobDir: boolean) => {
    if (isGlobDir ? inside(p, build) : strictlyInside(p, build)) return true
    const wt = worktreeOf(p, ctx.root)
    return wt !== null && inside(p, wt) && strictlyInside(p, trees)
  }
  for (const t of targets) {
    const abs = resolve(t, dir, ctx)
    if (abs === null) return refusal('R9', name + ': hedef değişkenden geliyor ya da klasör bilinmiyor: ' + t.text)
    if (hasGlob(t.text) || t.glob) {
      const at = abs.search(/[*?[{]/)
      const prefix = abs.slice(0, at).replace(/\/[^/]*$/, '')
      if (!allowed(prefix, true)) return refusal('R9', name + ' ' + t.text)
      const realPrefix = await ctx.realPath(prefix)
      if (realPrefix !== null && !allowed(realPrefix, true)) return refusal('R9', name + ' ' + t.text + ' -> ' + realPrefix + ' (bağlantı dışarı çıkıyor)')
      continue
    }
    if (!allowed(abs, false)) return refusal('R9', name + ' ' + t.text)
    const real = await ctx.realPath(abs)
    if (real !== null && !allowed(real, false)) return refusal('R9', name + ' ' + t.text + ' -> ' + real + ' (bağlantı dışarı çıkıyor)')
  }
  return null
}

async function judgeFind(args: Word[], kind: Kind, ctx: Ctx, box: Box, depth: number): Promise<string | null> {
  const roots: Word[] = []
  let i = 0
  while (i < args.length && !/^[-(!]/.test(args[i].text)) roots.push(args[i++])
  const startAt = roots.length ? roots : [{ text: '.', dynamic: false, glob: false, quoted: false }]
  for (; i < args.length; i++) {
    const t = args[i].text
    if (t === '-delete') {
      const r = (await protectedMoveDelete(startAt, box.dir, ctx, 'find -delete')) ?? (await recursiveTargets(startAt, box.dir, ctx, 'find -delete'))
      if (r) return r
    }
    if ((t === '-exec' || t === '-execdir' || t === '-ok' || t === '-okdir') && args[i + 1]) {
      let prog = programName(args[i + 1].text)
      if (kind === 'powershell' && PS_ALIASES[prog]) prog = PS_ALIASES[prog]
      if (DELETERS.has(prog) || MOVERS.has(prog)) {
        // the files find hands over all live under its starting folders
        const r = (await protectedMoveDelete(startAt, box.dir, ctx, 'find -exec ' + prog)) ?? (await recursiveTargets(startAt, box.dir, ctx, 'find -exec ' + prog))
        if (r) return r
      }
    }
    if (t === '-exec' || t === '-execdir' || t === '-ok' || t === '-okdir') {
      const sub: Word[] = []
      i++
      while (i < args.length && args[i].text !== ';' && args[i].text !== '+') {
        sub.push(args[i].text === '{}' ? { text: '{}', dynamic: true, glob: false, quoted: false } : args[i])
        i++
      }
      if (sub.length) {
        const r = await judgeSimple({ words: sub, redirects: [], heredocs: [], piped: false, nested: [] }, kind, ctx, { dir: box.dir, altGit: box.altGit }, depth + 1, '')
        if (r) return r
      }
    }
  }
  return null
}

async function judgeRobocopy(args: Word[], kind: Kind, ctx: Ctx, box: Box): Promise<string | null> {
  const paths = args.filter(a => !/^\//.test(a.text))
  const flags = args.filter(a => /^\//.test(a.text)).map(a => a.text.toLowerCase().replace(/:.*$/, ''))
  const [src, dst] = paths
  const files = paths.slice(2) // robocopy <src> <dst> [file filters]
  const inside = (base: Word, name: Word): Word => ({ ...name, text: base.text.replace(/[\\/]+$/, '') + '/' + name.text, dynamic: base.dynamic || name.dynamic })
  if (dst) {
    for (const target of [dst, ...files.map(f => inside(dst, f))]) {
      const r = await protectedWrite(target, box.dir, ctx, 'robocopy hedefi')
      if (r) return r
    }
    if (flags.includes('/mir') || flags.includes('/purge')) {
      const r = await recursiveTargets([dst], box.dir, ctx, 'robocopy ' + flags.join(' '))
      if (r) return r
    }
  }
  if (src && (flags.includes('/mov') || flags.includes('/move'))) {
    const moved = files.length > 0 ? files.map(f => inside(src, f)) : [src]
    const r = await protectedMoveDelete(flags.includes('/move') ? [src, ...moved] : moved, box.dir, ctx, 'robocopy taşıma')
    if (r) return r
    if (flags.includes('/move')) return recursiveTargets([src], box.dir, ctx, 'robocopy /move')
  }
  return null
}

// ---- git

function longPrefix(text: string, names: string[]): boolean {
  const opt = text.replace(/=.*$/, '')
  if (!opt.startsWith('--') || opt.length < 3) return false
  return names.some(name => name.startsWith(opt))
}

function shortCluster(text: string): string {
  return /^-[A-Za-z]+$/.test(text) ? text.slice(1) : ''
}

async function judgeGit(args: Word[], kind: Kind, ctx: Ctx, box: Box, envAlt: boolean): Promise<string | null> {
  let i = 0
  const dirs: Word[] = []
  const configs: string[] = []
  let altDir = envAlt || box.altGit
  while (i < args.length) {
    const w = args[i]
    const a = w.text
    if (a === '-C') {
      if (!args[i + 1]) return refusal('X2', 'git -C klasörsüz')
      dirs.push(args[i + 1])
      i += 2
      continue
    }
    if (a.startsWith('-C') && a.length > 2 && !a.startsWith('--')) {
      dirs.push({ ...w, text: a.slice(2) })
      i++
      continue
    }
    if (a === '-c' || (a.startsWith('-c') && !a.startsWith('--'))) {
      const value = a === '-c' ? args[i + 1] : { ...w, text: a.slice(2) }
      if (!value || value.dynamic || /^\s*alias\./i.test(value.text)) return refusal('X1', 'git -c alias.* / değişkenli yapılandırma: ' + (value ? value.text : ''))
      configs.push(value.text.trim().toLowerCase())
      i += a === '-c' ? 2 : 1
      continue
    }
    if (/^--config-env(=|$)/.test(a)) {
      const value = a.includes('=') ? a.slice(a.indexOf('=') + 1) : args[i + 1] ? args[i + 1].text : ''
      if (/alias\./i.test(value)) return refusal('X1', 'git --config-env alias.*')
      i += a.includes('=') ? 1 : 2
      continue
    }
    if (/^--(git-dir|work-tree)(=|$)/.test(a)) {
      altDir = true
      i += a.includes('=') ? 1 : 2
      continue
    }
    if (a === '--namespace' || a === '--super-prefix' || a === '--attr-source') {
      i += 2
      continue
    }
    if (a.startsWith('-')) {
      i++
      continue
    }
    break
  }
  if (i >= args.length) return null
  if (args[i].dynamic) return refusal('X1', 'git alt komutu değişkenden: ' + args[i].text)
  const sub = args[i].text.toLowerCase()
  const rest = args.slice(i + 1)
  if (!GIT_KNOWN.has(sub)) return refusal('X1', 'git ' + args[i].text + ' (takma ad olabilir; gerçek alt komutu yazın)')
  let dir = box.dir
  for (const d of dirs) dir = resolve(d, dir, ctx)
  const shown = 'git ' + args.slice(i).map(w => w.text).join(' ')

  switch (sub) {
    case 'push':
      if (configs.some(c => /^(push\.|remote\.)/.test(c))) return refusal('R1', shown + ' (-c push.* / remote.* yok)')
      return judgePush(rest, shown)
    case 'pull': {
      // a pull may rebase (--rebase, -r, pull.rebase): then it is a rebase
      const rebase = rest.some(w => w.text.startsWith('--rebase') || shortCluster(w.text).includes('r')) || configs.some(c => /^(pull\.rebase|branch\..*\.rebase)/.test(c))
      const merges = rest.some(w => w.text === '--ff-only' || w.text === '--no-rebase')
      return merges && !rebase ? null : rebaseFolder(altDir, dir, ctx, shown)
    }
    case 'add':
      return judgeAdd(rest, dir, ctx, shown)
    case 'commit': {
      const r = judgeCommit(rest, shown)
      if (typeof r === 'string') return r
      if (dir === null) return refusal('R12', 'commit klasörü bilinmiyor; assert denetimi yapılamaz')
      return ctx.onCommit(dir, r.amend)
    }
    case 'reset':
    case 'restore':
    case 'stash':
    case 'clean':
      return refusal('R4', shown)
    case 'checkout':
      return judgeCheckout(rest, dir, ctx, shown)
    case 'switch':
      for (const w of rest) {
        const t = w.text
        if (t === '--') break
        if (t === '-C' || longPrefix(t, ['--force-create'])) return refusal('X4', shown + ' (-C dalın üzerine yazar)')
        if (longPrefix(t, ['--discard-changes', '--force']) || shortCluster(t).includes('f')) return refusal('R5', shown)
      }
      return null
    case 'rebase':
      return rebaseFolder(altDir, dir, ctx, shown)
    case 'worktree': {
      const action = (rest.find(w => !w.text.startsWith('-'))?.text ?? '').toLowerCase()
      if (action === 'prune') return refusal('R7', shown)
      if (action === 'remove' && rest.some(w => shortCluster(w.text).includes('f') || longPrefix(w.text, ['--force']))) return refusal('R7', shown)
      return null
    }
    case 'branch':
      return judgeBranch(rest, shown)
    case 'update-ref':
    case 'filter-branch':
    case 'filter-repo':
    case 'prune':
      return refusal('X4', shown)
    case 'symbolic-ref':
      return rest.filter(w => !w.text.startsWith('-')).length >= 2 || rest.some(w => w.text === '-d' || w.text === '--delete') ? refusal('X4', shown) : null
    case 'reflog': {
      const action = (rest.find(w => !w.text.startsWith('-'))?.text ?? '').toLowerCase()
      return action === 'expire' || action === 'delete' ? refusal('X4', shown) : null
    }
    case 'gc':
      return rest.some(w => w.text.startsWith('--prune')) ? refusal('X4', shown) : null
    case 'rm': {
      const paths = rest.filter(w => !w.text.startsWith('-'))
      for (const p of paths) {
        const r = await protectedWrite(p, dir, ctx, 'git rm')
        if (r) return r
      }
      if (rest.some(w => w.text === '-r' || /^-[a-zA-Z]*r/.test(w.text) && !w.text.startsWith('--'))) return recursiveTargets(paths, dir, ctx, 'git rm -r')
      return null
    }
    case 'mv':
      for (const p of rest.filter(w => !w.text.startsWith('-'))) {
        const r = await protectedWrite(p, dir, ctx, 'git mv')
        if (r) return r
      }
      return null
    default:
      return null
  }
}

async function rebaseFolder(altDir: boolean, dir: string | null, ctx: Ctx, shown: string): Promise<string | null> {
  if (altDir) return refusal('R6', 'GIT_DIR / --git-dir / --work-tree ile rebase denetlenemez')
  if (dir === null) return refusal('R6', 'rebase klasörü bilinmiyor')
  const real = (await ctx.realPath(dir)) ?? dir
  if (worktreeOf(real, ctx.root) === null || worktreeOf(dir, ctx.root) === null) return refusal('R6', shown + ' (' + real + ')')
  return null
}

function judgePush(rest: Word[], shown: string): string | null {
  const VALUE = new Set(['-o', '--push-option', '--repo', '--receive-pack', '--exec'])
  const positional: Word[] = []
  let repoGiven = false
  for (let i = 0; i < rest.length; i++) {
    const w = rest[i]
    const t = w.text
    if (t === '--') {
      positional.push(...rest.slice(i + 1))
      break
    }
    if (t.startsWith('--repo=')) {
      repoGiven = true
      continue
    }
    if (VALUE.has(t)) {
      if (t === '--repo') repoGiven = true
      i++
      continue
    }
    if (longPrefix(t, ['--all', '--mirror', '--tags', '--follow-tags', '--prune', '--delete']) || t === '-d' || shortCluster(t).includes('d')) {
      return refusal('R1', shown + ' (--all / --mirror / --tags / --follow-tags / --prune / --delete yok)')
    }
    if (t.startsWith('-')) continue
    positional.push(w)
  }
  const refspecs = repoGiven ? positional : positional.slice(1)
  if (refspecs.length === 0) return refusal('R1', shown + ' (refspec yok: hedef dal açık değil)')
  for (const spec of refspecs) {
    if (spec.dynamic) return refusal('R1', shown + ' (değişkenli refspec ' + spec.text + ')')
    const s = spec.text.replace(/^\+/, '')
    const dst = s.includes(':') ? s.slice(s.lastIndexOf(':') + 1) : s
    if (!/^(refs\/heads\/)?ci\/[A-Za-z0-9._\/-]+$/.test(dst) || dst.includes('..') || /\/$/.test(dst)) {
      return refusal('R1', shown + ' (hedef ' + (dst || '(silme)') + ' ci/* değil)')
    }
  }
  return null
}

async function judgeAdd(rest: Word[], dir: string | null, ctx: Ctx, shown: string): Promise<string | null> {
  const paths: Word[] = []
  let update = false
  let endOfOptions = false
  for (const w of rest) {
    const t = w.text
    if (!endOfOptions && t === '--') {
      endOfOptions = true
      continue
    }
    if (!endOfOptions && t.startsWith('--')) {
      if (longPrefix(t, ['--all', '--no-ignore-removal', '--pathspec-from-file'])) return refusal('R2', shown)
      if (longPrefix(t, ['--update', '--renormalize'])) update = true
      continue
    }
    if (!endOfOptions && t.startsWith('-') && t.length > 1) {
      const cluster = shortCluster(t)
      if (cluster.includes('A')) return refusal('R2', shown)
      if (cluster.includes('u')) update = true
      continue
    }
    paths.push(w)
  }
  if (paths.length === 0) return update ? refusal('R2', shown + ' (-u yol olmadan)') : null
  for (const p of paths) {
    const t = p.text
    if (p.dynamic) return refusal('R2', 'değişkenli yol: ' + t)
    if (t.startsWith(':')) return refusal('R2', 'büyülü yol belirteci: ' + t)
    if (hasGlob(t)) return refusal('R2', 'joker: ' + t)
    if (/^[./\\]*$/.test(t) || normalize(t) === '') return refusal('R2', 'tüm klasör: ' + t)
    if (protectedKind(t) === 'uv.lock') return refusal('R2', 'uv.lock')
    const abs = resolve(p, dir, ctx)
    if (abs === null) return refusal('R2', 'klasör bilinmiyor: ' + t)
    if (protectedKind(abs) === 'uv.lock') return refusal('R2', 'uv.lock')
    const real = await ctx.realPath(abs)
    if (real !== null && protectedKind(real) === 'uv.lock') return refusal('R2', t + ' -> uv.lock')
    if ((await ctx.kindOf(abs)) === 'dir') return refusal('R2', 'klasör: ' + t + ' (dosyaları tek tek adlandırın)')
  }
  return null
}

function judgeCommit(rest: Word[], shown: string): { amend: boolean } | string {
  const VALUE_LONG = new Set(['--message', '--file', '--author', '--date', '--template', '--reuse-message', '--reedit-message',
    '--fixup', '--squash', '--cleanup', '--trailer'])
  let amend = false
  for (let i = 0; i < rest.length; i++) {
    const t = rest[i].text
    if (t === '--') return refusal('R3', shown + ' (yol verilmez: yalnız sahnelenmiş ağaç)')
    if (t.startsWith('--')) {
      const name = t.replace(/=.*$/, '')
      if (longPrefix(t, ['--all', '--include', '--only', '--pathspec-from-file', '--interactive', '--patch'])) return refusal('R3', shown)
      if (name === '--amend') amend = true
      if (VALUE_LONG.has(name) && !t.includes('=')) i++
      continue
    }
    if (t.startsWith('-') && t.length > 1) {
      const chars = t.slice(1)
      for (let k = 0; k < chars.length; k++) {
        const ch = chars[k]
        if (ch === 'a' || ch === 'i' || ch === 'o' || ch === 'p') return refusal('R3', shown)
        if ('mFCct'.includes(ch)) {
          if (k === chars.length - 1) i++ // the value is the next word
          break
        }
        if (ch === 'S' || ch === 'u') break // optional value glued on
      }
      continue
    }
    return refusal('R3', shown + ' (yol verilmez: yalnız sahnelenmiş ağaç)')
  }
  return { amend }
}

async function judgeCheckout(rest: Word[], dir: string | null, ctx: Ctx, shown: string): Promise<string | null> {
  const positional: Word[] = []
  for (let i = 0; i < rest.length; i++) {
    const t = rest[i].text
    if (t === '--') return refusal('R4', shown)
    if (t === '-b' || t === '--orphan' || t === '--conflict' || t === '-t' || t === '--track') {
      if (t === '-b' || t === '--orphan') i++
      continue
    }
    if (t === '-B' || longPrefix(t, ['--force', '--patch', '--ours', '--theirs', '--merge', '--overlay', '--no-overlay', '--pathspec-from-file'])) {
      return refusal(t === '-B' ? 'X4' : 'R4', shown)
    }
    const cluster = shortCluster(t)
    if (cluster && /[fpm]/.test(cluster)) return refusal('R4', shown)
    if (t.startsWith('-')) continue
    positional.push(rest[i])
  }
  if (positional.length > 1) return refusal('R4', shown + ' (dal + yol)')
  const one = positional[0]
  if (!one) return null
  if (one.dynamic || hasGlob(one.text) || one.text.startsWith(':') || /^[./\\]*$/.test(one.text)) return refusal('R4', shown)
  const abs = resolve(one, dir, ctx)
  if (abs === null) return refusal('R4', shown + ' (klasör bilinmiyor)')
  if ((await ctx.kindOf(abs)) !== null) return refusal('R4', shown + ' (' + one.text + ' bir dosya / klasör: geri yükleme olur)')
  return null
}

function judgeBranch(rest: Word[], shown: string): string | null {
  const VALUE = new Set(['-u', '--set-upstream-to', '--contains', '--no-contains', '--merged', '--no-merged', '--points-at', '-t', '--track'])
  let del = false
  let force = false
  let forceDelete = false
  const names: Word[] = []
  for (let i = 0; i < rest.length; i++) {
    const t = rest[i].text
    if (VALUE.has(t)) {
      i++
      continue
    }
    if (t.startsWith('--')) {
      if (longPrefix(t, ['--delete']) && t.length >= 4) del = true
      else if (longPrefix(t, ['--force']) && t.length >= 4) force = true
      continue
    }
    if (t.startsWith('-') && t.length > 1) {
      const cluster = t.slice(1)
      if (cluster.includes('D')) forceDelete = true
      if (cluster.includes('d')) del = true
      if (cluster.includes('f')) force = true
      if (cluster.includes('M') || cluster.includes('C')) return refusal('X4', shown + ' (-M / -C dalın üzerine yazar)')
      continue
    }
    names.push(rest[i])
  }
  if (del && force) forceDelete = true
  if (forceDelete) {
    for (const nm of names) {
      if (nm.dynamic || !/^worktree-[A-Za-z0-9._\/-]+$/.test(nm.text)) return refusal('R8', shown + ' (' + nm.text + ')')
    }
    return null
  }
  if (force) return refusal('X4', shown + ' (-f dalı yeniden yazar)')
  return null
}

// ---- file tools

export async function judgeFilePath(raw: string, ctx: Ctx): Promise<string | null> {
  const ex = expandHome(raw, ctx.home)
  const literal = protectedKind(ex.text)
  if (literal) return refusal('R10', raw + ' (' + literal + ')')
  const n = normalize(ex.text)
  const abs = isAbsolute(n) ? n : join(ctx.cwd, ex.text)
  const kind = protectedKind(abs)
  if (kind) return refusal('R10', raw + ' (' + kind + ')')
  const real = await ctx.realPath(abs)
  const kindReal = real ? protectedKind(real) : null
  if (kindReal) return refusal('R10', raw + ' -> ' + real + ' (' + kindReal + ')')
  return null
}
