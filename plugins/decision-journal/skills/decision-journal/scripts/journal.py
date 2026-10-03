#!/usr/bin/env python3
"""Decision journal: log predictions, grade them, and show calibration.

Usage: python journal.py {add,list,grade,stats} ...   (standard library only)
Log: ~/.claude/decision-journal.jsonl (override with DECISION_JOURNAL_PATH).
"""
import argparse
import contextlib
import json
import math
import os
import re
import statistics
import sys
import tempfile
import time
import uuid
from datetime import date
from pathlib import Path

DEFAULT_PATH = Path.home() / ".claude" / "decision-journal.jsonl"
REQUIRED = {"claim": ("confidence",), "estimate": ("unit", "estimate")}


class JournalError(Exception):
    """Invalid input (exit code 2)."""


class NotFound(Exception):
    """No such entry (exit code 1)."""


def log_path():
    return Path(os.environ.get("DECISION_JOURNAL_PATH") or DEFAULT_PATH)


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_date(text):
    if not isinstance(text, str) or not _ISO_DATE.match(text):
        raise JournalError(f"invalid date {text!r}; use YYYY-MM-DD")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise JournalError(f"invalid date {text!r}; use YYYY-MM-DD") from None


def today():
    raw = os.environ.get("DECISION_JOURNAL_TODAY")
    return parse_date(raw) if raw else date.today()


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valid(entry):
    """True only for entries whose fields have the right types and ranges."""
    if not (
        isinstance(entry, dict)
        and isinstance(entry.get("id"), int)
        and not isinstance(entry.get("id"), bool)
        and entry.get("type") in REQUIRED
        and isinstance(entry.get("text"), str)
        and entry.get("status") in ("open", "graded")
    ):
        return False
    tags = entry.get("tags", [])
    if not (isinstance(tags, list) and all(isinstance(t, str) for t in tags)):
        return False
    if entry.get("know_by") is not None and not isinstance(entry["know_by"], str):
        return False
    graded = entry["status"] == "graded"
    if entry["type"] == "claim":
        confidence = entry.get("confidence")
        if not (isinstance(confidence, int) and not isinstance(confidence, bool)
                and 50 <= confidence <= 99):
            return False
        return not graded or entry.get("outcome") in ("yes", "no")
    if not isinstance(entry.get("unit"), str):
        return False
    if not _is_number(entry.get("estimate")) or entry["estimate"] <= 0:
        return False
    low, high = entry.get("range_low"), entry.get("range_high")
    if (low is None) != (high is None):
        return False
    if low is not None and not (_is_number(low) and _is_number(high)):
        return False
    return not graded or (_is_number(entry.get("actual")) and entry["actual"] >= 0)


def _retry_permission(action, tries=200, delay=0.01):
    """Retry briefly on PermissionError: on Windows another process may hold the file for a moment."""
    for attempt in range(tries):
        try:
            return action()
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(delay)


