// The guard as Claude Code runs it: tool calls refused or let through, the file
// tools, the assert guard on `git commit`, the base guard, and failing closed.
import { expect, test } from 'claude-code/testing'
import { HOME, LANE, ROOT, call, prompt, start, world, type Git } from './world.ts'

const A = 'a'.repeat(40)
const B = 'b'.repeat(40)
const tests3 = 'import unittest\nclass T(unittest.TestCase):\n    def test(self):\n        self.assertTrue(1)\n        self.assertEqual(1, 1)\n        assert True\n'
const tests2 = 'import unittest\nclass T(unittest.TestCase):\n    def test(self):\n        self.assertTrue(1)\n        # self.assertEqual(1, 1)\n        x = "assert False"\n        assert True\n'
const tests4 = tests3 + '        self.fail("never")\n'

// git as a commit sees it: one staged test file, HEAD's and the staged text.
function commitGit(status: string, head: string, staged: string, extra: Git = () => null): Git {
  return args => {
    const s = args.join(' ')
    const more = extra(args)
    if (more) return more
    if (s === 'rev-parse --show-toplevel') return { exitCode: 0, stdout: ROOT + '\n' }
    if (s === 'rev-parse --verify -q HEAD') return { exitCode: 0, stdout: A + '\n' }
    if (s.startsWith('diff --cached --name-status --no-renames HEAD -- tests')) return { exitCode: 0, stdout: status + '\ttests/test_a.py\nM\tjarvis/x.py\n' }
    if (s === 'show HEAD:tests/test_a.py') return { exitCode: 0, stdout: head }
    if (s === 'show :tests/test_a.py') return { exitCode: 0, stdout: staged }
    return null
  }
}

test('a refused command never reaches the tool; an allowed one does', async ($, on) => {
  const w = world(on)
  await start($)
  expect(String(await call($, { tool: 'Bash', command: 'git push origin main' }))).toContain('[R1 ')
  expect(String(await call($, { tool: 'PowerShell', command: 'git stash' }))).toContain('[R4 ')
  expect(w.ran).toEqual([])
  expect(await call($, { tool: 'Bash', command: 'git status' })).toBe(null)
  expect(await call($, { tool: 'PowerShell', command: 'git log --oneline -3' })).toBe(null)
  expect(await call($, { tool: 'Read', file_path: ROOT + '/uv.lock' })).toBe(null)
  expect(w.ran).toEqual(['Bash', 'PowerShell', 'Read'])
})

test('outside the JARVIS repo the guard stands aside', async ($, on) => {
  const w = world(on, { isJarvis: false })
  await start($)
  expect(await call($, { tool: 'Bash', command: 'git push origin main' })).toBe(null)
  expect(w.ran).toEqual(['Bash'])
})

test('file tools: every protected path is refused, a link into one too', async ($, on) => {
  const w = world(on, { links: { [ROOT + '/build/jx']: ROOT + '/data/chrome-profile' } })
  await start($)
  const refused: [string, any][] = [
    ['Edit', { file_path: ROOT + '/uv.lock', old_string: 'a', new_string: 'b' }],
    ['Edit', { file_path: 'uv.lock', old_string: 'a', new_string: 'b' }],
    ['Write', { file_path: ROOT + '\\data\\jarvis.db', content: '' }],
    ['Write', { file_path: ROOT + '/data/jarvis.db-wal', content: '' }],
    ['Write', { file_path: ROOT + '/data/jarvis.pre-v2.db', content: '' }],
    ['MultiEdit', { file_path: ROOT + '/data/browser-profile/Default/Prefs', edits: [] }],
    ['Write', { file_path: ROOT + '/data/chrome-profile/Local State', content: '' }],
    ['Write', { file_path: HOME + '/JARVIS-backups/x/manifest.sha256', content: '' }],
    ['Edit', { file_path: HOME + '/.jarvis/conversation.json', old_string: 'a', new_string: 'b' }],
    ['NotebookEdit', { notebook_path: ROOT + '/.git/hooks/pre-commit', new_source: '' }],
    ['Write', { file_path: ROOT + '/.GIT/config', content: '' }],
    ['Write', { file_path: ROOT + '/UV.LOCK.', content: '' }],
    ['Write', { file_path: ROOT + '/build/jx/Local State', content: '' }],
    ['Write', { file_path: LANE + '/uv.lock', content: '' }],
  ]
  for (const [tool, input] of refused) {
    expect(String(await call($, { tool, ...input }))).toContain('[R10 ')
  }
  expect(w.ran).toEqual([])
  expect(await call($, { tool: 'Write', file_path: ROOT + '/build/reports/x.md', content: '' })).toBe(null)
  expect(await call($, { tool: 'Edit', file_path: ROOT + '/jarvis/ui/qml/Main.qml', old_string: 'a', new_string: 'b' })).toBe(null)
  expect(w.ran).toEqual(['Write', 'Edit'])
})

