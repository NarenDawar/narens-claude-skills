import type { LastTest } from '../types'

export type GitResult = { exitCode: number; stdout: string; stderr: string }

export const LABEL = '[local, no tokens]'

const NOT_A_REPO = /not a git repository/i
const NO_COMMITS = /does not have any commits yet/i
const NOT_A_REPO_ANSWER = `${LABEL} This folder is not inside a git repository.`
const MAX_LISTED = 10

/** Control characters become visible escapes (repository text is untrusted); long text is cut. */
export const escapeText = (text: string, max = 200): string => {
  const escaped = text.replace(/[\u0000-\u001f\u007f-\u009f]/g, char => {
    if (char === '\n') return '\\n'
    if (char === '\r') return '\\r'
    if (char === '\t') return '\\t'
    return `\\x${char.charCodeAt(0).toString(16).padStart(2, '0')}`
  })
  return escaped.length <= max ? escaped : escaped.slice(0, max - 1) + '…'
}

const plural = (count: number, unit: string): string => `${count} ${unit}${count === 1 ? '' : 's'} ago`

export const formatAge = (ms: number): string => {
  const seconds = Math.floor(ms / 1000)
  if (seconds < 10) return 'just now'
  if (seconds < 60) return plural(seconds, 'second')
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return plural(minutes, 'minute')
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return plural(hours, 'hour')
  return plural(Math.floor(hours / 24), 'day')
}

/** The answer for a git result that failed because there is no repository, else null. */
const notARepo = (...results: GitResult[]): string | null =>
  results.some(result => result.exitCode !== 0 && NOT_A_REPO.test(result.stderr)) ? NOT_A_REPO_ANSWER : null

export const answerBranch = (branch: GitResult, head: GitResult | null): string | null => {
  if (branch.exitCode !== 0) {
    return notARepo(branch)
  }
  const name = branch.stdout.trim()
  if (name !== '') {
    return `${LABEL} On branch ${escapeText(name)}`
  }
  if (head !== null && head.exitCode === 0 && head.stdout.trim() !== '') {
    return `${LABEL} Detached HEAD at ${escapeText(head.stdout.trim())}`
  }
  return null
}

const parseHeader = (header: string): { branch: string; tracking: string } => {
  const tracking = /\[([^\]]*)\]\s*$/.exec(header)?.[1] ?? ''
  if (header.startsWith('No commits yet on ')) {
    return { branch: `${header.slice('No commits yet on '.length).split(' ')[0]} (no commits yet)`, tracking }
  }
  if (header.startsWith('HEAD (no branch)')) {
    return { branch: 'detached HEAD', tracking }
  }
  return { branch: header.split('...')[0].split(' ')[0], tracking }
}

export const answerStatus = (res: GitResult): string | null => {
  if (res.exitCode !== 0) {
    return notARepo(res)
  }
  const lines = res.stdout.split('\n').filter(line => line !== '')
  if (lines.length === 0 || !lines[0].startsWith('## ')) {
    return null
  }
  const { branch, tracking } = parseHeader(lines[0].slice(3))
  const entries = lines.slice(1)
  let staged = 0
  let modified = 0
  let untracked = 0
  let conflicted = 0
  for (const entry of entries) {
    const x = entry[0]
    const y = entry[1]
    if (x === '?' && y === '?') {
      untracked += 1
    } else if (x === 'U' || y === 'U' || (x === 'A' && y === 'A') || (x === 'D' && y === 'D')) {
      conflicted += 1
    } else {
      if (x !== ' ' && x !== undefined) staged += 1
      if (y !== ' ' && y !== undefined) modified += 1
    }
  }
  const counts = [
    staged > 0 ? `${staged} staged` : '',
    modified > 0 ? `${modified} modified` : '',
    untracked > 0 ? `${untracked} untracked` : '',
    conflicted > 0 ? `${conflicted} conflicted` : '',
  ].filter(part => part !== '')
  const where = tracking !== '' ? ` (${escapeText(tracking)})` : ''
  const summary = `${LABEL} ${escapeText(branch)}${where}: ${counts.length > 0 ? counts.join(', ') : 'clean'}`
  const shown = entries.slice(0, MAX_LISTED).map(entry => `  ${escapeText(entry)}`)
  const more = entries.length > MAX_LISTED ? [`  +${entries.length - MAX_LISTED} more`] : []
  return [summary, ...shown, ...more].join('\n')
}

export const answerLastCommit = (res: GitResult): string | null => {
  if (res.exitCode !== 0) {
    if (NO_COMMITS.test(res.stderr)) {
      return `${LABEL} This repository has no commits yet.`
    }
    return notARepo(res)
  }
  const parts = res.stdout.trim().split('\t')
  if (parts.length < 4) {
    return null
  }
  const hash = parts[0]
  const when = parts[parts.length - 2]
  const author = parts[parts.length - 1]
  const subject = parts.slice(1, -2).join(' ')
  return `${LABEL} ${escapeText(hash)} ${escapeText(subject)} (${escapeText(when)}, ${escapeText(author)})`
}

const lastLine = (text: string): string => {
  const lines = text.split('\n').filter(line => line.trim() !== '')
  return lines.length === 0 ? 'none' : escapeText(lines[lines.length - 1].trim())
}

export const answerDiffSummary = (unstaged: GitResult, staged: GitResult): string | null => {
  const missing = notARepo(unstaged, staged)
  if (missing !== null) {
    return missing
  }
  if (unstaged.exitCode !== 0 || staged.exitCode !== 0) {
    return null
  }
  return `${LABEL} unstaged: ${lastLine(unstaged.stdout)}; staged: ${lastLine(staged.stdout)}`
}

export const answerLastTest = (record: LastTest | null, now: number): string | null => {
  if (record === null) {
    return null
  }
  const outcome = record.failed ? 'failed' : 'passed'
  return (
    `${LABEL} \`${escapeText(record.command, 120)}\` ${outcome}, ${formatAge(now - record.at)}. ` +
    'The model ran it; I did not re-run it, and files may have changed since.'
  )
}
