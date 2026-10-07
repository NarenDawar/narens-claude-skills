# local-answer-router: a Claude Code mod by Naren

> Answers trivial questions (what branch, what changed, did the last test pass) from git and the last test run on your machine, with no model tokens.

Part of [Naren's Claude Toolkit](https://github.com/NarenDawar/narens-claude-toolkit).

## What it does

Some questions get asked all day and have a deterministic answer on your machine. When you type exactly one of them, this mod answers it itself, instantly, and the prompt never reaches the model:

```text
> what branch am i on
[local, no tokens] On branch main

> what changed
[local, no tokens] main (ahead 1): 2 modified, 1 untracked
   M src/app.py
   M README.md
  ?? notes.txt

> did the tests pass
[local, no tokens] `pytest -q` failed, 4 minutes ago. The model ran it; I did not re-run it, and files may have changed since.
```

Every local answer starts with `[local, no tokens]`. To send a question to the model anyway, start it with `ask:`: `ask: what branch am i on` goes through as `what branch am i on`.

## The questions it answers

The whole message must be one of these (case, a trailing `?` and extra spaces do not matter). Anything longer or different goes to the model as usual.

| Question | Phrases | Answer comes from |
| --- | --- | --- |
| branch | `what branch am i on`, `which branch`, `current branch`, ... | `git branch --show-current` |
| what changed | `what changed`, `what did i change`, `git status`, `any uncommitted changes`, ... | `git status --porcelain` |
| last commit | `last commit`, `what was the last commit`, ... | `git log -1` |
| diff summary | `diff stat`, `how big is the diff`, `how many lines changed` | `git diff --stat` (unstaged and staged) |
| last test | `did the tests pass`, `did the last test pass`, `are the tests passing`, ... | the last test run the model made in this session |

The full list is the `PHRASES` table at the top of `hooks/match.ts`; adding a phrasing is one line plus a test.

## How the last test result works

The mod watches the Bash commands the model runs. When one is a test run (`pytest`, `python -m unittest`, `npm test`, `cargo test`, `go test`, `jest`, `vitest`, `make test` and the like, on their own or after a `cd`), it remembers the command, whether it passed or failed, and when. It is deliberately strict: a command that pipes the output, chains with `;`, `||` or `&`, or only mentions a runner (`echo pytest`) is not recorded, because a wrong "the tests passed" is worse than no answer. A run that was interrupted or timed out is not recorded either. If no test run has been seen, the question goes to the model, which can run the tests itself.

## Limits

- **The model does not see these exchanges.** A locally answered prompt never enters the session, so Claude will not know you asked. Use `ask:` when you want the question in the conversation.
- **It saves tokens only for the listed questions, typed as written.** Everything else costs what it always did.
- **The test answer is not live.** It reports the last run the model made, not tests you ran in your own terminal, and files may have changed since.
- **It only answers your own typed prompts.** Pasted attachments, messages from other plugins, peers or schedules, and anything it cannot answer cleanly (git missing, a timeout, an unexpected error) go to the model untouched.
- **It only reads.** It runs fixed `git` commands, never anything taken from your message, and changes nothing.
- **Not checked in a live session yet:** how a locally answered prompt looks on screen (the API documents the text as shown to you), and whether a failed Bash test run always arrives flagged as an error, which the recorder relies on.

## Try it

This is a mod, so it loads from a folder rather than from the skills directory:

```text
claude --plugin-dir plugins/local-answer-router
```

Then ask `what branch am i on`, have Claude run your tests, and ask `did the tests pass`. Tests for the mod run with `claude plugin test plugins/local-answer-router`.
