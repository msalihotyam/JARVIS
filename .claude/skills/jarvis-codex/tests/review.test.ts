import { expect, test } from 'claude-code/testing'
import {
  CODEX_ARGV,
  CODEX_EFFORT,
  CODEX_MODEL,
  pinProblem,
  missingFeatures,
  missingFlags,
  parseArgs,
  pathsOf,
  plainPath,
  protectedHits,
  protectedPath,
  resultFile,
  reviewPrompt,
  verdictOf,
  versionOf,
} from '../hooks/lib/review.ts'
import { HELP, shaOf, startSession, world } from './world.ts'

const run = ($: any, args: string) => $.command.run({ command: 'codex-inceleme', args })
const codexRuns = (w: any) => w.runs.filter((r: any) => r.argv[0] === 'powershell' && String(r.argv[4]).includes('# JF_CODEX\n'))
const gitRuns = (w: any) => w.runs.filter((r: any) => r.argv[0] === 'git')
const reviewFile = (w: any, name: string) => w.files.get('/repo/build/reviews/' + name + '-codex.md') as string | undefined

test('pure: the range must be A..B with plain revisions; the name is a plain word', async () => {
  expect(parseArgs('072d0fe..HEAD')).toMatchObject({ range: '072d0fe..HEAD', name: '072d0fe..HEAD', left: '072d0fe', dots: '..', right: 'HEAD' })
  expect(parseArgs('HEAD~3...main ma1-flow')).toMatchObject({ range: 'HEAD~3...main', name: 'ma1-flow' })
  expect(parseArgs('a1b2c3d..worktree-mods-wave2 x')).toMatchObject({ name: 'x' })
  for (const bad of ['', 'HEAD', '..HEAD', 'HEAD..', '--output=/etc/x..y', '-p..HEAD', 'a..b..c', 'a b..c', 'a;rm..b', 'a..b|x', 'a..$(x)', 'a..b one two', 'a..b ../x', 'a..b .hidden']) {
    expect('error' in parseArgs(bad), bad).toBe(true)
  }
})

test('pure: protected paths - uv.lock, data/**, build/** and other secret or state files', async () => {
  for (const p of ['prod.env', '.envrc', 'id_rsa', 'home/id_ed25519.pub', 'k.ppk', 'vault.kdbx', 'jarvis.db-wal', 'x.db-shm', '.npmrc', '.pypirc', '.netrc', 'credentials.json', 'secrets.yml', 'uv.lock', 'sub/uv.lock', 'data/x.json', 'data/jarvis.db', 'build/reports/a.md', 'DATA/x', '.git/config', '.env', '.env.local', 'cfg/key.pem', 'a/b.key', 'x.pfx', 'store.db', 'x.sqlite3', '.jarvis/conversation.json', '.codex/config.toml', 'auth.json', '.claude/settings.local.json', 'MASAST~1/x.py', '../outside', 'a\\..\\b'])
    expect(protectedPath(p), p).not.toBeNull()
  for (const p of ['jarvis/app.py', 'tests/test_app.py', 'tests/data/fixture.json', 'docs/build.md', '.gitignore', 'JARVIS_RUNBOOK.md', '.claude/skills/jarvis-codex/hooks/register.ts', 'scripts/quality.ps1', 'environment.md'])
    expect(protectedPath(p), p).toBeNull()
  expect(protectedHits(['jarvis/a.py', 'uv.lock', 'data/z']).map((h) => h.path)).toEqual(['uv.lock', 'data/z'])
  expect(pathsOf('a\0b c\0\0')).toEqual(['a', 'b c'])
})

