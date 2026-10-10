"""Regression tests from the final review of the model-bakeoff branch."""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers

sys.path.insert(0, str(helpers.REPO_ROOT / "plugins" / "model-bakeoff" / "skills" / "model-bakeoff" / "scripts"))
import agent_edit  # noqa: E402
import bakeoff  # noqa: E402
import bk_agents  # noqa: E402
import bk_cases  # noqa: E402
import bk_runner  # noqa: E402

FAKE = [sys.executable, str(helpers.REPO_ROOT / "tests" / "fake_claude_bakeoff.py")]
READER = "---\nname: reviewer\ndescription: Reviews code\ntools: Read, Grep\nmodel: opus\n---\nYou review code.\n"
GOOD = {"result": "found a bug", "cost": 0.01}


def cases(*items, agent="reviewer"):
    return {"casesVersion": 1, "agent": agent, "cases": list(items) or [
        {"id": "c1", "prompt": "Review c1", "checks": [{"type": "contains", "value": "bug"}]},
        {"id": "c2", "prompt": "Review c2", "checks": [{"type": "contains", "value": "bug"}]},
    ]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.agents = self.dir / "agents"
        self.agents.mkdir()
        (self.agents / "reviewer.md").write_text(READER, encoding="utf-8")
        self.cases_path = self.dir / "reviewer.cases.json"
        self.cases_path.write_text(json.dumps(cases()), encoding="utf-8")
        self.results = self.dir / "reviewer.results.json"
        self.script_path = self.dir / "script.json"
        self.log = self.dir / "log.jsonl"
        os.environ["FAKE_CLAUDE_SCRIPT"] = str(self.script_path)
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_SCRIPT", None)

    def script(self, **parts):
        parts.setdefault("log", str(self.log))
        self.script_path.write_text(json.dumps(parts), encoding="utf-8")

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = bakeoff.main(list(argv), claude=FAKE)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def run_all(self, *extra, models="haiku,sonnet,opus"):
        return self.cli(
            "run", "--agent", "reviewer", "--cases", str(self.cases_path), "--agents-dir", str(self.agents),
            "--models", models, "--out", str(self.results), *extra,
        )

    def saved(self):
        return json.loads(self.results.read_text(encoding="utf-8"))


class NameAndToolInjectionTests(unittest.TestCase):
    def test_agent_names_with_shell_or_path_characters_are_refused(self):
        for name in ("rev&echo>x", "a|b", "a b", "../../escaped", "-p", "a;b", "a%b%", "a^b", "a\"b", "a<b"):
            with self.subTest(name):
                with self.assertRaises(agent_edit.AgentError) as ctx:
                    bk_agents.parse_definition(f"---\nname: {name}\n---\nB\n")
                self.assertIn("agent name", str(ctx.exception))

    def test_a_file_name_that_is_not_a_safe_name_is_refused_too(self):
        with self.assertRaises(agent_edit.AgentError):
            bk_agents.parse_definition("---\ndescription: d\n---\nB\n", "my agent&x")

    def test_ordinary_names_are_fine(self):
        for name in ("reviewer", "code-reviewer", "a.b_c", "R2D2"):
            self.assertEqual(bk_agents.parse_definition(f"---\nname: {name}\n---\nB\n")["name"], name)

    def test_tool_names_with_shell_characters_are_refused(self):
        for tool in ("Bash&echo>x", "Read|x", "Read;x", "Read%PATH%", "Read^x", "Read<x", 'Read"x'):
            with self.subTest(tool):
                with self.assertRaises(agent_edit.AgentError) as ctx:
                    bk_agents.parse_definition(f"---\nname: a\ntools: Read, {tool}\n---\nB\n")
                self.assertIn("tool", str(ctx.exception))

    def test_ordinary_tool_names_are_fine(self):
        d = bk_agents.parse_definition("---\nname: a\ntools: Read, Bash(git *), mcp__srv__tool-1, WebFetch\n---\nB\n")
        self.assertEqual(d["tools"], ["Read", "Bash(git *)", "mcp__srv__tool-1", "WebFetch"])


class ShimTests(unittest.TestCase):
    def test_a_cmd_shim_refuses_arguments_cmd_would_interpret(self):
        for arg in ("a&b", "a|b", "a<b", "a>b", "a^b", "a%PATH%", 'a"b', "a\nb"):
            with self.subTest(arg):
                self.assertIsNotNone(bk_cases.shim_problem(["C:/x/claude.CMD", "--agent", arg], "nt"))
                self.assertIsNotNone(bk_cases.shim_problem(["tool.bat", arg], "nt"))

    def test_clean_arguments_programs_and_other_systems_are_fine(self):
        self.assertIsNone(bk_cases.shim_problem(["C:/x/claude.cmd", "--tools", "Read,Grep", "--allowedTools", "Bash(git *)"], "nt"))
        self.assertIsNone(bk_cases.shim_problem(["python", "a&b"], "nt"))
        self.assertIsNone(bk_cases.shim_problem(["claude.exe", "a&b"], "nt"))
        self.assertIsNone(bk_cases.shim_problem(["x.cmd", "a&b"], "posix"))

    @unittest.skipUnless(os.name == "nt", "cmd.exe shims exist only on Windows")
    def test_run_claude_does_not_run_cmd_injection_through_a_real_shim(self):
        with tempfile.TemporaryDirectory() as tmp:
            shim = Path(tmp) / "claude.cmd"
            shim.write_text("@echo off\r\necho {}\r\n", encoding="utf-8")
            marker = Path(tmp) / "INJECTED.txt"
            with self.assertRaises(bk_runner.RunnerError):
                bk_runner.run_claude([str(shim), "--agent", f"x&echo hi>{marker}"], "prompt", tmp, 20)
            self.assertFalse(marker.exists())


class ResolveProgramTests(unittest.TestCase):
    def test_a_relative_path_is_resolved_inside_the_scratch_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            (scratch / "tools").mkdir()
            (scratch / "tools" / "check").write_text("x", encoding="utf-8")
            self.assertEqual(Path(bk_cases.resolve_program("tools/check", scratch)), scratch / "tools" / "check")
            self.assertEqual(Path(bk_cases.resolve_program("./tools/check", scratch)).resolve(), (scratch / "tools" / "check").resolve())
            self.assertIsNone(bk_cases.resolve_program("tools/missing", scratch))

    def test_a_bare_name_is_looked_up_on_PATH_only(self):
        with mock.patch.object(bk_cases.shutil, "which", return_value="/usr/bin/x") as which:
            self.assertEqual(bk_cases.resolve_program("x", Path("/tmp")), "/usr/bin/x")
        self.assertIn("path", which.call_args.kwargs)


class ParseSafetyTests(unittest.TestCase):
    def test_a_json_object_without_a_result_is_not_a_successful_run(self):
        self.assertIsNone(bk_runner.parse_output(json.dumps({"type": "error", "message": "boom"})))
        self.assertIsNone(bk_runner.parse_output(json.dumps({"foo": 1})))
        self.assertIsNone(bk_runner.parse_output(json.dumps({"result": 5})))

    def test_error_results_without_text_are_still_understood(self):
        info = bk_runner.parse_output(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True}))
        self.assertEqual(bk_runner.classify(info, 1, False), "limit")
        info = bk_runner.parse_output(json.dumps({"subtype": "error_max_budget_usd", "is_error": True}))
        self.assertEqual(bk_runner.classify(info, 1, False), "budget")


