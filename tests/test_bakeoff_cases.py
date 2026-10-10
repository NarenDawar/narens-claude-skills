import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import helpers

sys.path.insert(0, str(helpers.REPO_ROOT / "plugins" / "model-bakeoff" / "skills" / "model-bakeoff" / "scripts"))
import bk_cases as bc  # noqa: E402


def case(**over):
    base = {"id": "c1", "prompt": "Review it", "checks": [{"type": "contains", "value": "bug"}]}
    base.update(over)
    return base


def data(*cases, **over):
    base = {"casesVersion": 1, "agent": "reviewer", "cases": list(cases) or [case()]}
    base.update(over)
    return base


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def problems(self, value):
        return bc.problems(value, self.dir)


class ValidationTests(Base):
    def test_a_valid_file_has_no_problems(self):
        full = data(
            case(
                id="a",
                fixture="fx",
                checks=[
                    {"type": "contains", "value": "x", "ignoreCase": False},
                    {"type": "not_contains", "value": "y"},
                    {"type": "regex", "value": "line \\d+"},
                    {"type": "json", "required": ["issues"]},
                    {"type": "command", "argv": ["python", "-V"], "expectExit": 0, "timeoutSeconds": 30},
                ],
                rubric="Names the real bug.",
            )
        )
        (self.dir / "fx").mkdir()
        self.assertEqual(self.problems(full), [])

    def test_top_level_problems(self):
        self.assertTrue(any("must be a JSON object" in p for p in self.problems([])))
        self.assertTrue(any("unknown field 'x'" in p for p in self.problems(data(x=1))))
        for version in (None, 2, "1", True):
            with self.subTest(version):
                self.assertTrue(any("casesVersion must be 1" in p for p in self.problems(data(casesVersion=version))))
        self.assertTrue(any("agent must be" in p for p in self.problems(data(agent=""))))
        self.assertTrue(any("cases must be a non-empty list" in p for p in self.problems(data(cases=[]))))

    def test_case_problems(self):
        cases = [
            (case(extra=1), "unknown field 'extra'"),
            (case(id=""), "id must be a non-empty string"),
            (case(prompt="  "), "prompt must be a non-empty string"),
            (case(checks=[]), "checks must be a non-empty list"),
            (case(checks="x"), "checks must be a non-empty list"),
            (case(rubric=""), "rubric must be a non-empty string"),
            (case(rubric=5), "rubric must be a non-empty string"),
        ]
        for value, fragment in cases:
            with self.subTest(fragment):
                self.assertTrue(any(fragment in p for p in self.problems(data(value))), self.problems(data(value)))

    def test_duplicate_ids(self):
        self.assertTrue(any("duplicate id 'c1'" in p for p in self.problems(data(case(), case()))))

    def test_check_problems(self):
        checks = [
            ("nope", "must be an object"),
            ({"type": "nope"}, "unknown check type"),
            ({"type": "contains"}, "value must be a non-empty string"),
            ({"type": "contains", "value": ""}, "value must be a non-empty string"),
            ({"type": "contains", "value": "x", "ignoreCase": "yes"}, "ignoreCase must be true or false"),
            ({"type": "contains", "value": "x", "extra": 1}, "unknown field 'extra'"),
            ({"type": "regex", "value": "("}, "does not compile"),
            ({"type": "regex", "value": "a" * 501}, "longer than 500"),
            ({"type": "json"}, "required must be a list of strings"),
            ({"type": "json", "required": [1]}, "required must be a list of strings"),
            ({"type": "command"}, "argv must be a non-empty list of strings"),
            ({"type": "command", "argv": []}, "argv must be a non-empty list of strings"),
            ({"type": "command", "argv": ["x", 1]}, "argv must be a non-empty list of strings"),
            ({"type": "command", "argv": ["x"], "expectExit": "0"}, "expectExit must be an integer"),
            ({"type": "command", "argv": ["x"], "expectExit": True}, "expectExit must be an integer"),
            ({"type": "command", "argv": ["x"], "timeoutSeconds": 0}, "timeoutSeconds must be between 1 and 600"),
            ({"type": "command", "argv": ["x"], "timeoutSeconds": 601}, "timeoutSeconds must be between 1 and 600"),
        ]
        for check, fragment in checks:
            with self.subTest(fragment):
                found = self.problems(data(case(checks=[check])))
                self.assertTrue(any(fragment in p for p in found), found)

    def test_a_json_check_may_require_nothing(self):
        self.assertEqual(self.problems(data(case(checks=[{"type": "json", "required": []}]))), [])

    def test_problems_name_the_case_and_check(self):
        found = self.problems(data(case(checks=[{"type": "contains", "value": "x"}, {"type": "regex", "value": "("}])))
        self.assertTrue(any(p.startswith("case 'c1': checks[1]:") for p in found), found)
        for problem in found:
            self.assertNotIn("\n", problem)


