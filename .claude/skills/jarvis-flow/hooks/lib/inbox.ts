import { parseHeader, type Family, type Header } from './header.ts'
import { lintPrompt, type LintContext } from './lint.ts'

export const MAX_BYTES = 64 * 1024

export type Entry = { name: string; kind: 'file' | 'dir' | 'other'; size: number; mtimeMs: number; isLink: boolean }

export type Candidate = { name: string; size: number; mtimeMs: number }

// Oldest first, by modification time then name. Only plain *.md files that are
// not links, not hidden, and not over the size cap are even looked at.
export function candidates(entries: readonly Entry[]): Candidate[] {
  return entries
    .filter(
      (entry) =>
        entry.kind === 'file' &&
        !entry.isLink &&
        entry.name.endsWith('.md') &&
        !entry.name.startsWith('.') &&
        !/[\\/:*?"<>|]/.test(entry.name) &&
        entry.size <= MAX_BYTES,
    )
    .map((entry) => ({ name: entry.name, size: entry.size, mtimeMs: entry.mtimeMs }))
    .sort((a, b) => a.mtimeMs - b.mtimeMs || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
}

// Entries that are .md but are rejected before reading, with the reason.
export function skipped(entries: readonly Entry[]): { name: string; reason: string }[] {
  const out: { name: string; reason: string }[] = []
  for (const entry of entries) {
    if (!entry.name.endsWith('.md') || entry.kind === 'dir') continue
    if (entry.isLink || entry.kind !== 'file') out.push({ name: entry.name, reason: 'bağlantı veya dosya değil' })
    else if (entry.size > MAX_BYTES) out.push({ name: entry.name, reason: 'çok büyük (> 64 KB)' })
    else if (entry.name.startsWith('.') || /[\\/:*?"<>|]/.test(entry.name)) out.push({ name: entry.name, reason: 'geçersiz ad' })
  }
  return out
}

function normal(path: string): string {
  return path.replace(/^\\\\\?\\/, '').replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase()
}

// True when the real path of a file is directly inside the folder (no
// traversal, no link out of it).
export function isInside(realPath: string, folder: string): boolean {
  const dir = normal(folder)
  const real = normal(realPath)
  if (!real.startsWith(dir + '/')) return false
  const rest = real.slice(dir.length + 1)
  return rest.length > 0 && !rest.includes('/') && rest !== '..' && rest !== '.'
}

export type Accepted = { ok: true; header: Header; text: string; bytes: number }
export type Rejected = { ok: false; reason: string }

export function acceptContent(content: string, family: Family, lintCtx: LintContext): Accepted | Rejected {
  const bytes = new TextEncoder().encode(content).length
  if (bytes > MAX_BYTES) return { ok: false, reason: 'çok büyük (> 64 KB)' }
  const parsed = parseHeader(content)
  if (parsed.kind === 'absent') return { ok: false, reason: '@jarvis başlığı yok' }
  if (parsed.kind === 'invalid') return { ok: false, reason: 'başlık geçersiz: ' + parsed.reason }
  if (parsed.header.model !== family) {
    return { ok: false, reason: 'başka sohbet için (' + parsed.header.model + ')' }
  }
  const verdict = lintPrompt(parsed, lintCtx)
  if (verdict.kind === 'block') return { ok: false, reason: verdict.message }
  return { ok: true, header: parsed.header, text: content, bytes }
}

export function firstLines(text: string, count: number): string {
  return text.split(/\r?\n/).slice(0, count).join('\n')
}

export function shortHash(hex: string): string {
  return hex.slice(0, 12)
}

// 20261008-141500 from epoch milliseconds (UTC)
export function stamp(ms: number): string {
  const iso = new Date(ms).toISOString()
  return iso.slice(0, 4) + iso.slice(5, 7) + iso.slice(8, 10) + '-' + iso.slice(11, 13) + iso.slice(14, 16) + iso.slice(17, 19)
}

export async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('')
}
