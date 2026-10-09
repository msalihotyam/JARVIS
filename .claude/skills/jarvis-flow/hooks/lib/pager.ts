// Wave 2 pager (ntfy). The topic is a secret: it comes only from the user environment
// variable JARVIS_NTFY_TOPIC, goes only into the curl argument list, and is never
// written to a file, a toast, a log or a report.

export const MIN_GAP_MS = 10 * 60 * 1000
export const WAIT_FIRST_MS = 5 * 60 * 1000
export const WAIT_REPEAT_MS = 30 * 60 * 1000
export const LOW_BATTERY_PCT = 20

// Only plain topic names: nothing that could change the URL's path, host or query.
export function validTopic(topic: unknown): topic is string {
  return typeof topic === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(topic)
}

// A fixed argument list: the topic and the text are not in it, so neither shows in a
// process list or a command log. Both go in on stdin as a curl config (-K -). `-q` first
// keeps curl from reading a .curlrc; the program is the system one, never a curl.exe that
// happens to sit in the working folder.
export function curlArgv(systemRoot: unknown): string[] {
  const root = typeof systemRoot === 'string' && /^[A-Za-z]:\\[A-Za-z0-9 _.\\-]{0,100}$/.test(systemRoot) ? systemRoot.replace(/\\+$/, '') : 'C:\\Windows'
  return [root + '\\System32\\curl.exe', '-q', '-sS', '--max-time', '10', '-K', '-']
}

function quoted(text: string): string {
  return '"' + text.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\r?\n/g, '\\n') + '"'
}

// quiet = information only (ntfy priority 2: no sound/vibration); default pages stay normal
export function curlConfig(topic: string, text: string, quiet = false): string {
  return [
    'url = ' + quoted('https://ntfy.sh/' + topic),
    'header = ' + quoted('Title: JARVIS'),
    ...(quiet ? ['header = ' + quoted('Priority: 2')] : []),
    'header = ' + quoted('Content-Type: text/plain; charset=utf-8'),
    'data-binary = ' + quoted(text),
    '',
  ].join('\n')
}

export type PushWhy = 'wait' | 'battery' | 'status'

export function hhmm(ms: number): string {
  const d = new Date(ms)
  const two = (n: number) => String(n).padStart(2, '0')
  return two(d.getHours()) + ':' + two(d.getMinutes())
}

// Short Turkish push texts: lane, what waits, since when.
export function pushText(why: PushWhy, lane: string, sinceMs: number, extra: string): string {
  if (why === 'wait') return 'JARVIS ' + lane + ': izin bekliyor, ' + hhmm(sinceMs) + "'den beri"
  if (why === 'battery') return 'JARVIS ' + lane + ': pil %' + extra + ', ' + hhmm(sinceMs) + "'den beri"
  return 'JARVIS ' + lane + ': ' + extra + ', ' + hhmm(sinceMs)
}

export function isBadStatus(word: string | null): boolean {
  return word === 'STOP' || word === 'BLOCKED' || word === 'FAIL'
}

// When the next permission-wait push is due, or null when not waiting.
export function nextWaitPush(waitingSince: number | null, lastWaitPush: number | null): number | null {
  if (waitingSince === null) return null
  return lastWaitPush === null ? waitingSince + WAIT_FIRST_MS : lastWaitPush + WAIT_REPEAT_MS
}
