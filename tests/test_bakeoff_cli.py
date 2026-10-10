import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import helpers

sys.path.insert(0, str(helpers.REPO_ROOT / "plugins" / "model-bakeoff" / "skills" / "model-bakeoff" / "scripts"))
import bakeoff  # noqa: E402

FAKE = [sys.executable, str(helpers.REPO_ROOT / "tests" / "fake_claude_bakeoff.py")]
READER = "---\nname: reviewer\ndescription: Reviews code\ntools: Read, Grep\nmodel: opus\n---\nYou review code.\n"
WRITER = "---\nname: writer\ndescription: Writes code\ntools: Read, Edit, Bash\nmodel: opus\n---\nYou write.\n"


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
        (self.agents / "writer.md").write_text(WRITER, encoding="utf-8")
        self.cases_path = self.dir / "reviewer.cases.json"
        self.cases_path.write_text(json.dumps(cases()), encoding="utf-8")
        self.results = self.dir / "reviewer.results.json"
        self.script_path = self.dir / "script.json"
        os.environ["FAKE_CLAUDE_SCRIPT"] = str(self.script_path)
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_SCRIPT", None)
        self.log = self.dir / "log.jsonl"

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

    def run_all(self, *extra, agent="reviewer", cases_path=None, models="haiku,sonnet,opus"):
        return self.cli(
            "run", "--agent", agent, "--cases", str(cases_path or self.cases_path), "--agents-dir", str(self.agents),
            "--models", models, "--out", str(self.results), *extra,
        )


class AgentsAndCasesTests(Base):
    def test_agents_lists_tools_and_read_only(self):
        code, out, _ = self.cli("agents", "--agents-dir", str(self.agents), "--json")
        rows = {r["name"]: r for r in json.loads(out)}
        self.assertEqual(code, 0)
        self.assertTrue(rows["reviewer"]["readOnly"])
        self.assertFalse(rows["writer"]["readOnly"])

    def test_check_cases_prints_each_problem_and_exits_2(self):
        bad = self.dir / "bad.json"
        bad.write_text(json.dumps({"casesVersion": 2, "agent": "", "cases": []}), encoding="utf-8")
        code, out, err = self.cli("check-cases", "--cases", str(bad))
        self.assertEqual(code, 2)
        self.assertGreaterEqual((out + err).count("\n"), 3)

    def test_check_cases_ok(self):
        code, out, _ = self.cli("check-cases", "--cases", str(self.cases_path))
        self.assertEqual(code, 0)
        self.assertIn("2 cases", out)


class EstimateTests(Base):
    def test_a_read_only_agent_needs_no_opt_in(self):
        code, out, _ = self.cli("estimate", "--agent", "reviewer", "--cases", str(self.cases_path), "--agents-dir", str(self.agents))
        self.assertEqual(code, 0)
        self.assertIn("18 agent runs", out)  # 2 cases x 3 models x 3 runs
        self.assertIn("opt-in needed: no", out)
        self.assertEqual(self.calls(), [])  # estimate never calls claude

    def test_a_write_agent_lists_its_tools_and_needs_opt_in(self):
        self.cases_path.write_text(json.dumps(cases(agent="writer")), encoding="utf-8")
        code, out, _ = self.cli("estimate", "--agent", "writer", "--cases", str(self.cases_path), "--agents-dir", str(self.agents))
        self.assertIn("opt-in needed: yes", out)
        self.assertIn("Edit", out)
        self.assertIn("Bash", out)

    def test_command_checks_are_listed_and_need_opt_in(self):
        data = cases({"id": "c", "prompt": "p", "checks": [{"type": "command", "argv": ["python", "-V"]}]})
        self.cases_path.write_text(json.dumps(data), encoding="utf-8")
        code, out, _ = self.cli("estimate", "--agent", "reviewer", "--cases", str(self.cases_path), "--agents-dir", str(self.agents))
        self.assertIn("opt-in needed: yes", out)
        self.assertIn("python -V", out)

    def test_judge_calls_are_counted(self):
        data = cases({"id": "c", "prompt": "p", "checks": [{"type": "contains", "value": "x"}], "rubric": "r"})
        self.cases_path.write_text(json.dumps(data), encoding="utf-8")
        _, out, _ = self.cli("estimate", "--agent", "reviewer", "--cases", str(self.cases_path), "--agents-dir", str(self.agents))
        self.assertIn("9 agent runs", out)
        self.assertIn("9 judge calls", out)

    def test_unknown_agents_and_models_are_usage_errors(self):
        code, _, err = self.cli("estimate", "--agent", "ghost", "--cases", str(self.cases_path), "--agents-dir", str(self.agents))
        self.assertEqual(code, 2)
        self.assertIn("ghost", err)
        code, _, err = self.cli("estimate", "--agent", "reviewer", "--cases", str(self.cases_path), "--agents-dir", str(self.agents), "--models", "gpt")
        self.assertEqual(code, 2)
        self.assertIn("gpt", err)


