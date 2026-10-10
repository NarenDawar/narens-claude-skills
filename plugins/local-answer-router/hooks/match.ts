export type Intent = 'branch' | 'status' | 'lastCommit' | 'diffSummary' | 'lastTest'

/**
 * The questions answered locally, as whole normalized messages. To answer another phrasing, add it
 * here and add it to match.test.ts; a phrase must not appear under two intents.
 */
export const PHRASES: Record<Intent, readonly string[]> = {
  branch: [
    'what branch am i on',
    'which branch am i on',
    'what branch is this',
    "what's the current branch",
    'current branch',
    'which branch',
    'what branch',
  ],
  status: [
    'what changed',
    "what's changed",
    'what did i change',
    'what have i changed',
    'what files changed',
    'which files changed',
    'git status',
    'any uncommitted changes',
  ],
  lastCommit: [
    'last commit',
    'what was the last commit',
    "what's the latest commit",
    'what was my last commit',
  ],
  diffSummary: ['diff stat', 'how big is the diff', 'how many lines changed'],
  lastTest: [
    'did the last test pass',
    'did the tests pass',
    'did the last test run pass',
    'do the tests pass',
    'are the tests passing',
    'what was the last test result',
  ],
}

const MAX_LENGTH = 60

export const normalize = (text: string): string =>
  text
    .replace(/[‘’ʼ]/g, "'")
    .toLowerCase()
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/[?!.]+$/, '')
    .trim()

const INTENT_OF = new Map<string, Intent>()
for (const [intent, phrases] of Object.entries(PHRASES)) {
  for (const phrase of phrases) {
    INTENT_OF.set(phrase, intent as Intent)
  }
}

/** The intent when the whole message is one of the known phrases; null for anything else. */
export const matchIntent = (text: string): Intent | null => {
  const trimmed = text.trim()
  if (trimmed.length === 0 || trimmed.length > MAX_LENGTH || /[\r\n]/.test(trimmed)) {
    return null
  }
  return INTENT_OF.get(normalize(trimmed)) ?? null
}

/** The message without a leading `ask:`, or null when it does not start with one (or is only that). */
export const stripAskPrefix = (text: string): string | null => {
  const match = /^\s*ask:\s*([\s\S]*)$/i.exec(text)
  if (match === null || match[1].trim().length === 0) {
    return null
  }
  return match[1]
}
