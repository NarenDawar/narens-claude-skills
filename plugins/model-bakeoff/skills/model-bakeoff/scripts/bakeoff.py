#!/usr/bin/env python3
"""bakeoff: pick the cheapest model that passes your own test cases for a custom subagent.

    python bakeoff.py agents       [--agents-dir DIR ...] [--json]
    python bakeoff.py check-cases  --cases FILE
    python bakeoff.py estimate     --agent NAME --cases FILE [--models LIST] [--runs N]
    python bakeoff.py run          --agent NAME --cases FILE [--allow-writes] [--out FILE] ...
    python bakeoff.py decide       --results FILE [--json]
    python bakeoff.py resolve      [--results FILE] [--models LIST] [--cases FILE]
    python bakeoff.py plan|apply|undo ...   (the guarded edit of the agent's model field)

Runs use the user's own `claude` login and therefore their usage. Standard library only.
"""
import argparse
import json
import math
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import agent_edit
import bk_agents
import bk_cases
import bk_decide
import bk_runner
from agent_edit import AgentError

RESULTS_VERSION = 1
DEFAULT_MODELS = "haiku,sonnet,opus"
ALIASES = ("haiku", "sonnet", "opus", "fable")
RUN_LIMIT_WITHOUT_YES = 60
LIMITS_SENTENCE = (
    "This is evidence on these cases only, not proof about all inputs; models are nondeterministic; "
    "cost is measured from claude -p output, not your subscription quota."
)


class UsageError(Exception):
    """A problem with the command line or an input file, reported as `error: ...` with exit 2."""


def _safe(value):
    out = []
    for char in str(value):
        code = ord(char)
        if char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        elif code < 32 or 127 <= code < 160:
            out.append(f"\\x{code:02x}")
        else:
            out.append(char)
    return "".join(out)


def _models(text):
    models = [part.strip() for part in text.split(",") if part.strip()]
    if not models:
        raise UsageError("--models needs at least one model")
    for model in models:
        try:
            if model == "inherit":
                raise AgentError("inherit is not a model")
            agent_edit.validate_model(model)
        except AgentError:
            raise UsageError(f"model {_safe(model)!r} is not an alias ({', '.join(ALIASES)}) or a full model id (claude-...)") from None
    return list(dict.fromkeys(models))


def _dirs(args):
    return [Path(d) for d in args.agents_dir] if args.agents_dir else agent_edit.default_dirs()


def _claude(claude):
    if claude is not None:
        return list(claude)
    path = shutil.which("claude")
    if path is None:
        raise UsageError("cannot find the claude command; install Claude Code and log in first")
    return [path]


def _load_cases_for(agent, cases_path):
    data, sha, base = bk_cases.require(cases_path)
    if data["agent"] != agent:
        raise UsageError(f"the cases file is for agent {_safe(data['agent'])!r}, not {_safe(agent)!r}")
    return data, sha, base


def _opt_in_reasons(definition, data):
    reasons = []
    if not bk_agents.is_read_only(definition["tools"]):
        listed = ", ".join(definition["tools"]) if definition["tools"] else "all tools (the agent lists none)"
        reasons.append(f"the agent can use {listed}, which can write or run commands, inside a scratch copy of the fixture")
    for case in data["cases"]:
        for check in case["checks"]:
            if check["type"] == "command":
                reasons.append(f"case {case['id']!r} runs the command: {' '.join(check['argv'])}")
    return reasons


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _finite_positive(value, flag):
    if not (isinstance(value, (int, float)) and math.isfinite(value) and value > 0):
        raise UsageError(f"{flag} must be a finite number greater than 0")


def _check_rule_numbers(min_case_runs, runs, min_pass_rate):
    if not (isinstance(min_case_runs, int) and 1 <= min_case_runs <= runs):
        raise UsageError(f"--min-case-runs must be between 1 and the number of runs ({runs})")
    if not (isinstance(min_pass_rate, (int, float)) and math.isfinite(min_pass_rate) and 0 < min_pass_rate <= 1):
        raise UsageError("--min-pass-rate must be greater than 0 and at most 1")


def _check_numbers(args):
    """Refuse numbers that would turn a safety limit into nothing (nan, 0, negatives) before any run."""
    if args.runs < 1:
        raise UsageError("--runs must be at least 1")
    _finite_positive(args.max_run_usd, "--max-run-usd")
    _finite_positive(args.max_total_usd, "--max-total-usd")
    if hasattr(args, "timeout"):
        _finite_positive(args.timeout, "--timeout")
    if hasattr(args, "min_case_runs"):
        _check_rule_numbers(args.min_case_runs, args.runs, args.min_pass_rate)


def _check_out(path):
    path = Path(path)
    if path.is_dir():
        raise UsageError(f"{path} is a folder, not a results file")
    path.parent.mkdir(parents=True, exist_ok=True)


