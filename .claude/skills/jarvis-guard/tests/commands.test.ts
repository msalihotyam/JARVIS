// The command table: every hard rule, the bypasses an attacker would try, and
// what must keep working. Judged by the pure rules with a simulated file system.
import { expect, test } from 'claude-code/testing'
import type { Kind } from '../hooks/lib/shell.ts'
import { judgeShell, type Ctx } from '../hooks/lib/rules.ts'

const ROOT = 'c:/repo'
const HOME = 'c:/users/u'
const FILES = new Set([ROOT + '/readme.md', ROOT + '/tests/x.py', ROOT + '/tests/test_x.py', ROOT + '/jarvis/a.py', ROOT + '/uv.lock'])
const DIRS = new Set([ROOT, ROOT + '/tests', ROOT + '/jarvis', ROOT + '/build', ROOT + '/build/fee2', ROOT + '/.claude/worktrees/lane'])
// links and junctions: where a path really lands
const LINKS: Record<string, string> = {
  [ROOT + '/link']: ROOT + '/uv.lock',
  [ROOT + '/build/link']: ROOT + '/jarvis',
  [ROOT + '/.claude/worktrees/j']: ROOT,
}

function ctx(cwd = ROOT): Ctx {
  return {
    root: ROOT,
    home: HOME,
    cwd,
    realPath: async (abs: string) => {
      for (const [from, to] of Object.entries(LINKS)) {
        if (abs === from) return to
        if (abs.startsWith(from + '/')) return to + abs.slice(from.length)
      }
      return abs
    },
    kindOf: async (abs: string) => (DIRS.has(abs) ? 'dir' : FILES.has(abs) ? 'file' : null),
    onCommit: async () => null,
  }
}

type Case = [string, Kind, string] // command, dialect, 'allow' or the rule id it must name