def _load_numbered(path):
    """Read the log as (kind, value, line_number): kind is "entry" or "raw" (kept verbatim)."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        text = _retry_permission(lambda: path.read_text(encoding="utf-8-sig"))
    except UnicodeDecodeError:
        raise JournalError(f"{path} is not valid UTF-8") from None
    slots = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            obj = None
        if _valid(obj):
            slots.append(("entry", obj, number))
        else:
            slots.append(("raw", line, number))
            print(f"warning: skipping malformed line {number} in {path}", file=sys.stderr)
    return slots


def load(path):
    """Read the log as slots: ("entry", dict) or ("raw", line). Malformed lines are kept."""
    return [(kind, value) for kind, value, _ in _load_numbered(path)]


def entries_of(slots):
    return [value for kind, value in slots if kind == "entry"]


def save(path, slots):
    """Atomically rewrite the log (through a symlink if there is one)."""
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(v) if kind == "entry" else v for kind, v in slots]
    data = "".join(line + "\n" for line in lines)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".journal-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(data)
        _retry_permission(lambda: os.replace(tmp, path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def _locked(path, wait=5.0, stale=30.0):
    """Hold a lock file next to the log so concurrent commands cannot lose updates.

    The lock file holds a token; only its owner removes it. A lock older than `stale`
    seconds (a crashed command) is cleared. If the lock file cannot be created at all
    (for example a read-only directory) this raises PermissionError after about a second.
    """
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    token = f"{os.getpid()}-{uuid.uuid4().hex}"
    started = time.monotonic()
    deadline = started + wait
    cannot_create_deadline = started + min(1.0, wait)
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, token.encode("ascii"))
            finally:
                os.close(fd)
            break
        except (FileExistsError, PermissionError) as exc:
            lock_exists = True
            try:
                if time.time() - lock.stat().st_mtime > stale:
                    lock.unlink()
                    continue
            except FileNotFoundError:
                lock_exists = False
            except OSError:
                pass
            now = time.monotonic()
            if isinstance(exc, PermissionError) and not lock_exists and now >= cannot_create_deadline:
                raise
            if now >= deadline:
                raise JournalError(
                    f"could not lock {path}; if no other journal command is running, delete {lock}"
                ) from None
            time.sleep(0.01)
    try:
        yield
    finally:
        try:
            if lock.read_text(encoding="ascii") == token:
                lock.unlink()
        except OSError:
            pass


MIN_ESTIMATE = 0.001


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JournalError(f"{name} must be a number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise JournalError(f"{name} must be a finite number")
    return value


def _clean_number(value):
    """Store whole numbers as ints, and never -0."""
    if isinstance(value, float) and value == int(value) and abs(value) < 1e15:
        return int(value)
    return value


def _normalize_tags(tags):
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = tags.split(",")
    if not isinstance(tags, (list, tuple)):
        raise JournalError("tags must be text or a list of text")
    out = []
    for tag in tags:
        if not isinstance(tag, str):
            raise JournalError("tags must be text")
        tag = tag.strip().casefold()
        if tag and tag not in out:
            out.append(tag)
    return out


def parse_confidence(text):
    """Accept 70, 70% or 0.7 (a fraction above 0 and up to 1) and return a whole percent."""
    raw = (text or "").strip()
    percent = raw.endswith("%")
    if percent:
        raw = raw[:-1].strip()
    try:
        value = float(raw)
    except ValueError:
        raise JournalError(
            f"confidence {text!r} is not a number; use a whole number like 70, 70% or 0.7"
        ) from None
    if not percent and 0 < value <= 1:
        value *= 100
    if not math.isfinite(value) or abs(value - round(value)) > 1e-6:
        raise JournalError("confidence must be a whole number (for example 70, 70% or 0.7)")
    percent_value = int(round(value))
    if not 50 <= percent_value <= 99:
        raise JournalError(
            f"confidence {(text or '').strip()!r} was read as {percent_value}%; it must be a whole "
            "number from 50 to 99 (a fraction like 0.7 means 70%; below 50, flip the claim; "
            "100 is not a prediction)"
        )
    return percent_value


def _raw_id(line):
    match = re.search(r'"id"\s*:\s*(\d+)', line)
    return int(match.group(1)) if match else None


def add_entry(path, *, type, text, confidence=None, unit=None, estimate=None,
              range_low=None, range_high=None, know_by=None, tags=None, project=None):
    if not isinstance(text, str):
        raise JournalError("text must be text")
    text = text.strip()
    if not text:
        raise JournalError("text must not be empty")
    entry = {"id": 0, "created": today().isoformat(), "type": type, "text": text}
    if type == "claim":
        if any(v is not None for v in (unit, estimate, range_low, range_high)):
            raise JournalError("claims take --confidence only, not --unit, --estimate or --range-*")
        if (not isinstance(confidence, int) or isinstance(confidence, bool)
                or not 50 <= confidence <= 99):
            raise JournalError(
                "confidence must be a whole number from 50 to 99 "
                "(below 50, flip the claim; 100 is not a prediction)"
            )
        entry["confidence"] = confidence
    elif type == "estimate":
        if confidence is not None:
            raise JournalError("estimates take --estimate and --unit, not --confidence")
        if unit is not None and not isinstance(unit, str):
            raise JournalError("unit must be text")
        unit = (unit or "").strip()
        if not unit:
            raise JournalError("estimates need --unit (for example hours)")
        _number(estimate, "estimate")
        if estimate < MIN_ESTIMATE:
            raise JournalError(f"estimate must be at least {MIN_ESTIMATE}")
        if (range_low is None) != (range_high is None):
            raise JournalError("give both --range-low and --range-high, or neither")
        if range_low is not None:
            _number(range_low, "range-low")
            _number(range_high, "range-high")
            if not range_low <= estimate <= range_high:
                raise JournalError("range must satisfy range-low <= estimate <= range-high")
            range_low, range_high = _clean_number(range_low), _clean_number(range_high)
        entry.update(
            unit=unit, estimate=_clean_number(estimate), range_low=range_low, range_high=range_high
        )
    else:
        raise JournalError(f"unknown type {type!r}; use claim or estimate")
    entry["know_by"] = parse_date(know_by).isoformat() if know_by else None
    entry["tags"] = _normalize_tags(tags)
    entry["project"] = project if project is not None else (Path.cwd().name or str(Path.cwd()))
    entry["status"] = "open"
    with _locked(path):
        slots = load(path)
        ids = [e["id"] for e in entries_of(slots)]
        ids += [_raw_id(value) for kind, value in slots if kind == "raw"]
        entry["id"] = max([i for i in ids if i is not None], default=0) + 1
        slots.append(("entry", entry))
        save(path, slots)
    return entry


def _is_due(entry, as_of):
    if entry["status"] != "open" or not entry.get("know_by"):
        return False
    try:
        return parse_date(entry["know_by"]) <= as_of
    except JournalError:
        return False


def list_entries(path, mode=None):
    entries = entries_of(load(path))
    if mode == "open":
        return [e for e in entries if e["status"] == "open"]
    if mode == "due":
        as_of = today()
        return [e for e in entries if _is_due(e, as_of)]
    return entries


def _join_lines(numbers):
    if len(numbers) == 1:
        return str(numbers[0])
    return ", ".join(str(n) for n in numbers[:-1]) + " and " + str(numbers[-1])


def grade_entry(path, entry_id, *, outcome=None, actual=None, note=None, force=False):
    with _locked(path):
        numbered = _load_numbered(path)
        matches = [n for kind, v, n in numbered if kind == "entry" and v["id"] == entry_id]
        if not matches:
            raise NotFound(f"no entry with id {entry_id}")
        if len(matches) > 1:
            raise JournalError(
                f"id {entry_id} appears on lines {_join_lines(matches)}; "
                "remove the duplicate by hand first"
            )
        slots = [(kind, value) for kind, value, _ in numbered]
        entry = next(v for kind, v in slots if kind == "entry" and v["id"] == entry_id)
        if entry["status"] == "graded" and not force:
            raise JournalError(f"entry {entry_id} is already graded; use --force to overwrite")
        if entry["type"] == "claim":
            if actual is not None:
                raise JournalError("claims are graded with --outcome yes|no, not --actual")
            if outcome not in ("yes", "no"):
                raise JournalError("claims need --outcome yes|no")
        else:
            if outcome is not None:
                raise JournalError("estimates are graded with --actual NUMBER, not --outcome")
            _number(actual, "actual")
            if actual < 0:
                raise JournalError("actual must be 0 or greater")
        entry.pop("outcome", None)
        entry.pop("actual", None)
        entry["status"] = "graded"
        entry["graded"] = today().isoformat()
        if entry["type"] == "claim":
            entry["outcome"] = outcome
        else:
            entry["actual"] = _clean_number(actual)
        if note is not None:
            entry["note"] = note.strip()
        save(path, slots)
    return entry


def _num_text(value):
    """The user's own digits: whole numbers print without a trailing .0 (old logs stored 3.0)."""
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        value = int(value)
    return str(value) if isinstance(value, int) else repr(value)


