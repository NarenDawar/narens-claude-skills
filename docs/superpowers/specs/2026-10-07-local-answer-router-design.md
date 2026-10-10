# local-answer-router: a mod that answers trivial questions without the model

Date: 2026-10-07

## Purpose

Some questions Claude is asked every day have a deterministic answer on the user's own machine: what branch am I on, what changed, what was the last commit, did the last test pass. Each costs a model turn. This mod answers a short, fixed list of such questions locally, from `git` and from the last test run it watched, and spends no model tokens. Everything else reaches the model exactly as if the mod were not installed.

## Success criteria

1. A prompt that is exactly one of the listed questions gets a correct, instant answer, labeled `[local, no tokens]`, and does not enter the session (no model turn).
2. Every other prompt behaves exactly as without the mod: no match, extra words, attachments, a prompt that did not come from the user, any git or runtime failure. The mod fails open.
3. `ask: <text>` always goes to the model with the `ask: ` prefix removed.
4. "Did the last test pass" is answered only from a test run the mod itself observed in this session, shows the command, exit status and age, and says it was not re-run. With no observed run it does not answer.
5. The mod only reads. It never changes the repository, never runs a command built from the user's message, and never alters a tool call or its result.
6. The mod's tests pass with `claude plugin test`, `claude plugin validate` passes, and the repo's validator, catalog and tests stay green.

## Decisions already made

- **Scope (v1):** git facts (branch, status, last commit, diff summary) plus the last observed test result. Nothing else.
- **Matching:** strict whole-message patterns from a short phrase table; no keywords, no fuzzy or model-based matching.
- **Escape hatch:** the label `[local, no tokens]` on every local answer, and the `ask:` prefix to force the model.
- **Mechanism:** a `prompt.submit` hook answers without calling `next`, returning `{ drop: <answer> }`, whose text the API shows to the user as the reason (checked in the type file shipped with the plugin-authoring skill, build 2.1.288). A `tool.call` hook on Bash observes test runs. Git is run through `$.process.run` with an argument list (no shell).

## Constraints and assumptions

- A dropped prompt does not enter the session, so the model never sees the question or the answer. The README says so.
- The last-test answer reflects only commands the model ran through the Bash tool in this session, not tests the user ran in a terminal, and files may have changed since. The answer says both.
- Only the user's own, text-only prompts are considered: `origin` must be the user's own, and `attachments` must be absent or empty. A prompt typed while a turn is running is treated the same way.
- No part of the user's message is ever placed into a command. The git argument lists are constants.
- File names and commit subjects in answers come from the repository and are untrusted text: control characters are escaped and long values are truncated.
- To be verified against the type file while writing the plan (each is a task step, not a guess): how a `drop` reason is displayed; the exact value that marks the user's own origin; how to match `tool.call` on Bash; the field of a Bash result that carries the exit code and whether an interrupted call has one; how `$.state` is declared for a session-scoped value.
- Mod tests run locally with `claude plugin test`; the repository CI does not run them (as for `subagent-meter`).

## The phrase table (v1)

The prompt is normalized (lower case; curly apostrophes made straight; whitespace collapsed; trailing `?`, `!` and `.` removed) and the whole result must equal an entry. A message over 60 characters, or with a newline, never matches.

- **branch:** `what branch am i on`, `which branch am i on`, `what branch is this`, `what's the current branch`, `current branch`, `which branch`, `what branch`
- **status:** `what changed`, `what's changed`, `what did i change`, `what have i changed`, `what files changed`, `which files changed`, `git status`, `any uncommitted changes`
- **lastCommit:** `last commit`, `what was the last commit`, `what's the latest commit`, `what was my last commit`
- **diffSummary:** `diff stat`, `how big is the diff`, `how many lines changed`
- **lastTest:** `did the last test pass`, `did the tests pass`, `did the last test run pass`, `do the tests pass`, `are the tests passing`, `what was the last test result`

Ambiguous phrases that depend on conversation context (for example `did it pass`) are deliberately absent. The table is a plain exported constant in `match.ts` so adding a phrasing is a one-line change with a test.

## The answers

Every answer starts with `[local, no tokens] ` and is plain text.