test('pure: help flags, version, verdict, prompt, command line and result file', async () => {
  expect(missingFlags(HELP)).toEqual([])
  expect(missingFlags(HELP.replace('read-only, ', ''))).toEqual(['read-only'])
  expect(missingFlags('')).toContain('--sandbox')
  expect(versionOf('codex-cli 0.159.2\n')).toBe('0.159.2')
  expect(versionOf('nope')).toBeNull()
  expect(verdictOf('x\nVERDICT: NOT-READY\n')).toBe('NOT-READY')
  expect(verdictOf('VERDICT: READY\n\nVERDICT: READY-WITH-NITS')).toBe('READY-WITH-NITS')
  expect(verdictOf('no verdict')).toBeNull()
  const prompt = reviewPrompt('a..b', 'DIFFBODY', 'n0nce')
  for (const word of ['BLOCKER', 'MAJOR', 'MINOR', 'file:line', 'READY-WITH-NITS', 'NOT-READY', 'DIFFBODY', '=== DIFF START n0nce ===', '=== DIFF END n0nce ===']) expect(prompt).toContain(word)
  expect(missingFeatures('shell_tool  stable  true\nunified_exec stable true')).toContain('apps')
  expect(missingFeatures('')).toContain('shell_tool')
  // the command line: read-only, no approvals, nothing dangerous, the prompt read from stdin
  const script = String(CODEX_ARGV[4])
  expect(script).toContain('-s read-only')
  expect(script).toContain('approval_policy=never')
  expect(script).toContain('--ephemeral')
  expect(script).toContain('-m gpt-6.1-sol -c model_reasoning_effort=high')
  expect([CODEX_MODEL, CODEX_EFFORT]).toEqual(['gpt-6.1-sol', 'high'])
  expect(pinProblem()).toBeNull()
  expect(pinProblem('', 'high')).toContain('CODEX_MODEL')
  expect(pinProblem('gpt-6.1-sol', '')).toContain('CODEX_EFFORT')
  expect(pinProblem('x; rm', 'high')).not.toBeNull()
  expect(script).toContain('--ignore-user-config')
  expect(script).toContain('--disable shell_tool')
  expect(script).toContain('--disable unified_exec')
  expect(script).toContain('Remove-Item Env:JARVIS_NTFY_TOPIC')
  expect(script).toContain('GetTempPath')
  expect(script).not.toContain('JF_CWD')
  expect(script).not.toContain('dangerously')
  expect(script).not.toContain('workspace-write')
  expect(script).not.toContain('full-access')
  expect(plainPath('/c/x/build/reviews')).toBe(true)
  expect(plainPath('/c/x & del')).toBe(false)
  const file = resultFile({ name: 'n', range: 'a..b', resolved: 'x..y', codex: '0.159.2', at: 't', files: 2, bytes: 10, sha: 'ab', treeChanged: true, exitCode: 0, message: 'text\nVERDICT: READY' })
  expect(file).toContain('VERDICT: READY')
  expect(file).toContain('CHANGED DURING THE RUN')
  // a failed run has no verdict, whatever the message says
  expect(resultFile({ name: 'n', range: 'a..b', resolved: 'x..y', codex: '1', at: 't', files: 1, bytes: 1, sha: 'a', treeChanged: false, exitCode: 3, message: 'VERDICT: READY' })).toContain('VERDICT: ERROR')
})

test('C3: a review runs in the background: band while it runs, the file and a toast when it is done, no Claude turn', async ($, on) => {
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  const w = world(on)
  let go!: () => void
  w.codexHold = new Promise<void>((r) => (go = r))
  await startSession($)
  const out = await run($, '072d0fe..HEAD ma1')
  expect(out.text).toContain('Codex inceliyor')
  expect(out.text).toContain('2 dosya')
  const ui = await $.ui.mount({
    plugin: 'jarvis-codex',
    component: 'AbovePrompt',
    requestId: 'band',
    surface: 'terminal',
    viewport: { columns: 120, rows: 30 },
    props: { hasSurvey: false, isWorking: false, maxRows: 3, bodyColumns: 120, scroll: { offset: 0, bodyRows: 3 }, view: {} },
  } as any)
  while (codexRuns(w).length === 0) await w.clock.advance(100)
  expect(await ui.find({ type: 'Text', text: /Codex inceliyor/ })).toBeDefined()
  await ui.unmount()
  // a second review while one runs is refused
  expect((await run($, 'a1b2c3d..HEAD other')).text).toContain('Zaten bir Codex incelemesi sürüyor')
  go()
  while (!w.toasts.some((t) => t.startsWith('Codex incelemesi bitti'))) await w.clock.advance(100)
  const toast = w.toasts.find((t) => t.startsWith('Codex incelemesi bitti')) as string
  expect(toast).toContain('ma1')
  expect(toast).toContain('READY-WITH-NITS')
  const file = reviewFile(w, 'ma1') as string
  expect(file).toContain('VERDICT: READY-WITH-NITS')
  expect(file).toContain('RANGE: 072d0fe..HEAD (' + shaOf('072d0fe') + '..' + shaOf('HEAD') + ')')
  expect(file).toContain('codex-cli 0.159.2')
  expect(file).toContain('unchanged by the run')
  expect(file).toContain('MINOR: jarvis/app.py:1 nit')
  // the .part file is gone, and no Claude prompt was submitted
  expect([...w.files.keys()].some((p) => p.endsWith('.part'))).toBe(false)
  expect(w.submitted).toEqual([])
})