class RunTests(Base):
    GOOD = {"result": "found a bug", "cost": 0.01}

    def test_a_read_only_run_writes_a_results_file_without_outputs(self):
        self.script(default=self.GOOD)
        code, out, err = self.run_all()
        self.assertEqual(code, 0, err)
        data = json.loads(self.results.read_text(encoding="utf-8"))
        self.assertEqual(data["resultsVersion"], 1)
        self.assertEqual(data["agent"], "reviewer")
        self.assertEqual(data["rule"], {"runs": 3, "minCaseRuns": 2, "minPassRate": 0.9})
        self.assertEqual(data["caseIds"], ["c1", "c2"])
        self.assertRegex(data["casesSha"], r"^[0-9a-f]{64}$")
        self.assertFalse(data["allowWrites"])
        self.assertEqual(data["currentModel"], "opus")
        self.assertEqual([m["requested"] for m in data["models"]], ["haiku", "sonnet", "opus"])
        self.assertEqual(data["models"][0]["resolvedId"], "claude-haiku-4-5-20251001")
        self.assertEqual(len(data["models"][0]["runs"]), 6)
        self.assertNotIn("output", data["models"][0]["runs"][0])
        self.assertEqual(len(self.calls()), 18)

    def test_a_write_agent_is_refused_without_the_opt_in_and_nothing_runs(self):
        self.script(default=self.GOOD)
        self.cases_path.write_text(json.dumps(cases(agent="writer")), encoding="utf-8")
        code, _, err = self.run_all(agent="writer")
        self.assertEqual(code, 2)
        self.assertIn("--allow-writes", err)
        self.assertIn("Edit", err)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.results.exists())

    def test_the_opt_in_lets_a_write_agent_run_in_write_mode(self):
        self.script(default=self.GOOD)
        self.cases_path.write_text(json.dumps(cases(agent="writer")), encoding="utf-8")
        code, _, err = self.run_all("--allow-writes", agent="writer", models="haiku")
        self.assertEqual(code, 0, err)
        argv = self.calls()[0]["argv"]
        self.assertIn("--allowedTools", argv)
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,Edit,Bash")
        self.assertTrue(json.loads(self.results.read_text(encoding="utf-8"))["allowWrites"])

    def test_the_opt_in_is_refused_when_nothing_needs_it(self):
        self.script(default=self.GOOD)
        code, _, err = self.run_all("--allow-writes")
        self.assertEqual(code, 2)
        self.assertIn("nothing", err)
        self.assertEqual(self.calls(), [])

    def test_a_command_check_needs_the_opt_in_but_the_agent_still_runs_read_only(self):
        data = cases({"id": "c", "prompt": "p", "checks": [{"type": "command", "argv": [sys.executable, "-c", "pass"]}]})
        self.cases_path.write_text(json.dumps(data), encoding="utf-8")
        self.script(default=self.GOOD)
        code, _, err = self.run_all(models="haiku")
        self.assertEqual(code, 2)
        self.assertIn("-c pass", err)
        code, _, err = self.run_all("--allow-writes", models="haiku")
        self.assertEqual(code, 0, err)
        argv = self.calls()[0]["argv"]
        self.assertIn("--tools", argv)
        self.assertNotIn("--allowedTools", argv)

    def test_more_than_60_runs_need_yes(self):
        items = [{"id": f"c{i}", "prompt": "p", "checks": [{"type": "contains", "value": "bug"}]} for i in range(8)]
        self.cases_path.write_text(json.dumps(cases(*items)), encoding="utf-8")
        self.script(default=self.GOOD)
        code, _, err = self.run_all()
        self.assertEqual(code, 2)
        self.assertIn("--yes", err)
        self.assertEqual(self.calls(), [])
        code, _, _ = self.run_all("--yes")
        self.assertEqual(code, 0)

    def test_invalid_cases_stop_before_any_call(self):
        self.cases_path.write_text(json.dumps({"casesVersion": 2}), encoding="utf-8")
        self.script(default=self.GOOD)
        code, _, err = self.run_all()
        self.assertEqual(code, 2)
        self.assertEqual(self.calls(), [])

    def test_cases_for_a_different_agent_are_refused(self):
        self.cases_path.write_text(json.dumps(cases(agent="someone-else")), encoding="utf-8")
        code, _, err = self.run_all()
        self.assertEqual(code, 2)
        self.assertIn("someone-else", err)

    def test_keep_outputs_stores_them(self):
        self.script(default=self.GOOD)
        self.run_all("--keep-outputs", models="haiku")
        run = json.loads(self.results.read_text(encoding="utf-8"))["models"][0]["runs"][0]
        self.assertEqual(run["output"], "found a bug")

    def test_an_aborted_run_is_saved_with_the_reason(self):
        self.script(default={"stdout_raw": "boom", "exit": 1})
        code, out, err = self.run_all(models="haiku")
        self.assertEqual(code, 2)
        data = json.loads(self.results.read_text(encoding="utf-8"))
        self.assertIn("in a row", data["aborted"])
        self.assertIn("aborted", (out + err).lower())


