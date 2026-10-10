"""A stand-in for `claude -p` in the bake-off tests. Behavior comes from a JSON script file named
by $FAKE_CLAUDE_SCRIPT: {"ids": {...}, "default": {...}, "rules": [{"model", "prompt_contains", ...}],
"log": "path"}. A rule/default may set: result, cost (omit for none), subtype, is_error, turns,
resolved, exit, sleep, write_files {name: text}, stdout_raw."""
import json
import os
import sys
import time

args = sys.argv[1:]
prompt = sys.stdin.buffer.read().decode("utf-8")
script = json.load(open(os.environ["FAKE_CLAUDE_SCRIPT"], encoding="utf-8"))


def option(name):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None


model = option("--model") or ""
if script.get("log"):
    agents_path = option("--agents")
    agents_text = open(agents_path, encoding="utf-8").read() if agents_path and os.path.exists(agents_path) else None
    with open(script["log"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": args, "cwd": os.getcwd(), "stdin": prompt, "files": sorted(os.listdir(".")), "agents_file": agents_text}) + "\n")

chosen = script.get("default", {})
for rule in script.get("rules", []):
    if rule.get("model") and rule["model"] != model:
        continue
    if rule.get("prompt_contains") and rule["prompt_contains"] not in prompt:
        continue
    chosen = rule
    break

time.sleep(chosen.get("sleep", 0))
for name, content in (chosen.get("write_files") or {}).items():
    with open(name, "w", encoding="utf-8") as handle:
        handle.write(content)

if chosen.get("stdout_raw") is not None:
    sys.stdout.write(chosen["stdout_raw"])
    sys.exit(chosen.get("exit", 0))

ids = script.get("ids", {"haiku": "claude-haiku-4-5-20251001", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"})
resolved = chosen.get("resolved") or ids.get(model, model)
cost = chosen.get("cost")
out = {
    "type": "result",
    "subtype": chosen.get("subtype", "success"),
    "is_error": chosen.get("is_error", False),
    "duration_ms": 123,
    "num_turns": chosen.get("turns", 2),
    "result": chosen.get("result", ""),
    "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5},
    "modelUsage": {resolved: ({"costUSD": cost} if cost is not None else {})},
}
if cost is not None:
    out["total_cost_usd"] = cost
sys.stdout.write(json.dumps(out))
sys.exit(chosen.get("exit", 0))