- **branch:** `git branch --show-current`; empty output means detached: `Detached HEAD at <short hash>` (from `git rev-parse --short HEAD`). Outside a repository (git's "not a git repository" error): `This folder is not inside a git repository.`
- **status:** `git status --porcelain=v1 -b`. First line: the branch (and ahead/behind when present) with counts: staged, modified, untracked, conflicted. Then up to 10 changed paths as `<code> <path>`, then `+N more`. A clean tree says `clean`.
- **lastCommit:** `git log -1 --format=%h%x09%s%x09%cr%x09%an`, shown as `<hash> <subject> (<relative time>, <author>)`. A repository with no commits says so.
- **diffSummary:** the last line of `git diff --stat` labeled `unstaged` and of `git diff --cached --stat` labeled `staged`; `none` when empty.
- **lastTest:** from the recorded run: `` `<command>` exited <code> (<passed|failed>), <age>. The model ran it; I did not re-run it, and files may have changed since. `` With no record, no answer (the prompt goes on to the model).
- Any git invocation that fails in a way other than "not a git repository" (git missing, timeout, unexpected exit) is not answered: the prompt goes on to the model unchanged.

## Observing test runs

A `tool.call` hook on the Bash tool calls `next(e)` first, then, when the command is a test command, stores `{ command, exitCode, at }` in session state. It never changes the call or its result, and a failure inside it is swallowed so a tool call can never break because of the recorder.

A command is a test command when, after splitting on `&&`, `||`, `;`, `|` and newlines, some segment (after removing leading `VAR=value` assignments and a leading `cd <dir>` segment) begins with one of these runners: `pytest`, `py.test`, `python -m pytest`, `python -m unittest`, `python3 -m pytest`, `python3 -m unittest`, `npm test`, `npm run test`, `npm t`, `yarn test`, `pnpm test`, `cargo test`, `go test`, `dotnet test`, `mvn test`, `gradle test`, `./gradlew test`, `rspec`, `jest`, `npx jest`, `vitest`, `npx vitest`, `make test`. Commands that merely mention a runner (`echo pytest`, `grep pytest notes.txt`) do not count. When a chain contains several commands, the recorded exit code is the Bash result's (the chain's), which is what "did the test pass" means for `cd app && pytest`.

If the result has no usable exit code (for example an interrupted call), nothing is recorded.

## Structure

New plugin `plugins/local-answer-router/` (plugin name `local-answer-router`, version 0.1.0, MIT, author Naren, as for `subagent-meter`):

- `.claude-plugin/plugin.json`: `types` points to `./types/index.d.ts`.
- `hooks/hooks.json`: `{ "modules": ["./register.ts"] }`.
- `hooks/match.ts`: `normalize(text)`, `PHRASES` (intent to list of phrases), `matchIntent(text) -> Intent | null`, `stripAskPrefix(text) -> string | null`.
- `hooks/answer.ts`: `answerBranch`, `answerStatus`, `answerLastCommit`, `answerDiffSummary`, `answerLastTest`, `formatAge`, `escapeText`; each takes captured command output (or the record), not a `$`, so they are pure.
- `hooks/testrun.ts`: `isTestCommand(command) -> boolean`.
- `hooks/register.ts`: the two hooks and the git runner; the only file that touches `$`.
- `hooks/*.test.ts`: tests for each module and for the hooks.
- `types/index.d.ts`: the session-state contract for the last test record.
- `README.md`: what it is, the phrase table, the limits, how to try it (`claude --plugin-dir plugins/local-answer-router`).

## Testing

Written first. No test touches a real repository or session: `$.process.run` is stubbed.

- **Matching:** every phrase matches (including capitalization, a trailing `?` and extra spaces). Near-misses do not: `what branch should i use`, `why does the branch check fail`, `what changed and fix it`, empty, 61 characters, two lines, `did it pass`.
- **Hook, prompt.submit:** each intent returns `{ drop }` starting with the label; `ask: what branch am i on` calls `next` with the prefix removed; a prompt with attachments, and one whose origin is not the user's, calls `next` unchanged; no match calls `next` with the event unchanged.
- **Fail-open:** for every intent, a rejected `$.process.run`, a nonzero exit that is not "not a git repository", a thrown formatter and a missing record all end in `next(e)` with the event unchanged.
- **Answers:** `git status` parsing for renames, paths with spaces and quotes, untracked, conflicted, ahead and behind, detached, no commits, and more than 10 paths; control characters escaped; long values truncated; `formatAge` for seconds, minutes, hours and days.
- **Recorder:** records `pytest -q`, `cd app && pytest`, `FOO=1 npm test`, `npm run test`; does not record `echo pytest`, `grep pytest notes.txt`, `cat package.json`; does not alter the call or the result; a throwing state write does not break the call; a result without an exit code records nothing.
- **Mutation check:** each of the main behaviors (label, whole-message matching, fail-open, origin check, attachments check, recorder position check) is proved by a mutant that makes a test fail.

## Repo integration

- A marketplace entry for the plugin; the README catalog and `llms.txt` regenerated with `scripts/build_catalog.py`; `scripts/validate.py`, `claude plugin validate plugins/local-answer-router` and the existing test suites stay green.
- `CHANGELOG.md` gets an `Added` entry.
- A live check is the owner's: `claude --plugin-dir plugins/local-answer-router`, ask `what branch am i on`, run a test through Claude, then ask `did the last test pass`.

## Out of scope

Fuzzy or model-based matching; counters of tokens saved; answers that need conversation context; tests the user ran outside Claude; commands beyond git and test-run observation; answering while the repository is in the middle of a merge or rebase beyond what `git status` itself reports; publishing.
