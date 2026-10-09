import { describe, expect, test } from 'claude-code/testing'

import { isTestCommand, observe } from './testrun'

describe('isTestCommand', () => {
  test('known runners count', () => {
    const yes = [
      'pytest',
      'pytest -q tests/test_a.py',
      'py.test -x',
      'python -m pytest',
      'python3 -m pytest -k thing',
      'python -m unittest discover -s tests',
      'python3.11 -m unittest',
      'npm test',
      'npm run test',
      'npm t',
      'yarn test',
      'pnpm test',
      'pnpm run test -- --watch=false',
      'cargo test',
      'cargo +nightly test --all',
      'go test ./...',
      'dotnet test',
      'mvn test',
      './mvnw test',
      'gradle test',
      './gradlew test',
      'rspec',
      'bundle exec rspec spec/a_spec.rb',
      'jest',
      'npx jest --ci',
      'vitest run',
      'npx vitest',
      'make test',
    ]
    for (const command of yes) {
      expect(isTestCommand(command)).toBe(true)
    }
  })

  test('a leading cd, environment assignments and spaces are allowed', () => {
    expect(isTestCommand('cd app && pytest -q')).toBe(true)
    expect(isTestCommand('cd app && cd tests && pytest')).toBe(true)
    expect(isTestCommand('FOO=1 npm test')).toBe(true)
    expect(isTestCommand('CI=true NODE_ENV=test npm run test')).toBe(true)
    expect(isTestCommand('cd app && FOO=1 pytest')).toBe(true)
    expect(isTestCommand('  pytest  ')).toBe(true)
    expect(isTestCommand('pytest 2>&1')).toBe(true)
    expect(isTestCommand('pytest > out.txt 2>&1')).toBe(true)
  })

  test('commands that only mention a runner do not count', () => {
    const no = [
      'echo pytest',
      'grep pytest notes.txt',
      'cat package.json',
      'ls tests',
      'git log --grep "npm test"',
      'pip install pytest',
      'npm install',
      'npm run build',
      'cargo build',
      'go build ./...',
      'make build',
      'python script.py',
      'python -m http.server',
      '',
      '   ',
    ]
    for (const command of no) {
      expect(isTestCommand(command)).toBe(false)
    }
  })

  test('commands whose exit status is not the test run is not a test command', () => {
    const no = [
      'pytest | tee out.txt',
      'pytest; echo done',
      'pytest || true',
      'pytest & ',
      'pytest\necho done',
      'echo start && pytest',
      'cd missing && pytest && npm run lint',
      'pytest && echo ok',
      'source venv/bin/activate && pytest',
      'npm test | grep fail',
      'pytest -k "a;b"',
    ]
    for (const command of no) {
      expect(isTestCommand(command)).toBe(false)
    }
  })
})

describe('observe', () => {
  const NOW = 1_700_000_000_000
  const record = (command: string, failed: boolean) => ({ kind: 'record', record: { command, failed, at: NOW } })

  test('a passing run is recorded as not failed', () => {
    expect(observe('pytest -q', false, { isError: false }, NOW)).toEqual(record('pytest -q', false))
    expect(observe('pytest -q', false, {}, NOW)).toEqual(record('pytest -q', false))
  })

  test('a failing run is recorded as failed', () => {
    expect(observe('npm test', false, { isError: true, text: '3 failing' }, NOW)).toEqual(record('npm test', true))
  })

  test('a failing run is recorded as failed whatever its output says', () => {
    const outputs = [
      'Exceeded timeout of 5000 ms for a test.',
      'asyncio.CancelledError',
      'FAILED tests/test_a.py::test_handles_interrupt',
      'operation aborted by the server',
    ]
    for (const text of outputs) {
      expect(observe('npx jest', false, { isError: true, text }, NOW)).toEqual(record('npx jest', true))
    }
  })

  test('a denied call changes nothing', () => {
    expect(observe('pytest', false, { deny: 'not allowed' }, NOW)).toEqual({ kind: 'ignore' })
  })

  test('a run that did not finish in the foreground clears the record', () => {
    const unfinished = [
      { result: { backgroundTaskId: 'b1' } },
      { result: { backgroundedByUser: true } },
      { result: { timedOutAfterMs: 120000 } },
      { result: { interrupted: true } },
    ]
    for (const ran of unfinished) {
      expect(observe('pytest', false, ran, NOW)).toEqual({ kind: 'clear' })
    }
    expect(observe('pytest', true, { isError: false }, NOW)).toEqual({ kind: 'clear' })
  })

  test('a result with the background fields unset or false still records', () => {
    const ran = { result: { backgroundTaskId: undefined, backgroundedByUser: false, interrupted: false } }
    expect(observe('pytest', false, ran, NOW)).toEqual(record('pytest', false))
  })

  test('a test run it cannot read cleanly clears the record', () => {
    const unclear = [
      'python -m pytest -q 2>&1 | tail -20',
      'pytest | tee out.txt',
      'uv run pytest',
      'npx jest | cat',
      'pytest; echo done',
      'cd missing && pytest && npm run lint',
      'echo pytest',
      'grep pytest notes.txt',
      'cargo test --no-run | head',
      'make test || true',
    ]
    for (const command of unclear) {
      expect(observe(command, false, { isError: false }, NOW)).toEqual({ kind: 'clear' })
    }
  })

  test('a command that is not about tests changes nothing', () => {
    for (const command of ['ls', 'git status', 'cat package.json', 'npm install', 'cargo build', 'make build', 'python script.py']) {
      expect(observe(command, false, { isError: false }, NOW)).toEqual({ kind: 'ignore' })
    }
  })

  test('the stored command is trimmed and capped', () => {
    const long = 'pytest ' + 'x'.repeat(500)
    const seen = observe('  ' + long + '  ', false, { isError: false }, NOW)
    expect(seen.kind).toBe('record')
    if (seen.kind === 'record') {
      expect(seen.record.command.length).toBe(200)
      expect(seen.record.command.startsWith('pytest xxx')).toBe(true)
    }
  })
})