class FixtureTests(Base):
    def test_a_good_fixture(self):
        (self.dir / "fx" / "sub").mkdir(parents=True)
        (self.dir / "fx" / "sub" / "a.txt").write_text("hi", encoding="utf-8")
        self.assertIsNone(bc.fixture_problem(self.dir, "fx"))

    def test_refusals(self):
        (self.dir / "fx").mkdir()
        outside = self.dir.parent / "outside-fixture"
        outside.mkdir(exist_ok=True)
        self.addCleanup(outside.rmdir)
        cases = {
            "missing": "does not exist",
            "../outside-fixture": "relative path inside",
            "fx/../../outside-fixture": "relative path inside",
            "fx/../fx": "relative path inside",  # stays inside, but ".." is refused outright
            str(outside): "relative path inside",
            "": "relative path inside",
        }
        for rel, fragment in cases.items():
            with self.subTest(rel):
                found = bc.fixture_problem(self.dir, rel)
                self.assertIsNotNone(found)
                self.assertIn(fragment, found)

    def test_a_symlink_inside_is_refused(self):
        (self.dir / "fx").mkdir()
        target = self.dir / "secret.txt"
        target.write_text("s", encoding="utf-8")
        try:
            os.symlink(target, self.dir / "fx" / "link.txt")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available here")
        self.assertIn("symlink", bc.fixture_problem(self.dir, "fx"))

    def test_too_many_files_or_too_big_is_refused(self):
        (self.dir / "many").mkdir()
        for i in range(201):
            (self.dir / "many" / f"{i}.txt").write_text("x", encoding="utf-8")
        self.assertIn("more than 200 files", bc.fixture_problem(self.dir, "many"))
        (self.dir / "big").mkdir()
        (self.dir / "big" / "a.bin").write_bytes(b"x" * (5 * 1024 * 1024 + 1))
        self.assertIn("larger than 5 MiB", bc.fixture_problem(self.dir, "big"))

    def test_copy_fixture_copies_and_never_touches_the_original(self):
        (self.dir / "fx" / "sub").mkdir(parents=True)
        (self.dir / "fx" / "sub" / "a.txt").write_text("hi", encoding="utf-8")
        scratch = self.dir / "scratch"
        scratch.mkdir()
        bc.copy_fixture(self.dir, {"fixture": "fx"}, scratch)
        self.assertEqual((scratch / "sub" / "a.txt").read_text(encoding="utf-8"), "hi")
        (scratch / "sub" / "a.txt").write_text("changed", encoding="utf-8")
        self.assertEqual((self.dir / "fx" / "sub" / "a.txt").read_text(encoding="utf-8"), "hi")

    def test_no_fixture_copies_nothing(self):
        scratch = self.dir / "scratch"
        scratch.mkdir()
        bc.copy_fixture(self.dir, {}, scratch)
        self.assertEqual(list(scratch.iterdir()), [])


class LoadTests(Base):
    def write(self, text, name="c.json"):
        path = self.dir / name
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        return path

    def test_load_returns_data_hash_and_folder(self):
        path = self.write(json.dumps(data()))
        loaded, problems, sha, base = bc.load(path)
        self.assertEqual((problems, loaded["agent"], base), ([], "reviewer", self.dir.resolve()))
        self.assertRegex(sha, r"^[0-9a-f]{64}$")
        self.assertEqual(sha, bc.load(path)[2])

    def test_unreadable_files_are_problems_not_crashes(self):
        cases = {
            "missing": (self.dir / "nope.json", "cannot read"),
            "bad json": (self.write("{nope", "bad.json"), "not valid JSON"),
            "not utf8": (self.write(b"\x80\x81", "u.json"), "not UTF-8"),
            "deep": (self.write("[" * 5000, "deep.json"), "not valid JSON"),
        }
        for label, (path, fragment) in cases.items():
            with self.subTest(label):
                loaded, problems, sha, base = bc.load(path)
                self.assertIsNone(loaded)
                self.assertTrue(any(fragment in p for p in problems), problems)

    def test_require_raises_with_the_first_problem(self):
        path = self.write(json.dumps(data(casesVersion=2)))
        with self.assertRaises(bc.CasesError) as ctx:
            bc.require(path)
        self.assertIn("casesVersion must be 1", str(ctx.exception))


