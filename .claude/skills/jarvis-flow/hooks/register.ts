// jarvis-flow: lane band, prompt lint, prompt inbox, report capture, power and pause.
// Active only in a session whose project is the JARVIS repo (JARVIS_RUNBOOK.md at
// the canonical root). Everywhere else every hook passes the event through.
import { familyOf, parseHeader, type Family, type Header } from './lib/header.ts'
import { EMPTY_COMPACT_MESSAGE, isBareCompact, lintPrompt, type LintContext } from './lib/lint.ts'
import {
  acceptContent,
  candidates,
  firstLines,
  isInside,
  sha256Hex,
  shortHash,
  skipped,
  stamp,
  type Entry,
} from './lib/inbox.ts'
import { BATTERY_ARGV, gate, isFreshWorking, parseBattery, type Gate, type Power } from './lib/power.ts'
import { canonicalRootOf, fingerprint, stateOf, worktreeOf, type LastAuto, type Status } from './lib/status.ts'
import { extractReport, safeLane } from './lib/report.ts'
import { conveyorOf, renderBand, type BandState, type Conveyor } from './lib/band.ts'
import {
  BEAT_MS,
  CAS_ARGV,
  HOLD_MAX_MS,
  POLL_SECONDS,
  PY_COUNT_ARGV,
  SLEEP_ARGV,
  SUITE_RUNNING_ARGV,
  classifyCommand,
  isGated,
  lockState,
  minutesSince,
  newLock,
  parseLock,
  parseTestOutput,
  plain,
  toCount,
  type LockState,
  type Parsed,
  type TestCommand,
} from './lib/suite.ts'
import {
  APPEND_ARGV,
  FLAKE_PATH,
  SLICE_PATH,
  dayOf,
  defterText,
  flakyText,
  isFlakeRow,
  isSliceRow,
  parseRows,
  statusLineOf,
  statusWord,
  usageOf,
  type FlakeRow,
  type SliceRow,
  type TestRun,
} from './lib/ledger.ts'
import { LOW_BATTERY_PCT, curlArgv, MIN_GAP_MS, curlConfig, isBadStatus, nextWaitPush, pushText, validTopic, type PushWhy } from './lib/pager.ts'

const TICK_MS = 5000
const POWER_MS = 120000
const START_GUARD_MS = 60000
const CONVEYOR_MS = 60000

type Item = { dir: string; kind: 'manual' | 'auto'; name: string; abs: string; text: string; sha: string; header: Header }
type Rejection = { kind: 'manual' | 'auto'; name: string; reason: string }

// ---- module state (rebuilt on every reload)
let active = false
let root = ''
let cwd = ''
let modelId: string | null = null
let famName = 'unknown'
let lastEffort: string | null = null
let lane: Header | null = null
let busy = false
let waiting = false
let startingSince: number | null = null
let head: string | null = null
let branch: string | null = null
let baseState: 'ok' | 'bad' | 'main' | null = null
let contextPct: number | null = null
let planPct: number | null = null
let power: Power = { ac: true, pct: null }
let paused = false
let lowBattery = false
let lowToasted = false
let conveyor: Conveyor | null = null
let conveyorReadAt: number | null = null
let manual: Item[] = []
let auto: Item[] = []
let rejected: Rejection[] = []
let lastAuto: LastAuto | null = null
let lastWritten = ''
let ticking = false
const seen = new Set<string>()
const contentCache = new Map<string, string>()
const usedKeys = new Set<string>()
let tickTimer: { cancel: () => void } | null = null
let powerTimer: { cancel: () => void } | null = null
let reportLane: string | null = null
// wave 2
let sessionId = ''
let sevenPct: number | null = null
let waitingSince: number | null = null
let lastWaitPush: number | null = null
let lastPush: number | null = null
let pushing = false
let topicToasted = false
let curlToasted = false
let lowPushed = false
let headerPending = false
type TurnRec = { startMs: number; lane: string; model: string | null; effort: string | null; five0: number | null; seven0: number | null; tools: number; tests: TestRun[] }
let turnRec: TurnRec | null = null
type BgRun = { tc: TestCommand; procs: number | null; otherHeld: boolean; t0: number }
const bgRuns: BgRun[] = []

const laneLabel = () => (lane ? lane.lane : famName)

function dirOf(kind: 'manual' | 'auto'): string {
  return root + '/build/queue/' + famName + (kind === 'auto' ? '/auto' : '')
}

async function sh($: any, argv: string[], where?: string): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  try {
    return await $.process.run(argv, where ? { cwd: where, timeoutMs: 15000 } : { timeoutMs: 15000 })
  } catch {
    return { exitCode: 1, stdout: '', stderr: '' }
  }
}

async function mainHead($: any): Promise<string | null> {
  const out = await sh($, ['git', 'rev-parse', '--short=12', 'main'], root)
  const sha = out.stdout.trim()
  return out.exitCode === 0 && /^[0-9a-f]{7,40}$/.test(sha) ? sha : null
}

