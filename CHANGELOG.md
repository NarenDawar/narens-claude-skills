# Changelog

All notable changes are recorded here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- `model-bakeoff` skill: runs a custom subagent's test cases on Haiku, Sonnet and Opus through `claude -p`, applies a stated pass rule (each case passes in 2 of 3 runs, 90% of all runs), recommends the cheapest passing model by measured cost, and after your yes writes the pinned model ID into the agent's `model` field with a backup and undo. Read-only by default; agents that can write or run commands need an explicit opt-in. Keeps a results file so a later run re-tests only the models that changed. Custom subagents only; not yet run against a real session.
- `mcp-fixer tasks` and `mcp-fixer bench` (third of three parts): `tasks` has a model write test requests for each tool into an editable file, and `bench` runs them against the original and the patched tool list (as `wrap` serves it) through `claude -p` or the Anthropic API, then reports both accuracies with 95% intervals, a paired comparison, token sizes and a sample-size-aware verdict (`worse`, `no drop detected` or `inconclusive`). It can detect a drop but does not show a patch improves tool selection, and it has not been run against a real server yet.
- `mcp-fixer patch` and `mcp-fixer wrap` (second of three parts): `patch` writes an editable patch file from the lint findings (enums found in descriptions and trimmed long descriptions are filled in and flagged for review; everything else becomes a todo), and `wrap` runs a stdio proxy in front of a real MCP server that shows the client the patched tool definitions, renames included, while forwarding every other message byte for byte. It never enforces an enum or changes calls, serves a tool unpatched when the server has changed it, and makes no claim yet that a patch improves tool selection.
- `mcp-fixer` MCP server tool (first of three parts): `python -m mcp_fixer score` reads an MCP server's tool list, from a spawned stdio server or a saved JSON file, and prints a deterministic lint score from 0 to 100 with per-tool scores and findings (missing or bloated descriptions, undescribed or untyped parameters, values listed in prose that should be enums, generic or inconsistent names, and tool-count and token-size limits). Read-only: it never calls a tool. The patcher and the accuracy proof come next.
- `subagent-meter` mod: a live status line showing how much of your session's tokens go to subagents (a share of output tokens and a share of all tokens, side by side) and which agent type uses the most. Tokens only, session scope, never changes a turn. The toolkit's first mod.
- `schedule-doctor` skill: before you schedule a Claude task it lints the prompt (likely tools to pre-approve, a time guard against stale catch-up runs, time-relative wording) and checks whether your machine keeps awake; afterwards it classifies why a run did not fire (permission halt, slept through, late catch-up, failed, or not enough data). Covers Desktop scheduled tasks, Claude Code crons and cloud routines.
- `subagent-tax-auditor` skill: reads your local session transcripts, shows which subagent types used up your quota (per type and model, with fixed context per spawn), recommends cheaper models, edits custom agents' `model` after confirmation with a backup and undo, and compares usage before and after.
- `rule-promoter` skill: turns the enforceable rules in CLAUDE.md into hooks (protected paths, blocked commands, banned content, must-pass-before-stopping), proves each rule blocks a violation, and installs them into `.claude/settings.json` after confirmation.

### Changed
- The repo and marketplace are now `narens-claude-toolkit` (was `narens-claude-skills`), and the tooling handles skills, mods and MCP servers. If you installed a plugin from the old marketplace, run `/plugin marketplace remove narens-claude-skills`, then `/plugin marketplace add NarenDawar/narens-claude-toolkit`, then `/plugin install <plugin-name>@narens-claude-toolkit`. Plugin names, versions and behavior are unchanged.

### Fixed
- `subagent-tax-auditor` 0.1.1: snapshot files and backup records are written with LF line endings on Windows; the date filters say they use UTC dates; a run that switches models counts as one spawn, on the model it started with.
- `rule-promoter` 0.1.3: a `settings.json` with comments or trailing commas is still refused, and the error now says Claude Code treats those as errors too.
- `rule-promoter` 0.1.2: shell `#` comments and the body of a heredoc that is not fed to a shell or interpreter (such as a commit message) no longer trigger or excuse a command rule; patterns with a repeated group that repeats (`(a+)+`) are refused because they can hang the hook; all stop checks share one 280-second budget, with the rest skipped and a warning when it runs out; command output in a legacy console code page is decoded instead of replaced.
- `rule-promoter` 0.1.1: a deny message names the matched path or command; rules with a non-boolean `enabled`, globs that could never match (`[ab]`, `{a,b}`, leading `./` or `/`) or a `timeout_seconds` above 280 are refused instead of silently misbehaving; very deeply nested tool input is still checked; the settings helper keeps Windows launcher paths intact, keeps file permissions, writes through a symlinked `settings.json`, and reports a write failure as a clean error; the force-push example also catches `-fu` and `+main`.

## [0.1.2] - 2026-10-01

### Fixed
- `decision-journal` 0.1.2: `--confidence 1` explains it was read as 100%; `stats --tag` matches entries logged by 0.1.0 (tags are casefolded when compared); the OS error names the file that actually failed; an estimate given `--confidence` reports the real mistake; a missing subcommand lists the commands; `grade`'s `result` line is documented as success-only; `grade`'s result line prints whole numbers from older logs as `3`, not `3.0`.

## [0.1.1] - 2026-10-01

### Fixed
- `comprehension-check` 0.1.1: grades only the current question when you answer ahead, gives a summary and stops when you say stop, drops a question your own fix already answered, re-asks flagged topics on a retake, scales the question count for medium changes; README example, line range and slash form corrected.
- `decision-journal` 0.1.1: unreadable or unwritable logs give a clean error, concurrent sessions can no longer lose entries (lock file), duplicate ids are reported by line, `70%` and `0.7` are accepted as confidence, `grade` prints a computed result line, tiny or odd numbers are handled, small estimate slices carry no verdict, symlinked and drive-root paths work, README example now shows the real report.

## [0.1.0] - 2026-10-01

### Added
- Repo scaffolding: plugin marketplace, skill template, validator, catalog generator, CI.
- `comprehension-check` skill: quizzes you on the code Claude wrote this session and flags the parts you could not maintain.
- `decision-journal` skill: log predictions about dev decisions, grade them later, and see where you are overconfident (JSONL log plus a standard-library calibration script).
