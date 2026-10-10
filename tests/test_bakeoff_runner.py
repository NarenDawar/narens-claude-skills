import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import helpers

sys.path.insert(0, str(helpers.REPO_ROOT / "plugins" / "model-bakeoff" / "skills" / "model-bakeoff" / "scripts"))
import bk_agents  # noqa: E402
import bk_runner as br  # noqa: E402

FAKE = [sys.executable, str(helpers.REPO_ROOT / "tests" / "fake_claude_bakeoff.py")]
AGENT = bk_agents.parse_definition("---\nname: reviewer\ndescription: Reviews code\ntools: Read, Grep\n---\nYou review code.\n")
WRITER = bk_agents.parse_definition("---\nname: writer\ndescription: Writes\ntools: Read, Edit, Bash\n---\nYou write.\n")


def case(cid="c1", checks=None, **over):
    base = {"id": cid, "prompt": f"Review {cid}", "checks": checks or [{"type": "contains", "value": "bug"}]}
    base.update(over)
    return base


def cases_data(*cases):
    return {"casesVersion": 1, "agent": "reviewer", "cases": list(cases) or [case()]}


OPTIONS = {"read_only": True, "max_run_usd": 1.0, "max_total_usd": 20.0, "timeout": 30, "keep_outputs": False, "judge_model": "opus"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
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

    def execute(self, definition=AGENT, data=None, models=("haiku",), runs=1, **opts):
        options = dict(OPTIONS, **opts)
        logs = []
        entries, aborted = br.execute(FAKE, definition, data or cases_data(), self.dir, list(models), runs, options, logs.append)
        return entries, aborted, logs


class ArgvTests(unittest.TestCase):
    def test_read_only_mode(self):
        argv = br.build_argv(["claude"], "haiku", "/tmp/a.json", "reviewer", True, ["Read", "Grep"], 1.0)
        self.assertEqual(argv[0], "claude")
        for flag in ("-p", "--no-session-persistence", "--strict-mcp-config"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
        self.assertEqual(argv[argv.index("--agents") + 1], "/tmp/a.json")
        self.assertEqual(argv[argv.index("--agent") + 1], "reviewer")
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "1")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("--allowedTools", argv)

    def test_read_only_mode_without_a_tool_list_uses_the_read_tools(self):
        argv = br.build_argv(["claude"], "haiku", "a.json", "r", True, None, 0.5)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "0.5")

    def test_read_only_mode_never_includes_a_write_tool(self):
        argv = br.build_argv(["claude"], "haiku", "a.json", "r", True, ["Read", "Bash", "Edit"], 1.0)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read")

    def test_write_mode_keeps_the_agents_own_tools(self):
        argv = br.build_argv(["claude"], "sonnet", "a.json", "w", False, ["Read", "Edit", "Bash"], 1.0)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,Edit,Bash")
        self.assertNotIn("--tools", argv)
        argv = br.build_argv(["claude"], "sonnet", "a.json", "w", False, None, 1.0)
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,Grep,Glob,Edit,Write,Bash")


class ParseTests(unittest.TestCase):
    def test_a_normal_result(self):
        out = json.dumps({
            "type": "result", "subtype": "success", "is_error": False, "result": "found a bug",
            "total_cost_usd": 0.0123, "num_turns": 3, "duration_ms": 456,
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 1},
            "modelUsage": {"claude-haiku-4-5-20251001": {"costUSD": 0.01}, "claude-other": {"costUSD": 0.002}},
        })
        info = br.parse_output(out)
        self.assertEqual(info["text"], "found a bug")
        self.assertEqual((info["cost"], info["turns"], info["durationMs"]), (0.0123, 3, 456))
        self.assertEqual(info["tokens"], 116)
        self.assertEqual(info["resolvedId"], "claude-haiku-4-5-20251001")
        self.assertFalse(info["isError"])

    def test_missing_fields_are_absent_not_invented(self):
        info = br.parse_output(json.dumps({"result": "x"}))
        self.assertEqual(info["text"], "x")
        self.assertIsNone(info["cost"])
        self.assertIsNone(info["resolvedId"])
        self.assertIsNone(info["turns"])

    def test_a_last_json_line_after_log_noise_is_used(self):
        info = br.parse_output("some warning\n" + json.dumps({"result": "ok", "total_cost_usd": 0.001}) + "\n")
        self.assertEqual(info["cost"], 0.001)

    def test_a_result_array_is_unwrapped(self):
        info = br.parse_output(json.dumps([{"type": "system"}, {"type": "result", "result": "done"}]))
        self.assertEqual(info["text"], "done")

    def test_garbage_is_none(self):
        for text in ("", "not json", "[]", "42", '"x"'):
            self.assertIsNone(br.parse_output(text))

    def test_a_non_numeric_cost_is_absent(self):
        self.assertIsNone(br.parse_output(json.dumps({"result": "x", "total_cost_usd": "free"}))["cost"])
        self.assertIsNone(br.parse_output(json.dumps({"result": "x", "total_cost_usd": True}))["cost"])


