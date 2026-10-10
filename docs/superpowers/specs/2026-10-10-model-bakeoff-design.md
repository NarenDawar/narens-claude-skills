# model-bakeoff: pick the cheapest model that passes your own test cases

Date: 2026-10-10

## Purpose

A custom subagent runs on whatever model its `model` field says (or the default), usually chosen by feel. This skill measures instead: it runs the agent's test cases on Haiku, Sonnet and Opus, applies a stated pass rule, recommends the cheapest model that passes, and, only after the user says yes, writes it into the agent's `model` field. It keeps a results file so a later run only re-tests the candidates that changed. It claims only what its runs show.

## Success criteria

1. For one custom agent with a cases file, `bakeoff.py run` and `decide` produce a table of pass rate and measured cost per model, per-case detail, and a recommendation under the pass rule, using only `claude -p` runs.
2. No run can write or execute anything unless the agent is read-only or the user has opted in after seeing the exact tools, commands and scratch folder.
3. The agent file is edited only through `plan` (diff, changes nothing), `apply` (backup) and `undo`, and only after an explicit yes in the conversation.
4. A results file records tested model IDs, date, rule and outcomes (no agent output by default); a later `resolve` finds which candidates changed and only those are re-run.
5. The skill states what it does not show: evidence on these cases only, nondeterminism, measured cost is not the subscription quota, a scratch copy is not a sandbox.
6. The repository's validator, catalog and tests pass; the skill's own tests call no real model.

## Decisions already made

