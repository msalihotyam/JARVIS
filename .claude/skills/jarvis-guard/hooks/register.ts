// jarvis-guard: the JARVIS hard rules as deterministic tool-call refusals.
// Defense in depth beside Claude Code's own permissions, never a sandbox.
// Active only in a session whose project is the JARVIS repo (JARVIS_RUNBOOK.md
// at the canonical root). It fails CLOSED: a guarded call the guard cannot
// judge (an error, a re-entry, a command it cannot read) is refused.
import type { Register } from 'claude-code'
import type { GuardHeader, GuardStart } from '../types'
import { countAsserts } from './lib/asserts.ts'
import { parseHeader } from './lib/header.ts'
import { canonicalRootOf, isAbsolute, join, normalize, worktreeOf } from './lib/paths.ts'
import { judgeFilePath, judgeShell, refusal, type Ctx } from './lib/rules.ts'

const HEADER = { plugin: 'jarvis-guard', key: 'header' } as const
const STARTS = { plugin: 'jarvis-guard', key: 'starts' } as const

const FILE_TOOLS: Record<string, string> = { Edit: 'file_path', Write: 'file_path', MultiEdit: 'file_path', NotebookEdit: 'notebook_path' }
const GUARDED = new Set(['Bash', 'PowerShell', 'ExitWorktree', ...Object.keys(FILE_TOOLS)])
const EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'

// ---- module state (rebuilt on every reload; the session's values live in $.state)
let rootRaw = '' // the canonical checkout as the file system spells it
let root = '' // the same, normal form
let home: string | null = null
let active: boolean | null = null

async function init($: any, cwdRaw: string): Promise<void> {
  rootRaw = canonicalRootOf(cwdRaw)
  root = normalize(rootRaw)
  active = await $.fs.exists(rootRaw + '/JARVIS_RUNBOOK.md')
  const profile = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME'))
  home = profile ? normalize(profile) : null
}

async function ensure($: any): Promise<void> {
  if (active === null) await init($, await $.session.cwd())
}

async function run($: any, argv: string[]): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  try {
    return await $.process.run(argv, { timeoutMs: 20000 })
  } catch {
    return { exitCode: 1, stdout: '', stderr: '' }
  }
}

function git($: any, dir: string, args: string[]) {
  return run($, ['git', '-C', dir, ...args])
}

// Where a path really lands: the nearest existing folder (links, junctions
// resolved) plus the part not there yet; null when nothing resolves.
async function realPathOf($: any, abs: string): Promise<string | null> {
  let path = abs
  let rest = ''
  for (let k = 0; k < 64; k++) {
    try {
      const st = await $.fs.stat(/^[a-z]:$/.test(path) ? path + '/' : path, { resolve: true })
      return st && st.realPath ? normalize(st.realPath + (rest ? '/' + rest : '')) : null
    } catch {
      const cut = path.lastIndexOf('/')
      if (cut <= 0) return null
      rest = path.slice(cut + 1) + (rest ? '/' + rest : '')
      path = path.slice(0, cut)
    }
  }
  return null
}

async function kindOf($: any, abs: string): Promise<'file' | 'dir' | null> {
  try {
    const st = await $.fs.stat(abs)
    return st.kind === 'dir' ? 'dir' : 'file'
  } catch {
    return null
  }
}

async function header($: any): Promise<GuardHeader | null> {
  const held = await $.state.get(HEADER)
  return held && held.value ? held.value : null
}

// ---- assert guard: a commit may not lower the assertion count of tests/**/*.py

async function assertGuard($: any, dir: string, amend?: boolean): Promise<string | null> {
  const top = await git($, dir, ['rev-parse', '--show-toplevel'])
  if (top.exitCode !== 0) return refusal('R12', 'git deposu okunamadı: ' + dir)
  const repo = top.stdout.trim()
  // the JARVIS repo always has a HEAD: one git cannot read is a refusal, not a first commit
  const head = await git($, repo, ['rev-parse', '--verify', '-q', 'HEAD'])
  if (head.exitCode !== 0) return refusal('R12', 'HEAD okunamadı: ' + repo)
  let base = 'HEAD'
  if (amend) {
    const parent = await git($, repo, ['rev-parse', '--verify', '-q', 'HEAD^'])
    base = parent.exitCode === 0 ? 'HEAD^' : EMPTY_TREE
  }
  const diff = await git($, repo, ['diff', '--cached', '--name-status', '--no-renames', base, '--', 'tests'])
  if (diff.exitCode !== 0) return refusal('R12', 'sahnelenmiş değişiklikler okunamadı')
  let before = 0
  let after = 0
  const drops: { path: string; from: number; to: number }[] = []
  for (const line of diff.stdout.split(/\r?\n/)) {
    const m = /^([AMDTUX])\t(.+)$/.exec(line.trim())
    if (!m || !/^tests\/.+\.py$/.test(m[2])) continue
    const path = m[2]
    let from = 0
    let to = 0
    if (m[1] !== 'A') {
      const old = await git($, repo, ['show', base + ':' + path])
      if (old.exitCode !== 0) return refusal('R12', path + ' (' + base + ') okunamadı')
      from = countAsserts(old.stdout)
    }
    if (m[1] !== 'D') {
      const staged = await git($, repo, ['show', ':' + path])
      if (staged.exitCode !== 0) return refusal('R12', path + ' (sahnelenmiş) okunamadı')
      to = countAsserts(staged.stdout)
    }
    before += from
    after += to
    if (to < from) drops.push({ path, from, to })
  }
  if (after >= before) return null
  const list = drops.map(d => d.path + ' ' + d.from + ' -> ' + d.to + ' (' + (d.to - d.from) + ')').join('; ')
  const lane = (await header($))?.lane ?? null
  if (lane !== null) {
    try {
      const approval = await $.fs.read(rootRaw + '/build/approvals/assert-drop-' + lane + '.txt')
      if (approval.includes(lane) && drops.every(d => approval.includes(d.path))) return null
    } catch {
      // no approval file
    }
  }
  return refusal('R12', 'sahnelenmiş ağaç ' + (after - before) + ' assert düşürüyor: ' + list +
    '. Onay: build/approvals/assert-drop-' + (lane ?? '<lane>') + '.txt (orkestratör, Salih onayından sonra)')
}

