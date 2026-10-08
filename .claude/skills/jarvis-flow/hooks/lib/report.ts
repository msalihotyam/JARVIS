// A final report is an assistant turn that has a line starting with "STATUS:".
export function extractReport(answer: string): string | null {
  const text = String(answer ?? '')
  return /^STATUS:/m.test(text) ? text : null
}

// The lane id becomes a file name, so it must be the safe kind (the header
// parser already guarantees this; the check is repeated where the path is built).
export function safeLane(lane: string | null): string | null {
  return lane !== null && /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(lane) ? lane : null
}