class DecideTests(Base):
    def make_results(self, behaviours):
        rules = [dict(model=model, **behaviour) for model, behaviour in behaviours.items()]
        self.script(default={"result": "no match", "cost": 0.01}, rules=rules)
        code, _, err = self.run_all()
        self.assertEqual(code, 0, err)

    def test_the_cheapest_passing_model_is_recommended(self):
        self.make_results({
            "haiku": {"result": "a bug", "cost": 0.002},
            "sonnet": {"result": "a bug", "cost": 0.01},
            "opus": {"result": "a bug", "cost": 0.05},
        })
        code, out, _ = self.cli("decide", "--results", str(self.results))
        self.assertEqual(code, 0)
        self.assertIn("haiku", out.split("recommendation")[1])
        self.assertIn("claude-haiku-4-5-20251001", out)
        self.assertIn("evidence on these cases", out)
        self.assertIn("not your subscription quota", out)

    def test_json_output(self):
        self.make_results({m: {"result": "a bug", "cost": c} for m, c in (("haiku", 0.002), ("sonnet", 0.01), ("opus", 0.05))})
        code, out, _ = self.cli("decide", "--results", str(self.results), "--json")
        data = json.loads(out)
        self.assertEqual(data["recommendation"]["model"], "haiku")
        self.assertEqual(data["recommendation"]["kind"], "change")
        self.assertEqual([m["requested"] for m in data["models"]], ["haiku", "sonnet", "opus"])

    def test_no_passing_model_is_exit_0_with_the_reason(self):
        self.make_results({m: {"result": "nothing", "cost": 0.01} for m in ("haiku", "sonnet", "opus")})
        code, out, _ = self.cli("decide", "--results", str(self.results))
        self.assertEqual(code, 0)
        self.assertIn("no model passed", out)

    def test_an_aborted_run_gets_no_recommendation(self):
        self.script(default={"stdout_raw": "boom", "exit": 1})
        self.run_all(models="haiku")
        code, out, err = self.cli("decide", "--results", str(self.results))
        self.assertEqual(code, 0)
        self.assertIn("aborted", out)
        self.assertNotIn("is the cheapest", out)

    def test_the_rule_can_be_tightened_when_deciding(self):
        self.make_results({m: {"result": "a bug", "cost": 0.01} for m in ("haiku", "sonnet", "opus")})
        code, out, _ = self.cli("decide", "--results", str(self.results), "--min-pass-rate", "1.0", "--json")
        self.assertEqual(json.loads(out)["rule"]["minPassRate"], 1.0)

    def test_the_current_model_already_cheapest(self):
        (self.agents / "reviewer.md").write_text(READER.replace("model: opus", "model: haiku"), encoding="utf-8")
        self.make_results({m: {"result": "a bug", "cost": c} for m, c in (("haiku", 0.002), ("sonnet", 0.01), ("opus", 0.05))})
        code, out, _ = self.cli("decide", "--results", str(self.results), "--json")
        self.assertEqual(json.loads(out)["recommendation"]["kind"], "unchanged")

    def test_bad_results_files_are_usage_errors(self):
        for text in ("{nope", json.dumps({"resultsVersion": 9}), "[]"):
            self.results.write_text(text, encoding="utf-8")
            code, _, err = self.cli("decide", "--results", str(self.results))
            self.assertEqual(code, 2, text)
            self.assertNotIn("Traceback", err)

    def test_control_characters_in_a_model_name_are_escaped_in_the_report(self):
        self.make_results({m: {"result": "a bug", "cost": 0.01} for m in ("haiku", "sonnet", "opus")})
        data = json.loads(self.results.read_text(encoding="utf-8"))
        data["models"][0]["resolvedId"] = "evil\x1b[2J"
        self.results.write_text(json.dumps(data), encoding="utf-8")
        code, out, _ = self.cli("decide", "--results", str(self.results))
        self.assertNotIn("\x1b", out)