test('C3: Codex is called read-only with the prompt on stdin, a neutral folder, and nothing of the diff on a command line', async ($, on) => {
  const w = world(on)
  await startSession($)
  await run($, '072d0fe..HEAD ma1')
  while (codexRuns(w).length === 0) await w.clock.advance(100)
  await w.clock.advance(1000)
  const call = codexRuns(w)[0]
  expect(call.stdin).toContain('+new')
  expect(call.stdin).toMatch(/=== DIFF START [0-9a-f]{24} ===/)
  expect(call.argv.join(' ')).not.toContain('+new')
  expect(String(call.argv[4])).toContain('-s read-only')
  expect(call.env.JF_CWD).toBeUndefined()
  expect(call.env.JF_OUT).toBe('/repo/build/reviews/ma1-codex.part')
  // only tracked-file diff commands ran, and only the ones named
  for (const g of gitRuns(w).filter((r: any) => r.argv[1] === 'diff')) {
    // both sides are fixed to commit ids once, for the file list and the diff alike
    expect(g.argv).toContain(shaOf('072d0fe') + '..' + shaOf('HEAD'))
    expect(g.argv).toContain('--ignore-submodules=all')
    expect(g.argv).toContain('--no-ext-diff')
    expect(g.argv).not.toContain('--cached')
  }
})

test('C3: the flag check - a Codex without the read-only sandbox flags is not run', async ($, on) => {
  const w = world(on)
  w.help = HELP.replace('read-only, ', '').replace('--ephemeral', '')
  await startSession($)
  const out = await run($, '072d0fe..HEAD ma1')
  expect(out.text).toContain('garanti edilemez')
  expect(out.text).toContain('read-only')
  expect(out.text).toContain('--ephemeral')
  expect(codexRuns(w).length).toBe(0)
  expect(gitRuns(w).length).toBe(0)
  expect(reviewFile(w, 'ma1')).toBeUndefined()
})

test('C3: codex missing - a clear message, nothing else runs', async ($, on) => {
  const w = world(on)
  w.codexVersion = null
  await startSession($)
  const out = await run($, '072d0fe..HEAD ma1')
  expect(out.text).toContain('Codex CLI bulunamadı')
  expect(codexRuns(w).length).toBe(0)
  expect(gitRuns(w).length).toBe(0)
})

test('C3: a diff that touches uv.lock, data/**, build/** or a protected path is refused before anything is sent', async ($, on) => {
  const w = world(on)
  await startSession($)
  for (const touched of ['uv.lock', 'data/jarvis.db', 'build/reports/x.md', '.env', 'keys/server.pem']) {
    w.paths = ['jarvis/app.py', touched]
    const out = await run($, '072d0fe..HEAD ma1')
    expect(out.text, touched).toContain('Reddedildi')
    expect(out.text, touched).toContain(touched)
  }
  expect(codexRuns(w).length).toBe(0)
  expect(gitRuns(w).filter((r: any) => r.argv[1] === 'diff' && !r.argv.includes('--name-only')).length).toBe(0)
})