class ClassifyTests(unittest.TestCase):
    def info(self, **over):
        base = {"text": "", "cost": None, "turns": 1, "durationMs": 1, "isError": False, "subtype": "success", "tokens": 0, "resolvedId": None}
        base.update(over)
        return base

    def test_kinds(self):
        self.assertIsNone(br.classify(self.info(), 0, False))
        self.assertEqual(br.classify(None, 0, True), "timeout")
        self.assertEqual(br.classify(self.info(), 0, True), "timeout")
        self.assertEqual(br.classify(None, 1, False), "cli")
        self.assertEqual(br.classify(None, 0, False), "no-result")
        self.assertEqual(br.classify(self.info(isError=True, subtype="error_max_budget_usd"), 1, False), "budget")
        self.assertEqual(br.classify(self.info(isError=True, subtype="error_max_turns"), 1, False), "limit")
        self.assertEqual(br.classify(self.info(isError=True, subtype="error_during_execution"), 1, False), "cli")
        self.assertEqual(br.classify(self.info(), 2, False), "cli")


class ExecuteTests(Base):
    def test_a_passing_run_is_recorded_with_cost_and_resolved_model(self):
        self.script(default={"result": "found a bug", "cost": 0.004})
        entries, aborted, _ = self.execute()
        self.assertIsNone(aborted)
        run = entries[0]["runs"][0]
        self.assertEqual((run["case"], run["run"], run["passed"], run["kind"]), ("c1", 1, True, None))
        self.assertEqual(run["cost"], 0.004)
        self.assertEqual(run["resolvedId"], "claude-haiku-4-5-20251001")
        self.assertEqual(entries[0]["requested"], "haiku")
        self.assertEqual(entries[0]["resolvedId"], "claude-haiku-4-5-20251001")
        self.assertEqual(run["checks"], [{"type": "contains", "passed": True, "detail": "found"}])
        self.assertNotIn("output", run)

    def test_a_failing_check_fails_the_run(self):
        self.script(default={"result": "all fine", "cost": 0.001})
        entries, _, _ = self.execute()
        self.assertFalse(entries[0]["runs"][0]["passed"])
        self.assertIsNone(entries[0]["runs"][0]["kind"])

    def test_every_model_case_and_run_is_executed_in_order(self):
        self.script(default={"result": "bug", "cost": 0.001})
        entries, _, _ = self.execute(data=cases_data(case("a"), case("b")), models=("haiku", "sonnet"), runs=2)
        self.assertEqual([e["requested"] for e in entries], ["haiku", "sonnet"])
        self.assertEqual([(r["case"], r["run"]) for r in entries[0]["runs"]], [("a", 1), ("a", 2), ("b", 1), ("b", 2)])
        self.assertEqual(len(self.calls()), 8)

    def test_the_prompt_goes_on_stdin_and_the_agent_file_is_not_in_the_scratch_folder(self):
        self.script(default={"result": "bug", "cost": 0.001})
        self.execute()
        call = self.calls()[0]
        self.assertEqual(call["stdin"], "Review c1")
        self.assertEqual(call["files"], [])
        self.assertEqual(call["argv"][call["argv"].index("--model") + 1], "haiku")
        self.assertNotEqual(Path(call["cwd"]).resolve(), self.dir.resolve())

    def test_the_agent_definition_file_has_the_agent_and_no_model(self):
        self.script(default={"result": "bug", "cost": 0.001})
        self.execute()
        call = self.calls()[0]
        self.assertTrue(call["argv"][call["argv"].index("--agents") + 1].endswith(".json"))
        self.assertEqual(
            json.loads(call["agents_file"]),
            {"reviewer": {"description": "Reviews code", "prompt": "You review code.", "tools": ["Read", "Grep"]}},
        )

    def test_scratch_directories_are_removed_on_success_and_on_failure(self):
        self.script(default={"result": "bug", "cost": 0.001}, rules=[{"model": "sonnet", "exit": 1, "stdout_raw": "boom"}])
        self.execute(models=("haiku", "sonnet"))
        for call in self.calls():
            self.assertFalse(Path(call["cwd"]).exists(), call["cwd"])

    def test_the_fixture_is_copied_into_the_scratch_folder_and_the_original_is_untouched(self):
        (self.dir / "fx").mkdir()
        (self.dir / "fx" / "a.txt").write_text("hi", encoding="utf-8")
        self.script(default={"result": "bug", "cost": 0.001, "write_files": {"a.txt": "changed", "new.txt": "n"}})
        self.execute(data=cases_data(case(fixture="fx")))
        self.assertEqual(self.calls()[0]["files"], ["a.txt"])
        self.assertEqual((self.dir / "fx" / "a.txt").read_text(encoding="utf-8"), "hi")
        self.assertFalse((self.dir / "fx" / "new.txt").exists())

    def test_command_checks_run_in_the_scratch_folder_after_the_agent(self):
        script = "import os, sys; sys.exit(0 if open('out.txt').read() == 'made' else 4)"
        checks = [{"type": "command", "argv": [sys.executable, "-c", script]}]
        self.script(default={"result": "x", "cost": 0.001, "write_files": {"out.txt": "made"}})
        entries, _, _ = self.execute(definition=WRITER, data=cases_data(case(checks=checks)), read_only=False)
        self.assertTrue(entries[0]["runs"][0]["passed"])

    def test_errors_are_failed_runs_with_a_kind(self):
        self.script(default={"result": "bug", "cost": 0.001}, rules=[
            {"model": "sonnet", "subtype": "error_max_budget_usd", "is_error": True, "exit": 1},
            {"model": "opus", "stdout_raw": "garbage", "exit": 0},
        ])
        entries, _, _ = self.execute(models=("haiku", "sonnet", "opus"))
        kinds = {e["requested"]: e["runs"][0]["kind"] for e in entries}
        self.assertEqual(kinds, {"haiku": None, "sonnet": "budget", "opus": "no-result"})
        self.assertFalse(entries[1]["runs"][0]["passed"])
        self.assertFalse(entries[2]["runs"][0]["passed"])

    def test_a_timeout_stops_the_run_and_is_recorded(self):
        self.script(default={"result": "bug", "cost": 0.001, "sleep": 20})
        started = time.monotonic()
        entries, _, _ = self.execute(timeout=1)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(entries[0]["runs"][0]["kind"], "timeout")

    def test_five_cli_failures_in_a_row_stop_the_whole_bakeoff_with_the_reason(self):
        self.script(default={"stdout_raw": "boom", "exit": 1})
        entries, aborted, _ = self.execute(data=cases_data(*[case(f"c{i}") for i in range(8)]))
        self.assertIn("5", aborted)
        self.assertIn("claude", aborted)
        self.assertEqual(len(entries[0]["runs"]), 5)
        self.assertEqual(len(self.calls()), 5)

    def test_a_check_failure_does_not_count_toward_the_early_stop(self):
        self.script(default={"result": "nothing relevant", "cost": 0.001})
        entries, aborted, _ = self.execute(data=cases_data(*[case(f"c{i}") for i in range(8)]))
        self.assertIsNone(aborted)
        self.assertEqual(len(entries[0]["runs"]), 8)

    def test_a_success_resets_the_failure_streak(self):
        # c0..c3 fail (4 in a row), c4 succeeds and resets the streak, c5..c9 fail (5 in a row): stop after c9.
        self.script(default={"stdout_raw": "boom", "exit": 1}, rules=[{"prompt_contains": "Review c4", "result": "bug", "cost": 0.001}])
        entries, aborted, _ = self.execute(data=cases_data(*[case(f"c{i}") for i in range(11)]))
        self.assertIsNotNone(aborted)
        self.assertEqual(len(entries[0]["runs"]), 10)
        self.assertTrue(entries[0]["runs"][4]["passed"])

    def test_the_total_spend_cap_stops_between_runs(self):
        self.script(default={"result": "bug", "cost": 0.6})
        entries, aborted, _ = self.execute(data=cases_data(*[case(f"c{i}") for i in range(5)]), max_total_usd=1.0)
        self.assertIn("spend cap", aborted)
        self.assertEqual(len(entries[0]["runs"]), 2)

    def test_outputs_are_stored_only_on_request_and_truncated(self):
        self.script(default={"result": "bug " + "x" * 30000, "cost": 0.001})
        entries, _, _ = self.execute()
        self.assertNotIn("output", entries[0]["runs"][0])
        entries, _, _ = self.execute(keep_outputs=True)
        self.assertEqual(len(entries[0]["runs"][0]["output"]), 20000)

    def test_missing_cost_is_recorded_as_none(self):
        self.script(default={"result": "bug"})
        entries, _, _ = self.execute()
        self.assertIsNone(entries[0]["runs"][0]["cost"])

    def test_progress_lines_are_logged(self):
        self.script(default={"result": "bug", "cost": 0.001})
        _, _, logs = self.execute()
        self.assertTrue(any("haiku" in line for line in logs))


