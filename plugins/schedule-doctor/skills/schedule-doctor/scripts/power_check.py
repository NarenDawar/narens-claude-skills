#!/usr/bin/env python3
"""power_check: read the OS sleep settings that decide whether a scheduled task can fire.

    python power_check.py [--json]

Read-only: runs `powercfg /query` on Windows or `pmset -g` on macOS (no shell involved).
It cannot see Claude Desktop's own "Keep computer awake" setting; the agent must ask the user.
Exit code 0 on success, 2 when the settings cannot be read.
"""
import argparse
import json
import re
import subprocess
import sys

LID_ACTIONS = {0: "do nothing", 1: "sleep", 2: "hibernate", 3: "shut down"}
SOURCE_LABELS = {"ac": "on power", "dc": "on battery", "any": "currently"}
CAVEAT = (
    "Claude Desktop's own Keep computer awake setting is not visible from here; "
    "ask the user to confirm it."
)
COMMAND_TIMEOUT_SECONDS = 15


class PowerError(Exception):
    """A problem the user can fix; reported as `error: ...` with exit 2."""


def platform_name():
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return None


def run_command(args):
    try:
        done = subprocess.run(
            args, capture_output=True, text=True, errors="replace", timeout=COMMAND_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PowerError(f"could not run {args[0]}: {exc}") from None
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()[:200]
        raise PowerError(f"{args[0]} failed (exit {done.returncode}): {detail}")
    return done.stdout


def battery_from_flag(flag):
    """GetSystemPowerStatus BatteryFlag: 128 means no system battery, 255 means unknown."""
    if flag == 255:
        return None
    return flag != 128


def battery_present():
    """True or False on Windows when it can be told, otherwise None."""
    try:
        import ctypes

        class PowerStatus(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", ctypes.c_ubyte),
                ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong),
                ("BatteryFullLifeTime", ctypes.c_ulong),
            ]

        status = PowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return None
    except (AttributeError, OSError, ImportError):
        return None
    return battery_from_flag(status.BatteryFlag)


def parse_powercfg_index(text):
    found = {}
    pattern = r"Current (AC|DC) Power Setting Index:\s*0x([0-9a-fA-F]+)"
    for kind, value in re.findall(pattern, text):
        found[kind.lower()] = int(value, 16)
    return {"ac": found.get("ac"), "dc": found.get("dc")}


def parse_pmset_sleep(text):
    match = re.search(r"^\s*sleep\s+(\d+)\b", text, re.MULTILINE)
    return int(match.group(1)) if match else None


def scheme_only(text):
    """Recognize a successful scheme header with no setting, not arbitrary unreadable text."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    guid = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
    if not lines or len(lines) > 2:
        return False
    if not re.fullmatch(rf"Power Scheme GUID:\s*{guid}(?:\s+\(.*\))?", lines[0]):
        return False
    return len(lines) == 1 or bool(re.fullmatch(r"GUID Alias:\s*SCHEME_[A-Z0-9_]+", lines[1]))


def summarize(os_name, sleep_after, lid_action=None, hibernate_after=None, has_battery=None,
              lid_unreadable=False):
    findings = []
    sleeps = False
    unknown = False
    for kind, values in (("sleep", sleep_after), ("hibernate", hibernate_after)):
        if values is None:
            continue
        for source, minutes in values.items():
            label = SOURCE_LABELS.get(source, source)
            if minutes is None:
                unknown = True
                findings.append(f"{kind} timeout {label}: could not be read")
            elif minutes == 0:
                findings.append(f"{kind} timeout {label}: never")
            else:
                sleeps = True
                findings.append(f"{kind} timeout {label}: {minutes:g} minutes idle")
    if lid_action is None:
        if os_name != "windows":
            findings.append(
                "lid close: not readable (a closed MacBook lid sleeps it unless it is on power "
                "with an external display)"
            )
        elif lid_unreadable:
            unknown = True
            findings.append("lid close: could not be read")
        elif has_battery is False:
            findings.append("lid close: no setting found (no battery, so likely a desktop)")
        else:
            unknown = True
            findings.append(
                "lid close: no setting found (this may be a laptop whose lid setting is hidden)"
            )
    else:
        for source, action in lid_action.items():
            label = SOURCE_LABELS.get(source, source)
            if action is None:
                unknown = True
                findings.append(f"lid close {label}: could not be read")
            elif action == "do nothing":
                findings.append(f"lid close {label}: do nothing")
            else:
                sleeps = True
                findings.append(f"lid close {label}: {action}")
    if sleeps:
        keeps_awake = False
    elif unknown or os_name == "macos":
        keeps_awake = None
    else:
        keeps_awake = True
    return {
        "os": os_name,
        "sleep_after_minutes": sleep_after,
        "hibernate_after_minutes": hibernate_after,
        "lid_close_action": lid_action,
        "keeps_awake": keeps_awake,
        "findings": findings,
        "caveat": CAVEAT,
    }


def query_setting(subgroup, setting):
    """`/qh` includes hidden settings (a laptop's lid action is one); `/query` is the fallback."""
    scope = ["SCHEME_CURRENT", subgroup, setting]
    try:
        return run_command(["powercfg", "/qh"] + scope)
    except PowerError:
        return run_command(["powercfg", "/query"] + scope)


def to_minutes(indexes):
    return {key: None if value is None else value / 60 for key, value in indexes.items()}


def collect():
    name = platform_name()
    if name == "windows":
        sleep_after = to_minutes(parse_powercfg_index(query_setting("SUB_SLEEP", "STANDBYIDLE")))
        try:
            hibernate_text = query_setting("SUB_SLEEP", "HIBERNATEIDLE")
        except PowerError:
            hibernate_text = ""
        hibernate_after = to_minutes(parse_powercfg_index(hibernate_text))
        try:
            lid_text = query_setting("SUB_BUTTONS", "LIDACTION")
        except PowerError:
            lid_text = ""
        lid = {key: LID_ACTIONS.get(value) for key, value in parse_powercfg_index(lid_text).items()}
        return summarize(
            name,
            sleep_after,
            None if all(v is None for v in lid.values()) else lid,
            hibernate_after,
            battery_present(),
            lid_unreadable=all(v is None for v in lid.values()) and not scheme_only(lid_text),
        )
    if name == "macos":
        return summarize(name, {"any": parse_pmset_sleep(run_command(["pmset", "-g"]))})
    raise PowerError(
        f"unsupported OS ({sys.platform}): only Windows (powercfg) and macOS (pmset) are read"
    )


def render(res):
    keeps = {True: "yes", False: "no", None: "unknown"}[res["keeps_awake"]]
    lines = [f"os: {res['os']}"] + res["findings"]
    lines += [f"keeps awake unattended: {keeps}", f"caveat: {res['caveat']}"]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="power_check.py", description="Read the OS sleep settings that stop scheduled tasks."
    )
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)
    try:
        res = collect()
    except PowerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(res, indent=2) if args.json else render(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