test('C3: bad arguments, a bad range, an empty or oversize diff are refused with a message', async ($, on) => {
  const w = world(on)
  await startSession($)
  expect((await run($, '')).text).toContain('Kullanım')
  expect((await run($, '--oops..HEAD')).text).toContain('geçersiz')
  w.gitFails = true
  expect((await run($, 'nope..HEAD x')).text).toContain('git diff çalışmadı')
  w.gitFails = false
  w.paths = []
  expect((await run($, '072d0fe..HEAD x')).text).toContain('Fark boş')
  w.paths = ['jarvis/app.py']
  w.diff = 'x'.repeat(400 * 1024)
  expect((await run($, '072d0fe..HEAD x')).text).toContain('çok büyük')
  expect(codexRuns(w).length).toBe(0)
})

test('C3: a Codex error is reported as an error', async ($, on) => {
  const w = world(on)
  w.codexExit = 3
  await startSession($)
  await run($, '072d0fe..HEAD bad')
  while (!w.toasts.some((t) => t.startsWith('Codex incelemesi bitti'))) await w.clock.advance(100)
  expect(w.toasts.find((t) => t.startsWith('Codex incelemesi bitti'))).toContain('HATA')
  expect(reviewFile(w, 'bad')).toContain('boom')
})

test('C3: a tree that changed during the run is flagged', async ($, on) => {
  const w2 = world(on)
  w2.statusAfter = ' M jarvis/app.py'
  await startSession($)
  await run($, '072d0fe..HEAD changed')
  while (!w2.toasts.some((t) => t.startsWith('Codex incelemesi bitti'))) await w2.clock.advance(100)
  expect(w2.toasts.find((t) => t.startsWith('Codex incelemesi bitti'))).toContain('UYARI')
  expect(reviewFile(w2, 'changed')).toContain('CHANGED DURING THE RUN')
})

test('C3: an older review of the same name is kept as a timestamped copy', async ($, on) => {
  const w = world(on)
  w.files.set('/repo/build/reviews/ma1-codex.md', 'older text')
  await startSession($)
  await run($, '072d0fe..HEAD ma1')
  while (!w.toasts.some((t) => t.startsWith('Codex incelemesi bitti'))) await w.clock.advance(100)
  const kept = [...w.files.entries()].filter(([p]) => /ma1-codex\.\d{8}-\d{6}\.md$/.test(p))
  expect(kept.length).toBe(1)
  expect(kept[0]?.[1]).toBe('older text')
})

test('C3: outside the JARVIS repo the command is left alone', async ($, on) => {
  const w = world(on, { isJarvis: false })
  await startSession($)
  const out = await run($, '072d0fe..HEAD ma1')
  expect(out.text).toBe('')
  expect(w.runs.length).toBe(0)
})

test('C3: the .part file of an older run is removed before the run and a failed run reports ERROR, not an old verdict', async ($, on) => {
  const w = world(on)
  w.files.set('/repo/build/reviews/ma1-codex.part', 'VERDICT: READY\n')
  w.codexExit = 5
  await startSession($)
  await run($, '072d0fe..HEAD ma1')
  while (!w.toasts.some((t) => t.startsWith('Codex incelemesi bitti'))) await w.clock.advance(100)
  const removeIdx = w.runs.findIndex((r) => String(r.argv[4]).includes('Remove-Item') && r.env.JF_SRC === '/repo/build/reviews/ma1-codex.part')
  const codexIdx = w.runs.findIndex((r) => String(r.argv[4]).includes('# JF_CODEX\n'))
  expect(removeIdx).toBeGreaterThanOrEqual(0)
  expect(removeIdx).toBeLessThan(codexIdx)
  const file = reviewFile(w, 'ma1') as string
  expect(file).toContain('VERDICT: ERROR')
  expect(file).not.toContain('VERDICT: READY')
})

test('C3: a Codex whose feature list lacks a feature the run switches off is not run', async ($, on) => {
  const w = world(on)
  w.features = 'goals                                    stable             true'
  await startSession($)
  const out = await run($, '072d0fe..HEAD ma1')
  expect(out.text).toContain('garanti edilemez')
  expect(out.text).toContain('özellik shell_tool')
  expect(codexRuns(w).length).toBe(0)
})

test('C3: a revision that does not resolve to a commit is refused', async ($, on) => {
  const w = world(on)
  w.gitFails = true
  await startSession($)
  expect((await run($, 'nope..HEAD x')).text).toContain('git diff çalışmadı')
  expect(codexRuns(w).length).toBe(0)
})
