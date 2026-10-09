import { describe, expect, test } from 'claude-code/testing'

import type { LastTest } from '../types'
import { LABEL } from './answer'
import { makeHandler, type Deps } from './register'

const COMPOSER = { kind: 'composer' } as const
const NOW = 1_700_000_000_000

const ok = (stdout: string) => ({ exitCode: 0, stdout, stderr: '' })
const failed = (stderr: string, exitCode = 128) => ({ exitCode, stdout: '', stderr })

type Script = Record<string, ReturnType<typeof ok> | Error>

// A scripted git: argv joined by spaces -> result. An unknown command fails the test loudly.
const deps = (script: Script, record: LastTest | null = null): { deps: Deps; ran: string[] } => {
  const ran: string[] = []
  return {
    ran,
    deps: {
      run: async argv => {
        const key = argv.join(' ')
        ran.push(key)
        const answer = script[key]
        if (answer === undefined) throw new Error(`unexpected command: ${key}`)
        if (answer instanceof Error) throw answer
        return answer
      },
      lastTest: async () => record,
      now: () => NOW,
    },
  }
}

const event = (text: string, over: Record<string, unknown> = {}) =>
  ({ text, wait: false, origin: COMPOSER, ...over }) as never

// `next` as the engine would answer: the prompt entered.
const passing = () => {
  const seen: unknown[] = []
  const next = async (e: { text: string }) => {
    seen.push(e)
    return { text: e.text }
  }
  return { next: next as never, seen }
}

describe('makeHandler: answering', () => {
  test('branch', async () => {
    const { deps: d } = deps({ 'git --no-optional-locks branch --show-current': ok('main\n') })
    const { next, seen } = passing()
    const result = await makeHandler(d)(event('what branch am i on'), next)
    expect(result).toEqual({ drop: `${LABEL} On branch main` })
    expect(seen).toEqual([])
  })

  test('branch on a detached head asks for the short hash', async () => {
    const { deps: d, ran } = deps({
      'git --no-optional-locks branch --show-current': ok('\n'),
      'git --no-optional-locks rev-parse --short HEAD': ok('abc1234\n'),
    })
    const result = await makeHandler(d)(event('which branch'), passing().next)
    expect(result).toEqual({ drop: `${LABEL} Detached HEAD at abc1234` })
    expect(ran).toEqual(['git --no-optional-locks branch --show-current', 'git --no-optional-locks rev-parse --short HEAD'])
  })

  test('status, last commit and diff summary use their fixed git commands', async () => {
    const { deps: d, ran } = deps({
      'git --no-optional-locks status --porcelain=v1 -b': ok('## main\n M a.py\n'),
      'git --no-optional-locks log -1 --format=%h%x09%s%x09%cr%x09%an': ok('abc\tsubject\t1 day ago\tN\n'),
      'git --no-optional-locks diff --stat': ok(' 1 file changed, 1 insertion(+)\n'),
      'git --no-optional-locks diff --cached --stat': ok(''),
    })
    const handler = makeHandler(d)
    expect(await handler(event('what changed'), passing().next)).toEqual({ drop: `${LABEL} main: 1 modified\n   M a.py` })
    expect(await handler(event('last commit'), passing().next)).toEqual({ drop: `${LABEL} abc subject (1 day ago, N)` })
    expect(await handler(event('diff stat'), passing().next)).toEqual({
      drop: `${LABEL} unstaged: 1 file changed, 1 insertion(+); staged: none`,
    })
    expect(ran).toEqual([
      'git --no-optional-locks status --porcelain=v1 -b',
      'git --no-optional-locks log -1 --format=%h%x09%s%x09%cr%x09%an',
      'git --no-optional-locks diff --stat',
      'git --no-optional-locks diff --cached --stat',
    ])
  })

  test('the last test answer comes from the record and runs no command', async () => {
    const { deps: d, ran } = deps({}, { command: 'pytest -q', failed: false, at: NOW - 120_000 })
    const result = await makeHandler(d)(event('did the tests pass?'), passing().next)
    expect(result).toEqual({ drop: expect.stringContaining('`pytest -q` passed, 2 minutes ago.') })
    expect(ran).toEqual([])
  })

  test('outside a repository the answer says so', async () => {
    const { deps: d } = deps({ 'git --no-optional-locks branch --show-current': failed('fatal: not a git repository') })
    expect(await makeHandler(d)(event('what branch'), passing().next)).toEqual({
      drop: `${LABEL} This folder is not inside a git repository.`,
    })
  })
})

