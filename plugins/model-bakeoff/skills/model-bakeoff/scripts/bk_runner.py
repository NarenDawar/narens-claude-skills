"""Run `claude -p` for the bake-off: argument lists, scratch folders, parsing, the judge, caps."""
import json
import os
import shutil
import subprocess
import tempfile

import bk_agents
import bk_cases
from bk_agents import DEFAULT_READ_TOOLS, DEFAULT_WRITE_TOOLS, READ_ONLY_TOOLS

CONSECUTIVE_CLI_FAILURES = 5
MAX_KEPT_OUTPUT = 20000
JUDGE_PROMPT_LIMIT = 8000


class RunnerError(Exception):
    """A problem running claude, reported as one line."""


_COMMON = ["-p", "--output-format", "json", "--no-session-persistence", "--strict-mcp-config", "--permission-prompts", "none"]


def build_argv(claude, model, agents_file, name, read_only, tools, max_run_usd):
    argv = list(claude) + _COMMON + [
        "--agents", str(agents_file), "--agent", name, "--model", model, "--max-budget-usd", f"{max_run_usd:g}",
    ]
    if read_only:
        allowed = [tool for tool in (tools or []) if tool in READ_ONLY_TOOLS] or list(DEFAULT_READ_TOOLS)
        argv += ["--tools", ",".join(allowed), "--permission-mode", "dontAsk"]
    else:
        argv += ["--permission-mode", "acceptEdits", "--allowedTools", ",".join(tools or DEFAULT_WRITE_TOOLS)]
    return argv


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _int(value):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def parse_output(stdout):
    """The fields of a `claude -p --output-format json` result, or None when it has no usable result."""
    text = (stdout or "").strip()
    obj = None
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        for line in reversed(text.splitlines()):
            try:
                obj = json.loads(line)
                break
            except (ValueError, RecursionError):
                continue
    if isinstance(obj, list):
        obj = next((x for x in reversed(obj) if isinstance(x, dict) and x.get("type") == "result"), None)
    if not isinstance(obj, dict):
        return None
    usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
    counts = [_int(usage.get(key)) for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
    models = obj.get("modelUsage") if isinstance(obj.get("modelUsage"), dict) else {}
    resolved = None
    best = -1.0
    for name, detail in models.items():
        cost = _number(detail.get("costUSD")) if isinstance(detail, dict) else None
        if resolved is None or (cost is not None and cost > best):
            resolved = name
            best = cost if cost is not None else best
    result = obj.get("result")
    return {
        "text": result if isinstance(result, str) else "",
        "cost": _number(obj.get("total_cost_usd")),
        "turns": _int(obj.get("num_turns")),
        "durationMs": _int(obj.get("duration_ms")),
        "isError": bool(obj.get("is_error")),
        "subtype": obj.get("subtype") if isinstance(obj.get("subtype"), str) else "",
        "tokens": sum(count for count in counts if count is not None) if any(c is not None for c in counts) else None,
        "resolvedId": resolved,
    }


def classify(info, returncode, timed_out):
    """The kind of failure, or None for a run that produced a usable result."""
    if timed_out:
        return "timeout"
    if info is None:
        return "cli" if returncode != 0 else "no-result"
    subtype = info["subtype"].lower()
    if "budget" in subtype:
        return "budget"
    if "turn" in subtype:
        return "limit"
    if info["isError"] or returncode != 0:
        return "cli"
    return None


def _kill_tree(proc):
    if os.name == "nt":
        if proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                proc.kill()
        return
    import signal
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        if proc.poll() is None:
            proc.kill()


def run_claude(argv, prompt, cwd, timeout):
    """(stdout, returncode, timed_out). The prompt goes on stdin; the process tree is stopped on timeout."""
    kwargs = {} if os.name == "nt" else {"start_new_session": True}
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(cwd), **kwargs)
    except OSError as exc:
        raise RunnerError(f"cannot start claude: {exc.strerror or exc}") from None
    try:
        out, _ = proc.communicate(prompt.encode("utf-8"), timeout=timeout)
        return out.decode("utf-8", errors="replace"), proc.returncode, False
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return "", None, True


def probe(claude, alias, timeout=120):
    """The model ID an alias resolves to today (one trivial call, no tools), or None."""
    argv = list(claude) + _COMMON + ["--tools", "", "--model", alias]
    scratch = tempfile.mkdtemp(prefix="bakeoff-probe-")
    try:
        out, code, timed_out = run_claude(argv, "Reply with the word ok.", scratch, timeout)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    info = parse_output(out) if not timed_out else None
    if info is None or classify(info, code, timed_out) is not None:
        return None
    return info["resolvedId"]


