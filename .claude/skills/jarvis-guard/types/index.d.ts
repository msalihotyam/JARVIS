// The values jarvis-guard keeps for the session in $.state.
export type GuardHeader = { lane: string; base: string }
export type GuardStart = { start: string; main: string }

declare module 'claude-code' {
  interface PluginState {
    'jarvis-guard': {
      // the last @jarvis prompt header of the session
      header: GuardHeader | null
      // per lane worktree (normal-form path): its HEAD and main's HEAD at entry
      starts: Record<string, GuardStart>
    }
  }
}
