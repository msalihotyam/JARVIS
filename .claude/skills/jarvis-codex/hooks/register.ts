// jarvis-codex: /codex-inceleme <rev-range> [name] runs the Codex CLI non-interactively as an
// independent, read-only reviewer of `git diff <rev-range>` and writes build/reviews/<name>-codex.md.
// No Claude turn is used. The only network use is Codex's own (Salih approved it, 2026-10-08).
// Active only in a session whose project is the JARVIS repo (JARVIS_RUNBOOK.md at the canonical root).
import {
  CODEX_ARGV,
  pinProblem,
  FEATURES_ARGV,
  HELP_ARGV,
  MAX_DIFF_BYTES,
  VERSION_ARGV,
  missingFeatures,
  missingFlags,
  parseArgs,
  pathsOf,
  plainPath,
  protectedHits,
  resultFile,
  reviewPrompt,
  verdictOf,
  versionOf,
} from './lib/review.ts'

let active = false
let root = ''
let cwd = ''
let running: { name: string; since: number } | null = null
let flagsChecked = false

function canonicalRootOf(path: string): string {
  const flat = path.replace(/\\/g, '/').replace(/\/+$/, '')
  const at = flat.indexOf('/.claude/worktrees/')
  return at === -1 ? flat : flat.slice(0, at)
}

async function run($: any, argv: readonly string[], init: Record<string, unknown> = {}) {
  try {
    return await $.process.run([...argv], { timeoutMs: 30000, ...init })
  } catch (err: any) {
    return { exitCode: 1, stdout: '', stderr: String(err && err.message ? err.message : err) }
  }
}

function stamp(ms: number): string {
  return new Date(ms).toISOString().replace(/[-:]/g, '').replace('T', '-').slice(0, 15)
}

async function sha256Hex(text: string): Promise<string> {
  const bytes = new TextEncoder().encode(text)
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('')
}

// The checks before anything is sent. Returns the text to show when the review must not run.
async function prepare($: any, args: { range: string; left: string; dots: string; right: string }): Promise<{ refuse: string } | { diff: string; files: number; resolved: string }> {
  const version = await run($, VERSION_ARGV)
  if (version.exitCode !== 0 || versionOf(version.stdout) === null) {
    return { refuse: 'Codex CLI bulunamadı (codex --version çalışmadı). Kurulum: npm i -g @openai/codex' }
  }
  if (!flagsChecked) {
    const help = await run($, HELP_ARGV)
    const feats = await run($, FEATURES_ARGV)
    const missing = [
      ...(help.exitCode === 0 ? missingFlags(help.stdout) : ['codex exec --help çalışmadı']),
      ...(feats.exitCode === 0 ? missingFeatures(feats.stdout).map((f) => 'özellik ' + f) : ['codex features list çalışmadı']),
    ]
    if (missing.length > 0) {
      return { refuse: 'Çalıştırılmadı: kurulu Codex bu bayrakları listelemiyor, salt-okunur kum havuzu garanti edilemez: ' + missing.join(', ') }
    }
    flagsChecked = true
  }
  // Both sides are fixed to commit ids once: the file list and the diff then describe the same commits
  // even if another session commits in between.
  const shas: string[] = []
  for (const rev of [args.left, args.right]) {
    const out = await run($, ['git', 'rev-parse', '--verify', '-q', rev + '^{commit}'], { cwd })
    const sha = String(out.stdout).trim()
    if (out.exitCode !== 0 || !/^[0-9a-f]{40,64}$/.test(sha)) return { refuse: 'git diff çalışmadı (rev-aralığı geçerli mi?): ' + rev.slice(0, 60) }
    shas.push(sha)
  }
  const range = shas[0] + args.dots + shas[1]
  const names = await run($, ['git', 'diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--ignore-submodules=all', '--name-only', '-z', range, '--'], { cwd })
  if (names.exitCode !== 0) {
    return { refuse: 'git diff çalışmadı (rev-aralığı geçerli mi?): ' + String(names.stderr).split('\n')[0]?.slice(0, 160) }
  }
  const paths = pathsOf(names.stdout)
  if (paths.length === 0) return { refuse: 'Fark boş: ' + range }
  const hits = protectedHits(paths)
  if (hits.length > 0) {
    return { refuse: 'Reddedildi: fark korumalı yola dokunuyor, hiçbir şey gönderilmedi: ' + hits.slice(0, 5).map((h) => h.path + ' (' + h.why + ')').join(', ') + (hits.length > 5 ? ' …' : '') }
  }
  const diff = await run($, ['git', 'diff', '--no-ext-diff', '--no-textconv', '--no-color', '--no-renames', '--ignore-submodules=all', range, '--'], { cwd, timeoutMs: 60000 })
  if (diff.exitCode !== 0) return { refuse: 'git diff çalışmadı: ' + String(diff.stderr).split('\n')[0]?.slice(0, 160) }
  const bytes = new TextEncoder().encode(String(diff.stdout)).length
  if (bytes > MAX_DIFF_BYTES) return { refuse: 'Fark çok büyük (' + bytes + ' bayt, sınır ' + MAX_DIFF_BYTES + '): aralığı küçült' }
  if (String(diff.stdout).trim() === '') return { refuse: 'Fark boş: ' + range }
  return { diff: String(diff.stdout), files: paths.length, resolved: range }
}