def judge(claude, model, rubric, prompt, output, timeout):
    """(passed, reason, cost): a separate model call that grades one output against a rubric."""
    text = (
        "You are grading an AI agent's answer against a rubric. Be strict and literal.\n\n"
        f"The task the agent was given:\n{prompt[:JUDGE_PROMPT_LIMIT]}\n\n"
        f"The rubric:\n{rubric[:JUDGE_PROMPT_LIMIT]}\n\n"
        f"The agent's answer:\n{output[:JUDGE_PROMPT_LIMIT]}\n\n"
        'Reply with exactly one JSON object and nothing else: {"pass": true or false, "reason": "one short sentence"}.'
    )
    argv = list(claude) + _COMMON + ["--tools", "", "--model", model]
    scratch = tempfile.mkdtemp(prefix="bakeoff-judge-")
    try:
        out, code, timed_out = run_claude(argv, text, scratch, timeout)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    info = parse_output(out) if not timed_out else None
    if info is None or classify(info, code, timed_out) is not None:
        return None, "the judge call failed", info["cost"] if info else None
    verdict = bk_cases.first_json_object(info["text"])
    if verdict is None or not isinstance(verdict.get("pass"), bool):
        return None, "the judge reply was not understood", info["cost"]
    return verdict["pass"], " ".join(str(verdict.get("reason", "")).split())[:200], info["cost"]


def _one_run(claude, definition, case, base_dir, model, number, agents_file, options):
    scratch = tempfile.mkdtemp(prefix="bakeoff-run-")
    record = {
        "case": case["id"], "run": number, "passed": False, "kind": None, "checks": [], "judge": None,
        "cost": None, "judgeCost": None, "tokens": None, "turns": None, "durationMs": None, "resolvedId": None,
    }
    try:
        bk_cases.copy_fixture(base_dir, case, scratch)
        argv = build_argv(claude, model, agents_file, definition["name"], options["read_only"], definition["tools"], options["max_run_usd"])
        out, code, timed_out = run_claude(argv, case["prompt"], scratch, options["timeout"])
        info = parse_output(out) if not timed_out else None
        record["kind"] = classify(info, code, timed_out)
        if info is not None:
            record.update(cost=info["cost"], tokens=info["tokens"], turns=info["turns"], durationMs=info["durationMs"], resolvedId=info["resolvedId"])
        if record["kind"] is not None:
            return record
        output = info["text"]
        record["checks"] = bk_cases.evaluate(case, output, scratch)
        passed = all(check["passed"] for check in record["checks"])
        if case.get("rubric"):
            verdict, reason, judge_cost = judge(claude, options["judge_model"], case["rubric"], case["prompt"], output, options["timeout"])
            record["judgeCost"] = judge_cost
            if verdict is None:
                record["kind"] = "judge"
                passed = False
            else:
                record["judge"] = {"passed": verdict, "reason": reason}
                passed = passed and verdict
        record["passed"] = passed
        if options["keep_outputs"]:
            record["output"] = output[:MAX_KEPT_OUTPUT]
        return record
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def execute(claude, definition, cases_data, base_dir, models, runs, options, log):
    """Run every model x case x run. Returns (entries, abort_reason or None); entries hold what ran."""
    meta = tempfile.mkdtemp(prefix="bakeoff-meta-")
    agents_file = os.path.join(meta, "agents.json")
    entries = []
    spent = 0.0
    streak = 0
    try:
        with open(agents_file, "w", encoding="utf-8") as handle:
            handle.write(bk_agents.agents_json(definition))
        for model in models:
            entry = {"requested": model, "resolvedId": None, "runs": []}
            entries.append(entry)
            for case in cases_data["cases"]:
                for number in range(1, runs + 1):
                    if spent >= options["max_total_usd"]:
                        return entries, f"the total spend cap of ${options['max_total_usd']:g} was reached after {spent:.2f} dollars"
                    record = _one_run(claude, definition, case, base_dir, model, number, agents_file, options)
                    entry["runs"].append(record)
                    entry["resolvedId"] = entry["resolvedId"] or record["resolvedId"]
                    spent += (record["cost"] or 0.0) + (record["judgeCost"] or 0.0)
                    if record["kind"] in ("cli", "no-result"):
                        streak += 1
                    else:
                        streak = 0
                    log(f"{model} {case['id']} run {number}: {'pass' if record['passed'] else 'fail'}" + (f" ({record['kind']})" if record["kind"] else ""))
                    if streak >= CONSECUTIVE_CLI_FAILURES:
                        return entries, f"claude failed {CONSECUTIVE_CLI_FAILURES} runs in a row (not a check failure); stopped. Check that claude is installed and logged in"
        return entries, None
    finally:
        shutil.rmtree(meta, ignore_errors=True)