describe('makeHandler: falling through', () => {
  test('no match calls next with the event unchanged', async () => {
    const { deps: d, ran } = deps({})
    const { next, seen } = passing()
    const e = event('please refactor the parser')
    expect(await makeHandler(d)(e, next)).toEqual({ text: 'please refactor the parser' })
    expect(seen).toEqual([e])
    expect(ran).toEqual([])
  })

  test('no recorded test falls through', async () => {
    const { deps: d } = deps({}, null)
    const { next, seen } = passing()
    const result = await makeHandler(d)(event('did the tests pass'), next)
    expect(result).toEqual({ text: 'did the tests pass' })
    expect(seen.length).toBe(1)
  })

  test('every intent falls through when git rejects, times out or exits unexpectedly', async () => {
    const asks = ['what branch', 'what changed', 'last commit', 'diff stat']
    const commands = [
      'git --no-optional-locks branch --show-current',
      'git --no-optional-locks status --porcelain=v1 -b',
      'git --no-optional-locks log -1 --format=%h%x09%s%x09%cr%x09%an',
      'git --no-optional-locks diff --stat',
      'git --no-optional-locks diff --cached --stat',
    ]
    for (const ask of asks) {
      for (const failure of [new Error('spawn git ENOENT'), failed('fatal: weird', 1)]) {
        const script: Script = {}
        for (const command of commands) script[command] = failure
        const { deps: d } = deps(script)
        const { next, seen } = passing()
        const result = await makeHandler(d)(event(ask), next)
        expect(result).toEqual({ text: ask })
        expect(seen.length).toBe(1)
      }
    }
  })

  test('a failing record read falls through', async () => {
    const d: Deps = {
      run: async () => ok(''),
      lastTest: async () => {
        throw new Error('state')
      },
      now: () => NOW,
    }
    const { next, seen } = passing()
    expect(await makeHandler(d)(event('did the tests pass'), next)).toEqual({ text: 'did the tests pass' })
    expect(seen.length).toBe(1)
  })
})

describe('makeHandler: whose prompt it is', () => {
  const origins = [
    { kind: 'plugin', name: 'x', asUser: true },
    { kind: 'peer' },
    { kind: 'scheduled-trigger' },
    { kind: 'task-notification' },
    { kind: 'sdk' },
    { kind: 'bridge' },
    { kind: 'unclassified' },
    { kind: 'auto-continuation' },
  ]

  test('only the composer is answered; every other origin passes through untouched', async () => {
    for (const origin of origins) {
      const { deps: d, ran } = deps({ 'git --no-optional-locks branch --show-current': ok('main\n') })
      const { next, seen } = passing()
      const e = event('what branch am i on', { origin })
      expect(await makeHandler(d)(e, next)).toEqual({ text: 'what branch am i on' })
      expect(seen).toEqual([e])
      expect(ran).toEqual([])
    }
  })

  test('attachments pass through untouched', async () => {
    const { deps: d, ran } = deps({ 'git --no-optional-locks branch --show-current': ok('main\n') })
    const { next, seen } = passing()
    const e = event('what branch am i on', { attachments: [{ kind: 'image' }] })
    expect(await makeHandler(d)(e, next)).toEqual({ text: 'what branch am i on' })
    expect(seen).toEqual([e])
    expect(ran).toEqual([])
  })

  test('an empty attachments list does not stop it', async () => {
    const { deps: d } = deps({ 'git --no-optional-locks branch --show-current': ok('main\n') })
    const result = await makeHandler(d)(event('what branch am i on', { attachments: [] }), passing().next)
    expect(result).toEqual({ drop: `${LABEL} On branch main` })
  })
})

describe('makeHandler: ask:', () => {
  test('goes to the model with the prefix removed and nothing else changed', async () => {
    const { deps: d, ran } = deps({})
    const { next, seen } = passing()
    const e = event('ask: what branch am i on', { context: ['ctx'], wait: true })
    const result = await makeHandler(d)(e, next)
    expect(result).toEqual({ text: 'what branch am i on' })
    expect(seen).toEqual([{ ...(e as object), text: 'what branch am i on' }])
    expect(ran).toEqual([])
  })

  test("is left alone on a prompt that is not the user's own", async () => {
    const { deps: d } = deps({})
    const { next, seen } = passing()
    const e = event('ask: do a thing', { origin: { kind: 'peer' } })
    expect(await makeHandler(d)(e, next)).toEqual({ text: 'ask: do a thing' })
    expect(seen).toEqual([e])
  })
})

// --- the registration, through the engine harness ---------------------------------------------

const BASH_OK = { result: {} as never, text: 'ok', isError: false, isReadOnly: false }

