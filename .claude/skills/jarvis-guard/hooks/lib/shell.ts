// A shell command line as the guard reads it: simple commands (words and the
// files their output is redirected into), the command texts nested inside them
// (`$(...)`, backticks, `<(...)`, a here-document a shell or interpreter reads)
// and the problems that make a line unreadable. Three dialects: bash (the Bash
// tool, Git Bash), PowerShell and cmd (`cmd /c`). It errs towards seeing MORE
// commands: a separator it is unsure of splits.

export type Kind = 'bash' | 'powershell' | 'cmd'

export type Word = { text: string; dynamic: boolean; glob: boolean; quoted: boolean; brace?: boolean }

export type HereDoc = { body: string; expands: boolean }

export type Simple = {
  words: Word[]
  redirects: Word[] // targets of output redirections (`>`, `>>`, `2>`, `&>`, `*>` ...)
  heredocs: HereDoc[] // here-documents / here-strings fed to this command's stdin
  piped: boolean // stdin comes from the previous command of a pipeline
  nested: string[] // command texts substituted into this command's words, same dialect
  depth: number // bash subshell depth: a `cd` inside `( ... )` ends at its `)`
  pipeOut?: boolean // its output feeds the next command (a bash pipeline stage is a subshell)
}

export type Parsed = { commands: Simple[]; problems: string[] }

type Tok =
  | { t: 'w'; w: Word; nested: string[] }
  | { t: 'op'; v: string }
  | { t: 'redir'; out: boolean }
  | { t: 'here'; doc: HereDoc }
  | { t: 'hstr' }

const NAME = /[A-Za-z0-9_]/ // a character of a variable name

// The text between `open` at `start` (an index just past the opening bracket)
// and its matching `)`, honouring quotes; -1 when unbalanced.
function balanced(src: string, start: number, kind: Kind): number {
  let depth = 1
  let i = start
  while (i < src.length) {
    const c = src[i]
    if (c === "'" && kind !== 'cmd') {
      const end = src.indexOf("'", i + 1)
      if (end < 0) return -1
      i = end + 1
      continue
    }
    if (c === '"') {
      let j = i + 1
      while (j < src.length && src[j] !== '"') j += src[j] === '\\' || src[j] === '`' ? 2 : 1
      if (j >= src.length) return -1
      i = j + 1
      continue
    }
    if (c === '(') depth++
    else if (c === ')') {
      depth--
      if (depth === 0) return i
    }
    i++
  }
  return -1
}