def _ratio_text(ratio):
    if ratio > 1000:
        return ">1000x"
    if 0 < ratio < 0.01:
        return "<0.01x"
    return f"{ratio:.2f}x"


def grade_result(entry):
    """One-line summary of a graded entry (computed for display, never stored)."""
    if entry["type"] == "claim":
        happened = "it happened" if entry.get("outcome") == "yes" else "it did not happen"
        return f"{entry['confidence']}% claim: {happened}"
    ratio = entry["actual"] / entry["estimate"]
    return (f"{_num_text(entry['estimate'])} {entry['unit']} estimated, "
            f"{_num_text(entry['actual'])} actual: {_ratio_text(ratio)}")


MIN_N = 5
BUCKETS = ((50, 59), (60, 69), (70, 79), (80, 89), (90, 99))


def _claim_slice(claims):
    n = len(claims)
    if not n:
        return None
    stated = sum(c["confidence"] for c in claims) / n
    actual = 100 * sum(1 for c in claims if c["outcome"] == "yes") / n
    return {"n": n, "stated": stated, "actual": actual, "gap": stated - actual}


def _estimate_slice(ests):
    if not ests:
        return None
    ratios = [e["actual"] / e["estimate"] for e in ests]
    return {"n": len(ests), "median_ratio": statistics.median(ratios)}