async function review($: any, range: string, resolved: string, name: string, diff: string, files: number): Promise<string> {
  const pin = pinProblem()
  if (pin) return 'Çalıştırılmadı: ' + pin
  const version = versionOf((await run($, VERSION_ARGV)).stdout) ?? '?'
  const reviews = root + '/build/reviews'
  const part = reviews + '/' + name + '-codex.part'
  const final = reviews + '/' + name + '-codex.md'
  if (!plainPath(part)) return 'Çalıştırılmadı: yol güvenli değil (özel karakter): ' + reviews
  const before = await run($, ['git', 'status', '--porcelain'], { cwd })
  // an old .part (a failed earlier run, a late write) must never be read as this run's answer
  await run($, ['powershell', '-NoProfile', '-NonInteractive', '-Command', 'Remove-Item -LiteralPath $env:JF_SRC -Force -ErrorAction SilentlyContinue'], { env: { JF_SRC: part }, timeoutMs: 15000 })
  const nonce = [...crypto.getRandomValues(new Uint8Array(12))].map((b) => b.toString(16).padStart(2, '0')).join('')
  const prompt = reviewPrompt(range, diff, nonce)
  const out = await run($, CODEX_ARGV, { env: { JF_OUT: part }, stdin: prompt, timeoutMs: 600000 })
  const after = await run($, ['git', 'status', '--porcelain'], { cwd })
  let message = ''
  try {
    if (await $.fs.exists(part)) message = await $.fs.read(part)
  } catch {
    message = ''
  }
  if (message.trim() === '') message = String(out.stdout ?? '')
  if (out.exitCode !== 0 && message.trim() === '') {
    message = 'Codex hata verdi (çıkış ' + out.exitCode + '): ' + String(out.stderr ?? '').split('\n').slice(-6).join('\n').slice(0, 800)
  }
  const treeChanged = before.exitCode === 0 && after.exitCode === 0 && before.stdout !== after.stdout
  const now = await $.clock.now()
  const body = resultFile({
    name,
    range,
    codex: version,
    at: new Date(now).toISOString(),
    files,
    bytes: new TextEncoder().encode(diff).length,
    sha: (await sha256Hex(diff)).slice(0, 16),
    treeChanged,
    exitCode: out.exitCode,
    message,
    resolved,
  })
  if (await $.fs.exists(final)) {
    const old = await $.fs.read(final)
    if (old !== body) await $.fs.write(reviews + '/' + name + '-codex.' + stamp(now) + '.md', old)
  }
  await $.fs.write(final, body)
  try {
    await $.process.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', 'Remove-Item -LiteralPath $env:JF_SRC -Force -ErrorAction SilentlyContinue'], { env: { JF_SRC: part }, timeoutMs: 15000 })
  } catch {
    // a leftover .part file is harmless
  }
  const verdict = out.exitCode === 0 ? (verdictOf(message) ?? 'VERDICT yok') : 'HATA'
  return 'Codex incelemesi bitti: ' + name + ' → ' + verdict + (treeChanged ? ' (UYARI: ağaç değişti)' : '') + ' · build/reviews/' + name + '-codex.md'
}

export function register(on: any) {
  on('session.start', async ($: any, e: any, next: any) => {
    try {
      cwd = (await $.session.cwd()).replace(/\\/g, '/')
      root = canonicalRootOf(cwd)
      active = await $.fs.exists(root + '/JARVIS_RUNBOOK.md')
      if (active) await $.command.register({ name: 'codex-inceleme', description: 'jarvis-codex: Codex ile bağımsız, salt-okunur diff incelemesi (rev-aralığı [ad])' })
    } catch (err: any) {
      active = false
      $.ui.toast('jarvis-codex başlatılamadı: ' + String(err && err.message ? err.message : err))
    }
    return next(e)
  })

  on('command.run', { command: 'codex-inceleme' }, async ($: any, e: any, next: any) => {
    if (!active) return next(e)
    try {
      // a review that never reported back (its timer was refused) stops counting after 12 minutes
      if (running !== null && (await $.clock.now()) - running.since < 12 * 60000) return { text: 'Zaten bir Codex incelemesi sürüyor: ' + running.name }
      const args = parseArgs(String(e.args ?? ''))
      if ('error' in args) return { text: args.error }
      const prepared = await prepare($, args)
      if ('refuse' in prepared) return { text: prepared.refuse }
      const since = await $.clock.now()
      running = { name: args.name, since }
      $.ui.invalidate('ui.render')
      // The review runs on its own; the command returns at once and no Claude turn starts.
      $.clock.after(50, async () => {
        let message: string
        try {
          message = await review($, args.range, prepared.resolved, args.name, prepared.diff, prepared.files)
        } catch (err: any) {
          message = 'Codex incelemesi çalışmadı: ' + String(err && err.message ? err.message : err)
        }
        running = null
        $.ui.invalidate('ui.render')
        $.ui.toast(message)
      })
      return { text: 'Codex inceliyor… (' + args.range + ', ' + prepared.files + ' dosya). Bitince bildirim gelir: build/reviews/' + args.name + '-codex.md' }
    } catch (err: any) {
      running = null
      return { text: '/codex-inceleme hatası: ' + String(err && err.message ? err.message : err) }
    }
  }).catch(($: any, e: any, next: any) => (next.called ? next(e) : { text: '/codex-inceleme çalıştırılmadı: kontrol başarısız oldu, hiçbir şey gönderilmedi.' }))

  on('ui.render', { component: 'AbovePrompt' }, async ($: any, e: any, next: any) => {
    if (!active || running === null) return next(e)
    try {
      const { Box, Text } = $.ui.resolve(e)
      const theirs = await next(e)
      const row = Text({ bold: true, color: 'yellow', children: ['Codex inceliyor… ' + running.name] })
      if (theirs === undefined || theirs === null) return Box({ flexDirection: 'row', children: [row] })
      return Box({ flexDirection: 'column', children: [Box({ flexDirection: 'row', children: [row] }), theirs] })
    } catch {
      return next(e)
    }
  })
}