class JudgeTests(Base):
    RUBRIC = case(rubric="Names the real bug.")

    def test_the_judge_can_only_make_a_case_stricter(self):
        self.script(default={"result": "bug found", "cost": 0.001}, rules=[
            {"model": "opus", "prompt_contains": "grading", "result": '{"pass": false, "reason": "invented extras"}', "cost": 0.01},
        ])
        entries, _, _ = self.execute(data=cases_data(self.RUBRIC))
        run = entries[0]["runs"][0]
        self.assertFalse(run["passed"])
        self.assertEqual(run["judge"], {"passed": False, "reason": "invented extras"})
        self.assertEqual(run["judgeCost"], 0.01)

    def test_a_judge_pass_does_not_rescue_a_failed_check(self):
        self.script(default={"result": "nothing", "cost": 0.001}, rules=[
            {"model": "opus", "prompt_contains": "grading", "result": '{"pass": true, "reason": "ok"}', "cost": 0.01},
        ])
        entries, _, _ = self.execute(data=cases_data(self.RUBRIC))
        self.assertFalse(entries[0]["runs"][0]["passed"])

    def test_a_judge_pass_with_passing_checks_passes(self):
        self.script(default={"result": "bug found", "cost": 0.001}, rules=[
            {"model": "opus", "prompt_contains": "grading", "result": 'Verdict: {"pass": true, "reason": "good"}', "cost": 0.01},
        ])
        entries, _, _ = self.execute(data=cases_data(self.RUBRIC))
        run = entries[0]["runs"][0]
        self.assertTrue(run["passed"])
        self.assertEqual(run["judge"]["passed"], True)

    def test_an_unusable_judge_reply_fails_the_run_with_kind_judge(self):
        self.script(default={"result": "bug found", "cost": 0.001}, rules=[
            {"model": "opus", "prompt_contains": "grading", "result": "maybe", "cost": 0.01},
        ])
        entries, _, _ = self.execute(data=cases_data(self.RUBRIC))
        run = entries[0]["runs"][0]
        self.assertFalse(run["passed"])
        self.assertEqual(run["kind"], "judge")

    def test_the_judge_call_has_no_tools_and_sees_the_task_the_rubric_and_the_output(self):
        self.script(default={"result": "bug found", "cost": 0.001}, rules=[
            {"model": "opus", "prompt_contains": "grading", "result": '{"pass": true, "reason": "x"}', "cost": 0.01},
        ])
        self.execute(data=cases_data(self.RUBRIC))
        judge_call = [c for c in self.calls() if "grading" in c["stdin"]][0]
        self.assertEqual(judge_call["argv"][judge_call["argv"].index("--tools") + 1], "")
        for part in ("Review c1", "Names the real bug.", "bug found"):
            self.assertIn(part, judge_call["stdin"])
        self.assertNotIn("--agents", judge_call["argv"])

    def test_judge_cost_counts_toward_the_total_cap_but_not_the_run_cost(self):
        self.script(default={"result": "bug found", "cost": 0.001}, rules=[
            {"model": "opus", "prompt_contains": "grading", "result": '{"pass": true, "reason": "x"}', "cost": 0.9},
        ])
        # Each run costs 0.001 plus a 0.9 judge call: after two runs 1.8 is spent, so the third never starts.
        data = cases_data(self.RUBRIC, dict(self.RUBRIC, id="c2"), dict(self.RUBRIC, id="c3"))
        entries, aborted, _ = self.execute(data=data, max_total_usd=1.0)
        self.assertIn("spend cap", aborted)
        self.assertEqual(len(entries[0]["runs"]), 2)
        self.assertEqual(entries[0]["runs"][0]["cost"], 0.001)


class ProbeTests(Base):
    def test_probe_returns_the_resolved_id(self):
        self.script(default={"result": "ok", "cost": 0.0001})
        self.assertEqual(br.probe(FAKE, "sonnet"), "claude-sonnet-5-5")

    def test_probe_returns_none_when_the_call_fails(self):
        self.script(default={"stdout_raw": "boom", "exit": 1})
        self.assertIsNone(br.probe(FAKE, "sonnet"))

    def test_probe_makes_no_tool_available(self):
        self.script(default={"result": "ok", "cost": 0.0001})
        br.probe(FAKE, "haiku")
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "")


if __name__ == "__main__":
    unittest.main()