export function parse(src: string, kind: Kind): Parsed {
  const problems: string[] = []
  const toks: Tok[] = []
  const pendingHere: { doc: HereDoc; delim: string; strip: boolean }[] = []
  let w: Word = { text: '', dynamic: false, glob: false, quoted: false }
  let started = false
  let nested: string[] = []
  const flush = () => {
    if (started && kind === 'bash' && w.brace) {
      // bash brace expansion: {a,b} and {1..3} become separate words
      const words = expandBraces(w.text)
      if (words === null) problems.push('çok geniş küme açılımı: ' + w.text)
      else words.forEach((text, k) => toks.push({ t: 'w', w: { ...w, text, brace: false, glob: /[*?[]/.test(text) }, nested: k === 0 ? nested : [] }))
    } else if (started) toks.push({ t: 'w', w, nested })
    w = { text: '', dynamic: false, glob: false, quoted: false }
    started = false
    nested = []
  }
  const n = src.length
  let i = 0
  const readHereBodies = () => {
    for (const pending of pendingHere) {
      const lines: string[] = []
      let found = false
      while (i < n) {
        const eol = src.indexOf('\n', i)
        const line = (eol < 0 ? src.slice(i) : src.slice(i, eol)).replace(/\r$/, '')
        i = eol < 0 ? n : eol + 1
        const cmp = pending.strip ? line.replace(/^\t+/, '') : line
        if (cmp === pending.delim) {
          found = true
          break
        }
        lines.push(line)
      }
      if (!found) problems.push('kapanmamış here-document (' + pending.delim + ')')
      pending.doc.body = lines.join('\n')
    }
    pendingHere.length = 0
  }

  while (i < n) {
    const c = src[i]
    const next = src[i + 1]

    if (c === '\n') {
      flush()
      toks.push({ t: 'op', v: ';' })
      i++
      if (pendingHere.length > 0) readHereBodies()
      continue
    }
    if (c === '\r') {
      i++
      continue
    }
    if (c === ' ' || c === '\t') {
      flush()
      i++
      continue
    }
    // comments
    if (c === '#' && !started && kind !== 'cmd') {
      while (i < n && src[i] !== '\n') i++
      continue
    }
    if (kind === 'powershell' && c === '<' && next === '#' && !started) {
      const end = src.indexOf('#>', i + 2)
      i = end < 0 ? n : end + 2
      if (end < 0) problems.push('kapanmamış <# yorum')
      continue
    }
    if (kind === 'cmd' && !started && /^rem(\s|$)/i.test(src.slice(i, i + 4))) {
      while (i < n && src[i] !== '\n') i++
      continue
    }
    // line continuations and escapes
    if (kind === 'bash' && c === '\\') {
      if (next === '\n') i += 2
      else if (next === '\r' && src[i + 2] === '\n') i += 3
      else {
        w.text += next ?? ''
        started = true
        i += 2
      }
      continue
    }
    if (kind === 'powershell' && c === '`') {
      if (next === '\n') i += 2
      else if (next === '\r' && src[i + 2] === '\n') i += 3
      else {
        w.text += next ?? ''
        started = true
        i += 2
      }
      continue
    }
    if (kind === 'cmd' && c === '^') {
      if (next === '\n') i += 2
      else {
        w.text += next ?? ''
        started = true
        i += 2
      }
      continue
    }
    // PowerShell here-strings @' ... '@ and @" ... "@
    if (kind === 'powershell' && c === '@' && (next === '"' || next === "'") && /^\r?\n/.test(src.slice(i + 2, i + 4))) {
      const quote = next
      const bodyStart = src.indexOf('\n', i) + 1
      const endRe = new RegExp('\\r?\\n' + quote + '@')
      const m = endRe.exec(src.slice(bodyStart - 1))
      if (!m) {
        problems.push('kapanmamış here-string')
        i = n
        continue
      }
      const body = src.slice(bodyStart, bodyStart - 1 + m.index)
      if (quote === '"') collectSubstitutions(body, kind, nested, problems, () => (w.dynamic = true))
      w.text += body
      w.quoted = true
      started = true
      i = bodyStart - 1 + m.index + m[0].length
      continue
    }
    // quotes
    if (c === "'" && kind !== 'cmd') {
      let j = i + 1
      let text = ''
      while (j < n) {
        if (src[j] === "'") {
          if (kind === 'powershell' && src[j + 1] === "'") {
            text += "'"
            j += 2
            continue
          }
          break
        }
        text += src[j]
        j++
      }
      if (j >= n) {
        problems.push("kapanmamış ' tırnağı")
        i = n
        continue
      }
      w.text += text
      w.quoted = true
      started = true
      i = j + 1
      continue
    }
    if (c === '"') {
      let j = i + 1
      let text = ''
      let closed = false
      let inner = false
      while (j < n) {
        const d = src[j]
        if (d === '"') {
          if (kind === 'powershell' && src[j + 1] === '"') {
            text += '"'
            j += 2
            continue
          }
          closed = true
          break
        }
        if (kind === 'bash' && d === '\\' && j + 1 < n) {
          const e = src[j + 1]
          text += '"$`\\\n'.includes(e) ? (e === '\n' ? '' : e) : '\\' + e
          j += 2
          continue
        }
        if (kind === 'powershell' && d === '`' && j + 1 < n) {
          text += src[j + 1]
          j += 2
          continue
        }
        if (kind !== 'cmd' && d === '$' && src[j + 1] === '(') {
          const end = balanced(src, j + 2, kind)
          if (end < 0) {
            problems.push('kapanmamış $(')
            inner = true
            j = n
            break
          }
          if (src[j + 2] !== '(') nested.push(src.slice(j + 2, end))
          w.dynamic = true
          text += src.slice(j, end + 1)
          j = end + 1
          continue
        }
        if (kind === 'bash' && d === '`') {
          const end = src.indexOf('`', j + 1)
          if (end < 0) {
            problems.push('kapanmamış `')
            inner = true
            j = n
            break
          }
          nested.push(src.slice(j + 1, end))
          w.dynamic = true
          j = end + 1
          continue
        }
        if ((kind !== 'cmd' && d === '$' && j + 1 < n && /[A-Za-z_{:]/.test(src[j + 1])) ||
            (kind === 'cmd' && d === '%' && /^%[A-Za-z_][A-Za-z0-9_]*%/.test(src.slice(j)))) {
          w.dynamic = true
        }
        text += d
        j++
      }
      if (!closed) {
        if (!inner) problems.push('kapanmamış " tırnağı')
        i = n
        continue
      }
      w.text += text
      w.quoted = true
      started = true
      i = j + 1
      continue
    }
    // command / process substitution outside quotes
    if (kind !== 'cmd' && c === '$' && next === '(') {
      const end = balanced(src, i + 2, kind)
      if (end < 0) {
        problems.push('kapanmamış $(')
        i = n
        continue
      }
      if (src[i + 2] !== '(') nested.push(src.slice(i + 2, end))
      w.text += src.slice(i, end + 1)
      w.dynamic = true
      started = true
      i = end + 1
      continue
    }
    if (kind === 'bash' && c === '`') {
      const end = src.indexOf('`', i + 1)
      if (end < 0) {
        problems.push('kapanmamış `')
        i = n
        continue
      }
      nested.push(src.slice(i + 1, end))
      w.dynamic = true
      started = true
      i = end + 1
      continue
    }
    if (kind === 'bash' && (c === '<' || c === '>') && next === '(') {
      const end = balanced(src, i + 2, kind)
      if (end < 0) {
        problems.push('kapanmamış ' + c + '(')
        i = n
        continue
      }
      nested.push(src.slice(i + 2, end))
      w.text += src.slice(i, end + 1)
      w.dynamic = true
      started = true
      i = end + 1
      continue
    }
    // variables
    if (kind !== 'cmd' && c === '$' && next !== undefined && /[A-Za-z_{?:@*#!$0-9]/.test(next)) {
      if (kind === 'powershell' && next === '{') {
        const end = src.indexOf('}', i + 2)
        const stop = end < 0 ? n : end + 1
        w.text += src.slice(i, stop)
        w.dynamic = true
        started = true
        i = stop
        continue
      }
      w.dynamic = true
      w.text += c
      started = true
      i++
      continue
    }
    if (kind === 'cmd' && c === '%' && /^%[A-Za-z_][A-Za-z0-9_]*%/.test(src.slice(i))) {
      w.dynamic = true
      w.text += c
      started = true
      i++
      continue
    }
    // redirections (before operators: `&>` and `2>&1`)
    if (kind === 'bash' && c === '&' && next === '>') {
      flush()
      i += src[i + 2] === '>' ? 3 : 2
      toks.push({ t: 'redir', out: true })
      continue
    }
    if (c === '>' || c === '<') {
      // a word of only digits (or PowerShell's *) right before is the stream number
      const fd = started && !w.quoted && /^(\d+|\*)$/.test(w.text)
      if (fd) {
        w = { text: '', dynamic: false, glob: false, quoted: false }
        started = false
        nested = []
      } else flush()
      if (c === '<' && next === '<' && kind === 'bash' && src[i + 2] !== '<') {
        // here-document
        let j = i + 2
        const strip = src[j] === '-'
        if (strip) j++
        while (src[j] === ' ' || src[j] === '\t') j++
        let delim = ''
        let quoted = false
        while (j < n && !/[\s;&|<>()]/.test(src[j])) {
          if (src[j] === "'" || src[j] === '"') {
            const q = src[j]
            const end = src.indexOf(q, j + 1)
            if (end < 0) break
            delim += src.slice(j + 1, end)
            quoted = true
            j = end + 1
            continue
          }
          if (src[j] === '\\') {
            quoted = true
            j++
            continue
          }
          delim += src[j]
          j++
        }
        if (delim === '') {
          problems.push('boş here-document sınırlayıcısı')
          i = j
          continue
        }
        const doc: HereDoc = { body: '', expands: !quoted }
        pendingHere.push({ doc, delim, strip })
        toks.push({ t: 'here', doc })
        i = j
        continue
      }
      if (c === '<' && next === '<' && src[i + 2] === '<') {
        // here-string <<< word: the next word is this command's stdin
        toks.push({ t: 'hstr' })
        i += 3
        continue
      }
      let j = i + 1
      if (src[j] === '>' || src[j] === '|') j++
      if (src[j] === '&') {
        // >&2, 2>&1, <&0: a stream, not a file
        j++
        while (j < n && /[0-9-]/.test(src[j])) j++
        i = j
        continue
      }
      toks.push({ t: 'redir', out: c === '>' })
      i = j
      continue
    }
    // operators
    if (c === ';' || c === '|' || c === '&') {
      flush()
      if (c === '&' && kind === 'powershell' && next !== '&') {
        toks.push({ t: 'op', v: '&call' })
        i++
        continue
      }
      const two = src.slice(i, i + 2)
      if (two === '&&' || two === '||' || two === ';;' || two === '|&') {
        toks.push({ t: 'op', v: two === '|&' ? '|' : two })
        i += 2
        continue
      }
      toks.push({ t: 'op', v: c })
      i++
      continue
    }
    if (c === '(' || c === ')') {
      flush()
      toks.push({ t: 'op', v: c })
      i++
      continue
    }
    if (kind === 'powershell' && (c === '{' || c === '}')) {
      flush()
      toks.push({ t: 'op', v: c })
      i++
      continue
    }
    if (kind === 'bash' && (c === '{' || c === '}') && !started && (next === undefined || /[\s;]/.test(next))) {
      toks.push({ t: 'op', v: c })
      i++
      continue
    }
    if (kind === 'powershell' && c === ',') {
      flush()
      i++
      continue
    }
    if (c === '*' || c === '?' || c === '[') w.glob = true
    if (kind === 'bash' && c === '{' && src[i - 1] !== '$') w.brace = true
    w.text += c
    started = true
    i++
  }
  flush()
  if (pendingHere.length > 0) readHereBodies()

  // split into simple commands
  const commands: Simple[] = []
  let depth = 0
  let cur: Simple = { words: [], redirects: [], heredocs: [], piped: false, nested: [], depth }
  let expectTarget: boolean | null = null
  let hereString = false
  const push = (pipedNext: boolean) => {
    cur.pipeOut = pipedNext
    if (cur.words.length > 0 || cur.redirects.length > 0 || cur.heredocs.length > 0 || cur.nested.length > 0) commands.push(cur)
    cur = { words: [], redirects: [], heredocs: [], piped: pipedNext, nested: [], depth }
  }
  for (const tok of toks) {
    if (tok.t === 'op') {
      if (expectTarget !== null) problems.push('yönlendirmenin hedefi yok')
      expectTarget = null
      if (kind === 'bash' && tok.v === '(') depth++
      if (kind === 'bash' && tok.v === ')') depth = Math.max(0, depth - 1)
      push(tok.v === '|')
      continue
    }
    if (tok.t === 'redir') {
      if (expectTarget !== null) problems.push('yönlendirmenin hedefi yok')
      expectTarget = tok.out
      continue
    }
    if (tok.t === 'here') {
      cur.heredocs.push(tok.doc)
      continue
    }
    if (tok.t === 'hstr') {
      hereString = true
      continue
    }
    cur.nested.push(...tok.nested)
    if (hereString) {
      cur.heredocs.push({ body: tok.w.text, expands: false })
      hereString = false
      continue
    }
    if (expectTarget !== null) {
      if (expectTarget) cur.redirects.push(tok.w)
      expectTarget = null
      continue
    }
    cur.words.push(tok.w)
  }
  if (expectTarget !== null) problems.push('yönlendirmenin hedefi yok')
  push(false)
  return { commands, problems }
}

// `$( ... )` inside an expanding here-string or here-document body.
export function collectSubstitutions(body: string, kind: Kind, into: string[], problems: string[], onDynamic: () => void): void {
  let i = 0
  while (i < body.length) {
    if (body[i] === '$' && body[i + 1] === '(') {
      const end = balanced(body, i + 2, kind)
      if (end < 0) {
        problems.push('kapanmamış $(')
        return
      }
      if (body[i + 2] !== '(') into.push(body.slice(i + 2, end))
      onDynamic()
      i = end + 1
      continue
    }
    if (kind === 'bash' && body[i] === '`') {
      const end = body.indexOf('`', i + 1)
      if (end < 0) {
        problems.push('kapanmamış `')
        return
      }
      into.push(body.slice(i + 1, end))
      onDynamic()
      i = end + 1
      continue
    }
    if (body[i] === '$' && i + 1 < body.length && NAME.test(body[i + 1])) onDynamic()
    i++
  }
}

// The program a word names: the file name without folders, quotes or a
// Windows executable extension, in lower case.
export function programName(word: string): string {
  const base = String(word).replace(/\\/g, '/').split('/').pop() ?? ''
  return base.toLowerCase().replace(/\.(exe|cmd|bat|com|ps1)$/, '')
}

// Bash brace expansion of one word: `{a,b}` and `{1..3}` / `{a..c}`, nested
// and repeated; at most `limit` words (a `for i in {1..100}` loop is fine),
// else null (the guard then refuses).
export function expandBraces(text: string, limit = 1024): string[] | null {
  for (let i = 0; i < text.length; i++) {
    if (text[i] !== '{' || text[i - 1] === '$') continue
    let level = 0
    let j = i
    const commas: number[] = []
    for (; j < text.length; j++) {
      if (text[j] === '{') level++
      else if (text[j] === '}') {
        level--
        if (level === 0) break
      } else if (text[j] === ',' && level === 1) commas.push(j)
    }
    if (j >= text.length) return [text]
    const inner = text.slice(i + 1, j)
    let parts: string[] | null = null
    if (commas.length > 0) {
      parts = []
      let from = i + 1
      for (const at of commas) {
        parts.push(text.slice(from, at))
        from = at + 1
      }
      parts.push(text.slice(from, j))
    } else {
      const num = /^(-?\d+)\.\.(-?\d+)$/.exec(inner)
      const chr = /^([A-Za-z])\.\.([A-Za-z])$/.exec(inner)
      if (num) {
        const a = Number(num[1])
        const b = Number(num[2])
        if (Math.abs(b - a) >= limit) return null
        parts = []
        for (let k = a; a <= b ? k <= b : k >= b; k += a <= b ? 1 : -1) parts.push(String(k))
      } else if (chr) {
        const a = chr[1].charCodeAt(0)
        const b = chr[2].charCodeAt(0)
        parts = []
        for (let k = a; a <= b ? k <= b : k >= b; k += a <= b ? 1 : -1) parts.push(String.fromCharCode(k))
      }
    }
    if (parts === null) continue
    const out: string[] = []
    for (const part of parts) {
      const more = expandBraces(text.slice(0, i) + part + text.slice(j + 1), limit)
      if (more === null) return null
      out.push(...more)
      if (out.length > limit) return null
    }
    return out
  }
  return [text]
}