class ResolveTests(Base):
    def test_resolve_reports_ids_and_what_changed(self):
        self.script(default={"result": "a bug", "cost": 0.01})
        self.run_all(models="haiku,sonnet")
        self.script(default={"result": "ok", "cost": 0.0001}, ids={"haiku": "claude-haiku-4-5-20251001", "sonnet": "claude-sonnet-6-0", "opus": "claude-opus-5-5"})
        code, out, _ = self.cli("resolve", "--results", str(self.results), "--json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["resolved"], {"haiku": "claude-haiku-4-5-20251001", "sonnet": "claude-sonnet-6-0"})
        self.assertEqual(data["changed"], ["sonnet"])
        self.assertEqual(data["new"], [])
        self.assertFalse(data["casesChanged"])

    def test_a_newly_named_model_and_a_changed_cases_file_are_reported(self):
        self.script(default={"result": "a bug", "cost": 0.01})
        self.run_all(models="haiku")
        self.cases_path.write_text(json.dumps(cases()) + "\n", encoding="utf-8")
        self.script(default={"result": "ok", "cost": 0.0001})
        code, out, _ = self.cli("resolve", "--results", str(self.results), "--models", "haiku,sonnet", "--cases", str(self.cases_path), "--json")
        data = json.loads(out)
        self.assertEqual(data["new"], ["sonnet"])
        self.assertTrue(data["casesChanged"])

    def test_a_probe_that_fails_is_reported_not_guessed(self):
        self.script(default={"stdout_raw": "boom", "exit": 1})
        code, out, _ = self.cli("resolve", "--models", "haiku", "--json")
        self.assertEqual(json.loads(out)["resolved"], {"haiku": None})

    def test_full_model_ids_are_not_probed(self):
        self.script(default={"result": "ok", "cost": 0.0001})
        code, out, _ = self.cli("resolve", "--models", "claude-haiku-4-5-20251001", "--json")
        self.assertEqual(json.loads(out)["resolved"], {"claude-haiku-4-5-20251001": "claude-haiku-4-5-20251001"})
        self.assertEqual(self.calls(), [])


class EditTests(Base):
    def test_plan_shows_a_diff_and_changes_nothing(self):
        code, out, _ = self.cli("plan", "--agent", "reviewer", "--model", "claude-haiku-4-5-20251001", "--agents-dir", str(self.agents))
        self.assertEqual(code, 0)
        self.assertIn("-model: opus", out)
        self.assertIn("+model: claude-haiku-4-5-20251001", out)
        self.assertEqual((self.agents / "reviewer.md").read_text(encoding="utf-8"), READER)

    def test_apply_backs_up_and_undo_restores(self):
        backups = self.dir / "backups"
        code, out, err = self.cli("apply", "--agent", "reviewer", "--model", "haiku", "--agents-dir", str(self.agents), "--backup-dir", str(backups))
        self.assertEqual(code, 0, err)
        self.assertIn("model: haiku", (self.agents / "reviewer.md").read_text(encoding="utf-8"))
        self.assertIn("undo", out)
        code, _, err = self.cli("undo", "--agent", "reviewer", "--backup-dir", str(backups))
        self.assertEqual(code, 0, err)
        self.assertEqual((self.agents / "reviewer.md").read_text(encoding="utf-8"), READER)

    def test_a_bad_model_and_a_bad_file_are_refused(self):
        code, _, err = self.cli("plan", "--agent", "reviewer", "--model", "gpt-4", "--agents-dir", str(self.agents))
        self.assertEqual(code, 2)
        (self.agents / "reviewer.md").write_text("no frontmatter\n", encoding="utf-8")
        code, _, err = self.cli("plan", "--agent", "reviewer", "--model", "haiku", "--agents-dir", str(self.agents))
        self.assertEqual(code, 2)


class SafetyTests(Base):
    def test_the_login_and_environment_are_never_printed(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test-secret-bakeoff"
        self.addCleanup(os.environ.pop, "ANTHROPIC_API_KEY", None)
        self.script(default={"result": "a bug", "cost": 0.01})
        code, out, err = self.run_all(models="haiku")
        self.assertNotIn("sk-test-secret-bakeoff", out + err)
        self.assertNotIn("sk-test-secret-bakeoff", self.results.read_text(encoding="utf-8"))

    def test_main_never_prints_a_traceback_for_bad_input(self):
        for argv in (["decide", "--results", str(self.dir / "nope.json")], ["check-cases", "--cases", str(self.dir / "nope.json")],
                     ["estimate", "--agent", "reviewer", "--cases", str(self.dir / "nope.json"), "--agents-dir", str(self.agents)]):
            code, out, err = self.cli(*argv)
            self.assertEqual(code, 2, argv)
            self.assertNotIn("Traceback", out + err)


if __name__ == "__main__":
    unittest.main()
