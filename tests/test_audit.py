import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import helpers
import transcript_builder as tb

sys.path.insert(
    0,
    str(helpers.REPO_ROOT / "plugins" / "subagent-tax-auditor" / "skills" / "subagent-tax-auditor" / "scripts"),
)
import audit  # noqa: E402

FIXTURE_ROOT = helpers.REPO_ROOT / "tests" / "fixtures" / "audit" / "real_shape" / "projects"

# Round numbers for tests only; these are not real prices.
RATES = {
    "as_of": "2026-10-01",
    "unit": "usd_per_million_tokens",
    "models": {
        "claude-sonnet-5-5": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write_5m": 3.75, "cache_write_1h": 6},
        "claude-opus-5-5": {"input": 15, "output": 75, "cache_read": 1.5, "cache_write_5m": 18.75, "cache_write_1h": 30},
        "claude-haiku-4-5": {"input": 1, "output": 5, "cache_read": 0.1, "cache_write_5m": 1.25, "cache_write_1h": 2},
    },
}


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.projects = self.root / "projects"


class UsageTests(unittest.TestCase):
    def test_breakdown_is_used(self):
        usage = {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3,
                 "cache_creation_input_tokens": 10,
                 "cache_creation": {"ephemeral_5m_input_tokens": 4, "ephemeral_1h_input_tokens": 6}}
        self.assertEqual(audit.usage_counts(usage),
                         {"input": 1, "output": 2, "cache_read": 3, "cache_write_5m": 4, "cache_write_1h": 6})

    def test_missing_breakdown_counts_all_cache_writes_as_five_minute(self):
        counts = audit.usage_counts({"cache_creation_input_tokens": 10})
        self.assertEqual((counts["cache_write_5m"], counts["cache_write_1h"]), (10, 0))

    def test_unexplained_remainder_goes_to_five_minute(self):
        usage = {"cache_creation_input_tokens": 10,
                 "cache_creation": {"ephemeral_5m_input_tokens": 4, "ephemeral_1h_input_tokens": 3}}
        counts = audit.usage_counts(usage)
        self.assertEqual((counts["cache_write_5m"], counts["cache_write_1h"]), (7, 3))

    def test_bad_values_become_zero(self):
        counts = audit.usage_counts({"input_tokens": "x", "output_tokens": -5, "cache_read_input_tokens": True})
        self.assertEqual(sum(counts.values()), 0)


class ReadMessagesTests(TempCase):
    def read(self, records, raw_tail=()):
        path = self.root / "t.jsonl"
        tb.write_jsonl(path, records, raw_tail)
        quality = audit.new_quality()
        start, messages = audit.read_messages(path, quality)
        return start, messages, quality

    def test_duplicate_ids_count_once_with_field_wise_maximum(self):
        first = tb.assistant("m1", inp=5, out=10, read=100)
        second = tb.assistant("m1", inp=5, out=50, read=100)
        third = tb.assistant("m1", inp=3, out=20, read=120)
        _, messages, _ = self.read([first, second, third])
        self.assertEqual(len(messages), 1)
        tokens = messages[0]["tokens"]
        self.assertEqual((tokens["input"], tokens["output"], tokens["cache_read"]), (5, 50, 120))

    def test_message_order_is_first_appearance(self):
        _, messages, _ = self.read([tb.assistant("a", out=1), tb.assistant("b", out=2), tb.assistant("a", out=3)])
        self.assertEqual([m["tokens"]["output"] for m in messages], [3, 2])

    def test_synthetic_models_are_ignored(self):
        _, messages, quality = self.read([tb.assistant("s", model="<synthetic>", out=5)])
        self.assertEqual(messages, [])
        self.assertEqual(quality["lines_without_usage"], 0)

    def test_assistant_line_without_usage_is_counted(self):
        record = {"type": "assistant", "timestamp": tb.DEFAULT_TS, "message": {"id": "x", "model": "claude-sonnet-5-5"}}
        _, messages, quality = self.read([record])
        self.assertEqual((messages, quality["lines_without_usage"]), ([], 1))

    def test_malformed_lines_are_skipped_and_counted(self):
        _, messages, quality = self.read([tb.assistant("a", out=1)], raw_tail=["{not json", "[1, 2]", ""])
        self.assertEqual(len(messages), 1)
        self.assertEqual(quality["malformed_lines"], 2)

    def test_non_utf8_line_is_malformed(self):
        path = self.root / "t.jsonl"
        path.write_bytes(json.dumps(tb.assistant("a", out=1)).encode() + b"\n" + b"\xff\xfe broken\n")
        quality = audit.new_quality()
        _, messages = audit.read_messages(path, quality)
        self.assertEqual((len(messages), quality["malformed_lines"]), (1, 1))

    def test_bom_on_the_first_line_is_tolerated(self):
        path = self.root / "t.jsonl"
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps(tb.assistant("a", out=1)).encode() + b"\r\n")
        quality = audit.new_quality()
        _, messages = audit.read_messages(path, quality)
        self.assertEqual((len(messages), quality["malformed_lines"]), (1, 0))

    def test_start_is_the_earliest_timestamp(self):
        start, _, _ = self.read([tb.assistant("a", ts="2026-10-02T00:00:00.000Z"), tb.user("2026-10-01T09:00:00.000Z")])
        self.assertEqual(start, "2026-10-01T09:00:00.000Z")

    def test_message_timestamp_keeps_the_earliest_content_block(self):
        _, messages, _ = self.read([
            tb.assistant("a", ts="2026-10-05T10:05:00.000Z", out=20),
            tb.assistant("a", ts="2026-10-05T10:00:00.000Z", out=10),
        ])
        self.assertEqual(messages[0]["timestamp"], "2026-10-05T10:00:00.000Z")
        self.assertEqual(messages[0]["tokens"]["output"], 20)

    def test_unreadable_file_is_counted(self):
        quality = audit.new_quality()
        self.assertEqual(audit.read_messages(self.root / "missing.jsonl", quality), (None, []))
        self.assertEqual(quality["unreadable_files"], 1)