describe('local-answer-router registration', () => {
  test('a plugin-origin prompt that looks like a question passes through untouched', async ($, on) => {
    const seen: string[] = []
    on('prompt.submit', (_, e) => {
      seen.push(e.text)
      return { text: e.text }
    })
    const result = await $.prompt.submit({ text: 'what branch am i on' })
    expect(result).toEqual({ text: 'what branch am i on' })
    expect(seen).toEqual(['what branch am i on'])
  })

  test('a Bash call passes through unchanged whether or not it is a test run', async ($, on) => {
    on('tool.call', () => BASH_OK as never)
    for (const command of ['pytest -q', 'ls']) {
      const result = await $.tool.call({ tool: 'Bash', command })
      expect(result).toEqual(expect.objectContaining({ text: 'ok' }))
      expect((result as { isError?: boolean }).isError).not.toBe(true)
    }
  })

  test('a failing Bash call is relayed as it was', async ($, on) => {
    on('tool.call', () => ({ ...BASH_OK, isError: true, text: '1 failed' }) as never)
    const result = await $.tool.call({ tool: 'Bash', command: 'pytest' })
    expect(result).toEqual(expect.objectContaining({ isError: true, text: '1 failed' }))
  })

  // The harness `$` has no state reader, so the session state is stood in for beneath the plugin:
  // an in-memory value behind `state.get` and `state.set`.
  const withState = (on: (name: string, hook: (...args: never[]) => unknown) => unknown) => {
    const store = { value: null as unknown, version: 0, writes: 0 }
    // Op-style stubs answer `{ value: <the call's result> }`.
    on('state.get', (() => ({ value: { value: store.value ?? undefined, version: store.version } })) as never)
    on('state.set', ((_: unknown, e: { value: unknown }) => {
      store.value = e.value
      store.version += 1
      store.writes += 1
      return { value: { isSet: true, version: store.version } }
    }) as never)
    return store
  }

  test('a test run is recorded and other commands leave the record alone', async ($, on) => {
    const store = withState(on as never)
    let failedRun = false
    on('tool.call', () => ({ ...BASH_OK, isError: failedRun, text: failedRun ? '1 failed' : 'ok' }) as never)
    await $.tool.call({ tool: 'Bash', command: 'pytest -q' })
    expect(store.value).toEqual({ command: 'pytest -q', failed: false, at: expect.any(Number) })
    await $.tool.call({ tool: 'Bash', command: 'ls' })
    await $.tool.call({ tool: 'Bash', command: 'git status' })
    expect(store.writes).toBe(1) // unrelated commands leave the record alone
    failedRun = true
    await $.tool.call({ tool: 'Bash', command: 'cd app && npm test' })
    expect(store.value).toEqual({ command: 'cd app && npm test', failed: true, at: expect.any(Number) })
    expect(store.writes).toBe(2)
  })

  test('a failing run whose output mentions a timeout is still recorded as failed', async ($, on) => {
    const store = withState(on as never)
    on('tool.call', () => ({ ...BASH_OK, isError: true, text: 'Exceeded timeout of 5000 ms for a test.' }) as never)
    await $.tool.call({ tool: 'Bash', command: 'npx jest' })
    expect(store.value).toEqual({ command: 'npx jest', failed: true, at: expect.any(Number) })
  })

  test('a run that was backgrounded or interrupted clears the record instead of keeping an older pass', async ($, on) => {
    const store = withState(on as never)
    let result: Record<string, unknown> = {}
    on('tool.call', () => ({ ...BASH_OK, result }) as never)
    for (const unfinished of [{ backgroundTaskId: 'b1' }, { timedOutAfterMs: 120000 }, { interrupted: true }]) {
      result = {}
      await $.tool.call({ tool: 'Bash', command: 'pytest -q' })
      expect(store.value).toEqual({ command: 'pytest -q', failed: false, at: expect.any(Number) })
      result = unfinished
      await $.tool.call({ tool: 'Bash', command: 'pytest -q' })
      expect(store.value).toBe(null)
    }
  })

  test('a call started in the background clears the record', async ($, on) => {
    const store = withState(on as never)
    on('tool.call', () => BASH_OK as never)
    await $.tool.call({ tool: 'Bash', command: 'pytest -q' })
    expect(store.value).not.toBe(null)
    await $.tool.call({ tool: 'Bash', command: 'pytest -q', run_in_background: true } as never)
    expect(store.value).toBe(null)
  })

  test('a test run it cannot read cleanly clears the record instead of keeping an older pass', async ($, on) => {
    const store = withState(on as never)
    on('tool.call', () => BASH_OK as never)
    for (const command of ['python -m pytest -q 2>&1 | tail -20', 'uv run pytest', 'npx jest | cat']) {
      await $.tool.call({ tool: 'Bash', command: 'pytest -q' })
      expect(store.value).not.toBe(null)
      await $.tool.call({ tool: 'Bash', command })
      expect(store.value).toBe(null)
    }
  })

  test('a failing state write never reaches the Bash call', async ($, on) => {
    on('state.get', (() => ({ value: { value: undefined, version: 0 } })) as never)
    on('state.set', (() => {
      throw new Error('state unavailable')
    }) as never)
    on('tool.call', () => BASH_OK as never)
    const result = await $.tool.call({ tool: 'Bash', command: 'pytest -q' })
    expect(result).toEqual(expect.objectContaining({ text: 'ok' }))
  })
})
