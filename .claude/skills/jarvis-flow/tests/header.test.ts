import { expect, test } from 'claude-code/testing'
import { familyOf, parseHeader, shaMatches } from '../hooks/lib/header.ts'

const GOOD = '@jarvis lane=SB1 model=sonnet effort=high base=main'

test('a valid header parses and keeps the rest of the prompt', async () => {
  const parsed = parseHeader(GOOD + '\nTASK X')
  expect(parsed).toEqual({
    kind: 'ok',
    header: { lane: 'SB1', model: 'sonnet', effort: 'high', base: 'main' },
    body: 'TASK X',
  })
})

test('a sha base, CRLF and a leading BOM are accepted', async () => {
  const parsed = parseHeader('﻿@jarvis lane=mods-flow1 model=opus effort=xhigh base=7c34ff0\r\nbody')
  expect(parsed.kind).toBe('ok')
  if (parsed.kind === 'ok') {
    expect(parsed.header.base).toBe('7c34ff0')
    expect(parsed.header.lane).toBe('mods-flow1')
    expect(parsed.body).toBe('body')
  }
})

test('field order is free', async () => {
  const parsed = parseHeader('@jarvis base=main effort=medium model=haiku lane=q0')
  expect(parsed.kind).toBe('ok')
})

test('no header means normal chat', async () => {
  expect(parseHeader('hello')).toEqual({ kind: 'absent' })
  expect(parseHeader('')).toEqual({ kind: 'absent' })
  expect(parseHeader('  @jarvis lane=x model=opus effort=high base=main')).toEqual({ kind: 'absent' })
  expect(parseHeader('first line\n' + GOOD)).toEqual({ kind: 'absent' })
  expect(parseHeader('@jarvisX lane=x')).toEqual({ kind: 'absent' })
  expect(parseHeader('/compact')).toEqual({ kind: 'absent' })
})

test('a malformed header is invalid, with the reason', async () => {
  const cases: [string, string][] = [
    ['@jarvis lane=a model=opus effort=high', 'eksik alan: base'],
    ['@jarvis', 'eksik alan: lane'],
    ['@jarvis lane=a lane=b model=opus effort=high base=main', 'yinelenen alan: lane'],
    ['@jarvis lane=a model=opus effort=high base=main extra=1', 'bilinmeyen alan: extra'],
    ['@jarvis lane=a model=gpt effort=high base=main', 'model geçersiz: gpt'],
    ['@jarvis lane=a model=opus effort=low base=main', 'effort geçersiz: low'],
    ['@jarvis lane=a model=opus effort=high base=develop', 'base geçersiz: develop'],
    ['@jarvis lane=a model=opus effort=high base=abc12', 'base geçersiz: abc12'],
    ['@jarvis lane=../x model=opus effort=high base=main', 'lane geçersiz: ../x'],
    ['@jarvis lane=a model=opus effort=high base main', 'bozuk parça: base'],
  ]
  for (const [text, reason] of cases) {
    expect(parseHeader(text)).toEqual({ kind: 'invalid', reason })
  }
})

test('familyOf reads the model id', async () => {
  expect(familyOf('claude-opus-5-5')).toBe('opus')
  expect(familyOf('claude-sonnet-5-5[1m]')).toBe('sonnet')
  expect(familyOf('claude-haiku-5-5')).toBe('haiku')
  expect(familyOf('gpt-5')).toBeNull()
  expect(familyOf('')).toBeNull()
})

test('shaMatches is a prefix match of at least 7 characters', async () => {
  expect(shaMatches('7c34ff0', '7c34ff0abcdef')).toBe(true)
  expect(shaMatches('7c34ff0abcdef', '7c34ff0')).toBe(true)
  expect(shaMatches('7c34ff0', '8aad14a')).toBe(false)
  expect(shaMatches('7c34', '7c34ff0')).toBe(false)
})
