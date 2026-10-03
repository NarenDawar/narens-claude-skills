import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers

sys.path.insert(
    0,
    str(helpers.REPO_ROOT / "plugins" / "decision-journal" / "skills" / "decision-journal" / "scripts"),
)
import journal  # noqa: E402
from journal import JournalError, NotFound  # noqa: E402


class JournalCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sub" / "j.jsonl"
        env = mock.patch.dict(
            os.environ,
            {"DECISION_JOURNAL_PATH": str(self.path), "DECISION_JOURNAL_TODAY": "2026-10-01"},
        )
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = journal.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def claim(self, text="c", confidence=70, **kw):
        return journal.add_entry(
            self.path, type="claim", text=text, confidence=confidence, project="proj", **kw
        )

    def estimate(self, text="e", estimate=2, unit="hours", **kw):
        return journal.add_entry(
            self.path, type="estimate", text=text, unit=unit, estimate=estimate, project="proj", **kw
        )

    def read(self):
        return journal.entries_of(journal.load(self.path))


class AddTests(JournalCase):
    def test_add_claim(self):
        e = self.claim("cache won't scale", 70, know_by="2026-10-15", tags="Perf, perf ,api")
        self.assertEqual(
            e,
            {
                "id": 1, "created": "2026-10-01", "type": "claim", "text": "cache won't scale",
                "confidence": 70, "know_by": "2026-10-15", "tags": ["perf", "api"],
                "project": "proj", "status": "open",
            },
        )
        self.assertEqual(self.read(), [e])

    def test_add_estimate_with_range(self):
        e = self.estimate("refactor", 2, range_low=1.5, range_high=4, tags=["refactor"])
        self.assertEqual(e["unit"], "hours")
        self.assertEqual((e["estimate"], e["range_low"], e["range_high"]), (2, 1.5, 4))
        self.assertIsNone(e["know_by"])

    def test_estimate_without_range_stores_nulls(self):
        e = self.estimate()
        self.assertIsNone(e["range_low"])
        self.assertIsNone(e["range_high"])

    def test_ids_are_max_plus_one(self):
        self.claim()
        self.claim()
        slots = journal.load(self.path)
        slots[0][1]["id"] = 7  # simulate a gap
        journal.save(self.path, slots)
        self.assertEqual(self.claim()["id"], 8)

    def test_add_validation(self):
        bad = [
            dict(type="claim", text="x", confidence=49),
            dict(type="claim", text="x", confidence=100),
            dict(type="claim", text="x", confidence=70.5),
            dict(type="claim", text="x", confidence=None),
            dict(type="claim", text="x", confidence=70, unit="hours"),
            dict(type="claim", text="x", confidence=70, estimate=2),
            dict(type="claim", text="", confidence=70),
            dict(type="claim", text="x", confidence=70, know_by="not-a-date"),
            dict(type="estimate", text="x", unit="hours", estimate=0),
            dict(type="estimate", text="x", unit="hours", estimate=-1),
            dict(type="estimate", text="x", unit="hours", estimate=float("inf")),
            dict(type="estimate", text="x", unit="hours", estimate=None),
            dict(type="estimate", text="x", unit="", estimate=2),
            dict(type="estimate", text="x", unit="hours", estimate=2, confidence=70),
            dict(type="estimate", text="x", unit="hours", estimate=2, range_low=1),
            dict(type="estimate", text="x", unit="hours", estimate=2, range_high=3),
            dict(type="estimate", text="x", unit="hours", estimate=2, range_low=3, range_high=4),
            dict(type="estimate", text="x", unit="hours", estimate=5, range_low=1, range_high=4),
            dict(type="mystery", text="x"),
        ]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(JournalError):
                    journal.add_entry(self.path, project="proj", **kwargs)
        self.assertFalse(self.path.exists())  # nothing written by failed adds

    def test_text_roundtrips_special_characters(self):
        text = 'Use "quotes", pipes | and émojis 🚀\nsecond line'
        self.claim(text)
        self.assertEqual(self.read()[0]["text"], text)
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 1)

    def test_creates_parent_directory(self):
        self.assertFalse(self.path.parent.exists())
        self.claim()
        self.assertTrue(self.path.is_file())

    def test_default_log_path(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("DECISION_JOURNAL_PATH")
            self.assertEqual(
                journal.log_path(), Path.home() / ".claude" / "decision-journal.jsonl"
            )

    def test_invalid_today_override_is_an_error(self):
        with mock.patch.dict(os.environ, {"DECISION_JOURNAL_TODAY": "garbage"}):
            with self.assertRaises(JournalError):
                self.claim()


class ListTests(JournalCase):
    def test_missing_log_lists_empty(self):
        self.assertEqual(journal.list_entries(self.path), [])
        self.assertEqual(journal.list_entries(self.path, "due"), [])

    def test_filters(self):
        self.claim("a", know_by="2026-09-30")   # 1 due (past)
        self.claim("b", know_by="2026-10-01")   # 2 due (today, inclusive)
        self.claim("c", know_by="2026-10-02")   # 3 open, not due
        self.claim("d")                          # 4 open, no know_by
        self.claim("e", know_by="2026-09-01")   # 5 graded, excluded from open/due
        journal.grade_entry(self.path, 5, outcome="yes")
        ids = lambda mode: [e["id"] for e in journal.list_entries(self.path, mode)]
        self.assertEqual(ids("due"), [1, 2])
        self.assertEqual(ids("open"), [1, 2, 3, 4])
        self.assertEqual(ids(None), [1, 2, 3, 4, 5])


class GradeTests(JournalCase):
    def test_grade_claim(self):
        self.claim()
        e = journal.grade_entry(self.path, 1, outcome="no", note="held up fine")
        self.assertEqual(
            (e["status"], e["outcome"], e["graded"], e["note"]),
            ("graded", "no", "2026-10-01", "held up fine"),
        )
        self.assertEqual(self.read(), [e])

    def test_grade_estimate(self):
        self.estimate()
        e = journal.grade_entry(self.path, 1, actual=3.5)
        self.assertEqual((e["status"], e["actual"]), ("graded", 3.5))

    def test_wrong_grade_kind_is_rejected(self):
        self.claim()
        self.estimate()
        for entry_id, kwargs in ((1, {"actual": 2}), (1, {}), (1, {"outcome": "maybe"}),
                                 (2, {"outcome": "yes"}), (2, {}), (2, {"actual": -1}),
                                 (2, {"actual": float("nan")})):
            with self.subTest(entry_id=entry_id, kwargs=kwargs):
                with self.assertRaises(JournalError):
                    journal.grade_entry(self.path, entry_id, **kwargs)
        self.assertEqual([e["status"] for e in self.read()], ["open", "open"])

    def test_grade_twice_requires_force(self):
        self.claim()
        journal.grade_entry(self.path, 1, outcome="yes")
        with self.assertRaises(JournalError):
            journal.grade_entry(self.path, 1, outcome="no")
        self.assertEqual(self.read()[0]["outcome"], "yes")
        e = journal.grade_entry(self.path, 1, outcome="no", force=True)
        self.assertEqual(e["outcome"], "no")

    def test_unknown_id(self):
        with self.assertRaises(NotFound):
            journal.grade_entry(self.path, 99, outcome="yes")


class RobustnessTests(JournalCase):
    def test_malformed_line_survives_rewrite(self):
        self.claim("a")
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("{not json\n")
        self.claim("b")  # rewrites the file
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[1], "{not json")
        journal.grade_entry(self.path, 2, outcome="yes")
        self.assertIn("{not json", self.path.read_text(encoding="utf-8"))

    def test_malformed_line_warns_on_stderr(self):
        self.claim("a")
        with open(self.path, "a", encoding="utf-8") as f:
            f.write('{"id": 1}\n')  # valid JSON, missing required fields
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(len(journal.entries_of(journal.load(self.path))), 1)
        self.assertIn("malformed line 2", err.getvalue())

    def test_crlf_and_bom_are_tolerated(self):
        e = self.claim("a")
        self.path.write_bytes(b"\xef\xbb\xbf" + json.dumps(e).encode("utf-8") + b"\r\n")
        self.assertEqual(self.read(), [e])

    def test_invalid_utf8_log_is_an_error(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"\xff\xfe\x00bad")
        code, _, err = self.run_cli("list")
        self.assertEqual(code, 2)
        self.assertIn("not valid UTF-8", err)

    def test_failed_write_leaves_original_intact(self):
        self.claim("a")
        before = self.path.read_bytes()
        with mock.patch("journal.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.claim("b")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["j.jsonl"])


class CliTests(JournalCase):
    def test_add_list_grade_roundtrip(self):
        code, out, _ = self.run_cli("add", "--type", "claim", "--text", "x", "--confidence", "70", "--tag", "perf")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["id"], 1)
        code, out, _ = self.run_cli("list", "--open")
        self.assertEqual([e["id"] for e in json.loads(out)], [1])
        code, out, _ = self.run_cli("grade", "1", "--outcome", "yes", "--note", "done")
        self.assertEqual((code, json.loads(out)["status"]), (0, "graded"))

    def test_add_estimate_via_cli(self):
        code, out, _ = self.run_cli(
            "add", "--type", "estimate", "--text", "refactor", "--unit", "hours",
            "--estimate", "2", "--range-low", "1.5", "--range-high", "4",
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["range_high"], 4)

    def test_invalid_input_exits_2(self):
        code, out, err = self.run_cli("add", "--type", "claim", "--text", "x", "--confidence", "100")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("error:"), err)

    def test_not_found_exits_1(self):
        code, _, err = self.run_cli("grade", "99", "--outcome", "yes")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error:"), err)

    def test_argparse_errors_exit_2(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_cli("add", "--type", "claim")
        self.assertEqual(cm.exception.code, 2)


class StatsTests(JournalCase):
    def build_dataset(self):
        """13 graded claims and 5 graded estimates with known, hand-computed results."""
        for conf, outcomes, tag in ((80, "yyyynn", "perf"), (70, "yyynn", "api"), (60, "yn", "api")):
            for o in outcomes:
                e = self.claim("c", conf, tags=tag)
                journal.grade_entry(self.path, e["id"], outcome="yes" if o == "y" else "no")
        for est, actual, lo, hi in ((2, 3, 1.5, 2.5), (4, 4, 3, 5), (1, 2, 0.5, 1.5),
                                    (10, 8, 7, 13), (3, 6, None, None)):
            e = self.estimate("e", est, range_low=lo, range_high=hi, tags="refactor")
            journal.grade_entry(self.path, e["id"], actual=actual)

    def stats(self, tag=None):
        entries = self.read()
        if tag:
            entries = [e for e in entries if tag in e["tags"]]
        return journal.compute_stats(entries)

    def test_compute_stats_known_values(self):
        self.build_dataset()
        self.claim("still open", 90)  # open entries are ignored
        s = self.stats()
        self.assertEqual((s["graded"], s["claim_count"], s["estimate_count"], s["too_few"]),
                         (18, 13, 5, False))
        self.assertAlmostEqual(s["claims"]["brier"], 3.21 / 13, places=9)
        by_label = {b["label"]: b for b in s["claims"]["buckets"]}
        self.assertEqual(list(by_label), ["60-69", "70-79", "80-89"])
        self.assertEqual((by_label["80-89"]["n"], by_label["80-89"]["stated"]), (6, 80))
        self.assertAlmostEqual(by_label["80-89"]["actual"], 200 / 3)
        self.assertAlmostEqual(by_label["80-89"]["gap"], 80 - 200 / 3)
        self.assertEqual((by_label["70-79"]["n"], by_label["70-79"]["actual"]), (5, 60))
        self.assertEqual((by_label["60-69"]["n"], by_label["60-69"]["actual"]), (2, 50))
        self.assertEqual(s["estimates"]["n"], 5)
        self.assertAlmostEqual(s["estimates"]["median_ratio"], 1.5)
        self.assertEqual((s["estimates"]["range_n"], s["estimates"]["range_hits"]), (4, 2))
        self.assertEqual(sorted(s["tags"]), ["api", "perf", "refactor"])
        self.assertEqual(s["tags"]["perf"]["claims"]["n"], 6)
        self.assertIsNone(s["tags"]["perf"]["estimates"])
        self.assertAlmostEqual(s["tags"]["refactor"]["estimates"]["median_ratio"], 1.5)

    def test_format_known_report(self):
        self.build_dataset()
        text = journal.format_stats(self.stats())
        self.assertEqual(
            text.splitlines(),
            [
                "Decision journal: 18 graded (13 claims, 5 estimates)",
                "",
                "Claims (n=13): Brier 0.247",
                "  60-69  n=2  stated 60%  actual 50%  gap +10  (n<5)",
                "  70-79  n=5  stated 70%  actual 60%  gap +10",
                "  80-89  n=6  stated 80%  actual 67%  gap +13",
                "  gap = stated - actual; positive means overconfident",
                "",
                "Estimates (n=5): median actual/estimate 1.50x (you run over)",
                "  Range hit: 2 of 4 = 50% (an 80% range should hit about 80%)  (n<5)",
                "",
                "By tag:",
                "  api: claims n=7 stated 67% actual 57% gap +10",
                "  perf: claims n=6 stated 80% actual 67% gap +13",
                "  refactor: estimates n=5 median 1.50x",
            ],
        )

    def test_too_few_prints_counts_only(self):
        for i in range(4):
            e = self.claim("c", 70)
            journal.grade_entry(self.path, e["id"], outcome="yes")
        text = journal.format_stats(self.stats())
        self.assertIn("Decision journal: 4 graded (4 claims, 0 estimates)", text)
        self.assertIn("Too few graded entries to conclude (need at least 5).", text)
        self.assertNotIn("Brier", text)
        e = self.claim("c", 70)
        journal.grade_entry(self.path, e["id"], outcome="no")
        self.assertNotIn("Too few", journal.format_stats(self.stats()))

    def test_empty_journal(self):
        text = journal.format_stats(journal.compute_stats([]))
        self.assertIn("0 graded (0 claims, 0 estimates)", text)
        self.assertIn("Too few graded entries", text)

    def test_small_slices_are_marked(self):
        for conf, o in ((70, "yes"), (70, "no")):
            e = self.claim("c", conf)
            journal.grade_entry(self.path, e["id"], outcome=o)
        for actual in (2, 2, 2):
            e = self.estimate("e", 2)
            journal.grade_entry(self.path, e["id"], actual=actual)
        lines = journal.format_stats(self.stats()).splitlines()
        claims_line = next(l for l in lines if l.startswith("Claims (n=2)"))
        est_line = next(l for l in lines if l.startswith("Estimates (n=3)"))
        self.assertTrue(claims_line.endswith("(n<5)"), claims_line)
        self.assertTrue(est_line.endswith("(n<5)"), est_line)
        self.assertNotIn("on target", est_line)  # no verdict below n=5
        self.assertNotIn("you run", est_line)
        self.assertIn("1.00x", est_line)
        self.assertNotIn("Range hit", "\n".join(lines))  # no ranges recorded

    def test_under_and_on_target_wording(self):
        for actual, expect in ((1, "0.50x (you run under)"), (2, "1.00x (on target)")):
            self.path.unlink(missing_ok=True)
            for _ in range(5):
                e = self.estimate("e", 2)
                journal.grade_entry(self.path, e["id"], actual=actual)
            self.assertIn(expect, journal.format_stats(self.stats()))

    def test_cli_stats_and_tag_filter(self):
        self.build_dataset()
        code, out, _ = self.run_cli("stats")
        self.assertEqual(code, 0)
        self.assertIn("Claims (n=13): Brier 0.247", out)
        code, out, _ = self.run_cli("stats", "--tag", "perf")
        self.assertIn("Decision journal (tag: perf): 6 graded (6 claims, 0 estimates)", out)
        self.assertIn("80-89  n=6", out)
        code, out, _ = self.run_cli("stats", "--tag", "nope")
        self.assertIn("0 graded", out)
        self.assertIn("Too few graded entries", out)


class RangeHitSampleTests(JournalCase):
    def test_range_hit_not_marked_with_five_ranges(self):
        for _ in range(5):
            e = self.estimate("e", 2, range_low=1, range_high=3)
            journal.grade_entry(self.path, e["id"], actual=2)
        line = next(l for l in journal.format_stats(
            journal.compute_stats(self.read())).splitlines() if "Range hit" in l)
        self.assertEqual(line, "  Range hit: 5 of 5 = 100% (an 80% range should hit about 80%)")


class HandEditedEntryTests(JournalCase):
    CLAIM = {"id": 2, "created": "2026-10-01", "type": "claim", "text": "x", "confidence": 70,
             "know_by": None, "tags": [], "project": "p", "status": "open"}
    EST = {"id": 2, "created": "2026-10-01", "type": "estimate", "text": "x", "unit": "hours",
           "estimate": 2, "range_low": None, "range_high": None, "know_by": None, "tags": [],
           "project": "p", "status": "open"}

    def variants(self):
        c, e = self.CLAIM, self.EST
        return {
            "estimate zero": {**e, "estimate": 0},
            "estimate string": {**e, "estimate": "2"},
            "estimate nan": {**e, "estimate": float("nan")},
            "unit not text": {**e, "unit": 5},
            "range one-sided": {**e, "range_low": 1, "range_high": None},
            "range as text": {**e, "range_low": "1", "range_high": 3},
            "confidence string": {**c, "confidence": "70"},
            "confidence too high": {**c, "confidence": 150},
            "confidence too low": {**c, "confidence": 30},
            "confidence bool": {**c, "confidence": True},
            "tags null": {**c, "tags": None},
            "tags string": {**c, "tags": "perf"},
            "tags mixed": {**c, "tags": ["a", 5]},
            "know_by int": {**c, "know_by": 5},
            "bool id": {**c, "id": True},
            "graded claim without outcome": {**c, "status": "graded"},
            "graded claim bad outcome": {**c, "status": "graded", "outcome": "maybe"},
            "graded estimate without actual": {**e, "status": "graded"},
            "graded estimate text actual": {**e, "status": "graded", "actual": "3"},
            "graded estimate negative actual": {**e, "status": "graded", "actual": -1},
        }

    def test_bad_shape_entries_are_malformed_not_crashes(self):
        good = self.claim("ok", 70)
        for name, bad in self.variants().items():
            with self.subTest(name):
                self.path.write_text(json.dumps(good) + "\n" + json.dumps(bad) + "\n", encoding="utf-8")
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    slots = journal.load(self.path)
                self.assertEqual([kind for kind, _ in slots], ["entry", "raw"])
                self.assertIn("malformed line 2", err.getvalue())
                for cmd in (("stats",), ("stats", "--tag", "perf"), ("list",), ("list", "--due")):
                    code, _, _ = self.run_cli(*cmd)
                    self.assertEqual(code, 0, (name, cmd))

    def test_entry_without_optional_keys_is_still_valid(self):
        slim = {k: v for k, v in self.CLAIM.items() if k not in ("tags", "know_by", "project")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(slim) + "\n", encoding="utf-8")
        self.assertEqual(len(journal.entries_of(journal.load(self.path))), 1)
        self.assertEqual(self.run_cli("stats")[0], 0)


class DeferredMinorTests(JournalCase):
    # --- unreadable or unwritable log -------------------------------------
    def test_directory_at_log_path_is_a_clean_error(self):
        self.path.mkdir(parents=True)
        code, _, err = self.run_cli("list")
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error: cannot read or write"), err)

    def test_write_failure_is_a_clean_error(self):
        with mock.patch("journal.os.replace", side_effect=PermissionError("denied")):
            code, _, err = self.run_cli("add", "--type", "claim", "--text", "x", "--confidence", "70")
        self.assertEqual(code, 2)
        self.assertIn("cannot read or write", err)

    # --- ids ---------------------------------------------------------------
    def test_duplicate_ids_stop_grading_and_name_the_lines(self):
        e = self.claim("a", 70)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(e) + "\n")
        code, _, err = self.run_cli("grade", "1", "--outcome", "yes")
        self.assertEqual(code, 2)
        self.assertIn("lines 1 and 2", err)

    def test_ids_on_malformed_lines_still_count(self):
        self.claim("a", 70)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write('{"id": 9, "type": "oops"}\n')
        self.assertEqual(self.claim("b", 70)["id"], 10)

    # --- numbers -----------------------------------------------------------
    def test_numbers_are_normalised(self):
        e = self.estimate("e", 2.0, range_low=1.0, range_high=3.0)
        for key in ("estimate", "range_low", "range_high"):
            self.assertIsInstance(e[key], int, key)
        g = journal.grade_entry(self.path, e["id"], actual=-0.0)
        self.assertIsInstance(g["actual"], int)
        self.assertEqual(g["actual"], 0)
        self.assertEqual(self.estimate("e", 2.5)["estimate"], 2.5)

    def test_tiny_estimate_is_rejected(self):
        with self.assertRaises(JournalError):
            self.estimate("e", 0.0005)

    def test_huge_ratio_is_capped_in_the_report(self):
        for _ in range(5):
            e = self.estimate("e", 1)
            journal.grade_entry(self.path, e["id"], actual=5000)
        text = journal.format_stats(journal.compute_stats(self.read()))
        self.assertIn("median actual/estimate >1000x", text)
        self.assertNotIn("inf", text)

    def test_api_type_errors_are_journal_errors(self):
        bad = [
            dict(type="estimate", text="x", unit="hours", estimate="2"),
            dict(type="estimate", text="x", unit="hours", estimate=10 ** 400),
            dict(type="claim", text=5, confidence=70),
            dict(type="estimate", text="x", unit=5, estimate=2),
            dict(type="claim", text="x", confidence=70, tags=["a", 5]),
        ]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(JournalError):
                    journal.add_entry(self.path, project="proj", **kwargs)

    # --- confidence input and argparse errors -----------------------------
    def test_parse_confidence(self):
        for text, want in (("70", 70), ("70%", 70), (" 85 % ", 85), ("0.7", 70), ("0.99", 99)):
            with self.subTest(text=text):
                self.assertEqual(journal.parse_confidence(text), want)
        for bad in ("70.5", "abc", "0.705", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(JournalError):
                    journal.parse_confidence(bad)

    def test_cli_accepts_percent_and_fraction_confidence(self):
        for text in ("70%", "0.7", "70"):
            code, out, _ = self.run_cli("add", "--type", "claim", "--text", "x", "--confidence", text)
            self.assertEqual(code, 0, text)
            self.assertEqual(json.loads(out)["confidence"], 70)

    def test_cli_bad_confidence_text_has_error_prefix(self):
        code, _, err = self.run_cli("add", "--type", "claim", "--text", "x", "--confidence", "70.5")
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error:"), err)

    def test_argparse_errors_have_error_prefix(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            journal.main(["add", "--type", "claim"])
        self.assertEqual(cm.exception.code, 2)
        self.assertTrue(err.getvalue().startswith("error:"), err.getvalue())

    # --- concurrency -------------------------------------------------------
    def test_parallel_adds_all_land(self):
        errors = []

        def worker(i):
            try:
                for j in range(3):
                    journal.add_entry(
                        self.path, type="claim", text=f"c{i}-{j}", confidence=70, project="p"
                    )
            except Exception as exc:  # noqa: BLE001 - collected and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(e["id"] for e in self.read()), list(range(1, 31)))
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["j.jsonl"])

    def test_stale_lock_is_cleared_and_fresh_lock_times_out(self):
        lock = self.path.with_name(self.path.name + ".lock")
        self.path.parent.mkdir(parents=True)
        lock.write_text("")
        old = time.time() - 120
        os.utime(lock, (old, old))
        self.claim("a")
        self.assertFalse(lock.exists())
        lock.write_text("")
        with self.assertRaises(JournalError):
            with journal._locked(self.path, wait=0.2):
                pass
        self.assertTrue(lock.exists())  # a fresh lock belongs to someone else

    # --- grade result line -------------------------------------------------
    def test_grade_prints_a_computed_result(self):
        self.claim("c", 70)
        self.claim("c2", 70)
        self.estimate("e", 2)
        _, out, _ = self.run_cli("grade", "1", "--outcome", "yes")
        self.assertEqual(json.loads(out)["result"], "70% claim: it happened")
        _, out, _ = self.run_cli("grade", "2", "--outcome", "no")
        self.assertEqual(json.loads(out)["result"], "70% claim: it did not happen")
        _, out, _ = self.run_cli("grade", "3", "--actual", "3.5")
        self.assertEqual(json.loads(out)["result"], "2 hours estimated, 3.5 actual: 1.75x")
        self.assertTrue(all("result" not in e for e in self.read()))  # computed, not stored

    # --- platform quirks ---------------------------------------------------
    def test_tags_are_casefolded(self):
        self.assertEqual(self.claim("c", 70, tags="Straße, STRASSE")["tags"], ["strasse"])

    def test_project_falls_back_to_full_path_at_drive_root(self):
        with mock.patch("journal.Path.cwd", return_value=Path("/")):
            e = journal.add_entry(self.path, type="claim", text="x", confidence=70)
        self.assertEqual(e["project"], str(Path("/")))

    def test_dates_are_strict_iso(self):
        for bad in ("20261005", "2026-W40-1", "2026-1-5"):
            with self.subTest(bad=bad):
                with self.assertRaises(JournalError):
                    journal.parse_date(bad)
        self.assertEqual(journal.parse_date("2026-10-05").isoformat(), "2026-10-05")

    def test_due_entries_require_strict_iso_dates(self):
        as_of = journal.parse_date("2026-10-02")
        compact = {"status": "open", "know_by": "20261001"}
        dashed = {"status": "open", "know_by": "2026-10-01"}
        self.assertFalse(journal._is_due(compact, as_of))
        self.assertTrue(journal._is_due(dashed, as_of))

    def test_symlinked_journal_is_written_through(self):
        real = Path(self.tmp.name) / "real.jsonl"
        real.write_text("")
        self.path.parent.mkdir(parents=True)
        try:
            os.symlink(real, self.path)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable here")
        self.claim("a")
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(len(real.read_text(encoding="utf-8").splitlines()), 1)


class ReviewFixTests(JournalCase):
    def test_unwritable_directory_fails_fast_not_forever(self):
        real_open = os.open

        def deny(path, *args, **kwargs):
            if str(path).endswith(".lock"):
                raise PermissionError(13, "denied")
            return real_open(path, *args, **kwargs)

        outcome = {}

        def attempt():
            try:
                with journal._locked(self.path, wait=0.3):
                    outcome["ok"] = True
            except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
                outcome["exc"] = exc

        with mock.patch("journal.os.open", side_effect=deny):
            worker = threading.Thread(target=attempt, daemon=True)
            worker.start()
            worker.join(5)
            self.assertFalse(worker.is_alive(), "lock acquisition never gave up")
            self.assertIsInstance(outcome.get("exc"), PermissionError)
            code, _, err = self.run_cli("add", "--type", "claim", "--text", "x", "--confidence", "70")
        self.assertEqual(code, 2)
        self.assertIn("cannot read or write", err)

    def test_readers_and_writers_do_not_collide(self):
        errors = []
        stop = threading.Event()

        def writer(i):
            try:
                for j in range(15):
                    journal.add_entry(
                        self.path, type="claim", text=f"c{i}-{j}", confidence=70, project="p"
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(("write", exc))

        def reader():
            try:
                while not stop.is_set():
                    journal.list_entries(self.path)
            except Exception as exc:  # noqa: BLE001
                errors.append(("read", exc))

        readers = [threading.Thread(target=reader) for _ in range(2)]
        writers = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in readers + writers:
            t.start()
        for t in writers:
            t.join()
        stop.set()
        for t in readers:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.read()), 60)

    def test_lock_release_leaves_a_lock_someone_else_took(self):
        lock = self.path.with_name(self.path.name + ".lock")
        with journal._locked(self.path):
            lock.write_text("someone-else")  # our lock was stolen and replaced while we held it
        self.assertTrue(lock.exists())
        self.assertEqual(lock.read_text(), "someone-else")

    def test_grade_result_keeps_large_and_precise_numbers(self):
        self.estimate("big", 2500000, unit="bytes")
        _, out, _ = self.run_cli("grade", "1", "--actual", "2.718281828")
        self.assertEqual(
            json.loads(out)["result"], "2500000 bytes estimated, 2.718281828 actual: <0.01x"
        )
        self.estimate("e2", 1)
        _, out, _ = self.run_cli("grade", "2", "--actual", "12345678")
        self.assertEqual(
            json.loads(out)["result"], "1 hours estimated, 12345678 actual: >1000x"
        )


