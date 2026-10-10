"""Read custom agent files for the bake-off. Standard library only."""
import json
import re
from pathlib import Path

import agent_edit
from agent_edit import AgentError

READ_ONLY_TOOLS = frozenset({"Read", "Grep", "Glob", "LS", "NotebookRead"})
DEFAULT_READ_TOOLS = ("Read", "Grep", "Glob")
DEFAULT_WRITE_TOOLS = ("Read", "Grep", "Glob", "Edit", "Write", "Bash")
_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$")
_BLOCK = ("|", ">", "|-", ">-", "|+", ">+")


def _unquote(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_definition(text, fallback_name=""):
    """name, description, tools, model and prompt of an agent file; single-line values only."""
    _, lines, close = agent_edit._split(text)
    fields = {}
    for raw in lines[1:close]:
        line = raw.rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t-":
            raise AgentError("the frontmatter has a multi-line or list value; the bake-off cannot read it safely")
        match = _KEY.match(line)
        if not match:
            raise AgentError(f"cannot read the frontmatter line {line[:40]!r}")
        value = re.sub(r"\s+#.*$", "", match.group(2)).strip()
        if value in _BLOCK:
            raise AgentError("the frontmatter has a multi-line value; the bake-off cannot read it safely")
        fields[match.group(1)] = _unquote(value)
    tools = [part.strip() for part in fields.get("tools", "").split(",") if part.strip()]
    return {
        "name": fields.get("name") or fallback_name,
        "description": fields.get("description") or "",
        "tools": tools or None,
        "model": fields.get("model") or None,
        "prompt": "".join(lines[close + 1:]).strip(),
    }


def is_read_only(tools):
    """True only when the agent lists tools and every one of them only reads."""
    return bool(tools) and all(tool in READ_ONLY_TOOLS for tool in tools)


def agents_json(definition):
    """The `--agents` JSON for one agent. The model is deliberately absent: --model decides."""
    entry = {"description": definition["description"] or definition["name"], "prompt": definition["prompt"]}
    if definition["tools"]:
        entry["tools"] = list(definition["tools"])
    return json.dumps({definition["name"]: entry})


def list_rows(dirs):
    rows = []
    for row in agent_edit.list_agents(dirs):
        info = {"name": row["name"], "path": row["path"], "model": row["model"], "tools": None, "readOnly": False}
        if "problem" in row:
            info["problem"] = row["problem"]
        else:
            try:
                definition = parse_definition(agent_edit._read(row["path"]), Path(row["path"]).stem)
                info["tools"] = definition["tools"]
                info["readOnly"] = is_read_only(definition["tools"])
            except AgentError as exc:
                info["problem"] = str(exc)
        rows.append(info)
    return rows


def load_definition(name, dirs):
    path = agent_edit.find_agent(name, dirs)
    return parse_definition(agent_edit._read(path), path.stem), path
