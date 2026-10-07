import { describe, expect, test } from 'claude-code/testing'

import { isTestCommand, recordFrom } from './testrun'

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

describe('recordFrom', () => {
  const NOW = 1_700_000_000_000

  test('a passing run is recorded as not failed', () => {
    expect(recordFrom('pytest -q', { isError: false }, NOW)).toEqual({ command: 'pytest -q', failed: false, at: NOW })
    expect(recordFrom('pytest -q', {}, NOW)).toEqual({ command: 'pytest -q', failed: false, at: NOW })
  })

  test('a failing run is recorded as failed', () => {
    expect(recordFrom('npm test', { isError: true, text: '3 failing' }, NOW)).toEqual({
      command: 'npm test',
      failed: true,
      at: NOW,
    })
  })

  test('a denied call records nothing', () => {
    expect(recordFrom('pytest', { deny: 'not allowed' }, NOW)).toBe(null)
  })

  test('an interrupted or timed-out failure records nothing', () => {
    for (const text of ['Command interrupted by user', 'Cancelled', 'operation aborted', 'Command timed out after 120s', 'Timeout']) {
      expect(recordFrom('pytest', { isError: true, text }, NOW)).toBe(null)
    }
  })

  test('a passing run whose output mentions a timeout is still recorded', () => {
    expect(recordFrom('pytest', { isError: false, text: 'test_timeout PASSED' }, NOW)?.failed).toBe(false)
  })

  test('a command that is not a test run records nothing', () => {
    expect(recordFrom('echo pytest', { isError: false }, NOW)).toBe(null)
  })

  test('the stored command is trimmed and capped', () => {
    const long = 'pytest ' + 'x'.repeat(500)
    const record = recordFrom('  ' + long + '  ', { isError: false }, NOW)
    expect(record?.command.length).toBe(200)
    expect(record?.command.startsWith('pytest xxx')).toBe(true)
  })
})
