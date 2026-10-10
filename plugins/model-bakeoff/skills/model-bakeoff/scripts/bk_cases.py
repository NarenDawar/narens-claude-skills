"""Cases files for the bake-off: validation, checks and fixtures. Standard library only."""
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

CASES_VERSION = 1
CHECK_FIELDS = {
    "contains": {"type", "value", "ignoreCase"},
    "not_contains": {"type", "value", "ignoreCase"},
    "regex": {"type", "value"},
    "json": {"type", "required"},
    "command": {"type", "argv", "expectExit", "timeoutSeconds"},
}
MAX_PATTERN = 500
MAX_FIXTURE_FILES = 200
MAX_FIXTURE_BYTES = 5 * 1024 * 1024
MAX_OUTPUT_EVAL = 200000
DEFAULT_COMMAND_TIMEOUT = 120


class CasesError(Exception):
    """A problem with a cases file, reported as one line."""


def _is_text(value):
    return isinstance(value, str) and value.strip() != ""


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def fixture_problem(base_dir, rel):
    """Why a fixture folder cannot be used, or None."""
    path = Path(rel) if isinstance(rel, str) else Path("")
    if not isinstance(rel, str) or rel.strip() == "" or path.is_absolute() or ".." in path.parts:
        return "fixture must be a relative path inside the cases file's folder"
    target = Path(base_dir) / path
    if not target.is_dir():
        return f"fixture folder {rel!r} does not exist"
    base = Path(base_dir).resolve()
    resolved = target.resolve()
    if resolved != base and base not in resolved.parents:
        return "fixture must be a relative path inside the cases file's folder"
    count = 0
    size = 0
    for root, dirs, files in os.walk(target):
        for name in dirs + files:
            if (Path(root) / name).is_symlink():
                return f"fixture {rel!r} contains a symlink, which is not allowed"
        for name in files:
            count += 1
            size += (Path(root) / name).stat().st_size
            if count > MAX_FIXTURE_FILES:
                return f"fixture {rel!r} has more than {MAX_FIXTURE_FILES} files"
            if size > MAX_FIXTURE_BYTES:
                return f"fixture {rel!r} is larger than 5 MiB"
    return None


def _check_problems(check):
    if not isinstance(check, dict):
        return ["must be an object"]
    kind = check.get("type")
    if kind not in CHECK_FIELDS:
        return [f"unknown check type {kind!r}"]
    found = [f"unknown field {key!r}" for key in check if key not in CHECK_FIELDS[kind]]
    if kind in ("contains", "not_contains", "regex") and not _is_text(check.get("value")):
        found.append("value must be a non-empty string")
    if kind in ("contains", "not_contains") and "ignoreCase" in check and not isinstance(check["ignoreCase"], bool):
        found.append("ignoreCase must be true or false")
    if kind == "regex" and _is_text(check.get("value")):
        if len(check["value"]) > MAX_PATTERN:
            found.append(f"regex is longer than {MAX_PATTERN} characters")
        else:
            try:
                re.compile(check["value"])
            except re.error as exc:
                found.append(f"regex does not compile ({exc})")
    if kind == "json":
        required = check.get("required")
        if not (isinstance(required, list) and all(isinstance(item, str) for item in required)):
            found.append("required must be a list of strings")
    if kind == "command":
        argv = check.get("argv")
        if not (isinstance(argv, list) and argv and all(_is_text(item) for item in argv)):
            found.append("argv must be a non-empty list of strings")
        if "expectExit" in check and not _is_int(check["expectExit"]):
            found.append("expectExit must be an integer")
        if "timeoutSeconds" in check:
            seconds = check["timeoutSeconds"]
            if not (isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and 1 <= seconds <= 600):
                found.append("timeoutSeconds must be between 1 and 600")
    return found


