#!/usr/bin/env python3
"""agent_edit: change `model` (and `effort`) in the frontmatter of custom Claude Code agent files.

    python agent_edit.py list  [--agents-dir DIR ...]
    python agent_edit.py plan  --agent NAME --model MODEL [--effort LEVEL] [--agents-dir DIR ...]
    python agent_edit.py apply --agent NAME --model MODEL [--effort LEVEL] [--agents-dir DIR ...] [--backup-dir DIR]
    python agent_edit.py undo  --agent NAME [--backup-dir DIR]

Only the `model` and `effort` lines inside the leading --- block are touched. A file that cannot be
parsed safely is refused and nothing is written. Standard library only.
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MODEL_ALIASES = ("sonnet", "opus", "haiku", "fable", "inherit")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_MODEL_ID = re.compile(r"^claude-[a-z0-9][a-z0-9.\-]*[a-z0-9]\Z")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_VALUE = re.compile(
    r"""^(?P<key>model|effort)(?P<sep>\s*:\s*)(?P<value>"[^"]*"|'[^']*'|[^\s#"'|>][^\s#]*)(?P<rest>\s*(?:\#.*)?)$"""
)


class AgentError(Exception):
    """A problem the user can fix; reported as `error: ...` with exit 2."""


def validate_model(value):
    if value in MODEL_ALIASES or (isinstance(value, str) and _MODEL_ID.match(value)):
        return value
    raise AgentError(f"model {value!r} is not an alias ({', '.join(MODEL_ALIASES)}) or a full model id (claude-...)")


def validate_effort(value):
    if value in EFFORT_LEVELS:
        return value
    raise AgentError(f"effort {value!r} is not one of {', '.join(EFFORT_LEVELS)}")


# --- frontmatter ---------------------------------------------------------------------------

def _split(text):
    bom = text.startswith("\ufeff")
    body = text[1:] if bom else text
    lines = re.findall(r"[^\n]*\n|[^\n]+", body)
    if not lines or lines[0].rstrip() != "---":
        raise AgentError("the file has no frontmatter (it must start with ---)")
    for index in range(1, len(lines)):
        if lines[index].rstrip() == "---":
            return bom, lines, index
    raise AgentError("the frontmatter is not closed (no closing ---)")


def _find(lines, close, key):
    hits = [i for i in range(1, close) if re.match(rf"{key}\s*:", lines[i])]
    if len(hits) > 1:
        raise AgentError(f"the frontmatter has more than one {key}: line")
    return hits[0] if hits else None


def _ending(line):
    return line[len(line.rstrip("\r\n")):]


def edit_text(text, model=None, effort=None):
    bom, lines, close = _split(text)
    for key, value in (("model", model), ("effort", effort)):
        if value is None:
            continue
        index = _find(lines, close, key)
        if index is None:
            newline = _ending(lines[close - 1]) or "\n"
            lines.insert(close, f"{key}: {value}{newline}")
            close += 1
            continue
        raw = lines[index]
        match = _VALUE.match(raw.rstrip("\r\n"))
        if not match or match["key"] != key:
            raise AgentError(f"the {key}: value is empty, multi-line or unusual; edit it by hand")
        lines[index] = f"{match['key']}{match['sep']}{value}{match['rest']}{_ending(raw)}"
    return ("\ufeff" if bom else "") + "".join(lines)


def current_values(text):
    _, lines, close = _split(text)
    found = {"name": None, "model": None, "effort": None}
    for key in found:
        index = _find(lines, close, key) if key != "name" else next(
            (i for i in range(1, close) if re.match(r"name\s*:", lines[i])), None)
        if index is None:
            continue
        match = re.match(rf"^{key}\s*:\s*(?P<v>.*?)\s*$", lines[index].rstrip("\r\n"))
        value = re.sub(r"\s+#.*$", "", match["v"]).strip().strip("\"'") if match else ""
        found[key] = value or None
    return found


# --- finding agents ------------------------------------------------------------------------

def _read(path):
    try:
        return Path(path).read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise AgentError(f"{path} is not valid UTF-8") from None


def list_agents(dirs):
    rows = []
    for directory in dirs:
        directory = Path(directory)
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            row = {"name": path.stem, "path": str(path), "model": None, "effort": None}
            try:
                values = current_values(_read(path))
                row.update(name=values["name"] or path.stem, model=values["model"], effort=values["effort"])
            except AgentError as exc:
                row["problem"] = str(exc)
            rows.append(row)
    return rows


def find_agent(name, dirs):
    rows = list_agents(dirs)
    for row in rows:
        if row["name"] == name or Path(row["path"]).stem == name:
            return Path(row["path"])
    known = ", ".join(sorted({row["name"] for row in rows})) or "none found"
    raise AgentError(f"no custom agent named {name!r} (found: {known}); built-in agent types have no file to edit")


# --- writing, backups, undo ----------------------------------------------------------------

