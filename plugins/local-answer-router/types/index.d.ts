export type LastTest = { command: string; failed: boolean; at: number }

declare module 'claude-code' {
  interface PluginState {
    'local-answer-router': { lastTest: LastTest | null }
  }
}
