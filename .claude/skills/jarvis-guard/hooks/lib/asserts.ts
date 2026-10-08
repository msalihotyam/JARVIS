// Assertion calls in Python test source: `self.assert*(...)`, `self.fail(...)`
// and `assert` statements. Strings and comments are blanked first, so text
// that only mentions an assertion never counts.

export function blankPython(src: string): string {
  let out = ''
  let i = 0
  const n = src.length
  while (i < n) {
    const c = src[i]
    if (c === '#') {
      while (i < n && src[i] !== '\n') i++
      continue
    }
    const prefix = /^[rRbBuUfF]{0,2}(?='|")/.exec(src.slice(i, i + 3))
    const atWordStart = i === 0 || !/[A-Za-z0-9_]/.test(src[i - 1])
    if ((c === '"' || c === "'") || (prefix && prefix[0] !== '' && atWordStart)) {
      const p = prefix ? prefix[0] : ''
      let j = i + p.length
      const q = src[j]
      const triple = src.slice(j, j + 3) === q + q + q
      const close = triple ? q + q + q : q
      j += close.length
      while (j < n && src.slice(j, j + close.length) !== close) {
        if (!triple && src[j] === '\n') break
        // a backslash keeps the next character in the string, raw or not
        j += src[j] === '\\' ? 2 : 1
      }
      j = Math.min(n, j + close.length)
      out += src.slice(i, j).replace(/[^\n]/g, ' ')
      i = j
      continue
    }
    out += c
    i++
  }
  return out
}

export function countAsserts(src: string): number {
  const code = blankPython(src)
  const calls = code.match(/\bself\s*\.\s*(assert\w*|fail)\s*\(/g) ?? []
  // `assert` is a keyword: outside strings and attributes it is a statement
  const statements = code.match(/(?<![\w.])assert\b/g) ?? []
  return calls.length + statements.length
}