const CASES: Case[] = [
  // ---- R1 git push
  ['git push origin main', 'bash', 'R1'],
  ['git push', 'bash', 'R1'],
  ['git push origin HEAD:refs/heads/ci/6g-k2', 'bash', 'allow'],
  ['git push --force origin HEAD:refs/heads/ci/x', 'bash', 'allow'],
  ['git push origin ci/x', 'bash', 'allow'],
  ['git push -o ci.skip origin HEAD:refs/heads/ci/x', 'bash', 'allow'],
  ['git push origin HEAD:refs/heads/ci/x main', 'bash', 'R1'],
  ['git push origin +HEAD:main', 'bash', 'R1'],
  ['git push origin HEAD:refs/heads/ci/../main', 'bash', 'R1'],
  ['git push --all origin', 'bash', 'R1'],
  ['git push --mirr origin', 'bash', 'R1'],
  ['git push --repo=origin', 'bash', 'R1'],
  ['git push origin "HEAD:refs/heads/$B"', 'bash', 'R1'],
  ['git -C ../x push origin main', 'bash', 'R1'],
  ['GIT_TRACE=1 git push origin main', 'bash', 'R1'],
  ['env GIT_DIR=.git git push origin main', 'bash', 'R1'],
  ["git 'push' origin main", 'bash', 'R1'],
  ['g\\it push origin main', 'bash', 'R1'],
  ['git status && git push origin main', 'bash', 'R1'],
  ['git status; git push origin main', 'bash', 'R1'],
  ['git status || git push origin main', 'bash', 'R1'],
  ['git status | git push origin main', 'bash', 'R1'],
  ['git status & git push origin main', 'bash', 'R1'],
  ['git status\ngit push origin main', 'bash', 'R1'],
  ['echo $(git push origin main)', 'bash', 'R1'],
  ['echo `git push origin main`', 'bash', 'R1'],
  ['echo "$(git push origin main)"', 'bash', 'R1'],
  ['bash -c "git push origin main"', 'bash', 'R1'],
  ["sh -lc 'git push origin main'", 'bash', 'R1'],
  ['"C:/Program Files/Git/cmd/git.exe" push origin main', 'bash', 'R1'],
  ['GIT.EXE push origin main', 'bash', 'R1'],
  ['sudo -u x git push origin main', 'bash', 'R1'],
  ['nohup git push origin main &', 'bash', 'R1'],
  ['{ git push origin main; }', 'bash', 'R1'],
  ['(git push origin main)', 'bash', 'R1'],
  ['echo main | xargs git push origin', 'bash', 'X2'],
  ['git -c alias.p=push p origin main', 'bash', 'X1'],
  ['git p origin main', 'bash', 'X1'],
  ['git push origin main', 'powershell', 'R1'],
  ['& git push origin main', 'powershell', 'R1'],
  ["& 'C:\\Program Files\\Git\\cmd\\git.exe' push origin main", 'powershell', 'R1'],
  ['git status; git push origin main', 'powershell', 'R1'],
  ['powershell -NoProfile -Command "git push origin main"', 'powershell', 'R1'],
  ['pwsh -c git push origin main', 'powershell', 'R1'],
  ['cmd /c "git push origin main"', 'powershell', 'R1'],
  ['cmd.exe /C git push origin main', 'bash', 'R1'],
  ['Invoke-Expression "git push origin main"', 'powershell', 'R1'],
  ["iex 'git push origin main'", 'powershell', 'R1'],
  ['Start-Process git -ArgumentList "push origin main"', 'powershell', 'R1'],
  ['Get-ChildItem | ForEach-Object { git push origin main }', 'powershell', 'R1'],
  ['powershell -EncodedCommand ZwBpAHQAIABwAHUAcwBoAA==', 'powershell', 'X2'],
  ['powershell -enc ZwBpAHQA', 'bash', 'X2'],
  ['iex $cmd', 'powershell', 'X2'],
  ['bash -c "$CMD"', 'bash', 'X2'],
  ['$g push origin main', 'bash', 'X2'],
  ['git push "unterminated', 'bash', 'X2'],
  ['powershell -NoProfile -ExecutionPolicy Bypass -File scripts\\ci.ps1 -Name 6g-k2', 'powershell', 'allow'],
  ['scripts\\ci.ps1 -Name x', 'powershell', 'allow'],
  ['git log | Select-String push', 'powershell', 'allow'],
  ['git log --grep "git push origin main"', 'bash', 'allow'],

  // ---- R2 git add
  ['git add -A', 'bash', 'R2'],
  ['git add --all', 'bash', 'R2'],
  ['git add --al', 'bash', 'R2'],
  ['git add -Av', 'bash', 'R2'],
  ['git add .', 'bash', 'R2'],
  ['git add ./', 'bash', 'R2'],
  ['git add :/', 'bash', 'R2'],
  ['git add ":(top)tests"', 'bash', 'R2'],
  ['git add -u', 'bash', 'R2'],
  ['git add --update', 'bash', 'R2'],
  ['git add -u tests/x.py', 'bash', 'allow'],
  ['git add uv.lock', 'bash', 'R2'],
  ['git add ./UV.LOCK', 'bash', 'R2'],
  ['git add tests/../uv.lock', 'bash', 'R2'],
  ['git add -- uv.lock', 'bash', 'R2'],
  ['git add link', 'bash', 'R2'],
  ['git add *.py', 'bash', 'R2'],
  ['git add tests', 'bash', 'R2'],
  ['git add "$F"', 'bash', 'R2'],
  ['git add --pathspec-from-file=list.txt', 'bash', 'R2'],
  ['git add tests/test_x.py jarvis/a.py', 'bash', 'allow'],
  ['git add tests\\test_x.py', 'powershell', 'allow'],

  // ---- R3 git commit
  ['git commit -a -m x', 'bash', 'R3'],
  ['git commit -am "x"', 'bash', 'R3'],
  ['git commit -vam x', 'bash', 'R3'],
  ['git commit --all -m x', 'bash', 'R3'],
  ['git commit --al -m x', 'bash', 'R3'],
  ['git commit -m x -- tests/x.py', 'bash', 'R3'],
  ['git commit -m x tests/x.py', 'bash', 'R3'],
  ['git commit -i -m x tests/x.py', 'bash', 'R3'],
  ['git commit -m "add -a flag docs"', 'bash', 'allow'],
  ['git commit -ma', 'bash', 'allow'],
  ['git commit -q -F msg.txt', 'bash', 'allow'],
  ["git commit -F - <<'EOF'\nfix: x\n\ngit push origin main and rm -rf / in the message\nEOF", 'bash', 'allow'],
  ['git commit -m "$(cat <<\'EOF\'\nmsg git reset --hard\nEOF\n)"', 'bash', 'allow'],

  // ---- R4 reset / restore / stash / clean / checkout -- paths
  ['git reset --hard HEAD', 'bash', 'R4'],
  ['git reset HEAD tests/x.py', 'bash', 'R4'],
  ['git reset', 'bash', 'R4'],
  ['git restore tests/x.py', 'bash', 'R4'],
  ['git stash', 'bash', 'R4'],
  ['git stash list', 'bash', 'R4'],
  ['git stash push -u -m tag', 'powershell', 'R4'],
  ['git clean -fdx', 'bash', 'R4'],
  ['git checkout -- tests/x.py', 'bash', 'R4'],
  ['git checkout .', 'bash', 'R4'],
  ['git checkout README.md', 'bash', 'R4'],
  ['git checkout HEAD tests/x.py', 'bash', 'R4'],
  ['git checkout -f main', 'bash', 'R4'],
  ['git checkout -B main', 'bash', 'X4'],
  ['git checkout main', 'bash', 'allow'],
  ['git checkout -b feature', 'bash', 'allow'],

  // ---- R5 switch
  ['git switch --discard-changes main', 'bash', 'R5'],
  ['git switch --disc main', 'bash', 'R5'],
  ['git switch -f main', 'bash', 'R5'],
  ['git switch main', 'bash', 'allow'],

  // ---- R6 rebase
  ['git rebase main', 'bash', 'R6'],
  ['git -C .claude/worktrees/lane rebase main', 'bash', 'allow'],
  ['cd .claude/worktrees/lane && git rebase main', 'bash', 'allow'],
  ['cd .claude/worktrees/lane; cd ../../..; git rebase main', 'bash', 'R6'],
  ['git -C c:/repo/.claude/worktrees/lane/../.. rebase main', 'bash', 'R6'],
  ['git -C .claude/worktrees/j rebase main', 'bash', 'R6'],
  ['cd "$X" && git rebase main', 'bash', 'R6'],
  ['git --git-dir=.claude/worktrees/lane/.git rebase main', 'bash', 'R6'],

  // ---- R7 worktree
  ['git worktree prune', 'bash', 'R7'],
  ['git worktree remove --force .claude/worktrees/lane', 'bash', 'R7'],
  ['git worktree remove -f .claude/worktrees/lane', 'bash', 'R7'],
  ['git worktree remove .claude/worktrees/lane', 'bash', 'allow'],
  ['git worktree list', 'bash', 'allow'],

  // ---- R8 / X4 branch
  ['git branch -D worktree-6g-k2-motion', 'bash', 'allow'],
  ['git branch -D main', 'bash', 'R8'],
  ['git branch -D worktree-x main', 'bash', 'R8'],
  ['git branch --delete --force feature', 'bash', 'R8'],
  ['git branch -df feature', 'bash', 'R8'],
  ['git branch -d feature', 'bash', 'allow'],
  ['git branch -f main HEAD~3', 'bash', 'X4'],
  ['git branch -M main', 'bash', 'X4'],
  ['git update-ref refs/heads/main HEAD~1', 'bash', 'X4'],
  ['git reflog expire --expire=now --all', 'bash', 'X4'],
  ['git filter-branch --force HEAD', 'bash', 'X4'],

  // ---- R9 recursive deletes
  ['rm -rf build/fee2', 'bash', 'allow'],
  ['rm -rf build/*', 'bash', 'allow'],
  ['rm -rf .claude/worktrees/lane', 'bash', 'allow'],
  ['rm -rf .claude/worktrees/lane/build', 'bash', 'allow'],
  ['rm -rf build', 'bash', 'R9'],
  ['rm -rf .', 'bash', 'R10'],
  ['rm -rf /', 'bash', 'R9'],
  ['rm -r -f jarvis', 'bash', 'R9'],
  ['rm -fr ../other', 'bash', 'R9'],
  ['rm -Rf build/../jarvis', 'bash', 'R9'],
  ['rm --recursive tests', 'bash', 'R9'],
  ['rm -rf .claude/worktrees', 'bash', 'R9'],
  ['rm -rf .claude/worktrees/*', 'bash', 'R9'],
  ['rm -rf "$HOME/x"', 'bash', 'R9'],
  ['rm -rf $DIR', 'bash', 'R10'],
  ['rm -rf build/link', 'bash', 'R9'],
  ['rm -rf C:\\repo\\jarvis', 'bash', 'R9'],
  ['rm -rf /c/repo/jarvis', 'bash', 'R9'],
  ['rm -rf BUILD/../JARVIS', 'bash', 'R9'],
  ['cd build && rm -rf ../jarvis', 'bash', 'R9'],
  ['Remove-Item -Recurse -Force build\\fee2', 'powershell', 'allow'],
  ['Remove-Item -Recurse -Force jarvis', 'powershell', 'R9'],
  ['Remove-Item jarvis -Rec', 'powershell', 'R9'],
  ['rm -r jarvis', 'powershell', 'R9'],
  ['ri -Recurse tests', 'powershell', 'R9'],
  ['Get-ChildItem jarvis | Remove-Item -Recurse', 'powershell', 'R9'],
  ['cmd /c rmdir /s /q jarvis', 'powershell', 'R9'],
  ['cmd /c "del /s /q tests\\*"', 'bash', 'R9'],
  ['find . -name x -delete', 'bash', 'R10'],
  ['find build -delete', 'bash', 'R9'],
  ['find build/fee2 -delete', 'bash', 'allow'],
  ['find . -exec rm -rf {} +', 'bash', 'R10'],
  ['python -c "import shutil; shutil.rmtree(\'jarvis\')"', 'bash', 'X3'],
  ['[System.IO.Directory]::Delete("C:\\repo\\jarvis", $true)', 'powershell', 'R9'],
  ['rm build/fee2/x.log', 'bash', 'allow'],
  ['rm tests/x.py', 'bash', 'allow'],

  // ---- R10 protected paths in write / move / delete positions
  ['echo x > uv.lock', 'bash', 'R10'],
  ['echo x >> UV.LOCK.', 'bash', 'R10'],
  ['echo x > uv.lock::$DATA', 'bash', 'R10'],
  ['echo x > UV~1.LOC', 'bash', 'R10'],
  ['rm uv.lock', 'bash', 'R10'],
  ['mv data/jarvis.db /tmp/x', 'bash', 'R10'],
  ['cp x data/jarvis.db-wal', 'bash', 'R10'],
  ['echo x >> .git/config', 'bash', 'R10'],
  ['Set-Content -Path data\\jarvis.pre-v2.db -Value x', 'powershell', 'R10'],
  ['"x" | Out-File data\\chrome-profile\\Local State', 'powershell', 'R10'],
  ['rm -rf data/browser-profile', 'bash', 'R10'],
  ['sqlite3 data/jarvis.db "delete from x"', 'bash', 'R10'],
  ['echo x > "$HOME/.jarvis/conversation.json"', 'bash', 'R10'],
  ['Remove-Item C:\\Users\\u\\JARVIS-backups\\old -Recurse', 'powershell', 'R10'],
  ['sed -i s/a/b/ uv.lock', 'bash', 'R10'],
  ['ln -s x uv.lock', 'bash', 'R10'],
  ['echo x > link', 'bash', 'R10'],
  ['cat uv.lock', 'bash', 'allow'],
  ['cp uv.lock build/uv.lock.bak', 'bash', 'allow'],
  ['cp build/x uv.lock', 'bash', 'R10'],
  ['ls .git', 'bash', 'allow'],

  // ---- X3 code read by an interpreter
  ['python -c "import os; os.system(\'git push origin main\')"', 'bash', 'X3'],
  ["python - <<'EOF'\nimport subprocess\nsubprocess.run(['git', 'reset', '--hard'])\nEOF", 'bash', 'X3'],
  ['node -e "require(\'child_process\').execSync(\'git push origin main\')"', 'bash', 'X3'],
  ['echo "git push origin main" | bash', 'bash', 'X3'],
  ['bash <<EOF\ngit push origin main\nEOF', 'bash', 'R1'],
  ['bash <<< "git push origin main"', 'bash', 'R1'],

  // ---- independent review, round 1 (each reproduced against the old judge first)
  ['if git status; then git push origin main; fi', 'bash', 'R1'],
  ['for b in x; do rm -rf jarvis; done', 'bash', 'R9'],
  ['! git push origin main', 'bash', 'R1'],
  ['while true; do git stash; done', 'bash', 'R4'],
  ['cmd /c if exist x git push origin main', 'bash', 'R1'],
  ['rm *.lock', 'bash', 'R10'],
  ['rm uv.lo*', 'bash', 'R10'],
  ['rm data/*', 'bash', 'R10'],
  ['Remove-Item *.lock', 'powershell', 'R10'],
  ['rm {uv.lock,x}', 'bash', 'R10'],
  ['git add {uv.lock,readme.md}', 'bash', 'R2'],
  ['rm -rf build/{fee2,../jarvis}', 'bash', 'R9'],
  ['rm -rf build/{a,b}', 'bash', 'allow'],
  ['rm -rf build/x{1..3}', 'bash', 'allow'],
  ['find jarvis -type f -exec rm -f {} +', 'bash', 'R9'],
  ['find data -name "jarvis.db*" -exec rm {} \\;', 'bash', 'R10'],
  ['find build/fee2 -name "*.log" -exec rm {} +', 'bash', 'allow'],
  ['Get-ChildItem -Recurse jarvis | Remove-Item -Force', 'powershell', 'R9'],
  ['ls -r data | rm', 'powershell', 'R9'],
  ['cp build/uv.lock .', 'bash', 'R10'],
  ['Copy-Item ..\\..\\..\\uv.lock .', 'powershell', 'R10'],
  ['mv build/jarvis.db data/', 'bash', 'R10'],
  ['cp build/a.txt build/fee2/', 'bash', 'allow'],
  ['mv data data.bak', 'bash', 'R10'],
  ['Rename-Item data data_old', 'powershell', 'R10'],
  ['mv ~/.jarvis ~/.jarvis.old', 'bash', 'R10'],
  ['mv build/fee2 build/fee3', 'bash', 'allow'],
  ['git pull --rebase', 'bash', 'R6'],
  ['git pull -r origin main', 'bash', 'R6'],
  ['git -c pull.rebase=true pull', 'bash', 'R6'],
  ['git pull', 'bash', 'R6'],
  ['git pull --ff-only', 'bash', 'allow'],
  ['git -C .claude/worktrees/lane pull --rebase', 'bash', 'allow'],
  ['(cd build && ls); rm -rf jarvis', 'bash', 'R9'],
  ['(cd .claude/worktrees/lane && git status); git rebase main', 'bash', 'R6'],
  ['cd .claude/worktrees/lane && GIT_DIR=c:/repo/.git git rebase main', 'bash', 'R6'],
  ['export GIT_DIR=c:/repo/.git; cd .claude/worktrees/lane; git rebase main', 'bash', 'R6'],
  ['$env:GIT_DIR = "c:/repo/.git"; cd .claude/worktrees/lane; git rebase main', 'powershell', 'R6'],
  ['uv run git push origin main', 'bash', 'R1'],
  ['uv run --with x rm -rf jarvis', 'bash', 'R9'],
  ['npx git push origin main', 'bash', 'R1'],
  ['uv run python -m unittest tests.test_ui', 'bash', 'allow'],
  ['git push --follow-tags origin HEAD:refs/heads/ci/x', 'bash', 'R1'],
  ['git -c push.followTags=true push origin HEAD:refs/heads/ci/x', 'bash', 'R1'],
  ['rsync -a --delete build/empty/ jarvis/', 'bash', 'R9'],
  ['git worktree remove -ff .claude/worktrees/lane', 'bash', 'R7'],
  ['cmd /c "rd /s/q jarvis"', 'bash', 'R9'],
  ['cd .claude/worktrees/lane && cd - && git rebase main', 'bash', 'R6'],
  ['node -e "require(\'fs\').rmdirSync(\'jarvis\',{recursive:true})"', 'bash', 'X3'],
  ['python -Bc "import shutil; shutil.rmtree(\'jarvis\')"', 'bash', 'X3'],
  ['python3.12 -c "import shutil; shutil.rmtree(\'jarvis\')"', 'bash', 'X3'],
  ['git switch -C main', 'bash', 'X4'],
  ['$env:PYTHONUTF8=1; python -m unittest tests.test_ui', 'powershell', 'allow'],
  ['$sha = git rev-parse HEAD', 'powershell', 'allow'],
  ['sqlite3 -readonly data/jarvis.db .tables', 'bash', 'allow'],

  // ---- independent review, round 2
  ['GIT_TRACE="1" git push origin main', 'bash', 'R1'],
  ["A='x' rm -rf jarvis", 'bash', 'R9'],
  ['X="1" git add uv.lock', 'bash', 'R2'],
  ['cd .claude/worktrees/lane && GIT_DIR="c:/repo/.git" git rebase main', 'bash', 'R6'],
  ['$env:GIT_DIR="c:/repo/.git"; cd .claude/worktrees/lane; git rebase main', 'powershell', 'R6'],
  ['$env:PYTHONUTF8="1"; python -m unittest tests.test_ui', 'powershell', 'allow'],
  ['$env:X="1"; git push origin main', 'powershell', 'R1'],
  ['rm -rf build/link/*', 'bash', 'R9'],
  ['rm build/link/*', 'bash', 'allow'],
  ['x=uv.lock; rm $x', 'bash', 'R10'],
  ['$f = "uv.lock"; Remove-Item $f', 'powershell', 'R10'],
  ['Remove-Item $null -ErrorAction SilentlyContinue', 'powershell', 'allow'],
  ['robocopy data build\\d /mov', 'powershell', 'R10'],
  ['robocopy build data jarvis.db', 'powershell', 'R10'],
  ['robocopy build\\a build\\b /mir', 'powershell', 'allow'],
  ['cp -t data build/jarvis.db', 'bash', 'R10'],
  ['cmd /c "for %i in (1) do git push origin main"', 'bash', 'R1'],
  ['cd .claude/worktrees/lane | true; git rebase main', 'bash', 'R6'],
  ['sqlite3 -readonly data/jarvis.db ".shell rm -rf jarvis"', 'bash', 'X3'],
  ['npx --yes rimraf jarvis', 'bash', 'R9'],
  ['npx rimraf build/fee2', 'bash', 'allow'],
  ['for i in {1..100}; do python -m unittest tests.test_ui; done', 'bash', 'allow'],
  ['rm -rf .claude/worktrees/lane/*', 'bash', 'allow'],
  ['rm -- -rf', 'bash', 'allow'],

  // ---- must keep working
  ['git status', 'bash', 'allow'],
  ['git log --oneline -6', 'bash', 'allow'],
  ['git diff --check', 'bash', 'allow'],
  ['git show HEAD:tests/x.py', 'bash', 'allow'],
  ['git cherry main worktree-6g-k2-motion', 'bash', 'allow'],
  ['git range-diff main...a main...b', 'bash', 'allow'],
  ['git rev-parse --short HEAD', 'bash', 'allow'],
  ['git merge-base main worktree-x', 'bash', 'allow'],
  ['git worktree list --porcelain', 'bash', 'allow'],
  ['git cherry-pick main..worktree-6g-k2-motion', 'bash', 'allow'],
  ['git cherry-pick --abort', 'bash', 'allow'],
  ['git merge --ff-only worktree-6g-k2-motion', 'bash', 'allow'],
  ['git branch -d worktree-6g-k2-motion', 'bash', 'allow'],
  ['git -C "C:/repo/.claude/worktrees/lane" status', 'bash', 'allow'],
  ['python scripts\\test_parallel.py', 'powershell', 'allow'],
  ['python scripts/test_parallel.py tests.test_ui', 'bash', 'allow'],
  ['python -m unittest tests.test_ui.FinalPolishTests', 'bash', 'allow'],
  ['scripts\\quality.ps1', 'powershell', 'allow'],
  ['powershell -NoProfile -ExecutionPolicy Bypass -File scripts\\quality.ps1 2>&1 | Select-String -NotMatch "WARNING"', 'powershell', 'allow'],
  ['powershell -NoProfile -ExecutionPolicy Bypass -File scripts\\ci.ps1 -Name 6g-k2', 'bash', 'allow'],
  ["cat > build/x.md <<'EOF'\nrm -rf /\ngit push origin main\nEOF", 'bash', 'allow'],
  ["python - <<'EOF'\nprint('hello')\nEOF", 'bash', 'allow'],
  ['ls .git/index.lock 2>/dev/null; git status --porcelain', 'bash', 'allow'],
  ['mkdir -p build/fee2 && echo ok > build/fee2/x.log', 'bash', 'allow'],
]

