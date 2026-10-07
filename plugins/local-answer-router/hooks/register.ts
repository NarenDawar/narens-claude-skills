import { atom, read, update } from 'claude-code'
import type { PromptSubmitInput, PromptSubmitResult, Register } from 'claude-code'

import type { LastTest } from '../types'
import {
  answerBranch,
  answerDiffSummary,
  answerLastCommit,
  answerLastTest,
  answerStatus,
  type GitResult,
} from './answer'
import { matchIntent, stripAskPrefix, type Intent } from './match'
import { recordFrom } from './testrun'

const GIT_TIMEOUT_MS = 5000

export const lastTestAtom = atom({ plugin: 'local-answer-router', key: 'lastTest' } as const, null as LastTest | null)

/** What the handler needs from the host, so it can be tested without a session. */
export type Deps = {
  run: (argv: readonly string[]) => Promise<GitResult>
  lastTest: () => Promise<LastTest | null>
  now: () => number
}

type Next = (e: PromptSubmitInput) => Promise<PromptSubmitResult>

const answerFor = async (intent: Intent, deps: Deps): Promise<string | null> => {
  switch (intent) {
    case 'branch': {
      const branch = await deps.run(['git', 'branch', '--show-current'])
      const head =
        branch.exitCode === 0 && branch.stdout.trim() === ''
          ? await deps.run(['git', 'rev-parse', '--short', 'HEAD'])
          : null
      return answerBranch(branch, head)
    }
    case 'status':
      return answerStatus(await deps.run(['git', 'status', '--porcelain=v1', '-b']))
    case 'lastCommit':
      return answerLastCommit(await deps.run(['git', 'log', '-1', '--format=%h%x09%s%x09%cr%x09%an']))
    case 'diffSummary':
      return answerDiffSummary(
        await deps.run(['git', 'diff', '--stat']),
        await deps.run(['git', 'diff', '--cached', '--stat']),
      )
    case 'lastTest':
      return answerLastTest(await deps.lastTest(), deps.now())
  }
}

/** Only the user's own typed prompt, without attachments, is ever answered or rewritten. */
const isPlainUserPrompt = (e: PromptSubmitInput): boolean =>
  e.origin.kind === 'composer' && (e.attachments === undefined || e.attachments.length === 0)

export const makeHandler =
  (deps: Deps) =>
  async (e: PromptSubmitInput, next: Next): Promise<PromptSubmitResult> => {
    if (!isPlainUserPrompt(e)) {
      return next(e)
    }
    const stripped = stripAskPrefix(e.text)
    if (stripped !== null) {
      return next({ ...e, text: stripped })
    }
    const intent = matchIntent(e.text)
    if (intent === null) {
      return next(e)
    }
    let answer: string | null = null
    try {
      answer = await answerFor(intent, deps)
    } catch {
      answer = null // fail open: any trouble means the model gets the prompt
    }
    return answer === null ? next(e) : { drop: answer }
  }

export const register: Register = on => {
  on('prompt.submit', ($, e, next) =>
    makeHandler({
      run: argv => $.process.run(argv, { timeoutMs: GIT_TIMEOUT_MS }),
      lastTest: async () => (await read($, lastTestAtom)) ?? null,
      now: () => Date.now(),
    })(e, next),
  )

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const ran = await next(e)

    try {
      const record = recordFrom(e.command, ran, Date.now())
      if (record !== null) {
        await update($, lastTestAtom, () => record)
      }
    } catch {
      // The recorder only watches; a failure here must never reach the tool call.
    }

    return ran
  })
}
