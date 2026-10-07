#!/usr/bin/env python3
"""audit: attribute Claude Code token usage to the main thread and to each subagent type.

    python audit.py report   [--project PATH | --all] [--since D] [--until D] [--projects-dir DIR]
                             [--rates FILE] [--json] [--show-descriptions]
    python audit.py snapshot --out FILE [same selectors]
    python audit.py compare  --snapshot FILE [same selectors]

Read-only (snapshot writes only its --out file). Standard library only. Prompts, responses and
file contents are never printed.
"""
import argparse
import json
import os
import re
import statistics
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

KINDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
MIN_SPAWNS = 5
STALE_DAYS = 90
SYNTHETIC = "<synthetic>"


class AuditError(Exception):
    """A problem the user can fix; reported as `error: ...` with exit 2."""


# --- locating transcripts ------------------------------------------------------------------

def projects_dir(arg=None):
    if arg:
        return Path(arg)
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def slug_for(path):
    """Claude Code names a project folder after its path with every non-alphanumeric character as -."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(str(path)))


def project_dirs(root, project=None, everything=False):
    root = Path(root)
    if not root.is_dir():
        raise AuditError(f"transcripts directory not found: {root}")
    if everything:
        return sorted(p for p in root.iterdir() if p.is_dir())
    target = root / slug_for(project or os.getcwd())
    return [target] if target.is_dir() else []


# --- reading transcripts -------------------------------------------------------------------

def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def usage_counts(usage):
    breakdown = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    write_5m = _count(breakdown.get("ephemeral_5m_input_tokens"))
    write_1h = _count(breakdown.get("ephemeral_1h_input_tokens"))
    total = _count(usage.get("cache_creation_input_tokens"))
    if write_5m + write_1h < total:
        write_5m += total - (write_5m + write_1h)
    return {
        "input": _count(usage.get("input_tokens")),
        "output": _count(usage.get("output_tokens")),
        "cache_read": _count(usage.get("cache_read_input_tokens")),
        "cache_write_5m": write_5m,
        "cache_write_1h": write_1h,
    }


def new_quality():
    return {"malformed_lines": 0, "lines_without_usage": 0, "unreadable_files": 0, "subagents_without_meta": 0}


def read_messages(path, quality):
    """(first timestamp, messages) for one transcript file.

    A message id can span several lines (one per content block, usage still growing), so each id
    is counted once, with the largest value seen for every token kind.
    """
    by_id, order, start, anonymous = {}, [], None, 0
    try:
        handle = open(path, "rb")
    except OSError:
        quality["unreadable_files"] += 1
        return None, []
    with handle:
        for raw in handle:
            if not raw.strip():
                continue
            try:
                record = json.loads(raw.decode("utf-8-sig"))
            except ValueError:
                quality["malformed_lines"] += 1
                continue
            if not isinstance(record, dict):
                quality["malformed_lines"] += 1
                continue
            stamp = record.get("timestamp")
            if isinstance(stamp, str) and (start is None or stamp < start):
                start = stamp
            message = record.get("message")
            if record.get("type") != "assistant" or not isinstance(message, dict):
                continue
            model = message.get("model")
            if model == SYNTHETIC:
                continue
            if not isinstance(message.get("usage"), dict):
                quality["lines_without_usage"] += 1
                continue
            mid = message.get("id")
            real_id = isinstance(mid, str) and bool(mid)
            if not real_id:
                anonymous += 1
                mid = f"line-{anonymous}"
            tokens = usage_counts(message["usage"])
            if mid in by_id:
                seen = by_id[mid]["tokens"]
                for kind in KINDS:
                    seen[kind] = max(seen[kind], tokens[kind])
                first_stamp = by_id[mid]["timestamp"]
                if isinstance(stamp, str) and (first_stamp is None or stamp < first_stamp):
                    by_id[mid]["timestamp"] = stamp
            else:
                by_id[mid] = {"id": mid if real_id else None,
                              "model": model if isinstance(model, str) and model else "unknown", "tokens": tokens,
                              "timestamp": stamp if isinstance(stamp, str) else None}
                order.append(mid)
    return start, [by_id[i] for i in order]


def read_meta(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _unit(kind, path, description, quality):
    start, messages = read_messages(path, quality)
    if not messages or start is None:
        return None
    return {"type": kind, "start": start, "messages": messages, "description": description}


def _dedupe_across_files(units):
    """A resumed session copies earlier history into a new file: keep each message id once, in the
    file that started first (ties go to the earlier file in scan order)."""
    owner = {}
    for index, unit in enumerate(units):
        for message in unit["messages"]:
            if message["id"] is None:
                continue
            best = owner.get(message["id"])
            if best is None or (unit["start"], index) < best:
                owner[message["id"]] = (unit["start"], index)
    kept = []
    for index, unit in enumerate(units):
        messages = unit["messages"]
        unit["messages"] = [m for m in messages
                            if m["id"] is None or owner[m["id"]] == (unit["start"], index)]
        if unit["messages"]:
            # Copied history must not date new work before --since or a snapshot cutoff.
            # Preserve ordinary file starts (including their first user message) and
            # the existing fallback when no retained assistant has a timestamp.
            if len(unit["messages"]) < len(messages):
                stamps = [m["timestamp"] for m in unit["messages"] if m["timestamp"] is not None]
                if stamps:
                    unit["start"] = min(stamps)
            kept.append(unit)
    return kept


def load_units(dirs, since, until, quality):
    """Every main session and every subagent run, as units attributed to a type."""
    units = []
    for pdir in dirs:
        for session in sorted(pdir.glob("*.jsonl")):
            unit = _unit("main", session, "", quality)
            if unit:
                units.append(unit)
        for subdir in sorted(p / "subagents" for p in pdir.iterdir() if (p / "subagents").is_dir()):
            for agent in sorted(subdir.glob("agent-*.jsonl")):
                meta = read_meta(agent.with_suffix(".meta.json"))
                kind = meta.get("agentType") if meta else None
                if not isinstance(kind, str) or not kind:
                    kind = "unknown"
                    quality["subagents_without_meta"] += 1
                description = meta.get("description", "") if meta else ""
                unit = _unit(kind, agent, description if isinstance(description, str) else "", quality)
                if unit:
                    units.append(unit)
    units = _dedupe_across_files(units)
    return [u for u in units
            if not ((since and u["start"][:10] < since) or (until and u["start"][:10] > until))]


# --- rates and cost ------------------------------------------------------------------------

def load_rates(path=None):
    path = Path(path) if path else Path(__file__).resolve().parent.parent / "rates.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise AuditError(f"cannot read the rates file {path} ({exc.strerror or exc})") from None
    except ValueError as exc:
        raise AuditError(f"{path} is not valid JSON ({exc})") from None
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict) or not isinstance(data.get("as_of"), str):
        raise AuditError(f'{path} needs "as_of" and "models"')
    for name, rate in models.items():
        if not isinstance(rate, dict) or any(
            not isinstance(rate.get(k), (int, float)) or isinstance(rate.get(k), bool) or rate[k] < 0 for k in KINDS
        ):
            raise AuditError(f"{path}: model {name!r} needs a non-negative number for each of {', '.join(KINDS)}")
    return data


def rate_for(rates, model):
    models = rates["models"]
    if model in models:
        return models[model]
    # Only a dated build of a listed model ("<id>-20251001") shares its price. A newer point release
    # such as claude-opus-5-6 must not inherit claude-opus-5's price: it gets "no rate" instead.
    for name in sorted(models, key=len, reverse=True):
        if re.fullmatch(re.escape(name) + r"-\d{8}", model):
            return models[name]
    return None


def cost_of(tokens, rate):
    return sum(tokens[kind] * rate[kind] for kind in KINDS) / 1_000_000


def rates_stale(rates, today=None):
    try:
        as_of = date.fromisoformat(rates["as_of"])
    except ValueError:
        return True
    return ((today or date.today()) - as_of).days > STALE_DAYS


# --- one unit's numbers --------------------------------------------------------------------

def summarize(unit, rates):
    models, per_model = Counter(), {}
    for message in unit["messages"]:
        models[message["model"]] += 1
        slot = per_model.setdefault(message["model"], dict.fromkeys(KINDS, 0))
        for kind in KINDS:
            slot[kind] += message["tokens"][kind]
    total, cost, priced = dict.fromkeys(KINDS, 0), 0.0, True
    for model, tokens in per_model.items():
        for kind in KINDS:
            total[kind] += tokens[kind]
        rate = rate_for(rates, model)
        if rate is None:
            priced = False
        else:
            cost += cost_of(tokens, rate)
    first = unit["messages"][0]
    return {
        "type": unit["type"],
        "start": unit["start"],
        "models": models,
        "per_model": per_model,
        "first_model": first["model"],
        "tokens": total,
        "cost": cost if priced else None,
        "fixed": sum(first["tokens"][kind] for kind in
                     ("input", "cache_read", "cache_write_5m", "cache_write_1h")),
        "messages": len(unit["messages"]),
        "description": unit["description"],
    }


# --- the report ----------------------------------------------------------------------------

def build_report(sums, rates, quality, selectors, show_descriptions=False, today=None):
    rows = {}
    for s in sums:
        for model, tokens in s["per_model"].items():
            row = rows.setdefault((s["type"], model), {
                "type": s["type"], "model": model, "spawns": 0, "messages": 0,
                "tokens": dict.fromkeys(KINDS, 0), "fixed": [], "descriptions": set()})
            if model == s["first_model"]:
                row["spawns"] += 1  # a run counts once, on the model it started with
            row["messages"] += s["models"][model]
            for kind in KINDS:
                row["tokens"][kind] += tokens[kind]
            if s["description"]:
                row["descriptions"].add(s["description"])
        rows[(s["type"], s["first_model"])]["fixed"].append(s["fixed"])
    out, unrated, total_cost, total_output, total_tokens = [], {}, 0.0, 0, 0
    for row in rows.values():
        rate = rate_for(rates, row["model"])
        cost = cost_of(row["tokens"], rate) if rate else None
        volume = sum(row["tokens"].values())
        if cost is None:
            unrated[row["model"]] = unrated.get(row["model"], 0) + volume
        else:
            total_cost += cost
        total_output += row["tokens"]["output"]
        total_tokens += volume
        entry = {
            "type": row["type"], "model": row["model"], "spawns": row["spawns"], "messages": row["messages"],
            "tokens": row["tokens"], "cost": cost, "has_rate": rate is not None,
            "fixed_context_median": int(statistics.median(row["fixed"])) if row["fixed"] else None,
        }
        if show_descriptions:
            entry["descriptions"] = sorted(row["descriptions"])[:5]
        out.append(entry)
    for entry in out:
        entry["cost_share"] = entry["cost"] / total_cost if entry["cost"] is not None and total_cost > 0 else None
        entry["output_share"] = entry["tokens"]["output"] / total_output if total_output > 0 else None
    out.sort(key=lambda e: (e["cost"] is None, -(e["cost"] or 0), -sum(e["tokens"].values())))
    days = sorted(s["start"][:10] for s in sums)
    return {
        "selectors": selectors,
        "rates_as_of": rates["as_of"],
        "rates_stale": rates_stale(rates, today),
        "totals": {"cost": total_cost, "output": total_output, "tokens": total_tokens},
        "rows": out,
        "quality": {
            **quality,
            "sessions": sum(1 for s in sums if s["type"] == "main"),
            "subagent_runs": sum(1 for s in sums if s["type"] != "main"),
            "first_day": days[0] if days else None,
            "last_day": days[-1] if days else None,
            "models_without_rate": unrated,
        },
    }


def _money(value):
    return "no rate" if value is None else f"${value:,.2f}"


def _share(value):
    return "-" if value is None else f"{value * 100:.1f}%"


def format_report(report):
    quality = report["quality"]
    if not report["rows"]:
        return "No usage found for this selection.\n"
    lines = [
        f"Subagent tax report: {quality['sessions']} sessions, {quality['subagent_runs']} subagent runs, "
        f"{quality['first_day']} to {quality['last_day']}",
        f"Cost is a proxy: tokens times published API rates as of {report['rates_as_of']}. "
        "Your subscription quota is not measured in dollars.",
        "",
        f"{'type':<22}{'model':<26}{'spawns':>7}{'cost':>11}{'cost%':>8}{'output%':>9}{'fixed ctx/spawn':>17}{'output tok':>12}",
    ]
    for row in report["rows"]:
        fixed = "-" if row["fixed_context_median"] is None else f"{row['fixed_context_median']:,}"
        lines.append(
            f"{row['type'][:21]:<22}{row['model'][:25]:<26}{row['spawns']:>7}{_money(row['cost']):>11}"
            f"{_share(row['cost_share']):>8}{_share(row['output_share']):>9}{fixed:>17}{row['tokens']['output']:>12,}")
        for description in row.get("descriptions", []):
            lines.append(f"    - {description}")
    totals = report["totals"]
    lines += [f"{'total':<48}{'':>7}{_money(totals['cost']):>11}{'':>8}{'':>9}{'':>17}{totals['output']:>12,}", ""]
    notes = []
    if quality["malformed_lines"]:
        notes.append(f"{quality['malformed_lines']} malformed lines skipped")
    if quality["lines_without_usage"]:
        notes.append(f"{quality['lines_without_usage']} assistant lines without usage skipped")
    if quality["unreadable_files"]:
        notes.append(f"{quality['unreadable_files']} unreadable files skipped")
    if quality["subagents_without_meta"]:
        notes.append(f"{quality['subagents_without_meta']} subagent runs without a readable meta file (typed unknown)")
    for model, tokens in sorted(quality["models_without_rate"].items()):
        notes.append(f"{model}: no rate in rates.json, {tokens:,} tokens left out of cost")
    if report["rates_stale"]:
        notes.append(f"rates.json is older than {STALE_DAYS} days; check the prices")
    lines.append("Data quality: " + ("; ".join(notes) if notes else "no problems found"))
    return "\n".join(lines) + "\n"


# --- snapshot and compare ------------------------------------------------------------------

def type_stats(sums):
    groups = {}
    for s in sums:
        groups.setdefault(s["type"], []).append(s)
    stats = {}
    for kind, items in groups.items():
        costs = [s["cost"] for s in items if s["cost"] is not None]
        stats[kind] = {
            "spawns": len(items),
            "models": sorted({model for s in items for model in s["models"]}),
            "fixed_median": statistics.median([s["fixed"] for s in items]),
            "tokens_median": statistics.median([sum(s["tokens"].values()) for s in items]),
            "cost_median": statistics.median(costs) if costs else None,
            "cost_spawns": len(costs),
        }
    return stats


def _stamp(moment=None):
    moment = moment or datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def snapshot_data(sums, rates, selectors, now=None):
    return {"timestamp": now or _stamp(), "rates_as_of": rates["as_of"], "selectors": selectors,
            "types": type_stats(sums)}


def verdict(before, after):
    if before is None:
        return "new since the snapshot; nothing to compare"
    if after is None:
        return "no spawns since the snapshot"
    if before["spawns"] < MIN_SPAWNS or after["spawns"] < MIN_SPAWNS:
        return (f"not enough data (need {MIN_SPAWNS}+ spawns on each side; "
                f"{before['spawns']} before, {after['spawns']} after)")
    if before["models"] == after["models"]:
        return ("model unchanged; transcripts do not record effort, so this cannot show the effect of an "
                "effort-only edit, and any difference is also the mix of tasks")
    key, label = "cost_median", "cost"
    for side in (before, after):  # a cost median must rest on enough priced spawns too
        priced = side.get("cost_spawns", side["spawns"] if side["cost_median"] is not None else 0)
        if side["cost_median"] is None or priced < MIN_SPAWNS:
            key, label = "tokens_median", "tokens"
    if not before[key]:
        return "cannot compare: the earlier median is zero"
    change = (after[key] - before[key]) / before[key] * 100
    word = "fell" if change < 0 else "rose"
    return f"median {label} per spawn {word} {abs(change):.0f}% (different tasks, so a trend, not proof)"


def compare(snapshot, sums, rates):
    try:
        stamp, before = snapshot["timestamp"], snapshot["types"]
        before_rates = snapshot.get("rates_as_of")
    except (KeyError, TypeError):
        raise AuditError("the snapshot file is missing its timestamp or types") from None
    after = type_stats([s for s in sums if s["start"] > stamp])
    note = None
    if before_rates != rates["as_of"]:
        note = (f"rates.json changed since the snapshot ({before_rates} to {rates['as_of']}); "
                "a change in cost may be a price change")
    kinds = sorted(set(before) | set(after))
    return {"rates_note": note, "types": [
        {"type": k, "before": before.get(k), "after": after.get(k), "verdict": verdict(before.get(k), after.get(k))}
        for k in kinds]}


def format_compare(result):
    def cell(stats):
        if stats is None:
            return "-"
        cost = "no rate" if stats["cost_median"] is None else f"${stats['cost_median']:.3f}"
        return f"{stats['spawns']} spawns, {cost}/spawn, {','.join(stats['models'])}"

    lines = ["Before and after the snapshot, per agent type:", ""]
    for entry in result["types"]:
        lines += [f"{entry['type']}", f"  before: {cell(entry['before'])}", f"  after:  {cell(entry['after'])}",
                  f"  verdict: {entry['verdict']}", ""]
    if result["rates_note"]:
        lines.append(f"Note: {result['rates_note']}")
    return "\n".join(lines).rstrip() + "\n"


# --- command line --------------------------------------------------------------------------

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def add_selectors(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--project", help="project directory (default: the current directory)")
    group.add_argument("--all", action="store_true", help="every project")
    parser.add_argument("--since", help="only files that started on or after YYYY-MM-DD (a UTC date, taken from "
                                        "the file's first timestamp)")
    parser.add_argument("--until", help="only files that started on or before YYYY-MM-DD (a UTC date, taken from "
                                        "the file's first timestamp)")
    parser.add_argument("--projects-dir", help="transcripts directory (default: $CLAUDE_CONFIG_DIR/projects or ~/.claude/projects)")
    parser.add_argument("--rates", help="rates file (default: rates.json next to scripts/)")


def gather(args, project=None, everything=None):
    for flag in ("since", "until"):
        value = getattr(args, flag, None)
        if value and not _DAY.match(value):
            raise AuditError(f"--{flag} must be YYYY-MM-DD")
    project = args.project if project is None else project
    everything = args.all if everything is None else everything
    rates = load_rates(args.rates)
    quality = new_quality()
    dirs = project_dirs(projects_dir(args.projects_dir), project, everything)
    sums = [summarize(u, rates) for u in load_units(dirs, args.since, args.until, quality)]
    selectors = {"project": None if everything else os.path.abspath(project or os.getcwd()), "all": bool(everything),
                 "since": args.since, "until": args.until}
    return sums, quality, selectors, rates, bool(dirs)


def cmd_report(args):
    sums, quality, selectors, rates, found = gather(args)
    if not found:
        print(f"No transcripts found for {os.path.abspath(args.project or os.getcwd())} "
              "(use --all to read every project, or --project PATH).")
        return 0
    report = build_report(sums, rates, quality, selectors, show_descriptions=args.show_descriptions)
    print(json.dumps(report, indent=2) if args.json else format_report(report), end="" if not args.json else "\n")
    return 0


def cmd_snapshot(args):
    sums, _, selectors, rates, found = gather(args)
    if not found:
        print(f"No transcripts found for {selectors['project']}; no snapshot written "
              "(use --all, or --project PATH).")
        return 0
    out = Path(args.out)
    out.write_bytes((json.dumps(snapshot_data(sums, rates, selectors), indent=2) + "\n").encode("utf-8"))
    print(f"Snapshot saved to {out}")
    return 0


def cmd_compare(args):
    try:
        snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise AuditError(f"cannot read the snapshot {args.snapshot} ({exc.strerror or exc})") from None
    except ValueError as exc:
        raise AuditError(f"{args.snapshot} is not valid JSON ({exc})") from None
    saved = snapshot.get("selectors") if isinstance(snapshot, dict) else None
    saved = saved if isinstance(saved, dict) else {}
    project, everything = args.project, args.all
    if not project and not everything:
        project, everything = saved.get("project"), bool(saved.get("all"))
    sums, _, _, rates, _ = gather(args, project=project, everything=everything)
    result = compare(snapshot, sums, rates)
    print(json.dumps(result, indent=2) if args.json else format_compare(result), end="" if not args.json else "\n")
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="audit.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    report = sub.add_parser("report", help="split usage between the main thread and each subagent type")
    add_selectors(report)
    report.add_argument("--json", action="store_true")
    report.add_argument("--show-descriptions", action="store_true", help="include task descriptions (off by default)")
    report.set_defaults(func=cmd_report)
    snap = sub.add_parser("snapshot", help="save the per-type numbers so a later compare has a baseline")
    add_selectors(snap)
    snap.add_argument("--out", required=True)
    snap.set_defaults(func=cmd_snapshot)
    comp = sub.add_parser("compare", help="per-type usage per spawn before and after a snapshot")
    add_selectors(comp)
    comp.add_argument("--snapshot", required=True)
    comp.add_argument("--json", action="store_true")
    comp.set_defaults(func=cmd_compare)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (AuditError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