test('assert guard: a commit that lowers the count is refused and lists the file', async ($, on) => {
  const w = world(on, { git: commitGit('M', tests3, tests2) })
  await start($)
  const verdict = String(await call($, { tool: 'Bash', command: 'git commit -m "x"' }))
  expect(verdict).toContain('[R12 ')
  expect(verdict).toContain('tests/test_a.py 3 -> 2 (-1)')
  expect(w.ran).toEqual([])
})

test('assert guard: equal and higher counts commit; a new file only adds', async ($, on) => {
  let staged = tests3
  let status = 'M'
  world(on, { git: args => commitGit(status, tests3, staged)(args) })
  await start($)
  expect(await call($, { tool: 'Bash', command: 'git commit -m x' })).toBe(null)
  staged = tests4
  expect(await call($, { tool: 'PowerShell', command: 'git commit -m x' })).toBe(null)
  status = 'A'
  staged = tests2
  expect(await call($, { tool: 'Bash', command: 'git commit -m x' })).toBe(null)
})

test('assert guard: a deleted test file is a drop', async ($, on) => {
  world(on, { git: commitGit('D', tests3, '') })
  await start($)
  expect(String(await call($, { tool: 'Bash', command: 'git commit -m x' }))).toContain('3 -> 0')
})

test('assert guard: only the approval file naming the lane and the file lets a drop through', async ($, on) => {
  const w = world(on, { git: commitGit('M', tests3, tests2) })
  await start($)
  await prompt($, '@jarvis lane=OB1 model=opus effort=high base=main\nwork')
  const path = ROOT + '/build/approvals/assert-drop-OB1.txt'
  w.files.set(path.toLowerCase(), 'lane OB1\ntests/other.py\n')
  expect(String(await call($, { tool: 'Bash', command: 'git commit -m x' }))).toContain('[R12 ')
  w.files.set(path.toLowerCase(), 'Salih approved 2026-10-08: lane OB1\ntests/test_a.py\n')
  expect(await call($, { tool: 'Bash', command: 'git commit -m x' })).toBe(null)
})

test('assert guard: git that cannot answer refuses the commit', async ($, on) => {
  world(on, { git: args => (args[0] === 'rev-parse' && args[1] === '--show-toplevel' ? { exitCode: 0, stdout: ROOT } : null) })
  await start($)
  expect(String(await call($, { tool: 'Bash', command: 'git commit -m x' }))).toContain('[R12 ')
})

