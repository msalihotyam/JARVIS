import { expect, test } from 'claude-code/testing'
import { MAX_BYTES, acceptContent, candidates, firstLines, isInside, sha256Hex, stamp } from '../hooks/lib/inbox.ts'
import { SONNET, header, put, startSession, world } from './world.ts'

const QUEUE = '/repo/build/queue/sonnet'
const ctx = { modelId: SONNET, effort: null, mainHead: null }
const entry = (name: string, over: Record<string, unknown> = {}) => ({ name, kind: 'file', size: 10, mtimeMs: 1, isLink: false, ...over }) as any

test('pure: only plain small *.md files are candidates, oldest first', { timeoutMs: 60000 }, async () => {
  const list = candidates([
    entry('b.md', { mtimeMs: 5 }),
    entry('a.md', { mtimeMs: 5 }),
    entry('old.md', { mtimeMs: 1 }),
    entry('note.txt'),
    entry('.hidden.md'),
    entry('link.md', { isLink: true }),
    entry('dir.md', { kind: 'dir' }),
    entry('big.md', { size: MAX_BYTES + 1 }),
    entry('edge.md', { size: MAX_BYTES, mtimeMs: 9 }),
  ])
  expect(list.map((c) => c.name)).toEqual(['old.md', 'a.md', 'b.md', 'edge.md'])
})

test('pure: isInside rejects traversal, a parent, a sibling folder and nesting', { timeoutMs: 60000 }, async () => {
  expect(isInside('/repo/build/queue/sonnet/a.md', QUEUE)).toBe(true)
  expect(isInside('\\repo\\build\\queue\\sonnet\\a.md', QUEUE)).toBe(true)
  expect(isInside('/REPO/build/queue/Sonnet/a.md', QUEUE)).toBe(true)
  expect(isInside('/repo/build/queue/sonnet/../opus/a.md', QUEUE)).toBe(false)
  expect(isInside('/repo/build/queue/opus/a.md', QUEUE)).toBe(false)
  expect(isInside('/repo/build/queue/sonnet-evil/a.md', QUEUE)).toBe(false)
  expect(isInside('/repo/build/queue/sonnet/done/a.md', QUEUE)).toBe(false)
  expect(isInside('/repo/secrets/a.md', QUEUE)).toBe(false)
  expect(isInside('/repo/build/queue/sonnet', QUEUE)).toBe(false)
  expect(isInside('\\\\?\\C:\\repo\\build\\queue\\sonnet\\a.md', 'C:/repo/build/queue/sonnet')).toBe(true)
  expect(isInside('/repo/build/queue/sonnet/a..b.md', QUEUE)).toBe(true)
})

test('pure: acceptContent applies header, family, size and lint rules', { timeoutMs: 60000 }, async () => {
  const ok = acceptContent(header('SB1') + '\nbody', 'sonnet', ctx)
  expect(ok.ok).toBe(true)
  expect(acceptContent('no header here', 'sonnet', ctx)).toEqual({ ok: false, reason: '@jarvis başlığı yok' })
  expect(acceptContent(header('SB1', 'opus'), 'sonnet', ctx)).toEqual({ ok: false, reason: 'başka sohbet için (opus)' })
  expect(acceptContent('@jarvis lane=x', 'sonnet', ctx)).toEqual({ ok: false, reason: 'başlık geçersiz: eksik alan: model' })
  expect(acceptContent(header('SB1') + '\n' + 'x'.repeat(MAX_BYTES), 'sonnet', ctx)).toEqual({ ok: false, reason: 'çok büyük (> 64 KB)' })
  const stale = acceptContent(header('SB1', 'sonnet', 'high', '8aad14a'), 'sonnet', { ...ctx, mainHead: '7c34ff0abcde' })
  expect(stale.ok).toBe(false)
})

test('pure: helpers', { timeoutMs: 60000 }, async () => {
  expect(firstLines('1\n2\n3\n4\n5\n6\n7\n8\n9\n10', 8)).toBe('1\n2\n3\n4\n5\n6\n7\n8')
  expect(stamp(Date.UTC(2026, 9, 8, 14, 5, 9))).toBe('20261008-140509')
  expect((await sha256Hex('abc')).slice(0, 16)).toBe('ba7816bf8f01cfea')
})

test('/basla shows the first 8 lines and a short sha, submits the whole text, moves the file to done', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  const body = header('SB1') + '\n' + ['l2', 'l3', 'l4', 'l5', 'l6', 'l7', 'l8', 'l9', 'l10'].join('\n')
  put(w, QUEUE + '/sb1.md', body)
  await startSession($)
  const out = await $.command.run({ command: 'basla', args: '' })
  expect(out.text).toContain('sha256 ' + (await sha256Hex(body)).slice(0, 12))
  expect(out.text).toContain('l8')
  expect(out.text).not.toContain('l9')
  expect(out.text).toContain('Gönderiliyor: SB1')
  // the prompt goes out a moment later, once the command has returned
  await w.clock.advance(100)
  expect(w.submitted).toEqual([body])
  expect(w.files.has(QUEUE + '/sb1.md')).toBe(false)
  const done = [...w.files.keys()].filter((k) => k.startsWith(QUEUE + '/done/'))
  expect(done).toEqual([QUEUE + '/done/19700101-000000-sb1.md'])
  expect(w.files.get(done[0] as string)).toBe(body)
})

