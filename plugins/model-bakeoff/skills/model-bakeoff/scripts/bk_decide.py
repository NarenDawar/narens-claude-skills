"""The pass rule and the choice of the cheapest passing model. Pure; standard library only."""

import math

DEFAULT_RULE = {"runs": 3, "minCaseRuns": 2, "minPassRate": 0.9}
TIERS = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 3}


def tier_of(name):
    lowered = (name or "").lower()
    for key, rank in TIERS.items():
        if key in lowered:
            return rank
    return len(TIERS)


def summarize(entry, case_ids, rule):
    """How one model did: whether it passes the rule, and the numbers behind that."""
    runs = entry["runs"]
    by_case = {case: [] for case in case_ids}
    errors = {}
    for run in runs:
        by_case.setdefault(run["case"], []).append(bool(run["passed"]))
        if run.get("kind"):
            errors[run["kind"]] = errors.get(run["kind"], 0) + 1
    weak = [case for case in case_ids if sum(by_case.get(case, [])) < rule["minCaseRuns"]]
    total = len(runs)
    passed = sum(1 for run in runs if run["passed"])
    pass_rate = passed / total if total else 0.0
    costs = [run["cost"] for run in runs if type(run.get("cost")) in (int, float)
             and 0 <= run["cost"] < math.inf]
    # Divide first so even very large finite costs do not overflow the sum.
    mean_cost = sum(cost / total for cost in costs) if total and len(costs) == total else None
    return {
        "requested": entry["requested"],
        "resolvedId": entry.get("resolvedId"),
        "passes": total > 0 and not weak and pass_rate >= rule["minPassRate"],
        "passRate": pass_rate,
        "runCount": total,
        "weakCases": weak,
        "meanCost": mean_cost,
        "errors": errors,
    }


def recommend(summaries, current):
    """The cheapest passing model by measured cost, falling back to tier order."""
    passing = [s for s in summaries if s["passes"]]
    if not passing:
        return {
            "kind": "none", "model": None, "resolvedId": None, "costFallback": False,
            "reason": "no model passed the rule; keep the current model (or fix the failing cases / try a more capable model)",
        }
    use_cost = all(s["meanCost"] is not None for s in passing)
    if use_cost:
        best = min(passing, key=lambda s: (s["meanCost"], tier_of(s["requested"])))
        reason = f"{best['requested']} is the cheapest model that passes (mean measured cost ${best['meanCost']:.4f} per run)"
    else:
        best = min(passing, key=lambda s: tier_of(s["requested"]))
        reason = (
            f"{best['requested']} is the smallest model that passes; measured costs were missing, "
            "so the choice follows tier order (Haiku, Sonnet, Opus, Fable)"
        )
    kind = "unchanged" if current is not None and current in (best["requested"], best["resolvedId"]) else "change"
    if kind == "unchanged":
        reason += "; the agent already uses it, so there is nothing to change"
    return {"kind": kind, "model": best["requested"], "resolvedId": best["resolvedId"], "reason": reason, "costFallback": not use_cost}