def _previous_results(path, agent, sha, rule):
    """(earlier results to reuse or None, a note saying why they were not reused or None)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None, None
    if not isinstance(data, dict) or data.get("resultsVersion") != RESULTS_VERSION or not isinstance(data.get("models"), list):
        return None, None
    if data.get("agent") != agent:
        return None, "the saved results are for another agent; they were not reused"
    if data.get("aborted"):
        return None, "the saved results come from an aborted run; they were not reused"
    if data.get("casesSha") != sha:
        return None, "the cases file changed since the saved results; they were not reused (everything was re-run)"
    if data.get("rule") != rule:
        return None, "the saved results used a different rule; they were not reused"
    return data, None


def _load_results(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise UsageError(f"cannot read {path}: {exc.strerror or exc}") from None
    except (ValueError, RecursionError):
        raise UsageError(f"{path} is not valid JSON") from None
    if not isinstance(data, dict) or data.get("resultsVersion") != RESULTS_VERSION or not isinstance(data.get("models"), list):
        raise UsageError(f"{path} is not a bake-off results file (resultsVersion {RESULTS_VERSION})")
    return data


# --- commands ----------------------------------------------------------------------------------

def cmd_agents(args, claude):
    rows = bk_agents.list_rows(_dirs(args))
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no custom agents found")
    for row in rows:
        tools = ", ".join(row["tools"]) if row["tools"] else "(inherits all tools)"
        note = f"  [problem: {_safe(row['problem'])}]" if "problem" in row else ""
        print(f"{_safe(row['name'])}  model={_safe(row['model'] or 'default')}  tools={_safe(tools)}  {'read-only' if row['readOnly'] else 'can write/run'}{note}")
    return 0


def cmd_check_cases(args, claude):
    data, found, sha, base = bk_cases.load(args.cases)
    if found:
        for line in found:
            print(_safe(line))
        return 2
    print(f"ok: {len(data['cases'])} cases for agent {_safe(data['agent'])} (sha256 {sha[:12]})")
    return 0


def cmd_estimate(args, claude):
    _check_numbers(args)
    definition, _ = bk_agents.load_definition(args.agent, _dirs(args))
    data, sha, _ = _load_cases_for(args.agent, args.cases)
    models = _models(args.models)
    agent_runs = len(data["cases"]) * len(models) * args.runs
    judged = sum(1 for c in data["cases"] if c.get("rubric")) * len(models) * args.runs
    reasons = _opt_in_reasons(definition, data)
    print(f"agent: {_safe(definition['name'])} ({'read-only' if bk_agents.is_read_only(definition['tools']) else 'not read-only'})")
    print(f"cases: {len(data['cases'])} (sha256 {sha[:12]})")
    print(f"models: {', '.join(models)} x {args.runs} runs = {agent_runs} agent runs and {judged} judge calls")
    print(f"ceiling: at most ${(agent_runs + judged) * args.max_run_usd:.2f} (every call at the ${args.max_run_usd:g} per-run cap); the total cap is ${args.max_total_usd:g}")
    print(f"opt-in needed: {'yes' if reasons else 'no'}")
    for reason in reasons:
        print(f"  - {_safe(reason)}")
    print("Runs use your own claude login and therefore your usage.")
    return 0


def cmd_run(args, claude):
    _check_numbers(args)
    claude_cmd = _claude(claude)
    definition, _ = bk_agents.load_definition(args.agent, _dirs(args))
    data, sha, base = _load_cases_for(args.agent, args.cases)
    models = _models(args.models)
    reasons = _opt_in_reasons(definition, data)
    if reasons and not args.allow_writes:
        raise UsageError("this bake-off needs --allow-writes because:\n  - " + "\n  - ".join(_safe(r) for r in reasons))
    if args.allow_writes and not reasons:
        raise UsageError("--allow-writes is not needed (the agent is read-only and no case runs a command): there is nothing to allow")
    total = len(data["cases"]) * len(models) * args.runs
    if total > RUN_LIMIT_WITHOUT_YES and not args.yes:
        raise UsageError(f"this bake-off makes {total} agent runs; pass --yes to continue")
    out_path = Path(args.out) if args.out else Path(".claude") / "bakeoff" / f"{definition['name']}.results.json"
    _check_out(out_path)  # before anything is spent
    rule = {"runs": args.runs, "minCaseRuns": args.min_case_runs, "minPassRate": args.min_pass_rate}
    previous, note = _previous_results(out_path, definition["name"], sha, rule)
    if note:
        print(f"note: {note}", file=sys.stderr)
    options = {
        "read_only": bk_agents.is_read_only(definition["tools"]),
        "max_run_usd": args.max_run_usd, "max_total_usd": args.max_total_usd, "timeout": args.timeout,
        "keep_outputs": args.keep_outputs, "judge_model": args.judge_model,
    }
    entries = []
    aborted = None
    try:
        _, aborted = bk_runner.execute(
            claude_cmd, definition, data, base, models, args.runs, options,
            lambda line: print(line, file=sys.stderr), entries=entries,
        )
    except KeyboardInterrupt:
        aborted = "interrupted (Ctrl-C)"
    except (bk_runner.RunnerError, OSError, ValueError) as exc:
        aborted = f"stopped by an error: {_safe(exc)}"
    saved_models = entries
    target = out_path
    if previous is not None and not aborted:
        fresh = {entry["requested"]: entry for entry in entries}
        saved_models = [fresh.pop(old["requested"], old) for old in previous["models"]] + list(fresh.values())
    elif previous is not None:
        target = out_path.with_name(out_path.stem + ".aborted.json")  # never overwrite good results with a partial run
    results = {
        "resultsVersion": RESULTS_VERSION,
        "agent": definition["name"],
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rule": rule,
        "casesSha": sha,
        "caseIds": [case["id"] for case in data["cases"]],
        "allowWrites": bool(args.allow_writes),
        "judgeModel": args.judge_model,
        "currentModel": definition["model"],
        "models": saved_models,
        "aborted": aborted,
        "applied": None,
    }
    _write_json(target, results)
    if aborted:
        if target != out_path:
            kept = f"Your earlier results were left untouched; this partial run is in {target}."
        else:
            kept = f"Partial results were saved to {target}."
        print(f"aborted: {aborted}. {kept} There is no recommendation.", file=sys.stderr)
        return 2
    print(f"done: {sum(len(e['runs']) for e in entries)} runs saved ({len(saved_models)} models in {target}). Next: decide --results <file>")
    return 0


def _report(results, rule):
    summaries = [bk_decide.summarize(entry, results["caseIds"], rule) for entry in results["models"]]
    if results.get("aborted"):
        recommendation = {"kind": "aborted", "model": None, "resolvedId": None, "costFallback": False,
                          "reason": f"the run aborted ({results['aborted']}); there is no recommendation"}
    else:
        recommendation = bk_decide.recommend(summaries, results.get("currentModel"))
    return summaries, recommendation


def cmd_decide(args, claude):
    results = _load_results(args.results)
    rule = dict(bk_decide.DEFAULT_RULE)
    rule.update(results.get("rule") if isinstance(results.get("rule"), dict) else {})
    if args.min_case_runs is not None:
        rule["minCaseRuns"] = args.min_case_runs
    if args.min_pass_rate is not None:
        rule["minPassRate"] = args.min_pass_rate
    _check_rule_numbers(rule["minCaseRuns"], rule["runs"], rule["minPassRate"])
    if not isinstance(results.get("caseIds"), list):
        raise UsageError(f"{args.results} has no caseIds list")
    summaries, recommendation = _report(results, rule)
    if args.json:
        print(json.dumps({"agent": results.get("agent"), "rule": rule, "models": summaries, "recommendation": recommendation,
                          "limits": LIMITS_SENTENCE}, indent=2))
        return 0
    print(f"model-bakeoff: {_safe(results.get('agent'))}  (tested {_safe(results.get('date', '?'))}, current model {_safe(results.get('currentModel') or 'default')})")
    print(f"rule: each case passes in >= {rule['minCaseRuns']} of {rule['runs']} runs, and >= {rule['minPassRate']:.0%} of all runs pass")
    print()
    print(f"{'model':<10}{'resolved id':<30}{'result':<8}{'pass rate':<11}{'mean cost/run':<15}{'weak cases':<14}errors")
    for s in summaries:
        cost = "n/a" if s["meanCost"] is None else f"${s['meanCost']:.4f}"
        weak = ",".join(s["weakCases"]) or "-"
        errors = ",".join(f"{k}:{v}" for k, v in sorted(s["errors"].items())) or "-"
        print(f"{_safe(s['requested']):<10}{_safe(s['resolvedId'] or '?'):<30}{'PASS' if s['passes'] else 'FAIL':<8}{s['passRate']:<11.0%}{cost:<15}{_safe(weak):<14}{_safe(errors)}")
    print()
    if recommendation["kind"] == "change":
        print(f"recommendation: {_safe(recommendation['model'])} ({_safe(recommendation['resolvedId'] or 'id unknown')}): {recommendation['reason']}")
    else:
        print(f"recommendation: {recommendation['reason']}")
    print(LIMITS_SENTENCE)
    return 0


def cmd_resolve(args, claude):
    results = _load_results(args.results) if args.results else None
    recorded = {m["requested"]: m.get("resolvedId") for m in results["models"]} if results else {}
    models = _models(args.models) if args.models else (list(recorded) or _models(DEFAULT_MODELS))
    claude_cmd = _claude(claude)
    resolved = {}
    for model in models:
        resolved[model] = bk_runner.probe(claude_cmd, model) if model in ALIASES else model
    changed = [m for m in models if m in recorded and resolved[m] is not None and recorded[m] is not None and resolved[m] != recorded[m]]
    new = [m for m in models if results is not None and m not in recorded]
    cases_changed = False
    if args.cases and results:
        _, _, sha, _ = bk_cases.load(args.cases)
        cases_changed = sha != results.get("casesSha")
    payload = {"resolved": resolved, "changed": changed, "new": new, "casesChanged": cases_changed}
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    for model, model_id in resolved.items():
        print(f"{_safe(model)} -> {_safe(model_id or 'unknown (the probe failed)')}")
    print("changed since the last run: " + (", ".join(changed) or "none"))
    print("not tested before: " + (", ".join(new) or "none"))
    if args.cases:
        print("cases file changed: " + ("yes (re-run everything)" if cases_changed else "no"))
    return 0


def cmd_edit(args, claude):
    return agent_edit.main([args.command] + args.rest)


# --- parser ------------------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(prog="bakeoff", description="Pick the cheapest model that passes your own test cases for a custom subagent.")
    sub = parser.add_subparsers(dest="command", required=True)

    def agent_opts(p):
        p.add_argument("--agents-dir", action="append", default=[], metavar="DIR", help="agent folder (repeatable; default the project's and your user .claude/agents)")

    def case_opts(p):
        p.add_argument("--agent", required=True)
        p.add_argument("--cases", required=True, metavar="FILE")
        p.add_argument("--models", default=DEFAULT_MODELS, metavar="LIST", help="comma list of aliases or full model ids (default haiku,sonnet,opus)")
        p.add_argument("--runs", type=int, default=3)
        p.add_argument("--max-run-usd", type=float, default=1.0)
        p.add_argument("--max-total-usd", type=float, default=20.0)
        agent_opts(p)

    p = sub.add_parser("agents", help="list custom agents")
    agent_opts(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(handler=cmd_agents)

    p = sub.add_parser("check-cases", help="validate a cases file")
    p.add_argument("--cases", required=True, metavar="FILE")
    p.set_defaults(handler=cmd_check_cases)

    p = sub.add_parser("estimate", help="show how many calls a bake-off makes and what it needs; calls nothing")
    case_opts(p)
    p.set_defaults(handler=cmd_estimate)

    p = sub.add_parser("run", help="run the cases on each model (uses your claude login)")
    case_opts(p)
    p.add_argument("--allow-writes", action="store_true", help="opt in to agents that can write/run commands and to command checks")
    p.add_argument("--yes", action="store_true", help=f"allow more than {RUN_LIMIT_WITHOUT_YES} agent runs")
    p.add_argument("--keep-outputs", action="store_true")
    p.add_argument("--timeout", type=float, default=300.0, metavar="SECONDS")
    p.add_argument("--judge-model", default="opus")
    p.add_argument("--min-case-runs", type=int, default=2)
    p.add_argument("--min-pass-rate", type=float, default=0.9)
    p.add_argument("--out", metavar="FILE")
    p.set_defaults(handler=cmd_run)

    p = sub.add_parser("decide", help="apply the pass rule to a results file")
    p.add_argument("--results", required=True, metavar="FILE")
    p.add_argument("--min-case-runs", type=int)
    p.add_argument("--min-pass-rate", type=float)
    p.add_argument("--json", action="store_true")
    p.set_defaults(handler=cmd_decide)

    p = sub.add_parser("resolve", help="find which model IDs the aliases point to today and what changed")
    p.add_argument("--results", metavar="FILE")
    p.add_argument("--models", metavar="LIST")
    p.add_argument("--cases", metavar="FILE")
    p.add_argument("--json", action="store_true")
    p.set_defaults(handler=cmd_resolve)

    for name in ("plan", "apply", "undo"):
        p = sub.add_parser(name, help=f"{name} the edit of the agent's model field (see agent_edit.py)", add_help=False)
        p.add_argument("rest", nargs=argparse.REMAINDER)
        p.set_defaults(handler=cmd_edit)
    return parser


def main(argv=None, claude=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("plan", "apply", "undo"):
        # The guarded edit is agent_edit.py's own command line; argparse would mangle its options.
        return agent_edit.main(argv)
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args, claude)
    except (UsageError, AgentError, bk_cases.CasesError, bk_runner.RunnerError, OSError, ValueError) as exc:
        print(f"error: {_safe(exc) if not isinstance(exc, UsageError) else exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