for (const [command, kind, want] of CASES) {
  test(kind + ': ' + JSON.stringify(command) + ' -> ' + want, async () => {
    const verdict = await judgeShell(command, kind, ctx())
    if (want === 'allow') expect(verdict).toBe(null)
    else expect(String(verdict)).toContain('[' + want + ' ')
  })
}

test('the table holds at least 60 command cases with bypasses and allows', async () => {
  expect(CASES.length >= 60).toBe(true)
  expect(CASES.filter(c => c[2] === 'allow').length >= 25).toBe(true)
})

test('a refusal is Turkish and names its rule', async () => {
  const verdict = String(await judgeShell('git push origin main', 'bash', ctx()))
  expect(verdict.startsWith('jarvis-guard reddetti [R1 git push yalnız refs/heads/ci/*')).toBe(true)
})

test('a commit runs the assert guard in the folder git would use', async () => {
  const seen: string[] = []
  const c = { ...ctx(), onCommit: async (dir: string, amend: boolean) => (seen.push(dir + (amend ? ' amend' : '')), 'R12 stop') }
  expect(await judgeShell('git -C .claude/worktrees/lane commit --amend -m x', 'bash', c)).toBe('R12 stop')
  expect(seen).toEqual([ROOT + '/.claude/worktrees/lane amend'])
  expect(String(await judgeShell('cd "$X" && git commit -m x', 'bash', c))).toContain('[R12 ')
})
