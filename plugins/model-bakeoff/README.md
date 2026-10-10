# model-bakeoff: a Claude Code skill by Naren

> Find the cheapest Claude model that still passes your own test cases for a custom subagent, then switch the agent to it.

Part of [Naren's Claude Toolkit](https://github.com/NarenDawar/narens-claude-toolkit).

## When it triggers

Only when you ask. Example phrasings:

- "Which model should my code-reviewer agent use?"
- "Could this agent run on Haiku?"
- "Bake-off my reviewer agent."
- "A new model shipped: re-check my agents."

The skill runs your test cases on Haiku, Sonnet and Opus, tells you which models pass and what each run cost, and, only after you say yes, writes the cheapest passing model into the agent's `model` field.

## Before and after

```text
> bake-off my code-reviewer agent

Cases (drafted from the agent's description, edited by you): 4
36 agent runs on haiku, sonnet, opus (ceiling $36 at the $1 per-run cap, $20 total cap; uses your claude usage). Go?   yes

model   resolved id                 result  pass rate  mean cost/run
haiku   claude-haiku-4-5-20251001   PASS    92%        $0.0021
sonnet  claude-sonnet-5-5           PASS    100%       $0.0094
opus    claude-opus-5-5             PASS    100%       $0.0310

recommendation: haiku is the cheapest model that passes. Edit reviewer.md to use it? (diff shown)
```

This is an illustration of the format, not a measured result.

## How it decides

- **Cases are yours.** Each case has a prompt and deterministic checks (text that must or must not appear, a regex, JSON keys, or a command that must exit 0). An optional rubric is judged by a model and can only make a case stricter. Claude drafts the cases file from the agent's description and you edit it.
- **The pass rule.** Every case must pass in at least 2 of 3 runs, and at least 90% of all runs must pass. An errored run counts as failed. Both numbers are options.
- **Cheapest passing.** The model with the lowest mean measured cost per run wins; if costs are missing the order is Haiku, Sonnet, Opus.
- **Safe to run.** Read-only agents run with read tools only. An agent that can write or run commands, or a case that runs a command, needs your explicit opt-in after you see exactly what will run. Every run happens in a throwaway copy of a fixture folder, with a spend cap, and without your MCP servers. That limits mistakes; it is not a sandbox.
- **Pinned by default.** The default is the full model ID that was tested, so the agent keeps running the model you proved. Aliases (`haiku`) are possible, but they move when a new model ships.
- **Remembers.** A results file records the tested model IDs, the date and the rule. Later, the skill checks which aliases now point to something new and re-tests only those.

## Limits

- Runs use your own `claude` login, so they use your usage; every case runs on up to three models, several times.
- A pass is evidence on your cases, not proof about all inputs, and models are nondeterministic.
- Cost is measured from `claude -p` output, not your subscription quota.
- Custom subagents only. Skills are a planned follow-up once they can be checked.
- **Cost per run is noisy with few runs.** The first run of each model pays to fill the prompt cache and costs several times more than the rest (in the live check below, $0.0144 against $0.0008 to $0.0015 for Haiku). With only a few runs per case, a close call between two models can flip on that one cold run; use more runs, or treat close costs as a tie.
- **What has been checked live.** One bake-off against a real `claude` (version 2.1.296, Windows): a read-only agent with text checks, Haiku and Sonnet, 2 cases × 2 runs. The flags (`--agents` file, `--agent`, `--tools`, `--permission-mode dontAsk`, `--permission-prompts none`, `--strict-mcp-config`, `--max-budget-usd`) were accepted, the JSON had the cost, turns and resolved model ID the script reads, and the agent used its tools. The whole run cost about 7 cents. **Not checked live:** the rubric judge, agents that can write or run commands (`--allow-writes`), `command` checks, and `resolve`. Try those on something small first.

## Install

**Plugin marketplace (recommended)**

```text
/plugin marketplace add NarenDawar/narens-claude-toolkit
/plugin install model-bakeoff@narens-claude-toolkit
```

The editing step uses the same guarded tool as [`subagent-tax-auditor`](../subagent-tax-auditor/README.md) (a copy of its `agent_edit.py`, kept identical by a test).