def _folded_tags(entry):
    return [t.casefold() for t in entry.get("tags", [])]


def compute_stats(entries):
    graded = [e for e in entries if e["status"] == "graded"]
    claims = [e for e in graded if e["type"] == "claim" and e.get("outcome") in ("yes", "no")]
    ests = [e for e in graded
            if e["type"] == "estimate" and isinstance(e.get("actual"), (int, float))]
    result = {
        "graded": len(claims) + len(ests),
        "claim_count": len(claims),
        "estimate_count": len(ests),
        "too_few": len(claims) + len(ests) < MIN_N,
        "claims": None,
        "estimates": None,
        "tags": {},
    }
    if claims:
        brier = sum(
            (c["confidence"] / 100 - (1 if c["outcome"] == "yes" else 0)) ** 2 for c in claims
        ) / len(claims)
        buckets = []
        for low, high in BUCKETS:
            part = _claim_slice([c for c in claims if low <= c["confidence"] <= high])
            if part:
                buckets.append({"label": f"{low}-{high}", **part})
        result["claims"] = {"n": len(claims), "brier": brier, "buckets": buckets}
    if ests:
        ranged = [e for e in ests
                  if e.get("range_low") is not None and e.get("range_high") is not None]
        hits = sum(1 for e in ranged if e["range_low"] <= e["actual"] <= e["range_high"])
        result["estimates"] = {
            **_estimate_slice(ests), "range_n": len(ranged), "range_hits": hits,
        }
    tags = sorted({t for e in claims + ests for t in _folded_tags(e)})
    for tag in tags:
        result["tags"][tag] = {
            "claims": _claim_slice([c for c in claims if tag in _folded_tags(c)]),
            "estimates": _estimate_slice([e for e in ests if tag in _folded_tags(e)]),
        }
    return result


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _small(n):
    return "  (n<5)" if n < MIN_N else ""


def _verdict(ratio):
    if round(ratio, 2) == 1.0:
        return "on target"
    return "you run over" if ratio > 1 else "you run under"