class RoundTwoMinorTests(JournalCase):
    def test_confidence_errors_say_how_the_value_was_read(self):
        for text, fragment in (("1", "read as 100%"), ("0.3", "read as 30%"), ("100%", "read as 100%")):
            with self.subTest(text=text):
                with self.assertRaises(JournalError) as cm:
                    journal.parse_confidence(text)
                self.assertIn(fragment, str(cm.exception))
        code, _, err = self.run_cli("add", "--type", "claim", "--text", "x", "--confidence", "1")
        self.assertEqual(code, 2)
        self.assertIn("read as 100%", err)

    def test_tags_match_entries_logged_with_lowercase_tags(self):
        for tags in (["straße"], ["straße"], ["straße"], ["strasse"], ["strasse"]):
            e = self.claim("c", 70)
            journal.grade_entry(self.path, e["id"], outcome="yes")
        slots = journal.load(self.path)
        for (kind, value), tags in zip(slots, (["straße"], ["straße"], ["straße"], ["strasse"], ["strasse"])):
            value["tags"] = tags  # as 0.1.0 stored them (lower(), not casefold())
        journal.save(self.path, slots)
        self.assertEqual(list(journal.compute_stats(self.read())["tags"]), ["strasse"])
        _, out, _ = self.run_cli("stats", "--tag", "Straße")
        self.assertIn("5 graded", out)

    def test_os_error_names_the_file_that_failed(self):
        # os.replace(tmp, log) failing: filename is the temp file, filename2 the destination log
        failure = PermissionError(13, "Access is denied", "C:/x/.journal-1.tmp", None, "C:/the/log.jsonl")
        with mock.patch("journal.os.replace", side_effect=failure):
            code, _, err = self.run_cli("add", "--type", "claim", "--text", "x", "--confidence", "70")
        self.assertEqual(code, 2)
        self.assertIn("C:/the/log.jsonl", err)
        self.assertNotIn(".journal-1.tmp", err)
        with mock.patch("journal.os.replace", side_effect=OSError("disk full")):
            code, _, err = self.run_cli("add", "--type", "claim", "--text", "x", "--confidence", "70")
        self.assertEqual(code, 2)
        self.assertIn(str(self.path), err)  # no filename on the error: name the log

    def test_estimate_with_confidence_reports_the_real_mistake(self):
        code, _, err = self.run_cli(
            "add", "--type", "estimate", "--text", "x", "--unit", "hours",
            "--estimate", "2", "--confidence", "abc",
        )
        self.assertEqual(code, 2)
        self.assertIn("estimates take --estimate and --unit, not --confidence", err)

    def test_missing_subcommand_message_lists_the_commands(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            journal.main([])
        self.assertIn("{add,list,grade,stats}", err.getvalue())
        self.assertNotIn("cmd", err.getvalue())


class LegacyNumberTests(JournalCase):
    def test_result_line_prints_whole_floats_from_old_logs_as_whole_numbers(self):
        # 0.1.0 and 0.1.1 stored 3.0 as a float; the result line must still say "3 hours".
        self.estimate("m", 3)
        slots = journal.load(self.path)
        slots[0][1]["estimate"] = 3.0
        journal.save(self.path, slots)
        _, out, _ = self.run_cli("grade", "1", "--actual", "5.5")
        self.assertEqual(json.loads(out)["result"], "3 hours estimated, 5.5 actual: 1.83x")


if __name__ == "__main__":
    unittest.main()