test('/basla takes the oldest file first and /kuyruk lists them', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  put(w, QUEUE + '/newer.md', header('SB2') + '\nb')
  put(w, QUEUE + '/older.md', header('SB1') + '\na')
  w.mtimes.set(QUEUE + '/older.md', 1)
  w.mtimes.set(QUEUE + '/newer.md', 2)
  await startSession($)
  const list = await $.command.run({ command: 'kuyruk', args: '' })
  expect(list.text).toContain('SB1 · older.md')
  expect(list.text).toContain('SB2 · newer.md')
  await $.command.run({ command: 'basla', args: '' })
  await w.clock.advance(100)
  expect(w.submitted).toEqual([header('SB1') + '\na'])
  expect(w.files.has(QUEUE + '/newer.md')).toBe(true)
})

test('/basla with an empty queue says so and sends nothing', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  const out = await $.command.run({ command: 'basla', args: '' })
  expect(out.text).toBe('Kuyrukta bekleyen istem yok.')
  await w.clock.advance(1000)
  expect(w.submitted).toEqual([])
})

test('the inbox refuses a path-traversal file, a wrong family, an oversize file and a missing header', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on, { realPaths: { [QUEUE + '/evil.md']: '/repo/secrets/evil.md' } })
  put(w, QUEUE + '/evil.md', header('EVIL'))
  put(w, QUEUE + '/opus-only.md', header('OPUSJOB', 'opus'))
  put(w, QUEUE + '/big.md', header('BIG') + '\n' + 'x'.repeat(MAX_BYTES + 5))
  put(w, QUEUE + '/plain.md', 'just some text, no header')
  put(w, QUEUE + '/note.txt', header('TXT'))
  await startSession($)
  const list = await $.command.run({ command: 'kuyruk', args: '' })
  expect(list.text).toContain('reddedildi: evil.md (klasör dışına çıkıyor)')
  expect(list.text).toContain('reddedildi: opus-only.md (başka sohbet için (opus))')
  expect(list.text).toContain('reddedildi: big.md (çok büyük (> 64 KB))')
  expect(list.text).toContain('reddedildi: plain.md (@jarvis başlığı yok)')
  expect(list.text).not.toContain('note.txt')
  expect(list.text).not.toContain('bekliyor')
  const out = await $.command.run({ command: 'basla', args: '' })
  expect(out.text).toBe('Kuyrukta bekleyen istem yok.')
  await w.clock.advance(1000)
  expect(w.submitted).toEqual([])
  expect(w.files.has(QUEUE + '/evil.md')).toBe(true)
})

test('a file whose base went stale is refused at acceptance', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on, { mainHead: '7c34ff0abcde' })
  put(w, QUEUE + '/stale.md', header('SB1', 'sonnet', 'high', '8aad14a'))
  await startSession($)
  const list = await $.command.run({ command: 'kuyruk', args: '' })
  expect(list.text).toContain('reddedildi: stale.md (İstem tabanı 8aad14a, main 7c34ff0abcde.')
})

test('/atla moves a file to done without sending it', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  put(w, QUEUE + '/skip.md', header('SB1'))
  await startSession($)
  const out = await $.command.run({ command: 'atla', args: 'skip.md' })
  expect(out.text).toBe('Atlandı (gönderilmedi): skip.md')
  expect(w.files.has(QUEUE + '/skip.md')).toBe(false)
  expect(w.files.has(QUEUE + '/done/19700101-000000-skip.md')).toBe(true)
  await w.clock.advance(1000)
  expect(w.submitted).toEqual([])
  const missing = await $.command.run({ command: 'atla', args: '../../etc/passwd' })
  expect(missing.text).toBe('Kuyrukta böyle bir dosya yok: ../../etc/passwd')
  const usage = await $.command.run({ command: 'atla', args: '' })
  expect(usage.text).toBe('Kullanım: /atla <dosya.md>')
})

test('each new file gets one toast, not one per scan', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  put(w, QUEUE + '/sb1.md', header('SB1'))
  await startSession($)
  await w.clock.advance(5000)
  await w.clock.advance(5000)
  expect(w.toasts.filter((t) => t === 'Yeni istem hazır: SB1 · /basla').length).toBe(1)
})

test('another family queue is never read', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  put(w, '/repo/build/queue/opus/x.md', header('OPUSJOB', 'opus'))
  await startSession($)
  const list = await $.command.run({ command: 'kuyruk', args: '' })
  expect(list.text).toBe('Kuyruk boş.')
})
