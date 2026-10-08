import { expect, test } from 'claude-code/testing'
import { parseHeader } from '../hooks/lib/header.ts'
import { EMPTY_COMPACT_MESSAGE, isBareCompact, lintPrompt } from '../hooks/lib/lint.ts'
import { OPUS, SONNET, header, startSession, userPrompt, world } from './world.ts'

const ctx = { modelId: SONNET, effort: 'high', mainHead: '7c34ff0abcde' }
const lint = (text: string, over: Partial<typeof ctx> = {}) => lintPrompt(parseHeader(text), { ...ctx, ...over })

test('pure: no header passes whatever the session is', { timeoutMs: 60000 }, async () => {
  expect(lint('hello', { modelId: OPUS, effort: 'medium' })).toEqual({ kind: 'pass' })
  expect(lint('hello', { modelId: null, effort: null, mainHead: null })).toEqual({ kind: 'pass' })
})

test('pure: a matching header passes', { timeoutMs: 60000 }, async () => {
  expect(lint(header('SB1'))).toEqual({ kind: 'pass' })
  expect(lint(header('SB1', 'sonnet', 'high', '7c34ff0'))).toEqual({ kind: 'pass' })
})

test('pure: wrong model family blocks with the Turkish message', { timeoutMs: 60000 }, async () => {
  expect(lint(header('SB1', 'opus'))).toEqual({
    kind: 'block',
    message: 'Bu istem OPUS için, bu sohbet SONNET. Doğru sohbete yapıştır.',
  })
  expect(lint(header('SB1', 'sonnet'), { modelId: OPUS })).toEqual({
    kind: 'block',
    message: 'Bu istem SONNET için, bu sohbet OPUS. Doğru sohbete yapıştır.',
  })
})

test('pure: wrong effort blocks, unknown effort passes', { timeoutMs: 60000 }, async () => {
  expect(lint(header('SB1', 'sonnet', 'xhigh'))).toEqual({ kind: 'block', message: 'Önce /effort xhigh yaz' })
  expect(lint(header('SB1', 'sonnet', 'xhigh'), { effort: null })).toEqual({ kind: 'pass' })
})

test('pure: a stale base blocks, base=main and unknown main head pass', { timeoutMs: 60000 }, async () => {
  expect(lint(header('SB1', 'sonnet', 'high', '8aad14a'))).toEqual({
    kind: 'block',
    message: 'İstem tabanı 8aad14a, main 7c34ff0abcde. Orkestratörden güncel istemi iste.',
  })
  expect(lint(header('SB1', 'sonnet', 'high', 'main'), { mainHead: 'ffffffffffff' })).toEqual({ kind: 'pass' })
  expect(lint(header('SB1', 'sonnet', 'high', '8aad14a'), { mainHead: null })).toEqual({ kind: 'pass' })
})

test('pure: an invalid header blocks', { timeoutMs: 60000 }, async () => {
  const verdict = lint('@jarvis lane=x')
  expect(verdict.kind).toBe('block')
})

test('pure: a bare /compact is recognised, /compact with text is not', { timeoutMs: 60000 }, async () => {
  expect(isBareCompact('compact', '')).toBe(true)
  expect(isBareCompact('compact', '   ')).toBe(true)
  expect(isBareCompact('compact', 'Keep: rules')).toBe(false)
  expect(isBareCompact('clear', '')).toBe(false)
})

test('hook: a wrong-family prompt is dropped and never submitted', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  const out = await userPrompt($, header('SB1', 'opus') + '\nbody')
  expect(out.drop).toBe('Bu istem OPUS için, bu sohbet SONNET. Doğru sohbete yapıştır.')
  expect(w.submitted).toEqual([])
  expect(w.toasts).toContain('Bu istem OPUS için, bu sohbet SONNET. Doğru sohbete yapıştır.')
})

test('hook: a prompt without a header goes through untouched', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  const out = await userPrompt($, 'merhaba, bir şey sor')
  expect(out.text).toBe('merhaba, bir şey sor')
  expect(w.submitted).toEqual(['merhaba, bir şey sor'])
})

test('hook: a matching header goes through and is remembered as the lane', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  const text = header('SB1') + '\nbody'
  const out = await userPrompt($, text)
  expect(out.text).toBe(text)
  expect(w.submitted).toEqual([text])
  const status = JSON.parse(w.files.get('/repo/build/lanes/sonnet.json') as string)
  expect(status.lane).toBe('SB1')
  expect(status.base).toBe('main')
})

test('hook: a stale sha base is dropped with both shas in the message', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on, { mainHead: '7c34ff0abcde' })
  await startSession($)
  const out = await userPrompt($, header('SB1', 'sonnet', 'high', '8aad14a') + '\nbody')
  expect(out.drop).toBe('İstem tabanı 8aad14a, main 7c34ff0abcde. Orkestratörden güncel istemi iste.')
  expect(w.submitted).toEqual([])
})

