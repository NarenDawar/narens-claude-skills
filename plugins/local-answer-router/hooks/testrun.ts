import type { LastTest } from '../types'

const RUNNERS: readonly RegExp[] = [
  /^(?:pytest|py\.test)(?: |$)/,
  /^python3?(?:\.\d+)? -m (?:pytest|unittest)(?: |$)/,
  /^(?:npm|pnpm|yarn) (?:run )?test(?: |$)/,
  /^npm t(?: |$)/,
  /^cargo(?: \+\S+)? test(?: |$)/,
  /^go test(?: |$)/,
  /^dotnet test(?: |$)/,
  /^(?:mvn|\.\/mvnw) test(?: |$)/,
  /^(?:gradle|\.\/gradlew) test(?: |$)/,
  /^(?:bundle exec )?rspec(?: |$)/,
  /^(?:npx )?jest(?: |$)/,
  /^(?:npx )?vitest(?: |$)/,
  /^make test(?: |$)/,
]

const ASSIGNMENTS = /^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+/
const CD = /^cd(?: |$)/
const GAVE_UP = /interrupt|cancel|abort|timed out|timeout/i
const MAX_COMMAND = 200

/**
 * True when the command's exit status is the test run's: runner last, only `cd` before it, and no
 * `;`, `|`, `&` or newline that would let another command decide the status. A wrong "passed"
 * answer is worse than none, so anything doubtful is not a test command.
 */
export const isTestCommand = (command: string): boolean => {
  const cleaned = command.replace(/\d*>&\d+/g, ' ').replace(/&&/g, '\u0000')
  if (/[;|&\r\n]/.test(cleaned)) {
    return false
  }
  const segments = cleaned.split('\u0000').map(segment => segment.trim())
  const last = segments[segments.length - 1].replace(ASSIGNMENTS, '')
  if (!RUNNERS.some(runner => runner.test(last))) {
    return false
  }
  return segments.slice(0, -1).every(segment => CD.test(segment))
}

/** The record to keep for a finished Bash call, or null when it should not be kept. */
export const recordFrom = (
  command: string,
  ran: { deny?: string; isError?: boolean; text?: string },
  now: number,
): LastTest | null => {
  if (ran.deny !== undefined || !isTestCommand(command)) {
    return null
  }
  const failed = ran.isError === true
  if (failed && GAVE_UP.test(ran.text ?? '')) {
    return null
  }
  return { command: command.trim().slice(0, MAX_COMMAND), failed, at: now }
}