// ---- base guard: edits in a lane worktree only on the base the prompt names

async function recordStart($: any, worktree: string): Promise<GuardStart | null> {
  const start = await git($, worktree, ['rev-parse', 'HEAD'])
  const main = await git($, rootRaw, ['rev-parse', 'main'])
  if (start.exitCode !== 0 || main.exitCode !== 0) return null
  const rec: GuardStart = { start: start.stdout.trim(), main: main.stdout.trim() }
  const held = await $.state.get(STARTS)
  await $.state.set(STARTS, { ...(held && held.value ? held.value : {}), [worktree]: rec })
  return rec
}

async function baseVerdict($: any, worktree: string, want: GuardHeader): Promise<string | null> {
  const held = await $.state.get(STARTS)
  const rec: GuardStart | null = (held && held.value && held.value[worktree]) || (await recordStart($, worktree))
  if (rec === null) return 'worktree başlangıç HEAD\'i okunamadı'
  if (want.base === 'main') {
    return rec.start === rec.main ? null : 'başlangıç ' + rec.start.slice(0, 12) + ', girişte main ' + rec.main.slice(0, 12) + ' (base=main)'
  }
  const sha = await git($, rootRaw, ['rev-parse', '--verify', '-q', want.base + '^{commit}'])
  if (sha.exitCode !== 0) return 'istem tabanı ' + want.base + ' çözülemedi'
  return rec.start === sha.stdout.trim() ? null : 'başlangıç ' + rec.start.slice(0, 12) + ', istem tabanı ' + want.base
}

// ---- the judgement of one guarded call

async function judge($: any, e: any, tool: string): Promise<string | null> {
  const cwdRaw = await $.session.cwd()
  const ctx: Ctx = {
    root,
    home,
    cwd: normalize(cwdRaw),
    realPath: (abs: string) => realPathOf($, abs),
    kindOf: (abs: string) => kindOf($, abs),
    onCommit: (dir: string, amend: boolean) => assertGuard($, dir, amend),
  }
  if (tool === 'Bash') return judgeShell(String(e.command ?? ''), 'bash', ctx)
  if (tool === 'PowerShell') return judgeShell(String(e.command ?? ''), 'powershell', ctx)
  if (tool === 'ExitWorktree') {
    return e.action === 'remove' && e.discard_changes === true ? refusal('X5', 'commit edilmemiş iş silinir; önce commit et, sonra keep') : null
  }
  const raw = e[FILE_TOOLS[tool]]
  if (typeof raw !== 'string' || raw === '') return refusal('X2', tool + ': dosya yolu yok')
  const protectedVerdict = await judgeFilePath(raw, ctx)
  if (protectedVerdict) return protectedVerdict
  const want = await header($)
  if (want === null) return null
  const n = normalize(raw)
  const abs = isAbsolute(n) ? n : join(ctx.cwd, raw)
  const real = (await realPathOf($, abs)) ?? abs
  const worktree = worktreeOf(real, root) ?? worktreeOf(abs, root)
  if (worktree === null) return null
  const base = await baseVerdict($, worktree, want)
  return base === null ? null : refusal('R13', base + '. Orkestratörden güncel istemi iste.')
}

export const register: Register = on => {
  on('session.start', async ($: any, e: any, next: any) => {
    try {
      await init($, String(e.cwd ?? (await $.session.cwd())))
    } catch {
      active = null // judged lazily, and refused if that fails
    }
    return next(e)
  })

  // The orchestrator's header: the lane (assert-drop approvals) and the base.
  on('prompt.submit', async ($: any, e: any, next: any) => {
    const out = await next(e)
    try {
      const h = parseHeader(String(e.text ?? ''))
      if (h !== null && !(out && out.drop)) await $.state.set(HEADER, h)
    } catch {
      // the header is advisory to this hook; the prompt is never held up
    }
    return out
  }).catch(($: any, e: any, next: any) => next(e))

  on('tool.call', async ($: any, e: any, next: any) => {
    const tool = String(e.tool)
    if (tool === 'EnterWorktree') {
      const out = await next(e)
      try {
        await ensure($)
        const path = out && !out.deny && !out.isError && out.result ? out.result.worktreePath : null
        if (active && typeof path === 'string') {
          const n = normalize(path)
          await recordStart($, worktreeOf(n, root) ?? n)
        }
      } catch {
        // recorded lazily at the first file edit instead
      }
      return out
    }
    if (!GUARDED.has(tool)) return next(e)
    await ensure($)
    if (!active) return next(e)
    const why = await judge($, e, tool)
    return why === null ? next(e) : { deny: why }
  }).catch(($: any, e: any, next: any) => {
    const tool = String(e.tool)
    if (!GUARDED.has(tool)) return next(e)
    if (next.error && next.error.kind === 're-entry') {
      return { deny: refusal('X2', tool + ' başka bir kancanın içinden çağrıldı (yeniden giriş); güvenli tarafta reddedildi') }
    }
    return next.called ? next(e) : { deny: refusal('X2', 'jarvis-guard denetimi hata verdi; güvenli tarafta reddedildi') }
  })
}