class ExecuteSafetyTests(Base):
    def execute(self, data=None, **opts):
        options = {"read_only": True, "max_run_usd": 1.0, "max_total_usd": 20.0, "timeout": 30, "keep_outputs": False, "judge_model": "opus"}
        options.update(opts)
        agent = bk_agents.parse_definition(READER)
        return bk_runner.execute(FAKE, agent, data or cases(), self.dir, ["haiku"], 1, options, lambda line: None)

    def test_a_reply_that_is_an_error_object_fails_even_a_not_contains_only_case(self):
        data = cases({"id": "c", "prompt": "p", "checks": [{"type": "not_contains", "value": "looks good"}]})
        self.script(default={"stdout_raw": json.dumps({"type": "error", "message": "boom"}), "exit": 0})
        entries, _ = self.execute(data)
        run = entries[0]["runs"][0]
        self.assertFalse(run["passed"])
        self.assertEqual(run["kind"], "no-result")

    def test_a_timed_out_run_counts_its_per_run_cap_toward_the_total(self):
        self.script(default={"result": "bug", "cost": 0.001, "sleep": 20})
        data = cases(*[{"id": f"c{i}", "prompt": "p", "checks": [{"type": "contains", "value": "bug"}]} for i in range(5)])
        started = time.monotonic()
        entries, aborted = self.execute(data, timeout=1, max_run_usd=0.6, max_total_usd=1.0)
        self.assertIn("spend cap", aborted)
        self.assertEqual(len(entries[0]["runs"]), 2)
        self.assertLess(time.monotonic() - started, 30)

    def test_judge_and_probe_calls_carry_a_budget_cap(self):
        self.script(default={"result": "bug", "cost": 0.001}, rules=[
            {"model": "opus", "prompt_contains": "grading", "result": '{"pass": true, "reason": "x"}', "cost": 0.001},
        ])
        self.execute(cases({"id": "c", "prompt": "p", "checks": [{"type": "contains", "value": "bug"}], "rubric": "r"}))
        judge_call = [c for c in self.calls() if "grading" in c["stdin"]][0]
        self.assertIn("--max-budget-usd", judge_call["argv"])
        self.log.unlink()
        bk_runner.probe(FAKE, "haiku")
        self.assertIn("--max-budget-usd", self.calls()[0]["argv"])

    def test_partial_results_survive_an_exception_passed_in(self):
        self.script(default=GOOD)
        collected = []
        count = {"n": 0}
        real = bk_runner.run_claude

        def flaky(*args, **kwargs):
            count["n"] += 1
            if count["n"] > 2:
                raise bk_runner.RunnerError("cannot start claude: gone")
            return real(*args, **kwargs)

        options = {"read_only": True, "max_run_usd": 1.0, "max_total_usd": 20.0, "timeout": 30, "keep_outputs": False, "judge_model": "opus"}
        agent = bk_agents.parse_definition(READER)
        with mock.patch.object(bk_runner, "run_claude", flaky):
            with self.assertRaises(bk_runner.RunnerError):
                bk_runner.execute(FAKE, agent, cases(), self.dir, ["haiku"], 2, options, lambda line: None, entries=collected)
        self.assertEqual(len(collected[0]["runs"]), 2)