def problems(data, base_dir):
    """Every problem in a cases file, one line each (empty when it is valid)."""
    if not isinstance(data, dict):
        return ["the cases file must be a JSON object"]
    found = [f"unknown field {key!r} in the cases file" for key in data if key not in ("casesVersion", "agent", "cases")]
    if type(data.get("casesVersion")) is not int or data["casesVersion"] != CASES_VERSION:
        found.append(f"casesVersion must be {CASES_VERSION}")
    if not _is_text(data.get("agent")):
        found.append("agent must be the agent's name (a non-empty string)")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        found.append("cases must be a non-empty list")
        return found
    seen = set()
    for index, item in enumerate(cases):
        if not isinstance(item, dict):
            found.append(f"case {index}: must be an object")
            continue
        label = f"case {item['id']!r}" if _is_text(item.get("id")) else f"case {index}"
        for key in item:
            if key not in ("id", "prompt", "fixture", "checks", "rubric"):
                found.append(f"{label}: unknown field {key!r}")
        if not _is_text(item.get("id")):
            found.append(f"{label}: id must be a non-empty string")
        elif item["id"] in seen:
            found.append(f"{label}: duplicate id {item['id']!r}")
        else:
            seen.add(item["id"])
        if not _is_text(item.get("prompt")):
            found.append(f"{label}: prompt must be a non-empty string")
        if "rubric" in item and not _is_text(item["rubric"]):
            found.append(f"{label}: rubric must be a non-empty string")
        if "fixture" in item:
            problem = fixture_problem(base_dir, item["fixture"])
            if problem:
                found.append(f"{label}: {problem}")
        checks = item.get("checks")
        if not isinstance(checks, list) or not checks:
            found.append(f"{label}: checks must be a non-empty list")
            continue
        for number, check in enumerate(checks):
            for problem in _check_problems(check):
                found.append(f"{label}: checks[{number}]: {problem}")
    return [" ".join(line.split()) for line in found]


def load(path):
    """(data or None, problems, sha256 of the file, folder). Never raises for a bad file."""
    path = Path(path)
    base = path.resolve().parent
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, [f"cannot read {path}: {exc.strerror or exc}"], "", base
    sha = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, [f"{path} is not UTF-8 text"], sha, base
    try:
        data = json.loads(text)
    except RecursionError:
        return None, [f"{path} is not valid JSON (nested too deeply)"], sha, base
    except ValueError as exc:
        return None, [f"{path} is not valid JSON ({exc})"], sha, base
    found = problems(data, base)
    return (data if not found else None), found, sha, base


def require(path):
    data, found, sha, base = load(path)
    if found:
        extra = f" (and {len(found) - 1} more)" if len(found) > 1 else ""
        raise CasesError(found[0] + extra)
    return data, sha, base


def needs_execute(case):
    return any(isinstance(c, dict) and c.get("type") == "command" for c in case.get("checks", []))


def first_json_object(text):
    """The first JSON object found in `text` (the whole text, a fenced block or inside prose)."""
    decoder = json.JSONDecoder()
    index = 0
    while index < len(text):
        if text[index] == "{":
            try:
                value, end = decoder.raw_decode(text, index)
            except (ValueError, RecursionError):
                index += 1
                continue
            if isinstance(value, dict):
                return value
            index = end
        else:
            index += 1
    return None


def run_check(check, output, scratch):
    """(passed, short detail) for one check on the agent's output."""
    kind = check["type"]
    text = output[:MAX_OUTPUT_EVAL]
    if kind in ("contains", "not_contains"):
        ignore = check.get("ignoreCase", True)
        haystack, needle = (text.lower(), check["value"].lower()) if ignore else (text, check["value"])
        found = needle in haystack
        return (found if kind == "contains" else not found), ("found" if found else "not found")
    if kind == "regex":
        found = re.search(check["value"], text) is not None
        return found, ("matched" if found else "no match")
    if kind == "json":
        obj = first_json_object(text)
        if obj is None:
            return False, "no JSON object in the output"
        missing = [key for key in check["required"] if key not in obj]
        return (not missing), ("has every required key" if not missing else "missing keys: " + ", ".join(missing))
    argv = list(check["argv"])
    program = shutil.which(argv[0])
    if program is None:
        return False, f"cannot find {argv[0]}"
    try:
        done = subprocess.run(
            [program] + argv[1:], cwd=str(scratch), capture_output=True,
            timeout=check.get("timeoutSeconds", DEFAULT_COMMAND_TIMEOUT),
        )
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except OSError as exc:
        return False, f"cannot run: {exc.strerror or exc}"
    expected = check.get("expectExit", 0)
    return done.returncode == expected, f"exit {done.returncode}"


def evaluate(case, output, scratch):
    results = []
    for check in case["checks"]:
        passed, detail = run_check(check, output, scratch)
        results.append({"type": check["type"], "passed": bool(passed), "detail": detail})
    return results


def copy_fixture(base_dir, case, scratch):
    rel = case.get("fixture")
    if rel:
        shutil.copytree(Path(base_dir) / rel, scratch, dirs_exist_ok=True, symlinks=False)
