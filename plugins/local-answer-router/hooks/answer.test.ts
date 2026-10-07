import { describe, expect, test } from 'claude-code/testing'

import {
  answerBranch,
  answerDiffSummary,
  answerLastCommit,
  answerLastTest,
  answerStatus,
  escapeText,
  formatAge,
  LABEL,
  type GitResult,
} from './answer'

const ok = (stdout: string): GitResult => ({ exitCode: 0, stdout, stderr: '' })
const failed = (stderr: string, exitCode = 128): GitResult => ({ exitCode, stdout: '', stderr })
const NOT_A_REPO = 'fatal: not a git repository (or any of the parent directories): .git'
const NOT_A_REPO_ANSWER = `${LABEL} This folder is not inside a git repository.`

describe('escapeText', () => {
  test('control characters become visible escapes and long text is cut', () => {
    expect(escapeText('a\x1b[2Jb')).toBe('a\\x1b[2Jb')
    expect(escapeText('line1\nline2\ttab\r')).toBe('line1\\nline2\\ttab\\r')
    expect(escapeText('x'.repeat(300), 20)).toBe('x'.repeat(19) + '…')
    expect(escapeText('plain text')).toBe('plain text')
  })
})

describe('formatAge', () => {
  test('seconds, minutes, hours and days', () => {
    expect(formatAge(-5)).toBe('just now')
    expect(formatAge(3_000)).toBe('just now')
    expect(formatAge(30_000)).toBe('30 seconds ago')
    expect(formatAge(60_000)).toBe('1 minute ago')
    expect(formatAge(4 * 60_000 + 5_000)).toBe('4 minutes ago')
    expect(formatAge(3_600_000)).toBe('1 hour ago')
    expect(formatAge(5 * 3_600_000)).toBe('5 hours ago')
    expect(formatAge(86_400_000)).toBe('1 day ago')
    expect(formatAge(3 * 86_400_000)).toBe('3 days ago')
  })
})

describe('answerBranch', () => {
  test('a branch name', () => {
    expect(answerBranch(ok('main\n'), null)).toBe(`${LABEL} On branch main`)
  })

  test('detached head uses the short hash', () => {
    expect(answerBranch(ok('\n'), ok('abc1234\n'))).toBe(`${LABEL} Detached HEAD at abc1234`)
  })

  test('detached head with an unreadable hash is not answered', () => {
    expect(answerBranch(ok(''), failed('fatal: ambiguous argument'))).toBe(null)
    expect(answerBranch(ok(''), null)).toBe(null)
  })

  test('not a repository is answered; other failures are not', () => {
    expect(answerBranch(failed(NOT_A_REPO), null)).toBe(NOT_A_REPO_ANSWER)
    expect(answerBranch(failed('error: unknown option `show-current`', 129), null)).toBe(null)
    expect(answerBranch(failed('', 1), null)).toBe(null)
  })

  test('a hostile branch name is escaped', () => {
    expect(answerBranch(ok('evil\x1b[2J\n'), null)).toBe(`${LABEL} On branch evil\\x1b[2J`)
  })
})

describe('answerStatus', () => {
  test('a clean tree', () => {
    expect(answerStatus(ok('## main...origin/main\n'))).toBe(`${LABEL} main: clean`)
  })

  test('counts and files, ahead and behind', () => {
    const out = [
      '## main...origin/main [ahead 2, behind 1]',
      'M  staged.py',
      ' M modified.py',
      'MM both.py',
      '?? new file.txt',
      'UU conflict.py',
      'R  old.py -> new.py',
      '',
    ].join('\n')
    const answer = answerStatus(ok(out)) as string
    const lines = answer.split('\n')
    expect(lines[0]).toBe(`${LABEL} main (ahead 2, behind 1): 3 staged, 2 modified, 1 untracked, 1 conflicted`)
    expect(lines).toContain('  M  staged.py')
    expect(lines).toContain('   M modified.py')
    expect(lines).toContain('  ?? new file.txt')
    expect(lines).toContain('  R  old.py -> new.py')
  })

  test('no upstream, no commits yet and a detached head', () => {
    expect(answerStatus(ok('## main\n?? a.txt\n'))).toBe(`${LABEL} main: 1 untracked\n  ?? a.txt`)
    expect(answerStatus(ok('## No commits yet on main\n'))).toBe(`${LABEL} main (no commits yet): clean`)
    expect(answerStatus(ok('## HEAD (no branch)\n M a.py\n'))).toBe(`${LABEL} detached HEAD: 1 modified\n   M a.py`)
  })

  test('only the first 10 files are listed', () => {
    const files = Array.from({ length: 13 }, (_, i) => ` M f${i}.py`).join('\n')
    const lines = (answerStatus(ok(`## main\n${files}\n`)) as string).split('\n')
    expect(lines.length).toBe(1 + 10 + 1)
    expect(lines[lines.length - 1]).toBe('  +3 more')
  })

  test('paths and branch names with control characters are escaped', () => {
    const answer = answerStatus(ok('## ev\x1bil\n M a\x1b[31mb.py\n')) as string
    expect(answer).not.toContain('\x1b')
    expect(answer).toContain('a\\x1b[31mb.py')
  })

  test('not a repository is answered; other failures and empty output are not', () => {
    expect(answerStatus(failed(NOT_A_REPO))).toBe(NOT_A_REPO_ANSWER)
    expect(answerStatus(failed('fatal: something else'))).toBe(null)
    expect(answerStatus(ok(''))).toBe(null)
    expect(answerStatus(ok('garbage without a header\n'))).toBe(null)
  })
})