class LoadUnitsTests(TempCase):
    def units(self, **kw):
        quality = audit.new_quality()
        dirs = audit.project_dirs(self.projects, everything=True)
        units = audit.load_units(dirs, kw.get("since"), kw.get("until"), quality)
        return units, quality

    def test_main_and_subagents_are_attributed(self):
        tb.add_session(self.projects, "C--p", "s1", [tb.assistant("m", out=1)], subagents=[
            {"id": "a1", "type": "Explore", "records": [tb.assistant("x", out=2, sidechain=True)]},
            {"id": "a2", "type": "reviewer", "records": [tb.assistant("y", out=3, sidechain=True)]},
        ])
        units, quality = self.units()
        self.assertEqual(sorted(u["type"] for u in units), ["Explore", "main", "reviewer"])
        self.assertEqual(quality["subagents_without_meta"], 0)

    def test_missing_meta_is_unknown_and_counted(self):
        tb.add_session(self.projects, "C--p", "s1", [tb.assistant("m", out=1)],
                       subagents=[{"id": "a1", "meta": False, "records": [tb.assistant("x", out=2)]}])
        units, quality = self.units()
        self.assertIn("unknown", [u["type"] for u in units])
        self.assertEqual(quality["subagents_without_meta"], 1)

    def test_garbled_meta_is_unknown(self):
        pdir = tb.add_session(self.projects, "C--p", "s1", [tb.assistant("m", out=1)],
                              subagents=[{"id": "a1", "records": [tb.assistant("x", out=2)]}])
        (pdir / "s1" / "subagents" / "agent-a1.meta.json").write_text("{nope", encoding="utf-8")
        units, quality = self.units()
        self.assertIn("unknown", [u["type"] for u in units])
        self.assertEqual(quality["subagents_without_meta"], 1)

    def test_subagents_are_found_even_without_the_main_session_file(self):
        pdir = tb.add_session(self.projects, "C--p", "s1", [tb.assistant("m", out=1)],
                              subagents=[{"id": "a1", "records": [tb.assistant("x", out=2)]}])
        (pdir / "s1.jsonl").unlink()
        units, _ = self.units()
        self.assertEqual([u["type"] for u in units], ["general-purpose"])

    def test_empty_transcripts_are_dropped(self):
        tb.write_jsonl(self.projects / "C--p" / "s1.jsonl", [tb.user()])
        units, _ = self.units()
        self.assertEqual(units, [])

    def test_date_filters_apply_to_each_file_start(self):
        tb.add_session(self.projects, "C--p", "old", [tb.assistant("a", ts="2026-09-01T10:00:00.000Z", out=1)])
        tb.add_session(self.projects, "C--p", "new", [tb.assistant("b", ts="2026-10-02T10:00:00.000Z", out=1)])
        units, _ = self.units(since="2026-10-01")
        self.assertEqual(len(units), 1)
        units, _ = self.units(until="2026-09-30")
        self.assertEqual(len(units), 1)

    def resumed_history(self):
        old = tb.assistant("old", ts="2026-10-01T10:00:00.000Z", out=100)
        tb.add_session(self.projects, "C--p", "a-original",
                       [tb.user("2026-10-01T09:00:00.000Z"), old])
        tb.add_session(self.projects, "C--p", "b-resumed", [
            tb.user("2026-10-05T09:00:00.000Z"), old,
            tb.assistant("new", ts="2026-10-05T09:01:00.000Z", out=50),
        ])

    def test_resumed_start_comes_from_owned_messages(self):
        self.resumed_history()
        units, _ = self.units()
        self.assertEqual([u["start"] for u in units],
                         ["2026-10-01T09:00:00.000Z", "2026-10-05T09:01:00.000Z"])
        self.assertEqual(sum(m["tokens"]["output"] for u in units for m in u["messages"]), 150)
        self.assertEqual([[m["id"] for m in u["messages"]] for u in units], [["old"], ["new"]])

    def test_resumed_work_is_included_by_since_and_excluded_by_until(self):
        self.resumed_history()
        units, _ = self.units(since="2026-10-05")
        self.assertEqual([[m["id"] for m in u["messages"]] for u in units], [["new"]])
        units, _ = self.units(until="2026-10-01")
        self.assertEqual([[m["id"] for m in u["messages"]] for u in units], [["old"]])

    def test_resumed_summary_is_after_snapshot_cutoff(self):
        self.resumed_history()
        units, _ = self.units()
        sums = [audit.summarize(u, RATES) for u in units]
        cutoff = "2026-10-03T00:00:00.000Z"
        self.assertEqual([s["tokens"]["output"] for s in sums if s["start"] > cutoff], [50])
        self.assertEqual([s["tokens"]["output"] for s in sums if s["start"] <= cutoff], [100])
        snapshot = audit.snapshot_data(
            [s for s in sums if s["start"] <= cutoff], RATES, {}, now=cutoff,
        )
        comparison = audit.compare(snapshot, sums, RATES)["types"][0]
        self.assertEqual(comparison["before"]["spawns"], 1)
        self.assertEqual(comparison["after"]["spawns"], 1)
        self.assertEqual(comparison["after"]["tokens_median"], 50)

    def test_resumed_message_without_timestamp_keeps_existing_fallback(self):
        old = tb.assistant("old", out=100)
        new = tb.assistant("new", out=50)
        del new["timestamp"]
        tb.add_session(self.projects, "C--p", "a-original", [old])
        tb.add_session(self.projects, "C--p", "b-resumed", [old, new])
        units, _ = self.units()
        self.assertEqual([u["start"] for u in units], [tb.DEFAULT_TS, tb.DEFAULT_TS])
        self.assertEqual([[m["id"] for m in u["messages"]] for u in units], [["old"], ["new"]])

    def test_resumed_subagent_uses_retained_message_start(self):
        old = tb.assistant("old", out=100)
        tb.add_session(self.projects, "C--p", "original", [old], subagents=[
            {"id": "resume", "type": "reviewer", "records": [
                old, tb.assistant("new", ts="2026-10-05T09:01:00.000Z", out=50),
            ]},
        ])
        units, _ = self.units(since="2026-10-05")
        self.assertEqual([(u["type"], u["start"]) for u in units],
                         [("reviewer", "2026-10-05T09:01:00.000Z")])

    def test_project_dirs_selects_by_slug_and_reports_missing(self):
        tb.add_session(self.projects, audit.slug_for(self.root / "proj"), "s1", [tb.assistant("m", out=1)])
        found = audit.project_dirs(self.projects, project=str(self.root / "proj"))
        self.assertEqual([p.name for p in found], [audit.slug_for(self.root / "proj")])
        self.assertEqual(audit.project_dirs(self.projects, project=str(self.root / "other")), [])
        with self.assertRaises(audit.AuditError):
            audit.project_dirs(self.root / "no-such-dir", everything=True)

    def test_slug_replaces_every_non_alphanumeric_character(self):
        slug = audit.slug_for(self.root / "my_proj.v2")
        self.assertRegex(slug, r"^[A-Za-z0-9-]+$")
        self.assertTrue(slug.endswith("my-proj-v2"))