def format_stats(stats, tag=None):
    label = f" (tag: {tag})" if tag else ""
    lines = [
        f"Decision journal{label}: {stats['graded']} graded "
        f"({_plural(stats['claim_count'], 'claim')}, {_plural(stats['estimate_count'], 'estimate')})"
    ]
    if stats["too_few"]:
        lines.append(f"Too few graded entries to conclude (need at least {MIN_N}).")
        return "\n".join(lines)
    claims = stats["claims"]
    if claims:
        lines += ["", f"Claims (n={claims['n']}): Brier {claims['brier']:.3f}{_small(claims['n'])}"]
        for b in claims["buckets"]:
            lines.append(
                f"  {b['label']}  n={b['n']}  stated {b['stated']:.0f}%  "
                f"actual {b['actual']:.0f}%  gap {b['gap']:+.0f}{_small(b['n'])}"
            )
        lines.append("  gap = stated - actual; positive means overconfident")
    est = stats["estimates"]
    if est:
        verdict = f" ({_verdict(est['median_ratio'])})" if est["n"] >= MIN_N else ""
        lines += [
            "",
            f"Estimates (n={est['n']}): median actual/estimate "
            f"{_ratio_text(est['median_ratio'])}{verdict}{_small(est['n'])}",
        ]
        if est["range_n"]:
            pct = 100 * est["range_hits"] / est["range_n"]
            lines.append(
                f"  Range hit: {est['range_hits']} of {est['range_n']} = {pct:.0f}% "
                f"(an 80% range should hit about 80%){_small(est['range_n'])}"
            )
    if stats["tags"]:
        lines += ["", "By tag:"]
        for name, parts in stats["tags"].items():
            c, e = parts["claims"], parts["estimates"]
            if c:
                lines.append(
                    f"  {name}: claims n={c['n']} stated {c['stated']:.0f}% "
                    f"actual {c['actual']:.0f}% gap {c['gap']:+.0f}{_small(c['n'])}"
                )
            if e:
                lines.append(
                    f"  {name}: estimates n={e['n']} median {_ratio_text(e['median_ratio'])}{_small(e['n'])}"
                )
    return "\n".join(lines)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(f"error: {message}", file=sys.stderr)
        sys.exit(2)


def build_parser():
    parser = _Parser(
        prog="journal.py", description="Log predictions, grade them, and review calibration."
    )
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="{add,list,grade,stats}")

    add = sub.add_parser("add", help="record a prediction")
    add.add_argument("--type", required=True, choices=["claim", "estimate"])
    add.add_argument("--text", required=True)
    add.add_argument("--confidence", help="claims: 50-99, for example 70, 70%% or 0.7")
    add.add_argument("--unit", help="estimates: for example hours")
    add.add_argument("--estimate", type=float, help="estimates: your point estimate")
    add.add_argument("--range-low", type=float, help="estimates: low end of your 80%% range")
    add.add_argument("--range-high", type=float, help="estimates: high end of your 80%% range")
    add.add_argument("--know-by", help="date you expect to know the outcome (YYYY-MM-DD)")
    add.add_argument("--tag", help="comma-separated tags")

    lst = sub.add_parser("list", help="print entries as JSON")
    group = lst.add_mutually_exclusive_group()
    group.add_argument("--open", action="store_true")
    group.add_argument("--due", action="store_true")

    grade = sub.add_parser("grade", help="record the outcome of a prediction")
    grade.add_argument("id", type=int)
    grade.add_argument("--outcome", choices=["yes", "no"])
    grade.add_argument("--actual", type=float)
    grade.add_argument("--note")
    grade.add_argument("--force", action="store_true", help="overwrite an existing grade")
    stats = sub.add_parser("stats", help="print the calibration report")
    stats.add_argument("--tag", help="only entries carrying this tag")
    return parser


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    path = log_path()
    try:
        if args.cmd == "add":
            confidence = args.confidence
            if confidence is not None and args.type == "claim":
                confidence = parse_confidence(confidence)
            result = add_entry(
                path, type=args.type, text=args.text, confidence=confidence,
                unit=args.unit, estimate=args.estimate, range_low=args.range_low,
                range_high=args.range_high, know_by=args.know_by, tags=args.tag,
            )
            print(json.dumps(result))
        elif args.cmd == "list":
            mode = "open" if args.open else "due" if args.due else None
            print(json.dumps(list_entries(path, mode)))
        elif args.cmd == "grade":
            result = grade_entry(
                path, args.id, outcome=args.outcome, actual=args.actual,
                note=args.note, force=args.force,
            )
            print(json.dumps({**result, "result": grade_result(result)}))
        elif args.cmd == "stats":
            entries = entries_of(load(path))
            if args.tag:
                tag = args.tag.strip().casefold()
                entries = [e for e in entries if tag in _folded_tags(e)]
            print(format_stats(compute_stats(entries), tag=args.tag and args.tag.strip().casefold()))
    except JournalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except NotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        target = exc.filename2 or exc.filename or path  # filename2 is the log when a temp-file swap fails
        print(f"error: cannot read or write {target}: {exc.strerror or exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