test('assert guard: --amend compares with the parent', async ($, on) => {
  const seen: string[] = []
  world(on, {
    git: commitGit('M', tests3, tests2, args => {
      seen.push(args.join(' '))
      const s = args.join(' ')
      if (s === 'rev-parse --verify -q HEAD^') return { exitCode: 0, stdout: B }
      if (s.startsWith('diff --cached --name-status --no-renames HEAD^ -- tests')) return { exitCode: 0, stdout: 'M\ttests/test_a.py\n' }
      if (s === 'show HEAD^:tests/test_a.py') return { exitCode: 0, stdout: tests4 }
      return null
    }),
  })
  await start($)
  expect(String(await call($, { tool: 'Bash', command: 'git commit --amend --no-edit' }))).toContain('4 -> 2')
  expect(seen.includes('show HEAD^:tests/test_a.py')).toBe(true)
})

function baseGit(start: string, main: string): Git {
  return args => {
    const s = args.join(' ')
    if (s === 'rev-parse HEAD') return { exitCode: 0, stdout: start + '\n' }
    if (s === 'rev-parse main') return { exitCode: 0, stdout: main + '\n' }
    if (s === 'rev-parse --verify -q 1234567^{commit}') return { exitCode: 0, stdout: B + '\n' }
    return null
  }
}

test('base guard: base=main allows edits in a worktree that started at main', async ($, on) => {
  const w = world(on, { git: baseGit(A, A) })
  await start($)
  await prompt($, '@jarvis lane=L1 model=opus effort=high base=main')
  expect(await call($, { tool: 'EnterWorktree', name: 'lane' })).toBe(null)
  expect(await call($, { tool: 'Write', file_path: LANE + '/x.md', content: '' })).toBe(null)
  expect(w.ran).toEqual(['EnterWorktree', 'Write'])
})

test('base guard: a worktree that did not start at main is refused under base=main', async ($, on) => {
  world(on, { git: baseGit(B, A) })
  await start($)
  await prompt($, '@jarvis lane=L1 model=opus effort=high base=main')
  await call($, { tool: 'EnterWorktree', name: 'lane' })
  const verdict = String(await call($, { tool: 'Edit', file_path: LANE + '/x.md', old_string: 'a', new_string: 'b' }))
  expect(verdict).toContain('[R13 ')
  // the canonical checkout is not a lane worktree
  expect(await call($, { tool: 'Write', file_path: ROOT + '/build/x.md', content: '' })).toBe(null)
})

test('base guard: base=<sha> must equal the start HEAD, recorded even without EnterWorktree', async ($, on) => {
  world(on, { cwd: LANE, git: baseGit(B, A) })
  await start($, LANE)
  await prompt($, '@jarvis lane=L1 model=opus effort=high base=1234567')
  expect(await call($, { tool: 'Write', file_path: LANE + '/x.md', content: '' })).toBe(null)
  await prompt($, '@jarvis lane=L1 model=opus effort=high base=main')
  expect(String(await call($, { tool: 'Write', file_path: 'x.md', content: '' }))).toContain('[R13 ')
})

test('base guard: no header, no base check', async ($, on) => {
  world(on, { git: baseGit(B, A) })
  await start($)
  await call($, { tool: 'EnterWorktree', name: 'lane' })
  expect(await call($, { tool: 'Write', file_path: LANE + '/x.md', content: '' })).toBe(null)
})

test('ExitWorktree may keep or remove, never discard uncommitted work', async ($, on) => {
  world(on)
  await start($)
  expect(String(await call($, { tool: 'ExitWorktree', action: 'remove', discard_changes: true }))).toContain('[X5 ')
  expect(await call($, { tool: 'ExitWorktree', action: 'keep' })).toBe(null)
})

test('fail closed: a guard that cannot judge refuses the guarded tool and only that', async ($, on) => {
  const w = world(on, { breakCwd: true })
  await start($)
  expect(String(await call($, { tool: 'Bash', command: 'git status' }))).toContain('[X2 ')
  expect(String(await call($, { tool: 'Write', file_path: ROOT + '/x.md', content: '' }))).toContain('[X2 ')
  expect(await call($, { tool: 'Read', file_path: ROOT + '/x.md' })).toBe(null)
  expect(w.ran).toEqual(['Read'])
})