class RatesTests(TempCase):
    def test_rate_for_exact_and_longest_prefix(self):
        rates = {"models": {"claude-sonnet": {"input": 1}, "claude-sonnet-5-5": {"input": 2}}}
        self.assertEqual(audit.rate_for(rates, "claude-sonnet-5-5")["input"], 2)
        self.assertEqual(audit.rate_for(rates, "claude-sonnet-5-5-20260101")["input"], 2)
        self.assertIsNone(audit.rate_for(rates, "claude-sonnet-4"))  # only a date suffix shares a price
        self.assertIsNone(audit.rate_for(rates, "claude-mystery-1"))

    def test_load_rates_validates(self):
        path = self.root / "rates.json"
        path.write_text(json.dumps(RATES), encoding="utf-8")
        self.assertEqual(audit.load_rates(path)["as_of"], "2026-10-01")
        bad = {"as_of": "2026-10-01", "models": {"m": {"input": 1}}}
        path.write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaises(audit.AuditError):
            audit.load_rates(path)
        path.write_text("{nope", encoding="utf-8")
        with self.assertRaises(audit.AuditError):
            audit.load_rates(path)
        with self.assertRaises(audit.AuditError):
            audit.load_rates(self.root / "absent.json")

    def test_rates_stale_after_ninety_days(self):
        self.assertFalse(audit.rates_stale(RATES, today=date(2026, 12, 29)))
        self.assertTrue(audit.rates_stale(RATES, today=date(2026, 12, 31)))

    def test_cost_of_uses_per_million_rates(self):
        tokens = {"input": 1_000_000, "output": 1_000_000, "cache_read": 1_000_000,
                  "cache_write_5m": 1_000_000, "cache_write_1h": 1_000_000}
        self.assertAlmostEqual(audit.cost_of(tokens, RATES["models"]["claude-sonnet-5-5"]), 3 + 15 + 0.3 + 3.75 + 6)