class CheckTests(Base):
    def run_check(self, check, output):
        return bc.run_check(check, output, self.dir)

    def test_contains_ignores_case_by_default(self):
        self.assertTrue(self.run_check({"type": "contains", "value": "BUG"}, "found a bug")[0])
        self.assertFalse(self.run_check({"type": "contains", "value": "BUG", "ignoreCase": False}, "found a bug")[0])
        self.assertTrue(self.run_check({"type": "contains", "value": "bug", "ignoreCase": False}, "found a bug")[0])

    def test_not_contains(self):
        self.assertTrue(self.run_check({"type": "not_contains", "value": "looks good"}, "has a bug")[0])
        self.assertFalse(self.run_check({"type": "not_contains", "value": "looks good"}, "Looks Good")[0])

    def test_regex(self):
        self.assertTrue(self.run_check({"type": "regex", "value": r"line \d+"}, "see line 42")[0])
        self.assertFalse(self.run_check({"type": "regex", "value": r"line \d+"}, "see the line")[0])

    def test_json_whole_output_or_first_object_in_prose(self):
        check = {"type": "json", "required": ["issues"]}
        self.assertTrue(self.run_check(check, '{"issues": []}')[0])
        self.assertTrue(self.run_check(check, 'Here you go:\n```json\n{"issues": [1]}\n```')[0])
        self.assertFalse(self.run_check(check, '{"other": 1}')[0])
        self.assertFalse(self.run_check(check, "no json at all")[0])
        self.assertFalse(self.run_check(check, '["issues"]')[0])
        self.assertTrue(self.run_check({"type": "json", "required": []}, '{"a": 1}')[0])

    def test_huge_output_is_cut_before_checking(self):
        output = "x" * (bc.MAX_OUTPUT_EVAL + 10) + "needle"
        self.assertFalse(self.run_check({"type": "contains", "value": "needle"}, output)[0])

    def test_command_exit_code_and_scratch_directory(self):
        (self.dir / "marker.txt").write_text("m", encoding="utf-8")
        script = "import os, sys; sys.exit(0 if os.path.exists('marker.txt') else 3)"
        ok, detail = self.run_check({"type": "command", "argv": [sys.executable, "-c", script]}, "")
        self.assertTrue(ok, detail)
        ok, detail = self.run_check({"type": "command", "argv": [sys.executable, "-c", "import sys; sys.exit(3)"]}, "")
        self.assertFalse(ok)
        self.assertIn("exit 3", detail)
        ok, _ = self.run_check({"type": "command", "argv": [sys.executable, "-c", "import sys; sys.exit(3)"], "expectExit": 3}, "")
        self.assertTrue(ok)

    def test_command_timeout_and_missing_program(self):
        ok, detail = self.run_check(
            {"type": "command", "argv": [sys.executable, "-c", "import time; time.sleep(30)"], "timeoutSeconds": 1}, ""
        )
        self.assertFalse(ok)
        self.assertIn("timed out", detail)
        ok, detail = self.run_check({"type": "command", "argv": ["definitely-not-a-real-program-xyz"]}, "")
        self.assertFalse(ok)
        self.assertIn("cannot find", detail)

    def test_command_text_is_never_run_through_a_shell(self):
        marker = self.dir / "pwned.txt"
        self.run_check({"type": "command", "argv": [sys.executable, "-c", "pass", f"; touch {marker}"]}, "")
        self.assertFalse(marker.exists())

    def test_evaluate_runs_every_check_and_reports_each(self):
        c = case(checks=[{"type": "contains", "value": "bug"}, {"type": "contains", "value": "missing"}, {"type": "regex", "value": "b.g"}])
        results = bc.evaluate(c, "a bug", self.dir)
        self.assertEqual([r["passed"] for r in results], [True, False, True])
        self.assertEqual([r["type"] for r in results], ["contains", "contains", "regex"])

    def test_needs_execute_only_for_command_checks(self):
        self.assertFalse(bc.needs_execute(case()))
        self.assertTrue(bc.needs_execute(case(checks=[{"type": "command", "argv": ["x"]}])))


if __name__ == "__main__":
    unittest.main()
