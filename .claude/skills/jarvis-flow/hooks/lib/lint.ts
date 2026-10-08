import { familyOf, shaMatches, type Family, type Parsed } from './header.ts'

export type Verdict = { kind: 'pass' } | { kind: 'block'; message: string }

export type LintContext = {
  // this session's model id (null when unknown)
  modelId: string | null
  // the effort last seen on a request of this session (null before the first turn)
  effort: string | null
  // canonical main's HEAD (null when git could not say)
  mainHead: string | null
}

const PASS: Verdict = { kind: 'pass' }

// Pure verdict for one prompt. Anything it cannot establish passes: the lint
// is a convenience, never a gate on a prompt that has no header.
export function lintPrompt(parsed: Parsed, ctx: LintContext): Verdict {
  if (parsed.kind === 'absent') return PASS
  if (parsed.kind === 'invalid') return { kind: 'block', message: 'İstem başlığı geçersiz: ' + parsed.reason }

  const { header } = parsed
  const sessionFamily: Family | null = ctx.modelId ? familyOf(ctx.modelId) : null
  if (sessionFamily !== null && sessionFamily !== header.model) {
    return {
      kind: 'block',
      message: 'Bu istem ' + header.model.toUpperCase() + ' için, bu sohbet ' + sessionFamily.toUpperCase() + '. Doğru sohbete yapıştır.',
    }
  }
  if (ctx.effort !== null && ctx.effort.toLowerCase() !== header.effort) {
    return { kind: 'block', message: 'Önce /effort ' + header.effort + ' yaz' }
  }
  if (header.base !== 'main' && ctx.mainHead !== null && !shaMatches(header.base, ctx.mainHead)) {
    return {
      kind: 'block',
      message: 'İstem tabanı ' + header.base + ', main ' + ctx.mainHead + '. Orkestratörden güncel istemi iste.',
    }
  }
  return PASS
}

export const EMPTY_COMPACT_MESSAGE = 'Boş /compact yok. /jcompact kullan'

export function isBareCompact(command: string, args: string): boolean {
  return command === 'compact' && String(args ?? '').trim() === ''
}