class NumberTests(Base):
    BAD = [
        ("--runs", "0"), ("--runs", "-1"), ("--max-total-usd", "nan"), ("--max-total-usd", "0"), ("--max-total-usd", "inf"),
        ("--max-run-usd", "nan"), ("--max-run-usd", "-1"), ("--timeout", "nan"), ("--timeout", "0"),
        ("--min-pass-rate", "0"), ("--min-pass-rate", "2"), ("--min-pass-rate", "nan"),
        ("--min-case-runs", "0"), ("--min-case-runs", "4"),
    ]

    def test_bad_numbers_are_refused_before_any_call(self):
        self.script(default=GOOD)
        for flag, value in self.BAD:
            with self.subTest(flag + " " + value):
                code, out, err = self.run_all(flag, value)
                self.assertEqual(code, 2, (flag, value, out, err))
                self.assertIn(flag, err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(self.calls(), [])
                self.assertFalse(self.results.exists())

    def test_estimate_refuses_bad_numbers_too(self):
        for flag, value in (("--runs", "0"), ("--max-run-usd", "nan"), ("--max-total-usd", "-5")):
            code, _, err = self.cli("estimate", "--agent", "reviewer", "--cases", str(self.cases_path), "--agents-dir", str(self.agents), flag, value)
            self.assertEqual(code, 2, flag)
            self.assertIn(flag, err)

    def test_decide_overrides_are_validated(self):
        self.script(default=GOOD)
        self.run_all()
        for flag, value in (("--min-case-runs", "0"), ("--min-case-runs", "9"), ("--min-pass-rate", "0"), ("--min-pass-rate", "2"), ("--min-pass-rate", "nan")):
            with self.subTest(flag + " " + value):
                code, _, err = self.cli("decide", "--results", str(self.results), flag, value)
                self.assertEqual(code, 2)
                self.assertIn(flag, err)

    def test_good_boundary_values_work(self):
        self.script(default=GOOD)
        code, _, err = self.run_all("--runs", "1", "--min-case-runs", "1", "--min-pass-rate", "1", models="haiku")
        self.assertEqual(code, 0, err)


class OutputAndPartialResultsTests(Base):
    def test_an_output_path_that_is_a_folder_is_refused_before_any_run(self):
        self.script(default=GOOD)
        code, _, err = self.cli(
            "run", "--agent", "reviewer", "--cases", str(self.cases_path), "--agents-dir", str(self.agents), "--out", str(self.dir),
        )
        self.assertEqual(code, 2)
        self.assertIn("folder", err)
        self.assertEqual(self.calls(), [])

    def test_a_failure_in_the_middle_saves_what_ran_and_says_why(self):
        self.script(default=GOOD)
        real = bk_runner.run_claude
        count = {"n": 0}

        def flaky(*args, **kwargs):
            count["n"] += 1
            if count["n"] > 2:
                raise bk_runner.RunnerError("cannot start claude: gone")
            return real(*args, **kwargs)

        with mock.patch.object(bk_runner, "run_claude", flaky):
            code, _, err = self.run_all(models="haiku")
        self.assertEqual(code, 2)
        data = self.saved()
        self.assertIn("gone", data["aborted"])
        self.assertEqual(len(data["models"][0]["runs"]), 2)

    def test_ctrl_c_saves_what_ran(self):
        self.script(default=GOOD)
        real = bk_runner.run_claude
        count = {"n": 0}

        def interrupted(*args, **kwargs):
            count["n"] += 1
            if count["n"] > 1:
                raise KeyboardInterrupt()
            return real(*args, **kwargs)

        with mock.patch.object(bk_runner, "run_claude", interrupted):
            code, _, err = self.run_all(models="haiku")
        self.assertEqual(code, 2)
        data = self.saved()
        self.assertIn("interrupted", data["aborted"])
        self.assertEqual(len(data["models"][0]["runs"]), 1)

    def test_no_traceback_for_an_os_error(self):
        self.script(default=GOOD)
        with mock.patch.object(bakeoff, "_write_json", side_effect=PermissionError(13, "Permission denied")):
            code, _, err = self.run_all(models="haiku")
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)


