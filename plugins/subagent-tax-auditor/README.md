# subagent-tax-auditor: a Claude Code skill by Naren

> Find which subagents ate your Claude Code quota, and make the expensive ones cheaper.

Part of [Naren's Claude Toolkit](https://github.com/NarenDawar/narens-claude-toolkit).

Every subagent starts with a large fixed context (system prompt, tool definitions, CLAUDE.md) that is sent again on each of its requests, so many small subagents can cost far more than the work they produce. `subagent-tax-auditor` reads your local session transcripts, splits usage between the main thread and each subagent type, explains the biggest spenders with the numbers behind each claim, proposes a cheaper model for your custom agents, and later checks whether the change helped.

## When it triggers

Only when you ask. Example phrasings:

- "Why is my Claude Code quota disappearing so fast?"
- "Which of my subagents should I make cheaper?"
- "Audit my subagents."
- "Which agent is eating my quota?"

## Install

**Plugin marketplace (recommended)**

```text
/plugin marketplace add NarenDawar/narens-claude-toolkit
/plugin install subagent-tax-auditor@narens-claude-toolkit
```

**Manual**

Copy `plugins/subagent-tax-auditor/skills/subagent-tax-auditor` into `~/.claude/skills/`. It needs Python 3 (standard library only).

## What it measures

For each agent type and model: spawns, cost, share of cost, share of output tokens, fixed context per spawn (the median size of a spawn's first request), and output tokens. A short data-quality footer says what it could not read: skipped lines, subagents with no meta file, and models with no price.

Fixed context counts all input tokens in the first request: uncached input, cache reads, and five-minute and one-hour cache writes. It excludes output tokens and later requests, so a spawn with caching off still shows its initial input size. This is a first-request input proxy, not a separate measurement of the system prompt alone; token totals and cost accounting are unchanged.

```text
type       model             spawns     cost   cost%  output%  fixed ctx/spawn  output tok
reviewer   claude-opus-5-5       14    $0.51   58.7%    39.6%           60,000       8,400
main       claude-opus-5-5        1    $0.31   35.7%    37.7%           30,000       8,000
Explore    claude-haiku-4-5       6    $0.05    5.7%    22.6%           20,000       4,800
total                                  $0.87                                        21,200
```

(A made-up project, to show the shape. Here `reviewer` costs 59% of the total but writes 40% of the output, and pays 60,000 tokens of context 14 times.)

## What it changes

Only your **custom agents** (`.claude/agents/*.md`), and only after you have seen the diff and said yes. It changes the `model` line (and `effort`, which Claude Code's docs list as a valid agent field), keeps everything else in the file exactly as it was, saves a backup first, and prints an undo command.

Built-in types (`general-purpose`, `Explore`, `Plan`, and so on) have no file. For those it reports the numbers and tells you to set `model` where the agent is spawned.

Before it edits, it saves a snapshot of the per-type numbers. Later, `compare` shows usage per spawn before and after, with sample sizes. With fewer than 5 spawns on either side it gives no verdict, and it says so when the model did not change.

## Example

**You:** Why is my quota disappearing so fast?

**Claude:** runs the report, leads with the biggest spender and its share of output, shows the table and the data-quality footer, and says once that the dollar figure is a proxy.

**Claude:** proposes `reviewer: opus → sonnet` with the diff and waits. After your yes it saves a snapshot, applies the change, and gives you the backup path and the undo command.

**You, a week later:** Did that help?

**Claude:** runs `compare` and reports median cost per spawn before and after, with how many spawns each number rests on.

## How it works

- Two small stdlib scripts do all the measuring and editing: `audit.py` (read-only) and `agent_edit.py`. Claude does the explaining and the judgment; the scripts do the arithmetic.
- A transcript message can span several lines, so each message id is counted once at its largest usage. A resumed session also copies earlier history into a new file, so each message id is counted once across files too (on the author's machine about 2% of messages were copies). Without that, usage is overcounted.
- Prices come from `rates.json`, dated and with its source. A model with no price shows `no rate` and is left out of cost totals, never guessed.

## Limits

- The dollar figure is a proxy: tokens times published API rates. Your subscription quota is not measured in dollars, and Anthropic does not publish how it is counted.
- Before/after compares different tasks run at different times. It shows a trend with a sample size, not proof.
- Only transcripts still on disk are counted. Deleted or expired sessions are invisible.
- It cannot edit built-in agent types, and it does not stop Claude Code from spawning agents.
- Date filters apply per file (a main session or one subagent run) by the UTC date of its first timestamp, so a run late in the evening west of UTC can fall on the next day. After copied history is removed from a resumed file, its start is the earliest retained assistant-message timestamp, when available; otherwise the original file timestamp is kept. Snapshot comparisons use this same corrected start.
- A run counts as one spawn, on the model it started with. If it switched models partway, the later model's row shows its tokens and cost but 0 spawns.
- Claude Code names a project's transcript folder after its path with every non-alphanumeric character turned into `-`. If a project seems missing, use `--all` or `--project PATH`.
- Prices and the transcript format change. `rates.json` is dated (the report warns when it is over 90 days old), and the parser reports what it could not read.
- Do not assume subagents are your problem: on the author's own machine (34 sessions, 333 subagent runs) the main thread was about 90% of the cost and subagents about 10%. Run the report before changing anything.

## Privacy

It reads only local files, makes no network calls, and never prints prompt or response text. Task descriptions appear only if you ask for them (`--show-descriptions`).

## Turning it off

It does nothing unless you ask. To undo an edit, run the `To undo:` command it printed, or restore the file from `.claude/agent-edit-backups/` (or the backup folder you chose).

---

Made by [Naren](https://github.com/NarenDawar). If this helped, star the repo.
