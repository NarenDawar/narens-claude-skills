import { describe, expect, test } from 'claude-code/testing'

import { matchIntent, normalize, PHRASES, stripAskPrefix } from './match'

describe('normalize', () => {
  test('lower-cases, straightens apostrophes, collapses spaces and drops trailing punctuation', () => {
    expect(normalize("  What’s   CHANGED?!  ")).toBe("what's changed")
    expect(normalize('git status.')).toBe('git status')
  })
})

describe('matchIntent', () => {
  test('every phrase in the table matches its own intent', () => {
    for (const [intent, phrases] of Object.entries(PHRASES)) {
      for (const phrase of phrases) {
        expect(matchIntent(phrase)).toBe(intent)
      }
    }
  })

  test('capitalization, a question mark and extra spaces still match', () => {
    expect(matchIntent('What branch am I on?')).toBe('branch')
    expect(matchIntent('  DID THE TESTS PASS  ')).toBe('lastTest')
    expect(matchIntent("What's changed")).toBe('status')
    expect(matchIntent('What’s changed?')).toBe('status')
  })

  test('the table has no phrase under two intents', () => {
    const seen = new Map<string, string>()
    for (const [intent, phrases] of Object.entries(PHRASES)) {
      for (const phrase of phrases) {
        expect(seen.get(phrase)).toBeUndefined()
        seen.set(phrase, intent)
      }
    }
  })

  test('a message with anything extra does not match', () => {
    const near = [
      'what branch should i use',
      'why does the branch check fail',
      'what changed and fix it',
      'what changed in the last release',
      'can you tell me what branch i am on',
      'did it pass',
      'did the tests pass on ci',
      'last commit message please fix the typo',
      'branch',
      '',
      '   ',
      '?',
    ]
    for (const text of near) {
      expect(matchIntent(text)).toBe(null)
    }
  })

  test('a message over 60 characters never matches', () => {
    expect(matchIntent('what branch am i on' + ' '.repeat(45) + 'x')).toBe(null)
    expect(matchIntent('what branch am i on' + ' '.repeat(60))).toBe('branch') // trimmed first
  })

  test('a message with a newline never matches', () => {
    expect(matchIntent('what branch\nam i on')).toBe(null)
    expect(matchIntent('what branch am i on\nplease')).toBe(null)
    expect(matchIntent('what branch am i on\n')).toBe('branch') // a trailing newline is trimmed
  })
})

describe('stripAskPrefix', () => {
  test('removes ask: and the spaces after it', () => {
    expect(stripAskPrefix('ask: what branch am i on')).toBe('what branch am i on')
    expect(stripAskPrefix('ASK:what changed')).toBe('what changed')
    expect(stripAskPrefix('  ask:   hello there')).toBe('hello there')
  })

  test('anything else is null', () => {
    expect(stripAskPrefix('what branch am i on')).toBe(null)
    expect(stripAskPrefix('ask:')).toBe(null)
    expect(stripAskPrefix('ask:   ')).toBe(null)
    expect(stripAskPrefix('please ask: something')).toBe(null)
    expect(stripAskPrefix('asking: something')).toBe(null)
  })
})