def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _write_atomic(path, data):
    path = Path(path)
    if path.is_symlink():
        path = Path(os.path.realpath(path))
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".agent-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def apply_edit(path, new_text, backup_dir, agent):
    if not _NAME.match(agent):
        raise AgentError(f"{agent!r} is not a usable agent name")
    path, backup_dir = Path(path), Path(backup_dir)
    before = path.read_bytes()
    after = new_text.encode("utf-8")
    if before == after:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir.mkdir(parents=True, exist_ok=True)
    counter = 0
    while any((backup_dir / f"{agent}.{stamp}-{counter:03d}{ext}").exists() for ext in (".json", ".undone")):
        counter += 1  # two applies in the same microsecond still get distinct, ordered names
    backup = backup_dir / f"{agent}.{stamp}-{counter:03d}.bak"
    meta = backup_dir / f"{agent}.{stamp}-{counter:03d}.json"
    backup.write_bytes(before)
    meta.write_bytes((json.dumps({
        "agent": agent, "path": os.path.realpath(path), "backup": backup.name,
        "before_sha256": _sha(before), "after_sha256": _sha(after)}, indent=2) + "\n").encode("utf-8"))
    try:
        _write_atomic(path, after)
    except BaseException:
        for leftover in (backup, meta):
            try:
                leftover.unlink()
            except OSError:
                pass
        raise
    return backup


def undo(agent, backup_dir):
    if not _NAME.match(agent):
        raise AgentError(f"{agent!r} is not a usable agent name")
    backup_dir = Path(backup_dir)
    record = re.compile(re.escape(agent) + r"\.\d{8}T\d{12}Z-\d{3}\.json")  # exactly this agent's records
    metas = sorted(p for p in backup_dir.iterdir() if record.fullmatch(p.name)) if backup_dir.is_dir() else []
    if not metas:
        raise AgentError(f"no backup found for {agent!r} in {backup_dir}")
    meta_path = metas[-1]
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        target, backup_name, after_sha = Path(meta["path"]), meta["backup"], meta["after_sha256"]
        if meta["agent"] != agent:
            raise KeyError("agent")
    except (ValueError, KeyError, TypeError):
        raise AgentError(f"the backup record {meta_path} is damaged; restore the file by hand from "
                         f"the .bak next to it") from None
    if not target.exists() or _sha(target.read_bytes()) != after_sha:
        raise AgentError(f"{target} has changed since the apply; not restoring over your later edits "
                         f"(the original is in {backup_dir / backup_name})")
    _write_atomic(target, (backup_dir / backup_name).read_bytes())
    meta_path.rename(meta_path.with_suffix(".undone"))
    return target


# --- command line --------------------------------------------------------------------------

def default_dirs():
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return [Path.cwd() / ".claude" / "agents", (Path(base) if base else Path.home() / ".claude") / "agents"]


def _quote(text):
    """Quote one argument for the shell the user is most likely running."""
    return f'"{text}"' if os.name == "nt" else shlex.quote(text)


def default_backup_dir():
    return Path.cwd() / ".claude" / "agent-edit-backups"


def _resolve(args):
    dirs = [Path(d) for d in args.agents_dir] if args.agents_dir else default_dirs()
    return find_agent(args.agent, dirs)


def _new_text(args, path):
    if args.model is None and args.effort is None:
        raise AgentError("pass --model and/or --effort")
    model = validate_model(args.model) if args.model is not None else None
    effort = validate_effort(args.effort) if args.effort is not None else None
    return edit_text(_read(path), model=model, effort=effort)


def cmd_list(args):
    dirs = [Path(d) for d in args.agents_dir] if args.agents_dir else default_dirs()
    rows = list_agents(dirs)
    if not rows:
        print("No custom agents found.")
    for row in rows:
        detail = f"  (problem: {row['problem']})" if row.get("problem") else ""
        print(f"{row['name']:<28} model={row['model'] or '-':<12} effort={row['effort'] or '-':<8} {row['path']}{detail}")
    return 0


def cmd_plan(args):
    path = _resolve(args)
    before, after = _read(path), _new_text(args, path)
    if before == after:
        print("No changes: the file already has these values.")
        return 0
    print("".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                       fromfile=str(path), tofile=f"{path} (after)")), end="")
    return 0


def cmd_apply(args):
    path = _resolve(args)
    backup_dir = Path(args.backup_dir) if args.backup_dir else default_backup_dir()
    backup = apply_edit(path, _new_text(args, path), backup_dir, args.agent)
    if backup is None:
        print("No changes: the file already has these values.")
    else:
        undo_line = (f"{_quote(sys.executable)} {_quote(str(Path(__file__).resolve()))} "
                     f"undo --agent {args.agent} --backup-dir {_quote(str(backup_dir.resolve()))}")
        print(f"Updated {path}\nBackup: {backup}\nTo undo: {undo_line}")
    return 0


def cmd_undo(args):
    print(f"Restored {undo(args.agent, args.backup_dir or default_backup_dir())}")
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="agent_edit.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    lister = sub.add_parser("list", help="custom agents and their current model and effort")
    lister.add_argument("--agents-dir", action="append")
    lister.set_defaults(func=cmd_list)
    for name, func in (("plan", cmd_plan), ("apply", cmd_apply)):
        p = sub.add_parser(name)
        p.add_argument("--agent", required=True)
        p.add_argument("--model")
        p.add_argument("--effort")
        p.add_argument("--agents-dir", action="append")
        if name == "apply":
            p.add_argument("--backup-dir")
        p.set_defaults(func=func)
    undoer = sub.add_parser("undo", help="restore the most recent backup of an agent")
    undoer.add_argument("--agent", required=True)
    undoer.add_argument("--backup-dir")
    undoer.set_defaults(func=cmd_undo)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (AgentError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
