// The prompt header contract:
//   @jarvis lane=<slice-id> model=<opus|sonnet|haiku> effort=<medium|high|xhigh> base=<sha|main>
// Parsed strictly. A prompt that does not start with @jarvis is normal chat.

export type Family = 'opus' | 'sonnet' | 'haiku'
export type Effort = 'medium' | 'high' | 'xhigh'

export const FAMILIES: readonly Family[] = ['opus', 'sonnet', 'haiku']
export const EFFORTS: readonly Effort[] = ['medium', 'high', 'xhigh']

export type Header = { lane: string; model: Family; effort: Effort; base: string }

export type Parsed =
  | { kind: 'absent' }
  | { kind: 'invalid'; reason: string }
  | { kind: 'ok'; header: Header; body: string }

const LANE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/
const SHA = /^[0-9a-f]{7,40}$/
const REQUIRED = ['lane', 'model', 'effort', 'base'] as const

export function parseHeader(text: string): Parsed {
  const clean = String(text ?? '').replace(/^﻿/, '')
  const newline = clean.indexOf('\n')
  const first = (newline === -1 ? clean : clean.slice(0, newline)).replace(/\r$/, '')
  if (!/^@jarvis(\s|$)/.test(first)) return { kind: 'absent' }

  const found = new Map<string, string>()
  for (const token of first.slice('@jarvis'.length).split(/\s+/).filter(Boolean)) {
    const match = /^([a-z]+)=(\S+)$/.exec(token)
    if (!match) return { kind: 'invalid', reason: 'bozuk parça: ' + token }
    const key = match[1] as string
    const value = match[2] as string
    if (!(REQUIRED as readonly string[]).includes(key)) return { kind: 'invalid', reason: 'bilinmeyen alan: ' + key }
    if (found.has(key)) return { kind: 'invalid', reason: 'yinelenen alan: ' + key }
    found.set(key, value)
  }
  for (const key of REQUIRED) {
    if (!found.has(key)) return { kind: 'invalid', reason: 'eksik alan: ' + key }
  }
  const lane = found.get('lane') as string
  const model = found.get('model') as string
  const effort = found.get('effort') as string
  const base = found.get('base') as string
  if (!LANE.test(lane)) return { kind: 'invalid', reason: 'lane geçersiz: ' + lane }
  if (!(FAMILIES as readonly string[]).includes(model)) return { kind: 'invalid', reason: 'model geçersiz: ' + model }
  if (!(EFFORTS as readonly string[]).includes(effort)) return { kind: 'invalid', reason: 'effort geçersiz: ' + effort }
  if (base !== 'main' && !SHA.test(base)) return { kind: 'invalid', reason: 'base geçersiz: ' + base }
  const body = newline === -1 ? '' : clean.slice(newline + 1)
  return { kind: 'ok', header: { lane, model: model as Family, effort: effort as Effort, base }, body }
}

// The model family of a session's model id, or null when it is none of ours.
export function familyOf(modelId: string): Family | null {
  const id = String(modelId ?? '').toLowerCase()
  for (const family of FAMILIES) {
    if (id.includes(family)) return family
  }
  return null
}

export function shaMatches(a: string, b: string): boolean {
  const x = a.toLowerCase()
  const y = b.toLowerCase()
  if (x.length < 7 || y.length < 7) return false
  return x.startsWith(y) || y.startsWith(x)
}