test('hook: the effort check starts once a request has shown the effort', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  on('turn.step', async function* (_$: any, e: any) {
    return { turnId: e.turnId, index: e.index, answer: 'ok', toolUses: [], stopReason: 'end_turn', usage: null }
  })
  await startSession($)
  // before any request the effort is unknown, so the prompt passes
  const first = await userPrompt($, header('SB1', 'sonnet', 'xhigh') + '\nbody')
  expect(first.text).toContain('@jarvis lane=SB1')
  const stream = $.turn.step({ turnId: 't1', index: 0, model: SONNET, effort: 'high', messageCount: 1 })
  let step = await stream.next()
  while (step.done !== true) step = await stream.next()
  const second = await userPrompt($, header('SB2', 'sonnet', 'xhigh') + '\nbody')
  expect(second.drop).toBe('Önce /effort xhigh yaz')
  const third = await userPrompt($, header('SB2', 'sonnet', 'high') + '\nbody')
  expect(third.text).toContain('lane=SB2')
})

test('hook: when the lint itself fails the prompt goes through with a toast', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  w.breakModel = true
  const out = await userPrompt($, header('SB1', 'opus') + '\nbody')
  expect(out.text).toContain('@jarvis lane=SB1')
  expect(w.submitted.length).toBe(1)
  expect(w.toasts.some((t) => t.startsWith('jarvis-flow lint hatası'))).toBe(true)
})

test('hook: nothing is linted outside the JARVIS repo', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on, { isJarvis: false })
  await startSession($)
  const out = await userPrompt($, header('SB1', 'opus') + '\nbody')
  expect(out.text).toContain('model=opus')
  expect(w.submitted.length).toBe(1)
})

test('hook: a bare /compact is blocked, /compact with text is not', { timeoutMs: 60000 }, async ($, on) => {
  world(on)
  await startSession($)
  const bare = await $.command.run({ command: 'compact', args: '' })
  expect(bare.text).toBe(EMPTY_COMPACT_MESSAGE)
  const withText = await $.command.run({ command: 'compact', args: 'Keep: rules' })
  expect(withText.text).toBe('')
})

test('hook: /jcompact fills the prompt box from the template and submits nothing', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  await startSession($)
  await userPrompt($, header('SB1') + '\nbody')
  const out = await $.command.run({ command: 'jcompact', args: '' })
  expect(out.text).toContain('istem kutusuna yazıldı')
  expect(w.filled.length).toBe(1)
  expect(w.filled[0]).toContain('/compact Keep: rules.')
  expect(w.filled[0]).toContain('lane=SB1 base=main')
  expect(w.submitted.length).toBe(1)
})

test('hook: after /effort the next header prompt is judged against the new level', { timeoutMs: 60000 }, async ($, on) => {
  const w = world(on)
  on('turn.step', async function* (_$: any, e: any) {
    return { turnId: e.turnId, index: e.index, answer: 'ok', toolUses: [], stopReason: 'end_turn', usage: null }
  })
  await startSession($)
  const stream = $.turn.step({ turnId: 't1', index: 0, model: SONNET, effort: 'medium', messageCount: 1 })
  let step = await stream.next()
  while (step.done !== true) step = await stream.next()
  const blocked = await userPrompt($, header('SB1', 'sonnet', 'high') + '\nbody')
  expect(blocked.drop).toBe('Önce /effort high yaz')
  // the user does what the message says; no request has been sent since
  await $.command.run({ command: 'effort', args: 'high' })
  const passed = await userPrompt($, header('SB1', 'sonnet', 'high') + '\nbody')
  expect(passed.text).toContain('lane=SB1')
  expect(w.submitted.length).toBe(1)
})

test('hook: /model forgets the known effort, so nothing is blocked until a request shows it', { timeoutMs: 60000 }, async ($, on) => {
  world(on)
  on('turn.step', async function* (_$: any, e: any) {
    return { turnId: e.turnId, index: e.index, answer: 'ok', toolUses: [], stopReason: 'end_turn', usage: null }
  })
  await startSession($)
  const stream = $.turn.step({ turnId: 't1', index: 0, model: SONNET, effort: 'medium', messageCount: 1 })
  let step = await stream.next()
  while (step.done !== true) step = await stream.next()
  await $.command.run({ command: 'model', args: 'opus' })
  const out = await userPrompt($, header('SB1', 'sonnet', 'xhigh') + '\nbody')
  expect(out.text).toContain('lane=SB1')
})

test('hook: a header-led prompt that is malformed is blocked, a plain @mention is not', { timeoutMs: 60000 }, async ($, on) => {
  world(on)
  await startSession($)
  const bad = await userPrompt($, '@jarvis bak şuna')
  expect(bad.drop).toContain('İstem başlığı geçersiz')
  const mention = await userPrompt($, 'hey @jarvis lane=x model=opus effort=high base=main')
  expect(mention.text).toContain('hey @jarvis')
})
