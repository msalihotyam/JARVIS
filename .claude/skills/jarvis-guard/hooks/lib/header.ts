// The orchestrator's prompt header: `@jarvis lane=<id> model=.. effort=.. base=<sha|main>`
// on the first line. The guard needs the lane (assert-drop approvals) and the base.

export type Header = { lane: string; base: string }

export function parseHeader(text: string): Header | null {
  const first = (String(text).split(/\r?\n/, 1)[0] ?? '').trim()
  if (!/^@jarvis(\s|$)/.test(first)) return null
  const lane = /(?:^|\s)lane=([A-Za-z0-9._-]+)(?=\s|$)/.exec(first)
  const base = /(?:^|\s)base=([0-9a-fA-F]{7,40}|main)(?=\s|$)/.exec(first)
  if (!lane || !base) return null
  return { lane: lane[1], base: base[1].toLowerCase() }
}
