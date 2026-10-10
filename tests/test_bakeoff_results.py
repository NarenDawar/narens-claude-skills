"""Edited results must produce useful errors and conservative cost decisions."""
import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import helpers

sys.path.insert(0, str(helpers.REPO_ROOT / "plugins" / "model-bakeoff" / "skills" / "model-bakeoff" / "scripts"))
import bakeoff
import bk_decide


def results():
    return {"resultsVersion": 1, "caseIds": ["c1"], "models": [
        {"requested": "haiku", "resolvedId": None, "runs": [
            {"case": "c1", "passed": True, "cost": 0.01} for _ in range(3)
        ]}
    ]}


class ResultsTests(unittest.TestCase):
    def decide(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = bakeoff.main(["decide", "--results", str(path), "--json"])
            return code, out.getvalue(), err.getvalue()

    def test_malformed_records_are_one_line_usage_errors(self):
        mutations = [
            lambda d: d["models"][0]["runs"][0].pop("passed"),
            lambda d: d["models"][0]["runs"][0].update(passed="false"),
            lambda d: d["models"][0]["runs"][0].update(case="unknown"),
            lambda d: d["models"][0]["runs"][0].update(kind=[]),
            lambda d: d["models"][0]["runs"].append(None),
            lambda d: d["models"][0].update(runs=None),
            lambda d: d["models"][0].pop("requested"),
            lambda d: d["models"][0].update(resolvedId=42),
            lambda d: d["models"].append(None),
            lambda d: d.update(caseIds=[[]]),
            lambda d: d.update(rule={"runs": "three"}),
            lambda d: d.update(rule={"runs": True}),
            lambda d: d.update(rule=[]),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                data = results()
                mutation(data)
                code, out, err = self.decide(data)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertEqual(len(err.splitlines()), 1)
                self.assertIn("results.json", err)

    def test_invalid_costs_fall_back_instead_of_winning_on_price(self):
        for cost in (-100, float("nan"), float("inf"), -float("inf"), True, "0.001", None):
            with self.subTest(cost=cost):
                data = results()
                sonnet = copy.deepcopy(data["models"][0])
                sonnet["requested"] = "sonnet"
                sonnet["runs"][0]["cost"] = cost
                data["models"].append(sonnet)
                code, out, err = self.decide(data)
                self.assertEqual(code, 0, err)
                report = json.loads(out)
                self.assertIsNone(report["models"][1]["meanCost"])
                self.assertEqual(report["recommendation"]["model"], "haiku")
                self.assertTrue(report["recommendation"]["costFallback"])

    def test_zero_cost_is_valid(self):
        entry = results()["models"][0]
        for run in entry["runs"]:
            run["cost"] = 0
        self.assertEqual(bk_decide.summarize(entry, ["c1"], bk_decide.DEFAULT_RULE)["meanCost"], 0)
