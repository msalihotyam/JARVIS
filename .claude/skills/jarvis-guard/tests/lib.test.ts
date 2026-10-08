// The pure parts: path spelling, the assertion counter, the prompt header.
import { expect, test } from 'claude-code/testing'
import { countAsserts } from '../hooks/lib/asserts.ts'
import { parseHeader } from '../hooks/lib/header.ts'
import { canonicalRootOf, normalize, protectedKind, worktreeOf } from '../hooks/lib/paths.ts'

test('paths: one normal form for every spelling', async () => {
  expect(normalize('C:\\Repo\\Sub\\..\\UV.LOCK')).toBe('c:/repo/uv.lock')
  expect(normalize('/c/Repo/./x/')).toBe('c:/repo/x')
  expect(normalize('\\\\?\\C:\\Repo\\x')).toBe('c:/repo/x')
  expect(normalize('C:/repo/uv.lock. ')).toBe('c:/repo/uv.lock')
  expect(normalize('C:/repo/uv.lock::$DATA')).toBe('c:/repo/uv.lock')
  expect(normalize('a/../../b')).toBe('b')
  expect(canonicalRootOf('C:\\Users\\x\\JARVIS\\.claude\\worktrees\\lane\\sub')).toBe('C:/Users/x/JARVIS')
  expect(worktreeOf('c:/r/.claude/worktrees/lane/x/y', 'c:/r')).toBe('c:/r/.claude/worktrees/lane')
  expect(worktreeOf('c:/r/build/x', 'c:/r')).toBe(null)
})

test('paths: what is protected and what is not', async () => {
  const yes = ['c:/r/uv.lock', 'c:/r/data/jarvis.db', 'c:/r/data/jarvis.db-shm', 'c:/r/data/jarvis.pre-v2.db', 'c:/r/data/browser-profile',
    'c:/r/data/chrome-profile/default/x', 'c:/users/u/jarvis-backups/a', 'c:/users/u/.jarvis/conversation.json', 'c:/r/.git',
    'c:/r/.git/objects/x', 'c:/r/progra~1/x']
  for (const p of yes) expect(protectedKind(p) !== null).toBe(true)
  const no = ['c:/r/uv.lock.bak', 'c:/r/.gitignore', 'c:/r/data/jarvis.json', 'c:/r/build/x', 'c:/r/tests/test_x.py', 'c:/users/u/.jarvis/other.json']
  for (const p of no) expect(protectedKind(p)).toBe(null)
})

test('asserts: calls and statements count, strings and comments do not', async () => {
  const src = [
    'class T:',
    '    def test(self):',
    '        self.assertEqual(1, 1)',
    '        self.assertTrue(x); self.fail("no")',
    '        self . assertIn ( 1 , y )',
    '        assert x, "assert y"',
    '        # self.assertEqual(2, 2)',
    '        s = "self.assertTrue(1)"',
    "        t = '''",
    '        self.assertFalse(0)',
    "        '''",
    '        m.assert_called_once()',
    '        if z: assert w',
    "        r = r'\\' assert'",
  ].join('\n')
  expect(countAsserts(src)).toBe(6)
})

test('header: lane and base from the first line only', async () => {
  expect(parseHeader('@jarvis lane=OB1 model=opus effort=high base=378F7EF\nbody')).toEqual({ lane: 'OB1', base: '378f7ef' })
  expect(parseHeader('@jarvis lane=OB1 model=opus effort=high base=main')).toEqual({ lane: 'OB1', base: 'main' })
  expect(parseHeader('hello\n@jarvis lane=x base=main')).toBe(null)
  expect(parseHeader('@jarvis lane=x model=opus')).toBe(null)
})