describe('answerLastCommit', () => {
  test('hash, subject, age and author', () => {
    expect(answerLastCommit(ok('abc1234\tFix the thing\t3 hours ago\tNaren\n'))).toBe(
      `${LABEL} abc1234 Fix the thing (3 hours ago, Naren)`,
    )
  })

  test('a tab inside the subject is kept', () => {
    expect(answerLastCommit(ok('abc1234\tfix\ta thing\t3 hours ago\tNaren\n'))).toBe(
      `${LABEL} abc1234 fix a thing (3 hours ago, Naren)`,
    )
  })

  test('a repository with no commits, not a repository, and failures', () => {
    expect(answerLastCommit(failed("fatal: your current branch 'main' does not have any commits yet"))).toBe(
      `${LABEL} This repository has no commits yet.`,
    )
    expect(answerLastCommit(failed(NOT_A_REPO))).toBe(NOT_A_REPO_ANSWER)
    expect(answerLastCommit(failed('fatal: other'))).toBe(null)
    expect(answerLastCommit(ok('only-one-field'))).toBe(null)
    expect(answerLastCommit(ok(''))).toBe(null)
  })

  test('hostile subjects are escaped', () => {
    expect(answerLastCommit(ok('abc\tx\x1b[2Jy\t1 day ago\tN\x07\n'))).toBe(`${LABEL} abc x\\x1b[2Jy (1 day ago, N\\x07)`)
  })
})

describe('answerDiffSummary', () => {
  test('uses the last line of each stat, and none when empty', () => {
    const unstaged = ok(' a.py | 2 +-\n b.py | 1 +\n 2 files changed, 2 insertions(+), 1 deletion(-)\n')
    expect(answerDiffSummary(unstaged, ok(''))).toBe(
      `${LABEL} unstaged: 2 files changed, 2 insertions(+), 1 deletion(-); staged: none`,
    )
    expect(answerDiffSummary(ok(''), ok(' 1 file changed, 4 insertions(+)\n'))).toBe(
      `${LABEL} unstaged: none; staged: 1 file changed, 4 insertions(+)`,
    )
  })

  test('not a repository is answered; any other failure is not', () => {
    expect(answerDiffSummary(failed(NOT_A_REPO), ok(''))).toBe(NOT_A_REPO_ANSWER)
    expect(answerDiffSummary(ok(''), failed(NOT_A_REPO))).toBe(NOT_A_REPO_ANSWER)
    expect(answerDiffSummary(failed('fatal: x'), ok(''))).toBe(null)
    expect(answerDiffSummary(ok(''), failed('fatal: x', 1))).toBe(null)
  })
})

describe('answerLastTest', () => {
  const NOW = 1_700_000_000_000

  test('says what ran, how it ended, how long ago, and that it was not re-run', () => {
    const text = answerLastTest({ command: 'pytest -q', failed: true, at: NOW - 4 * 60_000 }, NOW) as string
    expect(text.startsWith(`${LABEL} \`pytest -q\` failed, 4 minutes ago.`)).toBe(true)
    expect(text).toContain('The model ran it; I did not re-run it')
    expect(text).toContain('files may have changed since')
  })

  test('a passing run', () => {
    expect(answerLastTest({ command: 'npm test', failed: false, at: NOW - 10_000 }, NOW)).toContain('`npm test` passed, 10 seconds ago.')
  })

  test('no record is not answered', () => {
    expect(answerLastTest(null, NOW)).toBe(null)
  })

  test('a hostile command is escaped', () => {
    const text = answerLastTest({ command: 'pytest \x1b[2J', failed: false, at: NOW }, NOW) as string
    expect(text).not.toContain('\x1b')
  })
})