class SummarizeTests(unittest.TestCase):
    def unit(self, messages, kind="general-purpose"):
        return {"type": kind, "start": tb.DEFAULT_TS, "messages": messages, "description": "d"}

    def message(self, model, **tokens):
        full = dict.fromkeys(audit.KINDS, 0)
        full.update(tokens)
        return {"model": model, "tokens": full}

    def test_cost_and_fixed_context(self):
        unit = self.unit([
            self.message("claude-sonnet-5-5", input=1_000_000, cache_read=500, cache_write_5m=250),
            self.message("claude-sonnet-5-5", output=1_000_000),
        ])
        summary = audit.summarize(unit, RATES)
        self.assertAlmostEqual(summary["cost"], 3 + 15 + (500 * 0.3 + 250 * 3.75) / 1e6)
        self.assertEqual(summary["fixed"], 1_000_750)
        self.assertEqual(summary["messages"], 2)
        self.assertEqual(summary["first_model"], "claude-sonnet-5-5")

    def test_uncached_fixed_context_excludes_output_and_later_input(self):
        first = self.message("claude-sonnet-5-5", input=1200, output=50)
        original = first["tokens"].copy()
        summary = audit.summarize(self.unit([
            first, self.message("claude-sonnet-5-5", input=9000, output=70),
        ]), RATES)
        self.assertEqual(summary["fixed"], 1200)
        self.assertEqual(summary["tokens"]["input"], 10_200)
        self.assertEqual(summary["tokens"]["output"], 120)
        self.assertAlmostEqual(summary["cost"], (10_200 * 3 + 120 * 15) / 1e6)
        self.assertEqual(first["tokens"], original)

    def test_fixed_context_combines_all_first_request_input_categories(self):
        summary = audit.summarize(self.unit([
            self.message("claude-sonnet-5-5", input=100, cache_read=200,
                         cache_write_5m=300, cache_write_1h=400, output=500),
            self.message("claude-sonnet-5-5", input=999, cache_read=888, output=777),
        ]), RATES)
        self.assertEqual(summary["fixed"], 1000)
        self.assertEqual(summary["tokens"]["input"], 1099)
        self.assertEqual(summary["tokens"]["cache_read"], 1088)

    def test_fixed_context_uses_first_request_even_when_its_input_is_zero(self):
        summary = audit.summarize(self.unit([
            self.message("claude-sonnet-5-5", output=5),
            self.message("claude-sonnet-5-5", input=50),
        ]), RATES)
        self.assertEqual(summary["fixed"], 0)

    def test_uncached_fixed_context_does_not_require_a_known_model_rate(self):
        summary = audit.summarize(self.unit([
            self.message("claude-mystery-1", input=321, output=20),
        ]), RATES)
        self.assertEqual(summary["fixed"], 321)
        self.assertIsNone(summary["cost"])

    def test_unit_with_an_unrated_model_has_no_cost(self):
        summary = audit.summarize(self.unit([self.message("claude-mystery-1", output=10)]), RATES)
        self.assertIsNone(summary["cost"])
        self.assertEqual(summary["tokens"]["output"], 10)

    def test_a_unit_that_switches_models_keeps_per_model_tokens(self):
        unit = self.unit([self.message("claude-opus-5-5", output=1), self.message("claude-sonnet-5-5", output=2)], "main")
        summary = audit.summarize(unit, RATES)
        self.assertEqual({m: t["output"] for m, t in summary["per_model"].items()},
                         {"claude-opus-5-5": 1, "claude-sonnet-5-5": 2})


class RealShapeTests(unittest.TestCase):
    def test_real_shaped_tree_parses_and_deduplicates(self):
        quality = audit.new_quality()
        dirs = audit.project_dirs(FIXTURE_ROOT, everything=True)
        units = audit.load_units(dirs, None, None, quality)
        self.assertGreaterEqual(len(units), 3)  # one main session and two subagents
        self.assertEqual(quality["malformed_lines"], 0)
        for path in FIXTURE_ROOT.rglob("agent-*.jsonl"):
            lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            ids = [line["message"]["id"] for line in lines]
            self.assertGreater(len(ids), len(set(ids)), "fixture must contain a repeated message id")
        for unit in units:
            self.assertGreater(sum(m["tokens"]["output"] + m["tokens"]["cache_read"] for m in unit["messages"]), 0)


import contextlib
import io


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = audit.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ReportCase(TempCase):
    """main: one opus message; general-purpose: two sonnet runs; Explore: one run on an unrated model."""

    def setUp(self):
        super().setUp()
        self.project = self.root / "proj"
        self.slug = audit.slug_for(self.project)
        self.rates_path = self.root / "rates.json"
        self.rates_path.write_text(json.dumps(RATES), encoding="utf-8")
        gp = lambda n: {"id": f"gp{n}", "type": "general-purpose", "description": "a task", "records": [  # noqa: E731
            tb.assistant(f"g{n}a", read=1_000_000, write5=500_000, out=1_000, sidechain=True),
            tb.assistant(f"g{n}b", read=1_500_000, out=1_000, sidechain=True)]}
        explore = {"id": "ex1", "type": "Explore", "records": [
            tb.assistant("e1", model="claude-mystery-1", read=100, out=500, sidechain=True)]}
        tb.add_session(self.projects, self.slug, "s1",
                       [tb.assistant("m1", model="claude-opus-5-5", write5=2_000_000, out=10_000)],
                       subagents=[gp(1), gp(2), explore])
        tb.add_session(self.projects, self.slug, "old", [tb.assistant("o1", ts="2026-09-01T10:00:00.000Z", out=1)])

    def report(self, **kw):
        quality = audit.new_quality()
        dirs = audit.project_dirs(self.projects, project=str(self.project))
        units = audit.load_units(dirs, kw.get("since"), kw.get("until"), quality)
        sums = [audit.summarize(u, RATES) for u in units]
        return audit.build_report(sums, RATES, quality, {"project": str(self.project)},
                                  show_descriptions=kw.get("show", False), today=date(2026, 10, 3))

    def row(self, report, kind):
        return next(r for r in report["rows"] if r["type"] == kind)