class MergeTests(Base):
    def test_rerunning_one_model_keeps_the_others_and_decides_on_all_of_them(self):
        self.script(default={"result": "a bug", "cost": 0.001}, rules=[
            {"model": "sonnet", "result": "a bug", "cost": 0.01}, {"model": "opus", "result": "a bug", "cost": 0.05},
        ])
        self.run_all()
        self.script(default={"result": "a bug", "cost": 0.02})
        code, _, err = self.run_all(models="sonnet")
        self.assertEqual(code, 0, err)
        data = self.saved()
        self.assertEqual([m["requested"] for m in data["models"]], ["haiku", "sonnet", "opus"])
        sonnet = [m for m in data["models"] if m["requested"] == "sonnet"][0]
        self.assertEqual(sonnet["runs"][0]["cost"], 0.02)
        haiku = [m for m in data["models"] if m["requested"] == "haiku"][0]
        self.assertEqual(haiku["runs"][0]["cost"], 0.001)
        code, out, _ = self.cli("decide", "--results", str(self.results), "--json")
        self.assertEqual(json.loads(out)["recommendation"]["model"], "haiku")

    def test_a_new_model_is_added_after_the_saved_ones(self):
        self.script(default=GOOD)
        self.run_all(models="haiku")
        self.run_all(models="opus")
        self.assertEqual([m["requested"] for m in self.saved()["models"]], ["haiku", "opus"])

    def test_changed_cases_start_a_fresh_file(self):
        self.script(default=GOOD)
        self.run_all(models="haiku,sonnet")
        self.cases_path.write_text(json.dumps(cases()) + "\n", encoding="utf-8")
        code, out, err = self.run_all(models="sonnet")
        self.assertEqual(code, 0, err)
        self.assertEqual([m["requested"] for m in self.saved()["models"]], ["sonnet"])
        self.assertIn("cases", (out + err).lower())

    def test_a_different_rule_starts_a_fresh_file(self):
        self.script(default=GOOD)
        self.run_all(models="haiku,sonnet")
        self.run_all("--runs", "2", models="sonnet")
        self.assertEqual([m["requested"] for m in self.saved()["models"]], ["sonnet"])

    def test_an_aborted_rerun_never_overwrites_good_results(self):
        self.script(default=GOOD)
        self.run_all(models="haiku")
        before = self.results.read_text(encoding="utf-8")
        self.script(default={"stdout_raw": "boom", "exit": 1})
        code, _, err = self.run_all(models="sonnet")
        self.assertEqual(code, 2)
        self.assertEqual(self.results.read_text(encoding="utf-8"), before)
        aborted = self.results.with_name("reviewer.results.aborted.json")
        self.assertTrue(aborted.exists())
        self.assertIn("in a row", json.loads(aborted.read_text(encoding="utf-8"))["aborted"])
        self.assertIn("aborted.json", err)

    def test_a_previous_aborted_file_is_not_merged(self):
        self.script(default={"stdout_raw": "boom", "exit": 1})
        self.run_all(models="haiku")
        self.assertTrue(self.saved()["aborted"])
        self.script(default=GOOD)
        code, _, _ = self.run_all(models="sonnet")
        self.assertEqual(code, 0)
        data = self.saved()
        self.assertIsNone(data["aborted"])
        self.assertEqual([m["requested"] for m in data["models"]], ["sonnet"])


if __name__ == "__main__":
    unittest.main()
