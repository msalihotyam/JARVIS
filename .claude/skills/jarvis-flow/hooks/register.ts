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
import { renderBand, type BandState } from './lib/band.ts'

const TICK_MS = 5000
const POWER_MS = 120000
const START_GUARD_MS = 60000

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
  await writeStatus($)
  $.ui.invalidate('ui.render')
}

function bandState(): BandState {
  return {
    family: famName,
    lane: lane ? lane.lane : null,
    baseState,
    head,
    worktree: worktreeOf(cwd),
    branch,
    contextPct,
    planPct,
    power,
    paused,
    lowBattery,
    pendingLane: manual.length > 0 ? (manual[0] as Item).header.lane : null,
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
      await refreshGit($)
      await refreshUsage($)
      paused = await $.fs.exists(root + '/build/queue/PAUSE')
      if (tickTimer !== null) tickTimer.cancel()
      if (powerTimer !== null) powerTimer.cancel()
      tickTimer = $.clock.every(TICK_MS, async () => {
        await tick($)
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
      for (const name of ['basla', 'kuyruk', 'atla', 'mola', 'devam', 'jcompact']) {
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
    if (active && waiting) {
      waiting = false
      try {
        await writeStatus($)
      } catch {
        // ignore
      }
    }
    return next(e)
  })

  on('classic.PermissionRequest', async ($: any, e: any, next: any) => {
    if (active) {
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
      } catch {
        // keep what was known
      }
    }
    return result
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

  on('ui.render', { component: 'AbovePrompt' }, async ($: any, e: any, next: any) => {
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
