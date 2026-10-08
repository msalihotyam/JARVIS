// Path spelling for the guard. Every comparison is on one normal form: forward
// slashes, lower case (Windows file names are case-insensitive), `.`/`..`
// folded, the Win32 aliases of a name removed (trailing dots and spaces, an
// alternate data stream) and the Git Bash drive form `/c/...` read as `c:/...`.

export function normalize(raw: string): string {
  let p = String(raw).trim().replace(/\\/g, '/')
  p = p.replace(/^\/\/[?.]\//, '') // //?/C:/... and //./C:/... device prefixes
  if (/^\/[a-zA-Z](\/|$)/.test(p)) p = p[1] + ':' + p.slice(2) // Git Bash /c/...
  p = p.toLowerCase()
  const driveMatch = /^[a-z]:/.exec(p)
  const drive = driveMatch ? driveMatch[0] : ''
  const rest = drive ? p.slice(2) : p
  const absolute = drive !== '' || rest.startsWith('/')
  const out: string[] = []
  for (const raw of rest.split('/')) {
    // Win32 drops a trailing run of dots / spaces and reads `name:stream` as `name`
    let seg = raw.replace(/:.*$/, '')
    if (seg !== '.' && seg !== '..') seg = seg.replace(/[. ]+$/, '')
    if (seg === '' || seg === '.') continue
    if (seg === '..') {
      if (out.length > 0) out.pop()
      continue
    }
    out.push(seg)
  }
  return drive + (absolute ? '/' : '') + out.join('/')
}

export function isAbsolute(path: string): boolean {
  return /^[a-z]:\//.test(path) || path.startsWith('/')
}

export function join(dir: string, rel: string): string {
  return normalize(dir + '/' + rel)
}

export function inside(path: string, dir: string): boolean {
  return path === dir || path.startsWith(dir + '/')
}

export function strictlyInside(path: string, dir: string): boolean {
  return path.startsWith(dir + '/')
}

// `<root>/.claude/worktrees/<name>` holding `path`, or null.
export function worktreeOf(path: string, root: string): string | null {
  const base = root + '/.claude/worktrees/'
  if (!path.startsWith(base)) return null
  const name = path.slice(base.length).split('/')[0]
  return name ? base + name : null
}

// The canonical checkout for a session folder: a lane's native worktree
// `<canonical>/.claude/worktrees/<name>/...` maps to `<canonical>`.
export function canonicalRootOf(cwd: string): string {
  const raw = String(cwd).replace(/\\/g, '/').replace(/\/+$/, '')
  const at = raw.toLowerCase().indexOf('/.claude/worktrees/')
  return at >= 0 ? raw.slice(0, at) : raw
}

// Expands the home spellings the guard can know; anything else that expands
// at run time leaves the word dynamic.
export function expandHome(text: string, home: string | null): { text: string; dynamic: boolean } {
  let out = text
  if (home !== null) {
    if (out === '~' || out.startsWith('~/') || out.startsWith('~\\')) out = home + out.slice(1)
    out = out.replace(/\$\{HOME\}|\$HOME\b|\$env:(USERPROFILE|HOME)\b|%USERPROFILE%|\$USERPROFILE\b/gi, home)
  }
  const dynamic = /\$[{(A-Za-z_]|%[A-Za-z_][A-Za-z0-9_]*%/.test(out) || (home === null && out.startsWith('~'))
  return { text: out, dynamic }
}

// What a protected path is, by its spelling (normal form), or null.
export function protectedKind(path: string): string | null {
  const segs = normalize(path).split('/').filter(s => s !== '')
  if (segs.length === 0) return null
  const base = segs[segs.length - 1]
  const parent = segs.length >= 2 ? segs[segs.length - 2] : ''
  if (base === 'uv.lock') return 'uv.lock'
  if (segs.includes('.git')) return '.git'
  if (segs.includes('jarvis-backups')) return 'JARVIS-backups'
  for (let i = 0; i < segs.length - 1; i++) {
    if (segs[i] === 'data' && (segs[i + 1] === 'browser-profile' || segs[i + 1] === 'chrome-profile')) return 'data/' + segs[i + 1]
  }
  if (parent === 'data' && (base.startsWith('jarvis.db') || base === 'jarvis.pre-v2.db')) return 'data/' + base
  if (parent === '.jarvis' && base === 'conversation.json') return '~/.jarvis/conversation.json'
  // an 8.3 short name (UV~1.LOC, MASAST~1) can alias any of the above
  if (segs.some(s => /~\d/.test(s))) return 'kısa (8.3) ad'
  return null
}

export function hasGlob(text: string): boolean {
  return /[*?[]/.test(text)
}