async function refreshGit($: any): Promise<void> {
  const b = await sh($, ['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd)
  const h = await sh($, ['git', 'rev-parse', '--short=12', 'HEAD'], cwd)
  branch = b.exitCode === 0 ? b.stdout.trim() : null
  head = h.exitCode === 0 ? h.stdout.trim() : null
  if (lane === null) baseState = null
  else if (lane.base === 'main') baseState = 'main'
  else {
    const anc = await sh($, ['git', 'merge-base', '--is-ancestor', lane.base, 'HEAD'], cwd)
    baseState = anc.exitCode === 0 ? 'ok' : 'bad'
  }
}

async function refreshUsage($: any): Promise<void> {
  try {
    const usage = await $.session.usage()
    contextPct = typeof usage.context.percent === 'number' ? usage.context.percent : null
    const five = usage.rateLimits.find((row: any) => row.kind === 'five_hour')
    planPct = five ? Math.round(five.percentUsed) : null
    const seven = usage.rateLimits.find((row: any) => row.kind === 'seven_day')
    sevenPct = seven ? Math.round(seven.percentUsed) : null
  } catch {
    // not available on this surface: leave what was known
  }
}

async function readJson($: any, path: string): Promise<any | null> {
  try {
    return JSON.parse(await $.fs.read(path))
  } catch {
    return null
  }
}

async function otherLaneWorking($: any): Promise<boolean> {
  let entries: Entry[] = []
  try {
    entries = await $.fs.list(root + '/build/lanes')
  } catch {
    return false
  }
  const now = await $.clock.now()
  for (const entry of entries) {
    if (entry.kind !== 'file' || !entry.name.endsWith('.json') || entry.name === famName + '.json') continue
    const status = await readJson($, root + '/build/lanes/' + entry.name)
    if (isFreshWorking(status, now)) return true
  }
  return false
}

async function currentGate($: any): Promise<Gate> {
  paused = await $.fs.exists(root + '/build/queue/PAUSE')
  const others = !power.ac ? await otherLaneWorking($) : false
  const g = gate({ power, paused, otherLaneWorking: others })
  lowBattery = g.lowBattery
  return g
}

// The band's conveyor piece, from the tick (never from the render), at most once per CONVEYOR_MS.
async function refreshConveyor($: any, now: number): Promise<void> {
  if (conveyorReadAt !== null && now - conveyorReadAt < CONVEYOR_MS) return
  conveyorReadAt = now
  try {
    const text: string = await $.fs.read(root + '/build/conveyor/status.json')
    conveyor = conveyorOf(text, await $.fs.exists(root + '/build/conveyor/PAUSE'), now)
  } catch {
    conveyor = null
  }
}

async function writeStatus($: any): Promise<void> {
  if (!active) return
  const status: Status = {
    family: famName,
    model: modelId,
    effort: lastEffort,
    lane: lane ? lane.lane : null,
    base: lane ? lane.base : null,
    cwd,
    worktree: worktreeOf(cwd),
    branch,
    head,
    state: stateOf({ busy, waiting, lowBattery }),
    busy,
    updated_at: new Date(await $.clock.now()).toISOString(),
    context_pct: contextPct,
    power,
    paused,
    last_auto: lastAuto,
  }
  const print = fingerprint(status)
  if (print === lastWritten) return
  await $.fs.write(root + '/build/lanes/' + famName + '.json', JSON.stringify(status, null, 2) + '\n')
  lastWritten = print
}

function lintContext(mainSha: string | null): LintContext {
  return { modelId, effort: lastEffort, mainHead: mainSha }
}

async function runMove($: any, from: string, to: string): Promise<boolean> {
  try {
    const out = await $.process.run(
      ['powershell', '-NoProfile', '-NonInteractive', '-Command', 'New-Item -ItemType Directory -Force -Path (Split-Path -LiteralPath $env:JF_DST) | Out-Null; Move-Item -LiteralPath $env:JF_SRC -Destination $env:JF_DST'],
      { env: { JF_SRC: from, JF_DST: to }, timeoutMs: 15000 },
    )
    return out.exitCode === 0
  } catch {
    return false
  }
}

async function runRemove($: any, target: string): Promise<boolean> {
  try {
    const out = await $.process.run(
      ['powershell', '-NoProfile', '-NonInteractive', '-Command', 'Remove-Item -LiteralPath $env:JF_SRC -Force'],
      { env: { JF_SRC: target }, timeoutMs: 15000 },
    )
    return out.exitCode === 0
  } catch {
    return false
  }
}

async function scanDir($: any, kind: 'manual' | 'auto', outItems: Item[], outRejected: Rejection[]): Promise<void> {
  const folder = dirOf(kind)
  let mainSha: string | null | undefined
  let entries: Entry[] = []
  try {
    entries = await $.fs.list(folder)
  } catch {
    return
  }
  for (const skip of skipped(entries)) outRejected.push({ kind, name: skip.name, reason: skip.reason })
  const fam = famName as Family
  for (const cand of candidates(entries)) {
    const abs = folder + '/' + cand.name
    const key = abs + '|' + cand.size + '|' + cand.mtimeMs
    usedKeys.add(key)
    let content = contentCache.get(key)
    if (content === undefined) {
      try {
        const st = await $.fs.stat(abs, { resolve: true })
        const real = typeof st.realPath === 'string' ? st.realPath : abs
        if (!isInside(real, folder)) {
          outRejected.push({ kind, name: cand.name, reason: 'klasör dışına çıkıyor' })
          continue
        }
        content = await $.fs.read(abs)
      } catch {
        outRejected.push({ kind, name: cand.name, reason: 'okunamadı' })
        continue
      }
      contentCache.set(key, content as string)
    }
    if (mainSha === undefined) mainSha = await mainHead($)
    const verdict = acceptContent(content as string, fam, lintContext(mainSha))
    if (!verdict.ok) {
      outRejected.push({ kind, name: cand.name, reason: verdict.reason })
      continue
    }
    const sha = await sha256Hex(content as string)
    const item: Item = { dir: folder, kind, name: cand.name, abs, text: content as string, sha, header: verdict.header }
    outItems.push(item)
  }
}

async function scanQueue($: any): Promise<void> {
  // the family is pinned at session start; a fallback model never moves the queue
  if (familyOf(famName) === null) {
    manual = []
    auto = []
    rejected = []
    return
  }
  const nextManual: Item[] = []
  const nextAuto: Item[] = []
  const nextRejected: Rejection[] = []
  usedKeys.clear()
  await scanDir($, 'manual', nextManual, nextRejected)
  await scanDir($, 'auto', nextAuto, nextRejected)
  for (const key of [...contentCache.keys()]) {
    if (!usedKeys.has(key)) contentCache.delete(key)
  }
  manual = nextManual
  auto = nextAuto
  rejected = nextRejected
}

function idle(nowMs: number): boolean {
  const starting = startingSince !== null && nowMs - startingSince < START_GUARD_MS
  return !busy && !waiting && !starting
}

// Start one prompt: re-check it now, move it to done/, then submit it. Returns
// null on success or the Turkish reason it did not start.
async function startItem($: any, item: Item, how: 'manual' | 'auto'): Promise<string | null> {
  // reserve the start first, so a second path cannot pass idle() while the awaits below run
  const now = await $.clock.now()
  if (startingSince !== null && now - startingSince < START_GUARD_MS) return 'başka bir istem başlıyor'
  startingSince = now
  const release = (why: string): string => {
    startingSince = null
    return why
  }
  try {
    const fresh = await mainHead($)
    const verdict = acceptContent(item.text, famName as Family, lintContext(fresh))
    if (!verdict.ok) return release('istem artık geçerli değil: ' + verdict.reason)
    const g = await currentGate($)
    if (!g.allow) return release(g.reason)
    if (busy || waiting) return release('oturum meşgul')
    // the file on disk must still be the one that was accepted
    let onDisk: string | null = null
    try {
      onDisk = await $.fs.read(item.abs)
    } catch {
      onDisk = null
    }
    if (onDisk === null || (await sha256Hex(onDisk)) !== item.sha) return release('dosya değişti, yeniden taranacak')
    const done = item.dir + '/done/' + stamp(now) + '-' + item.name
    if (!(await runMove($, item.abs, done))) return release('dosya done/ içine taşınamadı, istem gönderilmedi')
    if (how === 'auto') {
      lastAuto = { file: item.name, sha256: item.sha, at: new Date(now).toISOString() }
      $.ui.toast('Otomatik başladı: ' + item.header.lane + ' (' + shortHash(item.sha) + ')')
    }
    $.clock.after(50, async () => {
      try {
        await $.prompt.submit({ text: item.text, asUser: true })
      } catch (err: any) {
        // the prompt did not go out: put the file back so nothing is lost
        startingSince = null
        if (how === 'auto') lastAuto = null
        const back = await runMove($, done, item.abs)
        $.ui.toast('jarvis-flow: istem gönderilemedi' + (back ? ' (dosya kuyruğa geri kondu)' : ' (dosya done/ içinde kaldı)') + ': ' + String(err && err.message ? err.message : err))
      }
    })
    return null
  } catch (err: any) {
    return release('beklenmeyen hata: ' + String(err && err.message ? err.message : err))
  }
}

async function tick($: any): Promise<void> {
  if (!active || ticking) return
  ticking = true
  try {
    // first, so a failing queue scan cannot freeze it (it never throws)
    await refreshConveyor($, await $.clock.now())
    const g = await currentGate($)
    await scanQueue($)
    for (const item of [...manual, ...auto]) {
      const key = item.kind + '/' + item.name + '/' + item.sha
      if (!seen.has(key)) {
        seen.add(key)
        $.ui.toast(item.kind === 'auto' ? 'Otomatik kuyruğa girdi: ' + item.header.lane : 'Yeni istem hazır: ' + item.header.lane + ' · /basla')
      }
    }
    for (const r of rejected) {
      const key = 'rejected/' + r.kind + '/' + r.name + '/' + r.reason
      if (!seen.has(key)) {
        seen.add(key)
        $.ui.toast('Kuyruk reddetti: ' + r.name + ' (' + r.reason + ')')
      }
    }
    const now = await $.clock.now()
    if (auto.length > 0 && manual.length === 0 && g.allow && idle(now)) {
      const why = await startItem($, auto[0] as Item, 'auto')
      if (why !== null) $.ui.toast('Otomatik başlamadı: ' + why)
    }
    await writeStatus($)
    $.ui.invalidate('ui.render')
  } catch (err: any) {
    $.ui.toast('jarvis-flow: kuyruk taraması hatası: ' + String(err && err.message ? err.message : err))
  } finally {
    ticking = false
  }
}

// The suite lock's heartbeat and the background-run watch, and the pager's permission-wait push.
// Apart from tick() so a failing queue scan cannot stop the heartbeat.
let ticking2 = false
async function wave2Tick($: any): Promise<void> {
  if (!active || ticking2) return
  ticking2 = true
  try {
    if (held.raw !== null || held.holds > 0) {
      if (!(await beatSuite($))) $.ui.toast('Test kilidi kayboldu: başka oturum aldı')
      await watchBackground($)
    }
    await pagerTick($, await $.clock.now())
  } catch {
    // tried again on the next tick
  } finally {
    ticking2 = false
  }
}

async function tickPower($: any): Promise<void> {
  if (!active) return
  const out = await sh($, [...BATTERY_ARGV])
  power = parseBattery(out.stdout)
  const g = await currentGate($)
  if (g.batteryPaused && !lowToasted) {
    lowToasted = true
    $.ui.toast('Pil %' + power.pct + ': yeni istem başlamaz')
  }
  if (!g.batteryPaused) lowToasted = false
  const low = !power.ac && power.pct !== null && power.pct < LOW_BATTERY_PCT
  if (!low) lowPushed = false
  else if (!lowPushed) {
    const r = await push($, 'battery', pushText('battery', laneLabel(), await $.clock.now(), String(power.pct)))
    if (r !== 'limited') lowPushed = true
  }
  await writeStatus($)
  $.ui.invalidate('ui.render')
}

// The chat's family follows the model the user chose (famName, queue folder, status file, band).
async function adoptFamily($: any): Promise<void> {
  const typed = familyOf(modelId ?? '')
  if (typed === null || typed === famName) return
  famName = typed
  lastWritten = ''
  manual = []
  auto = []
  rejected = []
  contentCache.clear()
  $.ui.toast('Sohbet artık ' + typed.toUpperCase())
  await writeStatus($)
  $.ui.invalidate('ui.render')
}

// ---- wave 2: suite queue. At most one full suite (python scripts\test_parallel.py) at a
// time across all sessions on this PC, through the lock file build/locks/suite.json.

// The suite lock this session holds (a count: parallel calls of ours share one lock).
const held = {
  holds: 0,
  raw: null as string | null,
  startedAt: null as string | null,
  lastBeat: 0,
  // a background full suite: no process of the runner for two polls in a row = it is over
  bg: null as { since: number; lastPoll: number; zero: number } | null,
}

// What the band shows while this session waits: the holder's lane and its minutes.
const suiteWaiting = { on: null as { lane: string; minutes: number } | null }

const lockPath = () => root + '/build/locks/suite.json'
const BUSY_DENY_MS = 60000

// Every change of `held` and of the lock file by this session runs one after the other
// (a heartbeat, a take and a release can never interleave).
let lockChain: Promise<unknown> = Promise.resolve()
function serial<T>(fn: () => Promise<T>): Promise<T> {
  const run = lockChain.then(fn, fn)
  lockChain = run.then(
    () => undefined,
    () => undefined,
  )
  return run
}

async function runCmd($: any, argv: readonly string[], env?: Record<string, string>, timeoutMs = 20000, stdin?: string) {
  try {
    const init: any = { timeoutMs }
    if (env) init.env = env
    if (stdin !== undefined) init.stdin = stdin
    return await $.process.run([...argv], init)
  } catch {
    return { exitCode: 1, stdout: '', stderr: '' }
  }
}

// null = no file; 'busy' = it exists but could not be read right now.
async function readLock($: any): Promise<string | null | 'busy'> {
  try {
    if (!(await $.fs.exists(lockPath()))) return null
    return await $.fs.read(lockPath())
  } catch {
    return 'busy'
  }
}

// Compare-and-swap on the lock file (one exclusive open, see CAS_ARGV).
// expect null = create; { stale: nowMs } = take over unless it is a fresh, valid record.
async function casLock($: any, expect: string | null | { stale: number }, next: string): Promise<boolean> {
  const env: Record<string, string> = { JF_PATH: lockPath(), JF_NEW: next }
  if (expect === null) env.JF_MODE = 'create'
  else if (typeof expect === 'object') {
    env.JF_MODE = 'stale'
    env.JF_NOW = String(expect.stale)
  } else {
    env.JF_MODE = 'swap'
    if (expect.trim() !== '') env.JF_EXPECT = expect
  }
  const out = await runCmd($, CAS_ARGV, env)
  return out.exitCode === 0
}

async function pyCount($: any): Promise<number | null> {
  const out = await runCmd($, PY_COUNT_ARGV, undefined, 15000)
  return out.exitCode === 0 ? toCount(out.stdout) : null
}

// A wait inside a `$.process.run` call: it does not count against the hook's time budget.
async function sleepSeconds($: any, seconds: number, signal: any): Promise<boolean> {
  if (signal && signal.aborted) return false
  const out = await runCmd($, SLEEP_ARGV, { JF_SECONDS: String(seconds) }, (seconds + 15) * 1000)
  return !(signal && signal.aborted) && out.exitCode === 0
}

type SuiteGate = { deny?: string; acquired: boolean; otherHeld: boolean }
type Attempt =
  | { kind: 'go'; acquired: boolean }
  | { kind: 'held'; lane: string; minutes: number }
  | { kind: 'busy' }
  | { kind: 'lost' }
  | { kind: 'deny'; text: string }

// One look at the lock and, when it is free or stale and the command needs it, one take.
function attempt($: any, tc: TestCommand, command: string): Promise<Attempt> {
  return serial(async (): Promise<Attempt> => {
    const now = await $.clock.now()
    const read = await readLock($)
    if (read === 'busy') return { kind: 'busy' }
    const st: LockState = lockState(read, now, sessionId)
    if (st.state === 'mine') {
      if (!tc.lock) return { kind: 'go', acquired: false }
      if (held.holds > 0) {
        return { kind: 'deny', text: "jarvis-flow suite queue: this session's own full suite is still running. Wait for it to finish before starting another." }
      }
      // the lock is ours from before a reload of this mod, or a release that did not finish: take it again
      const fresh = newLock(laneLabel(), sessionId, command, now, st.rec.started_at)
      if (!(await casLock($, read as string, fresh))) return { kind: 'lost' }
      held.raw = fresh
      held.startedAt = st.rec.started_at
      held.lastBeat = now
      held.holds = 1
      return { kind: 'go', acquired: true }
    }
    if (st.state === 'held') return { kind: 'held', lane: plain(st.rec.lane), minutes: minutesSince(st.rec.started_at, now) }
    if (!tc.lock) return { kind: 'go', acquired: false }
    const stamp = newLock(laneLabel(), sessionId, command, now)
    const expect = st.state === 'free' ? (read === null ? null : '') : { stale: now }
    if (!(await casLock($, expect, stamp))) return { kind: 'lost' }
    held.holds = 1
    held.raw = stamp
    held.startedAt = new Date(now).toISOString()
    held.lastBeat = now
    return { kind: 'go', acquired: true }
  })
}

// Decides whether a test command may start. A full suite takes the lock (or waits for
// it, up to 40 min); a UI-loading unittest run only waits while another session holds
// it. The holder's own runs are never held (a second full suite of its own is refused).
async function gateSuite($: any, tc: TestCommand, command: string, signal: any): Promise<SuiteGate> {
  const t0 = await $.clock.now()
  let busySince: number | null = null
  let otherHeld = false
  let lost = 0
  for (;;) {
    const r = await attempt($, tc, command)
    const now = await $.clock.now()
    if (r.kind === 'go') {
      suiteWaiting.on = null
      return { acquired: r.acquired, otherHeld }
    }
    if (r.kind === 'deny') {
      suiteWaiting.on = null
      return { deny: r.text, acquired: false, otherHeld }
    }
    if (r.kind === 'busy') {
      // an unreadable lock file (a directory, a permission): do not make the model wait 40 min for it
      busySince = busySince ?? now
      if (now - busySince >= BUSY_DENY_MS) {
        suiteWaiting.on = null
        return { deny: 'jarvis-flow suite queue: build/locks/suite.json cannot be read (is it a directory or locked?). Fix or remove that file, then retry the same command.', acquired: false, otherHeld }
      }
    } else if (r.kind === 'lost') {
      busySince = null
      if (++lost >= 6) {
        suiteWaiting.on = null
        return { deny: 'jarvis-flow suite queue: the suite lock file build/locks/suite.json could not be taken (6 tries). Wait a minute and retry the same command.', acquired: false, otherHeld }
      }
      if (!(await sleepSeconds($, 2, signal))) return { deny: 'jarvis-flow suite queue: the wait was cancelled. Retry the command when you want to run it.', acquired: false, otherHeld }
      continue
    } else {
      busySince = null
      otherHeld = true
      const waited = now - t0
      if (waited >= HOLD_MAX_MS) {
        suiteWaiting.on = null
        $.ui.invalidate('ui.render')
        return {
          deny:
            "jarvis-flow suite queue: lane '" + r.lane + "' has held the full-suite lock for " + r.minutes + ' min and this command waited ' + Math.round(waited / 60000) +
            ' min for it. Do not start another suite now: wait (for example sleep 300), then retry the same command. A lock with no heartbeat for 45 min is taken over automatically (build/locks/suite.json).',
          acquired: false,
          otherHeld,
        }
      }
      suiteWaiting.on = { lane: r.lane, minutes: r.minutes }
      $.ui.invalidate('ui.render')
    }
    if (!(await sleepSeconds($, POLL_SECONDS, signal))) {
      suiteWaiting.on = null
      $.ui.invalidate('ui.render')
      return { deny: 'jarvis-flow suite queue: the wait was cancelled. Retry the command when you want to run it.', acquired: false, otherHeld }
    }
  }
}

// Frees the lock file if it is still ours. Retries when the file is busy (another
// session reading it, a virus scanner); true when it is free or no longer ours.
async function freeIfOurs($: any, tries: number): Promise<boolean> {
  for (let i = 0; i < tries; i++) {
    const read = await readLock($)
    if (read === null) return true
    if (read !== 'busy') {
      if (read.trim() === '') return true
      const rec = parseLock(read)
      if (rec === null || rec.session !== plain(sessionId, 80)) return true
      if (await casLock($, read, '')) return true
    }
    if (i < tries - 1) await sleepSeconds($, 1, null)
  }
  return false
}

// Drops one hold; the last one frees the lock file. Safe to call with nothing held.
function releaseSuite($: any, all = false): Promise<void> {
  return serial(async () => {
    if (held.holds === 0) return
    held.holds = all ? 0 : held.holds - 1
    if (held.holds > 0) return
    held.bg = null
    // the text stays in `held` until the file is really free: the tick retries a release that failed
    if (await freeIfOurs($, 5)) {
      held.raw = null
      held.startedAt = null
    }
  })
}

// Mod (re)load: a lock record of this session whose suite still runs is adopted as a background hold,
// anything else of ours is freed.
function adoptOrFree($: any): Promise<void> {
  return serial(async () => {
    const read = await readLock($)
    const rec = typeof read === 'string' ? parseLock(read) : null
    if (typeof read === 'string' && rec !== null && rec.session === plain(sessionId, 80)) {
      const out = await runCmd($, SUITE_RUNNING_ARGV, undefined, 20000)
      if (out.exitCode === 0 && (toCount(out.stdout) ?? 0) > 0) {
        const now = await $.clock.now()
        held.holds = 1
        held.raw = read
        held.startedAt = rec.started_at
        held.lastBeat = 0
        held.bg = { since: now - 60000, lastPoll: now, zero: 0 }
        return
      }
    }
    await freeIfOurs($, 2)
  })
}

// Session end: free the file whenever its record is ours, whatever `held` says.
function endSuite($: any): Promise<void> {
  return serial(async () => {
    held.holds = 0
    held.bg = null
    held.raw = null
    held.startedAt = null
    await freeIfOurs($, 2)
  })
}

// Called by the 5 s tick: a heartbeat once a minute while a hold is on, and a retry of
// a release that did not finish. Returns false when the lock was lost to another session.
function beatSuite($: any): Promise<boolean> {
  return serial(async () => {
    if (held.raw === null) return true
    if (held.holds === 0) {
      if (await freeIfOurs($, 1)) {
        held.raw = null
        held.startedAt = null
      }
      return true
    }
    const now = await $.clock.now()
    if (now - held.lastBeat < BEAT_MS) return true
    const started = held.raw
    const old = parseLock(started)
    const next = newLock(laneLabel(), sessionId, old ? old.command : '', now, held.startedAt ?? undefined)
    if (await casLock($, started, next)) {
      held.raw = next
      held.lastBeat = now
      return true
    }
    const read = await readLock($)
    if (read === 'busy') return true
    if (read !== null) {
      const rec = parseLock(read)
      if (rec !== null && rec.session === plain(sessionId, 80)) {
        // ours, but the text moved under us: adopt it
        held.raw = read
        return true
      }
    }
    held.holds = 0
    held.raw = null
    held.bg = null
    return false
  })
}

// A background full suite: the Bash result came back at once, so the lock stays until
// the runner's processes are gone (two empty polls, 30 s apart, after the first 30 s).
async function watchBackground($: any): Promise<void> {
  const bg = held.bg
  if (bg === null || held.holds === 0) return
  const now = await $.clock.now()
  if (now - bg.since < 30000 || now - bg.lastPoll < 30000) return
  bg.lastPoll = now
  const out = await runCmd($, SUITE_RUNNING_ARGV, undefined, 20000)
  const n = out.exitCode === 0 ? toCount(out.stdout) : null
  if (n === null) return
  bg.zero = n === 0 ? bg.zero + 1 : 0
  if (bg.zero >= 2) await releaseSuite($, true)
}

// One JSON row appended to a jsonl file (tries three times: another session may be writing).
// A line is capped so the environment value stays spawnable.
async function appendRow($: any, path: string, row: unknown): Promise<boolean> {
  let line = JSON.stringify(row)
  if (line.length > 8000) {
    const r = row as { failures?: string[] }
    line = JSON.stringify({ ...r, failures: (r.failures ?? []).slice(0, 20).map((f) => String(f).slice(0, 200)), targets: [], truncated: true })
  }
  for (let i = 0; i < 3; i++) {
    const out = await runCmd($, APPEND_ARGV, { JF_PATH: path, JF_LINE: line }, 15000)
    if (out.exitCode === 0) return true
  }
  return false
}

// ---- wave 2: pager

async function push($: any, why: PushWhy, text: string, quiet = false): Promise<'sent' | 'limited' | 'notopic' | 'failed'> {
  if (pushing) return 'limited'
  pushing = true
  try {
    const now = await $.clock.now()
    if (lastPush !== null && now - lastPush < MIN_GAP_MS) return 'limited'
    const topic = await $.env.get('JARVIS_NTFY_TOPIC')
    if (!validTopic(topic)) {
      if (!topicToasted) {
        topicToasted = true
        $.ui.toast('Pager kapalı: JARVIS_NTFY_TOPIC yok ya da geçersiz (setx JARVIS_NTFY_TOPIC <konu>)')
      }
      return 'notopic'
    }
    lastPush = now
    // topic and text go in on stdin as a curl config; nothing secret is in argv, a file or a toast
    const out = await $.process.run(curlArgv(await $.env.get('SystemRoot')), { stdin: curlConfig(topic, text, quiet), timeoutMs: 15000 })
    if (out.exitCode === 0) return 'sent'
    if (!curlToasted) {
      curlToasted = true
      $.ui.toast('Pager gönderemedi (curl çıkış ' + out.exitCode + ')')
    }
    return 'failed'
  } catch {
    if (!curlToasted) {
      curlToasted = true
      $.ui.toast('Pager gönderemedi (curl çalışmadı)')
    }
    return 'failed'
  } finally {
    pushing = false
  }
}

async function pagerTick($: any, now: number): Promise<void> {
  const due = nextWaitPush(waitingSince, lastWaitPush)
  if (due !== null && waitingSince !== null && now >= due) {
    const r = await push($, 'wait', pushText('wait', laneLabel(), waitingSince, ''))
    if (r !== 'limited') lastWaitPush = now
  }
}

// ---- wave 2: suite queue, flake radar, slice ledger

function shellCommand(e: any): string | null {
  return (e.tool === 'Bash' || e.tool === 'PowerShell') && typeof e.command === 'string' ? e.command : null
}

function testCommandOf(e: any): TestCommand | null {
  const command = shellCommand(e)
  return command === null ? null : classifyCommand(command, e.run_in_background === true)
}

async function otherHolds($: any): Promise<boolean> {
  const read = await readLock($)
  if (read === 'busy') return true
  return lockState(read, await $.clock.now(), sessionId).state === 'held'
}

async function recordRun($: any, tc: TestCommand, parsed: Parsed, procs: number | null, otherHeld: boolean, measuredS: number): Promise<void> {
  const row: FlakeRow = {
    time: new Date(await $.clock.now()).toISOString(),
    lane: laneLabel(),
    kind: tc.kind,
    targets: tc.targets,
    ran: parsed.ran,
    failures: parsed.failures,
    ok: parsed.ok,
    wall_s: parsed.wallS ?? measuredS,
    python_procs: procs,
    other_lane_held: otherHeld,
  }
  await appendRow($, root + FLAKE_PATH, row)
  if (turnRec !== null) {
    turnRec.tests.push({ kind: tc.kind, ran: parsed.ran, failures: parsed.failures.length, python_procs: procs, wall_s: row.wall_s })
  }
}

// A test command: wait for the suite lock if it needs it, run it, then read the result.
async function runTest($: any, e: any, next: any, tc: TestCommand): Promise<any> {
  const command = shellCommand(e) as string
  let acquired = false
  let otherHeld = false
  if (tc.lock || tc.ui) {
    const g = await gateSuite($, tc, command, next.signal)
    if (g.deny !== undefined) return { deny: g.deny }
    acquired = g.acquired
    otherHeld = g.otherHeld
  } else {
    otherHeld = await otherHolds($)
  }
  // whatever happens below, a lock taken here is freed unless it is handed to the background watch
  let handoff = false
  let procs: number | null = null
  let t0 = 0
  let t1 = 0
  let res: any
  let started = false
  try {
    procs = await pyCount($)
    t0 = await $.clock.now()
    res = await next(e)
    t1 = await $.clock.now()
    started = res !== undefined && res !== null && res.deny === undefined && res.isError !== true
    if (acquired && tc.background && started) {
      held.bg = { since: t1, lastPoll: t1, zero: 0 }
      handoff = true
    }
  } finally {
    if (acquired && !handoff) await releaseSuite($)
  }
  try {
    if (!started) return res
    if (tc.background) {
      bgRuns.push({ tc, procs, otherHeld, t0 })
      if (bgRuns.length > 5) bgRuns.shift()
    } else {
      const parsed = parseTestOutput(typeof res.text === 'string' ? res.text : '')
      if (parsed !== null) await recordRun($, tc, parsed, procs, otherHeld, Math.round((t1 - t0) / 1000))
    }
  } catch {
    // the ledger must never break a test run
  }
  return res
}

// The result of a background test run arrives in a later tool result (reading its log).
async function takeBackgroundResult($: any, res: any): Promise<void> {
  if (bgRuns.length === 0 || !res || typeof res.text !== 'string') return
  const parsed = parseTestOutput(res.text)
  if (parsed === null || parsed.ran === null) return
  const run = bgRuns.shift() as BgRun
  await recordRun($, run.tc, parsed, run.procs, run.otherHeld, Math.round(((await $.clock.now()) - run.t0) / 1000))
}

function bandState(): BandState {
  return {
    family: famName,
    baseState,
    head,
    contextPct,
    planPct,
    power,
    paused,
    lowBattery,
    pendingLane: manual.length > 0 ? (manual[0] as Item).header.lane : null,
    suiteWait: suiteWaiting.on,
    conveyor,
  }
}

async function fillTemplate($: any): Promise<string> {
  const template: string = await $.fs.read($.plugin.root + '/compact-template.md')
  const values: Record<string, string> = {
    lane: lane ? lane.lane : '(yok)',
    base: lane ? lane.base : '(yok)',
    head: head ?? '(bilinmiyor)',
    worktree: worktreeOf(cwd) ?? '(canonical)',
    branch: branch ?? '(bilinmiyor)',
    cwd,
  }
  return template.trim().replace(/\{\{(\w+)\}\}/g, (_all, key: string) => values[key] ?? '')
}

export function register(on: any) {
  on('session.start', async ($: any, e: any, next: any) => {
    try {
      cwd = (await $.session.cwd()).replace(/\\/g, '/')
      root = canonicalRootOf(cwd)
      active = await $.fs.exists(root + '/JARVIS_RUNBOOK.md')
      if (!active) return next(e)
      modelId = await $.session.model()
      famName = familyOf(modelId ?? '') ?? 'unknown'
      try {
        sessionId = plain(await $.session.id(), 80)
      } catch {
        sessionId = ''
      }
      if (sessionId === '') sessionId = 'sess-' + (await $.clock.now()).toString(36)
      try {
        // a lock of this session left by a reload of the mod: keep it if its suite still runs, else free it
        await adoptOrFree($)
      } catch {
        // goes stale after 45 min
      }
      await refreshGit($)
      await refreshUsage($)
      paused = await $.fs.exists(root + '/build/queue/PAUSE')
      if (tickTimer !== null) tickTimer.cancel()
      if (powerTimer !== null) powerTimer.cancel()
      tickTimer = $.clock.every(TICK_MS, async () => {
        await tick($)
        await wave2Tick($)
      })
      powerTimer = $.clock.every(POWER_MS, async () => {
        try {
          await tickPower($)
        } catch (err: any) {
          $.ui.toast('jarvis-flow: pil okunamadı: ' + String(err && err.message ? err.message : err))
        }
      })
      try {
        await tickPower($)
      } catch {
        // no battery reading: mains power is assumed
      }
      try {
        await tick($)
      } catch {
        // the timer will try again
      }
      for (const name of ['basla', 'kuyruk', 'atla', 'mola', 'devam', 'jcompact', 'flaky', 'defter']) {
        try {
          await $.command.register({ name, description: 'jarvis-flow: ' + name })
        } catch (err: any) {
          $.ui.toast('jarvis-flow: /' + name + ' kaydedilemedi: ' + String(err && err.message ? err.message : err))
        }
      }
    } catch (err: any) {
      active = false
      $.ui.toast('jarvis-flow başlatılamadı: ' + String(err && err.message ? err.message : err))
    }
    return next(e)
  })

  on('prompt.submit', async ($: any, e: any, next: any) => {
    if (!active) return next(e)
    try {
      const parsed = parseHeader(e.text)
      if (e.origin && e.origin.kind !== 'plugin') {
        modelId = await $.session.model()
        const needMain = parsed.kind === 'ok' && parsed.header.base !== 'main'
        const verdict = lintPrompt(parsed, lintContext(needMain ? await mainHead($) : null))
        if (verdict.kind === 'block') {
          $.ui.toast(verdict.message)
          return { drop: verdict.message }
        }
      }
      reportLane = parsed.kind === 'ok' ? parsed.header.lane : null
      headerPending = parsed.kind === 'ok'
      if (parsed.kind === 'ok') {
        lane = parsed.header
        await refreshGit($)
        await writeStatus($)
        $.ui.invalidate('ui.render')
      }
    } catch (err: any) {
      $.ui.toast('jarvis-flow lint hatası (istem geçti): ' + String(err && err.message ? err.message : err))
    }
    return next(e)
  })

  on('turn.start', async ($: any, e: any, next: any) => {
    if (active) {
      busy = true
      startingSince = null
      turnRec = null
      if (headerPending) {
        // a turn started by an @jarvis header prompt gets a row in the slice ledger
        headerPending = false
        try {
          await refreshUsage($)
          turnRec = {
            startMs: await $.clock.now(),
            lane: laneLabel(),
            model: modelId,
            effort: lastEffort,
            five0: planPct,
            seven0: sevenPct,
            tools: 0,
            tests: [],
          }
        } catch {
          turnRec = null
        }
      }
      try {
        await writeStatus($)
        $.ui.invalidate('ui.render')
      } catch {
        // a status file that cannot be written must not stop a turn
      }
    }
    return next(e)
  })

  on('turn.step', async function* ($: any, e: any, next: any) {
    if (active && e.agentId === undefined) {
      if (e.effort !== undefined) lastEffort = String(e.effort)
    }
    return yield* next(e)
  })

  on('tool.call', async ($: any, e: any, next: any) => {
    if (!active) return next(e)
    if (turnRec !== null) turnRec.tools++
    if (waiting) {
      waiting = false
      waitingSince = null
      lastWaitPush = null
      try {
        await writeStatus($)
      } catch {
        // ignore
      }
    }
    const tc = testCommandOf(e)
    if (tc === null) {
      const res = await next(e)
      try {
        await takeBackgroundResult($, res)
      } catch {
        // the ledger must never break a tool call
      }
      return res
    }
    return runTest($, e, next, tc)
  }).catch(($: any, e: any, next: any) => {
    // fail closed for a test command: when the queue check broke, the command does not run
    if (next.called) return next(e)
    if (isGated(testCommandOf(e))) {
      return { deny: 'jarvis-flow suite queue: the check failed before the command started. Wait a minute and retry the same command.' }
    }
    return next(e)
  })

  on('session.end', async ($: any, e: any, next: any) => {
    if (active) {
      try {
        // /clear and resume go on in the same process: a suite still running keeps its lock (the watch frees it)
        if (!(['clear', 'resume'].includes(String(e.reason)) && held.holds > 0)) await endSuite($)
      } catch {
        // a lock that could not be freed goes stale after 45 min
      }
    }
    return next(e)
  })

  on('classic.PermissionRequest', async ($: any, e: any, next: any) => {
    if (active) {
      if (!waiting) {
        waitingSince = await $.clock.now()
        lastWaitPush = null
      }
      waiting = true
      try {
        await writeStatus($)
        $.ui.invalidate('ui.render')
      } catch {
        // ignore
      }
    }
    return next(e)
  })

  on('turn.complete', async ($: any, e: any, next: any) => {
    if (active && e.agentId === undefined) {
      busy = false
      waiting = false
      startingSince = null
      try {
        const report = extractReport(e.answer)
        const name = safeLane(reportLane)
        if (report !== null && name !== null) {
          const path = root + '/build/reports/' + name + '.md'
          if (await $.fs.exists(path)) {
            const old = await $.fs.read(path)
            if (old !== report) {
              await $.fs.write(root + '/build/reports/' + name + '.' + stamp(await $.clock.now()) + '.md', old)
              await $.fs.write(path, report)
            }
          } else {
            await $.fs.write(path, report)
          }
        }
        await refreshGit($)
        await refreshUsage($)
        await writeStatus($)
        $.ui.invalidate('ui.render')
      } catch (err: any) {
        $.ui.toast('jarvis-flow: rapor/durum yazılamadı: ' + String(err && err.message ? err.message : err))
      }
      waitingSince = null
      lastWaitPush = null
      const rec = turnRec
      turnRec = null
      if (rec !== null) {
        try {
          const endMs = await $.clock.now()
          const row: SliceRow = {
            start: new Date(rec.startMs).toISOString(),
            end: new Date(endMs).toISOString(),
            wall_s: Math.round((typeof e.durationMs === 'number' ? e.durationMs : endMs - rec.startMs) / 1000),
            lane: rec.lane,
            model: e.usage && typeof e.usage.model === 'string' ? e.usage.model : rec.model,
            effort: rec.effort ?? lastEffort,
            tokens: usageOf(e.usage),
            five_hour: { start: rec.five0, end: planPct },
            seven_day: { start: rec.seven0, end: sevenPct },
            tool_calls: rec.tools,
            test_runs: rec.tests,
            status: statusLineOf(e.answer),
          }
          await appendRow($, root + SLICE_PATH, row)
          const word = statusWord(e.answer)
          // bad statuses go to the conveyor attention list (orchestrator first); Salih only gets an
          // information push so he can wake the orchestrator early if he is free; it rings by day and is silent 02:00-08:00 (local)
          if (isBadStatus(word)) await push($, 'status', pushText('status', rec.lane, endMs, 'STATUS: ' + word + ' (bilgi; orkestratör bakacak)'), [2, 3, 4, 5, 6, 7].includes(new Date(endMs).getHours()))
        } catch (err: any) {
          $.ui.toast('jarvis-flow: defter yazılamadı: ' + String(err && err.message ? err.message : err))
        }
      }
    }
    return next(e)
  })

  on('command.run', { command: 'effort' }, async ($: any, e: any, next: any) => {
    const result = await next(e)
    if (active) {
      const level = String(e.args ?? '').trim().toLowerCase()
      // an explicit level is believed at once; anything else is unknown until the next request shows it
      lastEffort = ['low', 'medium', 'high', 'xhigh', 'max'].includes(level) ? level : null
    }
    return result
  })

  on('command.run', { command: 'model' }, async ($: any, e: any, next: any) => {
    const result = await next(e)
    if (active) {
      lastEffort = null
      try {
        modelId = await $.session.model()
        await adoptFamily($)
      } catch {
        // keep what was known
      }
    }
    return result
  })

  // The user's own model change (/model, the picker, the SDK) moves the chat's family;
  // source 'auto' is the automatic fallback, which the start pin keeps out.
  on('classic.PostModelSwitch', async ($: any, e: any, next: any) => {
    if (active && ['command', 'picker', 'sdk'].includes(String(e.source))) {
      try {
        lastEffort = null
        modelId = typeof e.to_model === 'string' && e.to_model !== '' ? e.to_model : await $.session.model()
        await adoptFamily($)
      } catch {
        // keep what was known
      }
    }
    return next(e)
  })

  on('command.run', { command: 'compact' }, async ($: any, e: any, next: any) => {
    if (active && isBareCompact(e.command, e.args)) return { text: EMPTY_COMPACT_MESSAGE }
    return next(e)
  })

  on('command.run', { command: 'jcompact' }, async ($: any, e: any, next: any) => {
    if (!active) return next(e)
    try {
      await refreshGit($)
      const text = '/compact ' + (await fillTemplate($))
      const filled = await $.prompt.fill({ text, mode: 'replace' })
      return { text: filled.isFilled ? '/compact talimatı istem kutusuna yazıldı. Enter ile gönder.' : 'İstem kutusu doldurulamadı; talimat:\n' + text }
    } catch (err: any) {
      return { text: '/jcompact hatası: ' + String(err && err.message ? err.message : err) }
    }
  })

  on('command.run', { command: 'basla' }, async ($: any, e: any, next: any) => {
    if (!active) return next(e)
    try {
      await scanQueue($)
      const g = await currentGate($)
      const item = manual[0]
      if (!item) return { text: 'Kuyrukta bekleyen istem yok.' }
      if (!g.allow) return { text: 'Başlatılmadı: ' + g.reason }
      if (!idle(await $.clock.now())) return { text: 'Başlatılmadı: oturum meşgul.' }
      const preview = 'İlk 8 satır (sha256 ' + shortHash(item.sha) + ', ' + item.name + '):\n' + firstLines(item.text, 8)
      const why = await startItem($, item, 'manual')
      if (why !== null) return { text: 'Başlatılmadı: ' + why }
      return { text: preview + '\n\nGönderiliyor: ' + item.header.lane }
    } catch (err: any) {
      return { text: '/basla hatası: ' + String(err && err.message ? err.message : err) }
    }
  })

  on('command.run', { command: 'kuyruk' }, async ($: any, e: any, next: any) => {
    if (!active) return next(e)
    try {
      await scanQueue($)
      const lines: string[] = []
      for (const item of manual) lines.push('bekliyor (/basla): ' + item.header.lane + ' · ' + item.name + ' · ' + shortHash(item.sha))
      for (const item of auto) lines.push('otomatik: ' + item.header.lane + ' · ' + item.name + ' · ' + shortHash(item.sha))
      for (const r of rejected) lines.push('reddedildi: ' + r.name + ' (' + r.reason + ')')
      return { text: lines.length > 0 ? lines.join('\n') : 'Kuyruk boş.' }
    } catch (err: any) {
      return { text: '/kuyruk hatası: ' + String(err && err.message ? err.message : err) }
    }
  })

  on('command.run', { command: 'atla' }, async ($: any, e: any, next: any) => {
    if (!active) return next(e)
    try {
      const wanted = String(e.args ?? '').trim()
      if (!wanted) return { text: 'Kullanım: /atla <dosya.md>' }
      await scanQueue($)
      let item: Item | undefined = [...manual, ...auto].find((x) => x.name === wanted)
      let reject: Rejection | undefined = rejected.find((x) => x.name === wanted)
      const kind = item ? item.kind : reject ? reject.kind : null
      if (kind === null) return { text: 'Kuyrukta böyle bir dosya yok: ' + wanted }
      const folder = dirOf(kind)
      const moved = await runMove($, folder + '/' + wanted, folder + '/done/' + stamp(await $.clock.now()) + '-' + wanted)
      await tick($)
      return { text: moved ? 'Atlandı (gönderilmedi): ' + wanted : 'Taşınamadı: ' + wanted }
    } catch (err: any) {
      return { text: '/atla hatası: ' + String(err && err.message ? err.message : err) }
    }
  })

  on('command.run', { command: 'mola' }, async ($: any, e: any, next: any) => {
    if (!active) return next(e)
    try {
      await $.fs.write(root + '/build/queue/PAUSE', new Date(await $.clock.now()).toISOString() + '\n')
      paused = true
      await writeStatus($)
      $.ui.invalidate('ui.render')
      return { text: 'MOLA: yeni istem başlamaz. Devam için /devam' }
    } catch (err: any) {
      return { text: '/mola hatası: ' + String(err && err.message ? err.message : err) }
    }
  })

  on('command.run', { command: 'devam' }, async ($: any, e: any, next: any) => {
    if (!active) return next(e)
    try {
      if (await $.fs.exists(root + '/build/queue/PAUSE')) {
        const ok = await runRemove($, root + '/build/queue/PAUSE')
        if (!ok) return { text: 'MOLA bayrağı silinemedi: build/queue/PAUSE' }
      }
      paused = false
      await writeStatus($)
      $.ui.invalidate('ui.render')
      return { text: 'Devam: kuyruk yeniden başlatabilir.' }
    } catch (err: any) {
      return { text: '/devam hatası: ' + String(err && err.message ? err.message : err) }
    }
  })

  on('command.run', { command: 'flaky' }, async ($: any, e: any, next: any) => {
    if (!active) return next(e)
    try {
      const path = root + FLAKE_PATH
      const rows = (await $.fs.exists(path)) ? parseRows<FlakeRow>(await $.fs.read(path), isFlakeRow) : []
      return { text: flakyText(rows) }
    } catch (err: any) {
      return { text: '/flaky hatası: ' + String(err && err.message ? err.message : err) }
    }
  })

  on('command.run', { command: 'defter' }, async ($: any, e: any, next: any) => {
    if (!active) return next(e)
    try {
      const path = root + SLICE_PATH
      const rows = (await $.fs.exists(path)) ? parseRows<SliceRow>(await $.fs.read(path), isSliceRow) : []
      return { text: defterText(rows, dayOf(await $.clock.now())) }
    } catch (err: any) {
      return { text: '/defter hatası: ' + String(err && err.message ? err.message : err) }
    }
  })

  on('ui.render', { component: 'AbovePrompt' },async ($: any, e: any, next: any) => {
    if (!active) return next(e)
    try {
      const { Box, Text } = $.ui.resolve(e)
      const columns = (e.props && e.props.bodyColumns) || (e.viewport && e.viewport.columns) || 120
      const theirs = await next(e)
      return renderBand({ Box, Text }, bandState(), columns, theirs)
    } catch {
      return next(e)
    }
  })
}