class ReportTests(ReportCase):
    def test_uncached_spawns_have_fixed_context_in_json_and_text_reports(self):
        tb.add_session(self.projects, self.slug, "uncached", [
            tb.assistant("uncached-main", out=1),
        ], subagents=[
            {"id": "uncached1", "type": "uncached-test", "records": [
                tb.assistant("uncached1a", inp=1200, out=10, sidechain=True),
            ]},
            {"id": "uncached2", "type": "uncached-test", "records": [
                tb.assistant("uncached2a", inp=2400, out=15, sidechain=True),
                tb.assistant("uncached2b", inp=9600, out=5, sidechain=True),
            ]},
        ])
        report = self.report(since="2026-10-01")
        row = self.row(report, "uncached-test")
        self.assertEqual(row["fixed_context_median"], 1800)
        self.assertEqual((row["spawns"], row["messages"]), (2, 3))
        self.assertEqual((row["tokens"]["input"], row["tokens"]["output"]), (13_200, 30))
        self.assertAlmostEqual(row["cost"], (13_200 * 3 + 30 * 15) / 1e6)
        decoded = json.loads(json.dumps(report))
        json_row = self.row(decoded, "uncached-test")
        self.assertEqual(json_row["fixed_context_median"], 1800)
        text_row = next(line for line in audit.format_report(report).splitlines()
                        if line.startswith("uncached-test"))
        self.assertIn("1,800", text_row)

    def test_rows_costs_and_shares(self):
        report = self.report(since="2026-10-01")
        main = self.row(report, "main")
        self.assertAlmostEqual(main["cost"], 2_000_000 * 18.75 / 1e6 + 10_000 * 75 / 1e6)  # 38.25
        gp = self.row(report, "general-purpose")
        self.assertEqual((gp["spawns"], gp["messages"]), (2, 4))
        self.assertAlmostEqual(gp["cost"], 2 * (0.3 + 1.875 + 0.015 + 0.45 + 0.015))  # 5.31
        self.assertEqual(gp["fixed_context_median"], 1_500_000)
        self.assertEqual(gp["tokens"]["output"], 4_000)
        total = 38.25 + 5.31
        self.assertAlmostEqual(report["totals"]["cost"], total)
        self.assertAlmostEqual(gp["cost_share"], 5.31 / total)
        self.assertAlmostEqual(gp["output_share"], 4_000 / 14_500)

    def test_unrated_model_shows_no_rate_and_stays_out_of_cost(self):
        report = self.report(since="2026-10-01")
        explore = self.row(report, "Explore")
        self.assertIsNone(explore["cost"])
        self.assertFalse(explore["has_rate"])
        self.assertIsNone(explore["cost_share"])
        self.assertEqual(report["quality"]["models_without_rate"], {"claude-mystery-1": 600})
        self.assertIn("no rate", audit.format_report(report))

    def test_rows_are_sorted_by_cost_with_unrated_last(self):
        report = self.report(since="2026-10-01")
        self.assertEqual([r["type"] for r in report["rows"]], ["main", "general-purpose", "Explore"])

    def test_date_filter_excludes_old_sessions(self):
        self.assertEqual(self.report(since="2026-10-01")["quality"]["sessions"], 1)
        self.assertEqual(self.report()["quality"]["sessions"], 2)

    def test_quality_footer_numbers(self):
        quality = self.report(since="2026-10-01")["quality"]
        self.assertEqual((quality["sessions"], quality["subagent_runs"]), (1, 3))
        self.assertEqual(quality["first_day"], "2026-10-01")

    def test_no_prompt_text_in_any_output(self):
        report = self.report(show=True)
        self.assertNotIn(tb.SECRET, json.dumps(report))
        self.assertNotIn(tb.SECRET, audit.format_report(report))

    def test_descriptions_only_when_asked(self):
        self.assertNotIn("descriptions", self.row(self.report(), "general-purpose"))
        shown = self.report(show=True)
        self.assertEqual(self.row(shown, "general-purpose")["descriptions"], ["a task"])
        self.assertIn("a task", audit.format_report(shown))

    def test_text_report_states_the_proxy_caveat_and_the_rates_date(self):
        text = audit.format_report(self.report(since="2026-10-01"))
        self.assertIn("proxy", text)
        self.assertIn("2026-10-01", text)
        self.assertIn("general-purpose", text)

    def test_stale_rates_are_flagged(self):
        quality = audit.new_quality()
        dirs = audit.project_dirs(self.projects, project=str(self.project))
        sums = [audit.summarize(u, RATES) for u in audit.load_units(dirs, None, None, quality)]
        report = audit.build_report(sums, RATES, quality, {}, today=date(2027, 3, 1))
        self.assertTrue(report["rates_stale"])
        self.assertIn("older than", audit.format_report(report))

    def test_empty_report_renders(self):
        report = audit.build_report([], RATES, audit.new_quality(), {})
        self.assertIn("No usage found", audit.format_report(report))