- **Targets (v1):** custom subagents only (a file in `.claude/agents/` or `~/.claude/agents/`). Built-in agents are reported only (no file). Skills are a flagged follow-up to build only after `claude -p "/skill-name ..."` and a skill `model` field are verified.
- **What "passing" means:** every case needs at least one deterministic check; a case passes when all its checks pass. An optional rubric is judged by a separate model call and can only make a case stricter.
- **The pass rule** (all options): each case passes in at least 2 of 3 runs, and at least 90% of all runs pass. Defaults `--runs 3`, `--min-case-runs 2`, `--min-pass-rate 0.9`.
- **Safety:** read-only by default (tools limited to the agent's own read-only tools); an agent with write or execute tools, or a `command` check, needs an explicit opt-in; every run is capped (budget, turns, time) and the whole bake-off has a total cap.
- **What is written:** the pinned full model ID by default (the model tested is the model that runs); the alias if the user picks it, with a note that the result then expires when the alias moves.
- **Re-running:** a results file plus a `resolve` step that probes each alias for the model ID it points to today. No scheduler.
- Python 3 standard library only; the skill's scripts live in the skill folder; its tests live in the repo's `tests/` folder like the other skills'.

## Constraints and assumptions

- Cases run through the user's own `claude` login, so they use the user's usage. The skill never runs anything before `estimate` has shown the number of calls and the user has said yes.
- Cost per run is read from the `claude -p --output-format json` result (`total_cost_usd` and token counts) and the model ID the run used from its model usage; if costs are absent, `decide` falls back to tier order (Haiku, Sonnet, Opus) and says so.
- Models are nondeterministic: a pass is evidence on the user's cases, not proof.
- To be verified against the installed CLI while writing the plan (each is a task step with a stated fallback, not a guess): the exact `--agents` JSON shape and whether `--agent <name>` loads it from a scratch directory; how `--tools` restricts tools for an agent session; the JSON output fields for cost, turns and the resolved model ID; whether `--model` accepts a full model ID; that a `claude -p` run exits nonzero or reports an error subtype on a budget or turn limit.
- Agent files, cases files, fixtures and model outputs are untrusted text: nothing from them is ever placed in a shell string (every command is an argument list), control characters are escaped in reports, and the API key / login is never printed.

## The cases file

`.claude/bakeoff/<agent>.cases.json` in the project (or the path given with `--cases`), UTF-8 JSON:

```
{
  "casesVersion": 1,
  "agent": "<agent name>",
  "cases": [
    {
      "id": "<unique string>",
      "prompt": "<what the user would ask the agent>",
      "fixture": "<optional folder, relative to the cases file>",
      "checks": [
        {"type": "contains", "value": "text", "ignoreCase": true},
        {"type": "not_contains", "value": "text", "ignoreCase": true},
        {"type": "regex", "value": "pattern"},
        {"type": "json", "required": ["key", "..."]},
        {"type": "command", "argv": ["prog", "arg"], "expectExit": 0, "timeoutSeconds": 120}
      ],
      "rubric": "<optional text>"
    }
  ]
}
```

- `casesVersion` (1), `agent` and `cases` (non-empty) are required; unknown fields are errors. Case `id`s are unique; `prompt` is a non-empty string; `checks` is a non-empty list.
- `contains` / `not_contains`: `value` is a non-empty string; `ignoreCase` defaults to true. `regex`: a pattern that compiles (Python `re`), searched in the output; patterns longer than 500 characters or that fail a compile are errors. `json`: the output (or the first JSON object in it) must parse as an object containing every key in `required` (a list of strings, may be empty). `command`: `argv` is a non-empty list of strings (no shell), `expectExit` an integer (default 0), run in the scratch directory after the agent finishes; it makes the case need the execute opt-in.
- `fixture` must stay inside the cases file's folder (no `..`, no absolute path, no symlink escaping it) and exist; its size is limited (at most 200 files and 5 MiB) and it is copied into a fresh scratch directory for every run.
- `rubric`, when present, is a non-empty string; the case then also needs the judge's pass. A case with only a rubric is invalid.
- `check-cases` prints one line per problem and exits 2, or a summary and exits 0.

## The scripts

`scripts/bakeoff.py` (subcommands; exit 0 on success, which includes "no model passed" since that is a result and not an error; exit 2 for usage and input errors, an aborted run, or a refused edit):

- `agents [--agents-dir DIR ...] [--json]`: custom agents with name, model, tools, file, and whether the agent is read-only (every tool in the read-only set Read, Grep, Glob, or the tools list is exactly such a subset).
- `check-cases --cases FILE`.
- `estimate --agent NAME --cases FILE [--models LIST] [--runs N]`: number of runs (`cases x models x runs`, plus judge calls), the agent's tools, commands that would execute, whether opt-in is needed, and a ceiling cost from the caps. Prints the opt-in list when needed.
- `run --agent NAME --cases FILE [--models LIST] [--runs N] [--allow-writes] [--max-run-usd X] [--max-total-usd X] [--max-turns N] [--timeout S] [--keep-outputs] [--out FILE]`: executes and writes the raw results file. `--allow-writes` is refused when the agent is read-only (nothing to allow) and required when it is not.
- `decide --results FILE [--runs-rule ...] [--json]`: applies the pass rule and prints the table and recommendation.
- `resolve [--results FILE] [--models LIST]`: probes each alias with a trivial call, prints the resolved model ID per alias and, against a results file, which candidates changed.
- `plan|apply|undo --agent NAME --model MODEL [--backup-dir DIR]`: the guarded edit.

Default `--models` is `haiku,sonnet,opus` (aliases accepted by `claude --model`); any full model ID can be added.

## A run

For each model, case and run number, `run`:

1. makes a fresh scratch directory (under the system temp folder), copies the fixture into it, and writes the agent definition so `claude` can load it from there (`--agents` JSON built from the agent file's frontmatter and body; the model in the definition is overridden by `--model`);
2. runs `claude -p --output-format json --agents ... --agent NAME --model M --max-budget-usd X --max-turns N` with the case prompt on stdin, in the scratch directory, with a timeout, and the tool limit below; the process tree is stopped on timeout;
3. in read-only mode limits the tools to the read-only ones the agent already has (if the agent lists none, to Read, Grep, Glob); with `--allow-writes` the agent keeps its own tools;
4. evaluates the checks on the result text (the `command` checks after the run, in the scratch directory), and the judge call when a rubric exists (a `claude -p` call to the judge model, default Opus, asked for exactly `{"pass": true|false, "reason": "..."}`, parsed leniently and counted as a fail when unparseable);
5. records pass or fail per check, the cost, tokens, turns, duration and the model ID the run used, then removes the scratch directory (also on failure).

A run that errors (the CLI fails, no usable result, a budget or turn limit, a timeout) counts as a failed run and is labeled with its kind. After 5 failed runs in a row because of the CLI itself (not a check), the whole bake-off stops with the reason. The total spend cap is checked between runs. Runs are sequential in v1. The default is to store only check results; `--keep-outputs` also stores the agent's output text (truncated to 20,000 characters per run).

## Deciding

- A model *passes* when every case passes in at least `--min-case-runs` of `--runs` runs and at least `--min-pass-rate` of all runs pass.
- The recommendation is the passing model with the lowest mean measured cost per run (total measured cost divided by runs), ties to the smaller tier. If measured costs are missing for any passing model, the order is Haiku, then Sonnet, then Opus, and the report says so.
- No passing model: "no model passed; keep the current model" with the failing cases. The current model already the cheapest passing one: say so, recommend no change.
- The report lists for each model: pass or fail, run-level pass rate, cases below the per-case minimum, mean cost per run, resolved model ID, errored runs by kind; and the rule and the sentence that this is evidence on these cases, not proof, and not the subscription quota.

## The edit

`plan` prints a unified diff and changes nothing. `apply` writes it, saves a backup (default `<project>/.claude/bakeoff/backups/`), and prints the exact `undo` line. `undo` restores the last backup for the agent and refuses when the file changed since `apply`. The file is refused with the reason when it has no frontmatter, a duplicate `model` key, or a multi-line `model` value; a missing `model` line is added. Only the `model` line changes. A pinned model ID is the default argument the skill passes; the alias is used only if the user chose it.

## Results file and re-running

`.claude/bakeoff/<agent>.results.json` holds `resultsVersion`, agent, date, the rule, the cases file's SHA-256, whether writes were allowed, and for each tested model: requested name, resolved model ID, per-case and per-run outcomes, cost and token totals, the recommendation and what was applied. On a later invocation the skill runs `resolve`: if a candidate resolves to a different ID than recorded, a model is newly named, or the cases file hash changed, it offers a re-run limited to the changed candidates (all of them when the cases changed), reusing the other saved outcomes.

## The SKILL.md process

Frontmatter `name: model-bakeoff` and a one-line `description` starting `Use when` (the user wants to pick the cheapest model that still works for a subagent, asks whether Haiku or Sonnet would do, or wants to re-check an agent's model after new models ship). Steps: list agents; get or draft the cases file with the user (Claude drafts it from the agent's description and shows it; nothing runs before the user has seen the cases); `check-cases`; `estimate` and the explicit yes (naming write/execute tools and commands when the opt-in is needed); `run`; `decide`; explain with the limits; `plan`, wait for yes, `apply`, give the `undo` line; save and later `resolve`. Rules: arithmetic only through the script; never edit without the user's yes; never run before `estimate` was shown; never print agent outputs unless `--keep-outputs` was chosen; state the limits every time.

## Testing

Written first with `unittest` in `tests/` (like `test_agent_edit.py`); a stand-in `claude` executable returns canned JSON, so no test calls a real model.

- **Cases:** every field and check type valid and invalid (unknown fields, duplicate ids, empty checks, bad regex, oversize pattern, fixture escaping through `..`, an absolute path or a symlink, fixture size limits, rubric-only case, bad `casesVersion`).
- **Checks:** each type on matching and non-matching output, case handling, JSON in prose, the `command` check exit code and timeout, no shell interpretation of argument text.
- **Run:** argument lists passed to the stand-in (agent definition, model, caps, tool limit in both modes), scratch isolation (fixture copied, original untouched, scratch removed on success and failure), the opt-in refusal for a write or execute agent, errors of every kind counted as failed runs, the early stop after 5 CLI failures, the total cap, output not stored by default and truncated when kept.
- **Decide:** the pass rule at its boundaries (2 of 3 and not 1 of 3; exactly 90% and not 89%), the cheapest passing model, ties, missing costs falling back to tier order, no model passing, the current model already cheapest.
- **Edit:** plan changes nothing; apply writes only the `model` line and backs up; undo restores and refuses a changed file; every refusal case; a missing `model` line added.
- **Resolve and results:** alias resolution from canned output, changed-candidate detection, the cases hash change forcing a full re-run, reuse of unchanged results.
- **Report:** control characters escaped; the limits sentence present; the login and key never printed.
- A mutation check of the pass rule, the opt-in gate, the cheapest choice and the edit refusals, then a fresh Opus review of the whole branch.

## Repo integration

`plugins/model-bakeoff/` with `.claude-plugin/plugin.json`, `skills/model-bakeoff/SKILL.md` and `scripts/bakeoff.py`, a README (keyword-rich H1, when it triggers, install, a before/after example, the honest limits); a marketplace entry; the generated catalog; the CHANGELOG; tests under `tests/`. The CI already runs the repo's unit tests.

## Out of scope

Skills as targets (follow-up after verification), generating cases by script, parallel runs, comparing effort levels, scheduling re-runs, judging by several models, measuring the subscription quota, and publishing.
