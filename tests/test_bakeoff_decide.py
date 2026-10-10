import sys
import unittest

import helpers

sys.path.insert(0, str(helpers.REPO_ROOT / "plugins" / "model-bakeoff" / "skills" / "model-bakeoff" / "scripts"))
import bk_decide as bd  # noqa: E402

RULE = {"runs": 3, "minCaseRuns": 2, "minPassRate": 0.9}


def runs(case, outcomes, cost=0.01, kinds=None):
    out = []
    for number, passed in enumerate(outcomes, start=1):
        kind = (kinds or {}).get(number)
        out.append({"case": case, "run": number, "passed": passed and kind is None, "kind": kind, "cost": cost})
    return out


def entry(requested, per_case, cost=0.01, resolved=None, kinds=None):
    all_runs = []
    for case, outcomes in per_case.items():
        all_runs += runs(case, outcomes, cost, kinds)
    return {"requested": requested, "resolvedId": resolved or f"id-{requested}", "runs": all_runs}


class SummarizeTests(unittest.TestCase):
    def test_every_run_passing_passes(self):
        s = bd.summarize(entry("haiku", {"a": [True] * 3, "b": [True] * 3}), ["a", "b"], RULE)
        self.assertTrue(s["passes"])
        self.assertEqual((s["passRate"], s["runCount"], s["weakCases"]), (1.0, 6, []))

    def test_exactly_two_of_three_passes_a_case_but_one_of_three_does_not(self):
        s = bd.summarize(entry("haiku", {"a": [True, True, False]}), ["a"], dict(RULE, minPassRate=0.5))
        self.assertTrue(s["passes"])
        s = bd.summarize(entry("haiku", {"a": [True, False, False]}), ["a"], dict(RULE, minPassRate=0.0))
        self.assertFalse(s["passes"])
        self.assertEqual(s["weakCases"], ["a"])

    def test_the_overall_rate_boundary_is_90_percent_inclusive(self):
        # 10 cases x 3 runs = 30 runs; three failing runs (one in each of h, i, j) = 27 passing = exactly 90%,
        # and every case still passes in 2 of 3 runs.
        spread = {c: [True, True, True] for c in "abcdefg"}
        spread.update({"h": [True, True, False], "i": [True, True, False], "j": [True, True, False]})
        s = bd.summarize(entry("haiku", spread), list(spread), RULE)
        self.assertEqual(s["passRate"], 0.9)
        self.assertTrue(s["passes"])
        worse = dict(spread)
        worse["g"] = [True, True, False]  # 4 failing runs of 30 = 86.7%
        s = bd.summarize(entry("haiku", worse), list(worse), RULE)
        self.assertLess(s["passRate"], 0.9)
        self.assertFalse(s["passes"])

    def test_an_errored_run_counts_as_failed_and_is_counted_by_kind(self):
        e = entry("haiku", {"a": [True, True, True]}, kinds={3: "timeout"})
        s = bd.summarize(e, ["a"], dict(RULE, minPassRate=0.5))
        self.assertTrue(s["passes"])
        self.assertAlmostEqual(s["passRate"], 2 / 3)
        self.assertEqual(s["errors"], {"timeout": 1})

    def test_a_case_with_too_few_runs_fails(self):
        s = bd.summarize(entry("haiku", {"a": [True]}), ["a", "b"], RULE)
        self.assertFalse(s["passes"])
        self.assertEqual(s["weakCases"], ["a", "b"])

    def test_no_runs_never_passes(self):
        s = bd.summarize({"requested": "haiku", "resolvedId": None, "runs": []}, ["a"], RULE)
        self.assertFalse(s["passes"])
        self.assertEqual(s["passRate"], 0.0)

    def test_mean_cost_needs_every_run_to_have_a_cost(self):
        s = bd.summarize(entry("haiku", {"a": [True] * 3}, cost=0.02), ["a"], RULE)
        self.assertAlmostEqual(s["meanCost"], 0.02)
        e = entry("haiku", {"a": [True] * 3})
        e["runs"][1]["cost"] = None
        self.assertIsNone(bd.summarize(e, ["a"], RULE)["meanCost"])


class TierTests(unittest.TestCase):
    def test_tiers(self):
        self.assertLess(bd.tier_of("haiku"), bd.tier_of("sonnet"))
        self.assertLess(bd.tier_of("sonnet"), bd.tier_of("opus"))
        self.assertLess(bd.tier_of("claude-haiku-4-5-20251001"), bd.tier_of("claude-sonnet-5-5"))
        self.assertLess(bd.tier_of("opus"), bd.tier_of("fable"))
        self.assertGreater(bd.tier_of("something-else"), bd.tier_of("fable"))
        self.assertGreater(bd.tier_of(None), bd.tier_of("fable"))


def summary(requested, passes=True, cost=0.01, resolved=None):
    return {"requested": requested, "resolvedId": resolved or f"id-{requested}", "passes": passes, "meanCost": cost,
            "passRate": 1.0, "runCount": 3, "weakCases": [], "errors": {}}


class RecommendTests(unittest.TestCase):
    def test_the_cheapest_passing_model_wins(self):
        r = bd.recommend([summary("haiku", cost=0.002), summary("sonnet", cost=0.01), summary("opus", cost=0.05)], "opus")
        self.assertEqual((r["kind"], r["model"], r["resolvedId"]), ("change", "haiku", "id-haiku"))
        self.assertFalse(r["costFallback"])

    def test_a_failing_cheaper_model_is_skipped(self):
        r = bd.recommend([summary("haiku", passes=False, cost=0.002), summary("sonnet", cost=0.01), summary("opus", cost=0.05)], "opus")
        self.assertEqual(r["model"], "sonnet")

    def test_measured_cost_beats_tier_order(self):
        r = bd.recommend([summary("sonnet", cost=0.004), summary("haiku", cost=0.009)], None)
        self.assertEqual(r["model"], "sonnet")

    def test_equal_cost_goes_to_the_smaller_tier(self):
        r = bd.recommend([summary("sonnet", cost=0.01), summary("haiku", cost=0.01)], None)
        self.assertEqual(r["model"], "haiku")

    def test_missing_costs_fall_back_to_tier_order_and_say_so(self):
        r = bd.recommend([summary("opus", cost=None), summary("sonnet", cost=0.001), summary("haiku", passes=False, cost=None)], None)
        self.assertEqual(r["model"], "sonnet")
        self.assertTrue(r["costFallback"])
        self.assertIn("tier order", r["reason"])

    def test_no_passing_model_is_a_result(self):
        r = bd.recommend([summary("haiku", passes=False), summary("opus", passes=False)], "opus")
        self.assertEqual(r["kind"], "none")
        self.assertIsNone(r["model"])
        self.assertIn("keep the current model", r["reason"])

    def test_the_current_model_already_cheapest_means_no_change(self):
        r = bd.recommend([summary("haiku", cost=0.002), summary("sonnet", cost=0.01)], "haiku")
        self.assertEqual(r["kind"], "unchanged")
        r = bd.recommend([summary("haiku", cost=0.002, resolved="claude-haiku-4-5-20251001")], "claude-haiku-4-5-20251001")
        self.assertEqual(r["kind"], "unchanged")

    def test_an_agent_with_no_model_set_gets_a_change(self):
        self.assertEqual(bd.recommend([summary("haiku")], None)["kind"], "change")

    def test_nothing_to_recommend_from_no_models(self):
        self.assertEqual(bd.recommend([], None)["kind"], "none")


if __name__ == "__main__":
    unittest.main()