class ReportCliTests(ReportCase):
    def args(self, *extra):
        return ["report", "--projects-dir", str(self.projects), "--rates", str(self.rates_path),
                "--project", str(self.project), *extra]

    def test_json_matches_the_text_numbers(self):
        code, out, _ = run_cli(*self.args("--json", "--since", "2026-10-01"))
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertAlmostEqual(data["totals"]["cost"], 38.25 + 5.31)
        code, text, _ = run_cli(*self.args("--since", "2026-10-01"))
        self.assertEqual(code, 0)
        self.assertIn("$38.25", text)
        self.assertIn("$5.31", text)

    def test_all_flag_reads_every_project(self):
        tb.add_session(self.projects, "C--other", "s9", [tb.assistant("z", out=7)])
        code, out, _ = run_cli("report", "--all", "--projects-dir", str(self.projects),
                               "--rates", str(self.rates_path), "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["quality"]["sessions"], 3)

    def test_missing_projects_dir_is_an_error(self):
        code, _, err = run_cli("report", "--all", "--projects-dir", str(self.root / "nope"), "--rates", str(self.rates_path))
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error:"))

    def test_project_without_transcripts_is_a_message_not_an_error(self):
        code, out, _ = run_cli("report", "--projects-dir", str(self.projects), "--rates", str(self.rates_path),
                               "--project", str(self.root / "elsewhere"))
        self.assertEqual(code, 0)
        self.assertIn("No transcripts found", out)

    def test_bad_date_and_bad_rates_are_errors(self):
        code, _, err = run_cli(*self.args("--since", "10/01/2026"))
        self.assertEqual(code, 2)
        self.assertIn("YYYY-MM-DD", err)
        bad = self.root / "bad.json"
        bad.write_text("{nope", encoding="utf-8")
        code, _, err = run_cli("report", "--projects-dir", str(self.projects), "--rates", str(bad))
        self.assertEqual(code, 2)

    def test_malformed_lines_surface_in_the_footer(self):
        pdir = self.projects / self.slug
        with open(pdir / "s1.jsonl", "ab") as f:
            f.write(b"{broken\n")
        code, out, _ = run_cli(*self.args("--since", "2026-10-01"))
        self.assertEqual(code, 0)
        self.assertIn("1 malformed", out)


BEFORE = "2026-10-01T10:00:00.000Z"
AFTER = "2026-10-05T10:00:00.000Z"
STAMP = "2026-10-03T00:00:00.000Z"


class CompareCase(TempCase):
    def setUp(self):
        super().setUp()
        self.project = self.root / "proj"
        self.slug = audit.slug_for(self.project)
        self.rates_path = self.root / "rates.json"
        self.rates_path.write_text(json.dumps(RATES), encoding="utf-8")
        self.count = 0

    def spawns(self, kind, model, n, ts):
        subs = []
        for _ in range(n):
            self.count += 1
            subs.append({"id": f"a{self.count}", "type": kind, "ts": ts, "records": [
                tb.assistant(f"x{self.count}", model=model, read=1_000_000, out=1_000, ts=ts, sidechain=True)]})
        return subs

    def build(self, subagents):
        tb.add_session(self.projects, self.slug, f"s{self.count}", [tb.assistant(f"m{self.count}", out=1)],
                       subagents=subagents)

    def sums(self):
        quality = audit.new_quality()
        dirs = audit.project_dirs(self.projects, project=str(self.project))
        return [audit.summarize(u, RATES) for u in audit.load_units(dirs, None, None, quality)]

    def snapshot_and_compare(self):
        sums = self.sums()
        snap = audit.snapshot_data([s for s in sums if s["start"] <= STAMP], RATES, {"project": str(self.project), "all": False}, now=STAMP)
        return snap, audit.compare(snap, sums, RATES)

    def entry(self, result, kind):
        return next(t for t in result["types"] if t["type"] == kind)


class SnapshotCompareTests(CompareCase):
    def test_snapshot_shape(self):
        self.build(self.spawns("reviewer", "claude-sonnet-5-5", 6, BEFORE))
        snap, _ = self.snapshot_and_compare()
        self.assertEqual(snap["timestamp"], STAMP)
        self.assertEqual(snap["rates_as_of"], "2026-10-01")
        stats = snap["types"]["reviewer"]
        self.assertEqual((stats["spawns"], stats["models"]), (6, ["claude-sonnet-5-5"]))
        self.assertEqual(stats["fixed_median"], 1_000_000)
        self.assertAlmostEqual(stats["cost_median"], 0.3 + 0.015)

    def test_model_change_reports_the_percentage_change(self):
        self.build(self.spawns("reviewer", "claude-sonnet-5-5", 6, BEFORE) + self.spawns("reviewer", "claude-haiku-4-5", 6, AFTER))
        _, result = self.snapshot_and_compare()
        entry = self.entry(result, "reviewer")
        self.assertEqual((entry["before"]["spawns"], entry["after"]["spawns"]), (6, 6))
        self.assertIn("fell 67%", entry["verdict"])
        self.assertIn("trend, not proof", entry["verdict"])

    def test_unchanged_model_says_so(self):
        self.build(self.spawns("reviewer", "claude-sonnet-5-5", 6, BEFORE) + self.spawns("reviewer", "claude-sonnet-5-5", 6, AFTER))
        self.assertIn("model unchanged", self.entry(self.snapshot_and_compare()[1], "reviewer")["verdict"])

    def test_small_samples_give_no_verdict(self):
        self.build(self.spawns("reviewer", "claude-sonnet-5-5", 6, BEFORE) + self.spawns("reviewer", "claude-haiku-4-5", 3, AFTER))
        verdict = self.entry(self.snapshot_and_compare()[1], "reviewer")["verdict"]
        self.assertIn("not enough data", verdict)
        self.assertNotIn("%", verdict)

    def test_type_with_no_spawns_after(self):
        self.build(self.spawns("reviewer", "claude-sonnet-5-5", 6, BEFORE))
        self.assertIn("no spawns since", self.entry(self.snapshot_and_compare()[1], "reviewer")["verdict"])

    def test_type_that_is_new_after(self):
        self.build(self.spawns("reviewer", "claude-sonnet-5-5", 6, BEFORE) + self.spawns("planner", "claude-sonnet-5-5", 6, AFTER))
        self.assertIn("new since", self.entry(self.snapshot_and_compare()[1], "planner")["verdict"])

    def test_cost_not_comparable_falls_back_to_tokens(self):
        self.build(self.spawns("reviewer", "claude-mystery-1", 6, BEFORE) + self.spawns("reviewer", "claude-mystery-2", 6, AFTER))
        verdict = self.entry(self.snapshot_and_compare()[1], "reviewer")["verdict"]
        self.assertIn("median tokens per spawn", verdict)

    def test_rates_change_is_noted(self):
        self.build(self.spawns("reviewer", "claude-sonnet-5-5", 6, BEFORE))
        sums = self.sums()
        snap = audit.snapshot_data(sums, RATES, {"project": None, "all": True}, now=STAMP)
        newer = dict(RATES, as_of="2026-12-01")
        self.assertIn("rates", audit.compare(snap, sums, newer)["rates_note"])
        self.assertIsNone(audit.compare(snap, sums, RATES)["rates_note"])


class SnapshotCompareCliTests(CompareCase):
    def common(self):
        return ["--projects-dir", str(self.projects), "--rates", str(self.rates_path), "--project", str(self.project)]

    def test_snapshot_writes_a_file_and_compare_reads_it(self):
        self.build(self.spawns("reviewer", "claude-sonnet-5-5", 6, BEFORE))
        out = self.root / "snap.json"
        code, stdout, _ = run_cli("snapshot", "--out", str(out), *self.common())
        self.assertEqual(code, 0)
        self.assertIn(str(out), stdout)
        saved = json.loads(out.read_text(encoding="utf-8"))
        self.assertIn("reviewer", saved["types"])
        self.assertNotIn(tb.SECRET, out.read_text(encoding="utf-8"))
        code, stdout, _ = run_cli("compare", "--snapshot", str(out), *self.common())
        self.assertEqual(code, 0)
        self.assertIn("reviewer", stdout)

    def test_compare_inherits_the_snapshot_project(self):
        self.build(self.spawns("reviewer", "claude-sonnet-5-5", 6, BEFORE))
        out = self.root / "snap.json"
        run_cli("snapshot", "--out", str(out), *self.common())
        code, stdout, _ = run_cli("compare", "--snapshot", str(out), "--projects-dir", str(self.projects),
                                  "--rates", str(self.rates_path), "--json")
        self.assertEqual(code, 0)
        self.assertTrue(any(t["type"] == "reviewer" for t in json.loads(stdout)["types"]))

    def test_bad_snapshot_file_is_an_error(self):
        bad = self.root / "bad.json"
        bad.write_text("{nope", encoding="utf-8")
        code, _, err = run_cli("compare", "--snapshot", str(bad), *self.common())
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error:"))
        code, _, err = run_cli("compare", "--snapshot", str(self.root / "absent.json"), *self.common())
        self.assertEqual(code, 2)

    def test_unwritable_snapshot_path_is_a_clean_error(self):
        code, _, err = run_cli("snapshot", "--out", str(self.root), *self.common())  # a directory
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error:"))


class ShippedRatesTests(unittest.TestCase):
    def test_shipped_rates_file_loads_and_is_dated(self):
        rates = audit.load_rates()
        self.assertTrue(rates["models"])
        self.assertRegex(rates["as_of"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(rates.get("source", "").startswith("http"))

    def test_models_seen_in_real_transcripts_have_rates(self):
        rates = audit.load_rates()
        for model in ("claude-sonnet-5", "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
                      "claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5"):
            self.assertIsNotNone(audit.rate_for(rates, model), model)
        self.assertEqual(audit.rate_for(rates, "claude-opus-5-5")["output"], 20)
        self.assertEqual(audit.rate_for(rates, "claude-opus-5")["output"], 25)



class FinalReviewTests(ReportCase):
    # F0: a resumed session copies earlier history into a new file; each message id counts once
    def test_resumed_session_copy_counts_each_message_once(self):
        slug = audit.slug_for(self.root / "resume")
        first = [tb.assistant("r1", out=100, ts="2026-10-01T10:00:00.000Z"), tb.assistant("r2", out=200, ts="2026-10-01T10:05:00.000Z")]
        copy = [tb.user("2026-10-02T09:00:00.000Z"), tb.assistant("r1", out=100, ts="2026-10-01T10:00:00.000Z"),
                tb.assistant("r2", out=200, ts="2026-10-01T10:05:00.000Z"), tb.assistant("r3", out=50, ts="2026-10-02T09:01:00.000Z")]
        tb.write_jsonl(self.projects / slug / "orig.jsonl", [tb.user("2026-10-01T10:00:00.000Z")] + first)
        tb.write_jsonl(self.projects / slug / "resumed.jsonl", copy)
        quality = audit.new_quality()
        units = audit.load_units(audit.project_dirs(self.projects, project=str(self.root / "resume")), None, None, quality)
        total_out = sum(m["tokens"]["output"] for u in units for m in u["messages"])
        self.assertEqual(total_out, 350)  # not 650

    def test_a_copied_message_belongs_to_the_earlier_file(self):
        slug = audit.slug_for(self.root / "resume2")
        tb.write_jsonl(self.projects / slug / "orig.jsonl", [tb.user("2026-10-01T10:00:00.000Z"), tb.assistant("r1", out=100, ts="2026-10-01T10:00:00.000Z")])
        tb.write_jsonl(self.projects / slug / "later.jsonl", [tb.user("2026-10-03T10:00:00.000Z"), tb.assistant("r1", out=100, ts="2026-10-01T10:00:00.000Z")])
        quality = audit.new_quality()
        units = audit.load_units(audit.project_dirs(self.projects, project=str(self.root / "resume2")), None, None, quality)
        self.assertEqual([u["start"][:10] for u in units], ["2026-10-01"])  # the later copy owns nothing and is dropped

    # F1: a model that is not listed must not be priced at an older release's rate
    def test_unlisted_point_release_has_no_rate_but_a_date_suffix_still_matches(self):
        rates = {"models": {"claude-opus-5": {"input": 1}, "claude-haiku-4-5": {"input": 2}}}
        self.assertIsNone(audit.rate_for(rates, "claude-opus-5-6"))
        self.assertIsNone(audit.rate_for(rates, "claude-opus-50"))
        self.assertEqual(audit.rate_for(rates, "claude-haiku-4-5-20251001")["input"], 2)
        self.assertEqual(audit.rate_for(rates, "claude-opus-5")["input"], 1)


class FinalCompareTests(CompareCase):
    # F2: the 5-spawn rule counts the spawns a cost median rests on
    def test_cost_verdict_needs_enough_priced_spawns(self):
        self.build(self.spawns("reviewer", "claude-sonnet-5-5", 6, BEFORE)
                   + self.spawns("reviewer", "claude-mystery-1", 5, AFTER)
                   + self.spawns("reviewer", "claude-haiku-4-5", 1, AFTER))
        verdict = self.entry(self.snapshot_and_compare()[1], "reviewer")["verdict"]
        self.assertIn("median tokens per spawn", verdict)
        self.assertNotIn("median cost per spawn", verdict)

    # F3: transcripts do not record effort, so an effort-only edit cannot be judged
    def test_unchanged_model_verdict_says_effort_is_not_measured(self):
        self.build(self.spawns("reviewer", "claude-sonnet-5-5", 6, BEFORE) + self.spawns("reviewer", "claude-sonnet-5-5", 6, AFTER))
        verdict = self.entry(self.snapshot_and_compare()[1], "reviewer")["verdict"]
        self.assertIn("effort", verdict)
        self.assertNotIn("not the edit", verdict)

    # F5: a snapshot without --project remembers the project it was taken in
    def test_snapshot_without_project_stores_the_current_directory(self):
        self.build(self.spawns("reviewer", "claude-sonnet-5-5", 6, BEFORE))
        out = self.root / "snap.json"
        proj = self.project
        proj.mkdir(exist_ok=True)
        cwd = os.getcwd()
        os.chdir(proj)
        try:
            code, _, _ = run_cli("snapshot", "--out", str(out), "--projects-dir", str(self.projects), "--rates", str(self.rates_path))
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["selectors"]["project"], os.path.abspath(proj))

    def test_snapshot_of_a_project_with_no_transcripts_writes_nothing(self):
        self.projects.mkdir(parents=True)
        out = self.root / "snap.json"
        code, stdout, _ = run_cli("snapshot", "--out", str(out), "--projects-dir", str(self.projects),
                                  "--rates", str(self.rates_path), "--project", str(self.root / "nowhere"))
        self.assertEqual(code, 0)
        self.assertIn("No transcripts found", stdout)
        self.assertFalse(out.exists())

    # F6: compare really reads the project saved in the snapshot, not the current directory's
    def test_compare_uses_the_snapshot_project_from_another_directory(self):
        self.build(self.spawns("reviewer", "claude-sonnet-5-5", 6, BEFORE))
        out = self.root / "snap.json"
        run_cli("snapshot", "--out", str(out), "--projects-dir", str(self.projects), "--rates", str(self.rates_path),
                "--project", str(self.project))
        future = "2999-01-01T00:00:00.000Z"
        self.build(self.spawns("reviewer", "claude-haiku-4-5", 6, future))
        other = self.root / "other-cwd"
        other.mkdir()
        cwd = os.getcwd()
        os.chdir(other)
        try:
            code, stdout, _ = run_cli("compare", "--snapshot", str(out), "--projects-dir", str(self.projects),
                                      "--rates", str(self.rates_path), "--json")
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0)
        entry = next(t for t in json.loads(stdout)["types"] if t["type"] == "reviewer")
        self.assertIsNotNone(entry["after"])
        self.assertEqual(entry["after"]["spawns"], 6)



class SmallBatchTests(CompareCase):
    # LF line endings for every file the audit writes, on every platform
    def test_snapshot_file_uses_lf_line_endings(self):
        self.build(self.spawns("reviewer", "claude-sonnet-5-5", 6, BEFORE))
        out = self.root / "snap.json"
        code, _, _ = run_cli("snapshot", "--out", str(out), "--projects-dir", str(self.projects),
                             "--rates", str(self.rates_path), "--project", str(self.project))
        self.assertEqual(code, 0)
        self.assertNotIn(b"\r\n", out.read_bytes())

    # the date filters are UTC dates, and the help says so
    def test_date_filter_help_says_utc(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                audit.main(["report", "--help"])
        text = " ".join(out.getvalue().split())
        self.assertIn("UTC", text)

    # a run that switches models is one spawn, counted where it started
    def test_a_model_switching_run_is_one_spawn(self):
        records = [tb.assistant("m1", model="claude-opus-5-5", read=1000, out=10),
                   tb.assistant("m2", model="claude-haiku-4-5", read=2000, out=20)]
        tb.add_session(self.projects, self.slug, "s1", records)
        quality = audit.new_quality()
        dirs = audit.project_dirs(self.projects, project=str(self.project))
        sums = [audit.summarize(u, RATES) for u in audit.load_units(dirs, None, None, quality)]
        report = audit.build_report(sums, RATES, quality, {})
        rows = {r["model"]: r for r in report["rows"]}
        self.assertEqual(rows["claude-opus-5-5"]["spawns"], 1)
        self.assertEqual(rows["claude-haiku-4-5"]["spawns"], 0)
        self.assertEqual(sum(r["spawns"] for r in report["rows"]), report["quality"]["sessions"])
        self.assertEqual(rows["claude-haiku-4-5"]["messages"], 1)
        self.assertEqual(rows["claude-haiku-4-5"]["tokens"]["output"], 20)


if __name__ == "__main__":
    unittest.main()
