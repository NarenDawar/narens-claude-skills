# schedule-doctor: a Claude Code skill by Naren

> Make Claude scheduled tasks actually run: check a task before you schedule it, and find out why one did not fire.

Part of [Naren's Claude Toolkit](https://github.com/NarenDawar/narens-claude-toolkit).

## When it triggers

Ask things like "will this scheduled task actually run?", "check this before I schedule it", "why didn't my 7am task fire?", or "my scheduled task ran hours late". It covers Claude Desktop scheduled tasks, Claude Code session crons (`/loop`), and cloud routines.

## What it does

**Before you schedule:** reads the task prompt, lists the tools it will likely need so you can pre-approve them, adds a time guard so a late catch-up run does not act on stale data, and checks whether your machine keeps awake (sleep timeout and lid-close action).

On Windows, a scheme-only lid query is reported as no setting found. A failed query, empty response, or setting output whose labels cannot be parsed is instead reported as could not be read; it does not establish that a desktop stays awake. This is diagnostic wording, not support for parsing localized `powercfg` labels.

**After a run:** reads the run's outcome and tells you which of these happened: it halted on a tool nobody approved, the machine slept through it, it ran late as a catch-up, it failed with an error, or there is not enough data to say.

Small stdlib-only Python scripts do the checking and the classifying, so the answers are not guesses. Nothing is edited without your yes.

## Install

**Plugin marketplace (recommended)**

```text
/plugin marketplace add NarenDawar/narens-claude-toolkit
/plugin install schedule-doctor@narens-claude-toolkit
```

**Manual**

Copy `plugins/schedule-doctor/skills/schedule-doctor` into `~/.claude/skills/`.

## Example

**Before:** you schedule "pull the latest build and git push the result" for 7am. The laptop sleeps, Desktop runs it at 10:12, and the push waits on a permission nobody approved.

**After:** the skill flags `git push` as approve-once, notes "latest" as a staleness risk, adds "if this run started more than 2 hours late, stop and report", and shows that your machine sleeps after 15 minutes on battery.

---

Made by [Naren](https://github.com/NarenDawar). If this helped, star the repo.
