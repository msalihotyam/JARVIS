export type Power = { ac: boolean; pct: number | null }

// Win32_Battery BatteryStatus values that mean "on mains power": 2 (AC), 3 (fully
// charged), 6-9 (charging), 11 (partially charged). 1, 4, 5 are discharging.
const MAINS = new Set([2, 3, 6, 7, 8, 9, 11])

// stdout of: Get-CimInstance Win32_Battery | Select-Object EstimatedChargeRemaining,BatteryStatus | ConvertTo-Json -Compress
// No battery (a desktop), empty or unreadable output: treated as mains power.
export function parseBattery(stdout: string): Power {
  const text = String(stdout ?? '').trim()
  if (!text) return { ac: true, pct: null }
  let value: unknown
  try {
    value = JSON.parse(text)
  } catch {
    return { ac: true, pct: null }
  }
  const row = (Array.isArray(value) ? value[0] : value) as { EstimatedChargeRemaining?: unknown; BatteryStatus?: unknown } | undefined
  if (!row || typeof row !== 'object') return { ac: true, pct: null }
  const status = Number(row.BatteryStatus)
  const pct = Number(row.EstimatedChargeRemaining)
  return {
    ac: Number.isFinite(status) ? MAINS.has(status) : true,
    pct: Number.isFinite(pct) ? Math.max(0, Math.min(100, Math.round(pct))) : null,
  }
}

export const BATTERY_ARGV = [
  'powershell',
  '-NoProfile',
  '-NonInteractive',
  '-Command',
  'Get-CimInstance Win32_Battery | Select-Object EstimatedChargeRemaining,BatteryStatus | ConvertTo-Json -Compress',
] as const

export type GateInput = {
  power: Power
  paused: boolean
  // another lane's status file says working (fresh enough to believe)
  otherLaneWorking: boolean
}

export type Gate = {
  allow: boolean
  // why not (Turkish, for the band, a toast or a command reply); '' when allowed
  reason: string
  // battery < 20%: the status file says low_battery
  lowBattery: boolean
  // battery < 40%: behave as paused
  batteryPaused: boolean
}

// What may START a new prompt. Running turns are never touched.
export function gate(input: GateInput): Gate {
  const { power, paused, otherLaneWorking } = input
  const onBattery = !power.ac
  const low = onBattery && power.pct !== null && power.pct < 20
  const batteryPaused = onBattery && power.pct !== null && power.pct < 40
  if (paused) return { allow: false, reason: 'MOLA açık (/devam yaz)', lowBattery: low, batteryPaused }
  if (batteryPaused) {
    return { allow: false, reason: 'pil %' + power.pct + ' (< %40), yeni istem başlamaz', lowBattery: low, batteryPaused }
  }
  if (onBattery && otherLaneWorking) {
    return { allow: false, reason: 'pilde tek istem: başka hat çalışıyor', lowBattery: low, batteryPaused }
  }
  return { allow: true, reason: '', lowBattery: low, batteryPaused }
}

// A status file older than this is not believed to still be "working".
export const STALE_MS = 3 * 60 * 60 * 1000

export function isFreshWorking(status: { state?: unknown; updated_at?: unknown } | null, nowMs: number): boolean {
  if (!status || status.state !== 'working') return false
  const at = typeof status.updated_at === 'string' ? Date.parse(status.updated_at) : NaN
  return Number.isFinite(at) && nowMs - at < STALE_MS
}
