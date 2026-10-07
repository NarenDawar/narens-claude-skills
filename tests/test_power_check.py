import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import helpers

sys.path.insert(
    0,
    str(helpers.REPO_ROOT / "plugins" / "schedule-doctor" / "skills" / "schedule-doctor" / "scripts"),
)
import power_check  # noqa: E402

POWER = helpers.REPO_ROOT / "tests" / "fixtures" / "schedule_doctor" / "power"


def fixture(name):
    return (POWER / name).read_text(encoding="utf-8")


def run_main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = power_check.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ParseTests(unittest.TestCase):
    def test_real_laptop_sleep_never(self):
        self.assertEqual(
            power_check.parse_powercfg_index(fixture("windows_sleep_real.txt")),
            {"ac": 0, "dc": 0},
        )

    def test_laptop_sleep_values_are_seconds(self):
        self.assertEqual(
            power_check.parse_powercfg_index(fixture("windows_sleep_laptop_synthetic.txt")),
            {"ac": 1800, "dc": 900},
        )

    def test_query_omits_the_hidden_lid_setting(self):
        # `powercfg /query` leaves hidden settings out; this laptop's lid action is hidden.
        self.assertEqual(
            power_check.parse_powercfg_index(fixture("windows_lid_query_hidden_real.txt")),
            {"ac": None, "dc": None},
        )

    def test_qh_shows_the_hidden_lid_setting(self):
        self.assertEqual(
            power_check.parse_powercfg_index(fixture("windows_lid_qh_real.txt")),
            {"ac": 1, "dc": 1},
        )

    def test_hibernate_timeout_is_read(self):
        self.assertEqual(
            power_check.parse_powercfg_index(fixture("windows_hibernate_real.txt")),
            {"ac": 0, "dc": 0xE100},
        )

    def test_battery_flag(self):
        self.assertIs(power_check.battery_from_flag(128), False)
        self.assertIsNone(power_check.battery_from_flag(255))
        self.assertIs(power_check.battery_from_flag(1), True)
        self.assertIs(power_check.battery_from_flag(9), True)

    def test_laptop_lid_values(self):
        self.assertEqual(
            power_check.parse_powercfg_index(fixture("windows_lid_laptop_synthetic.txt")),
            {"ac": 0, "dc": 1},
        )

    def test_unparseable_text_gives_none(self):
        self.assertEqual(
            power_check.parse_powercfg_index("Parametres invalides"), {"ac": None, "dc": None}
        )

    def test_pmset_sleep_minutes(self):
        self.assertEqual(power_check.parse_pmset_sleep(fixture("pmset_synthetic.txt")), 10)

    def test_pmset_does_not_confuse_displaysleep_or_disksleep(self):
        self.assertIsNone(power_check.parse_pmset_sleep(" displaysleep 2\n disksleep 10\n"))

    def test_pmset_without_a_sleep_line(self):
        self.assertIsNone(power_check.parse_pmset_sleep("nothing useful"))


class SummarizeTests(unittest.TestCase):
    def test_machine_without_a_battery_and_without_a_lid_setting_keeps_awake(self):
        res = power_check.summarize("windows", {"ac": 0.0, "dc": 0.0}, None, has_battery=False)
        self.assertIs(res["keeps_awake"], True)
        self.assertTrue(any(f.startswith("lid close: no setting found") for f in res["findings"]))

    def test_missing_lid_setting_on_a_machine_with_a_battery_is_unknown_not_yes(self):
        for has_battery in (True, None):
            with self.subTest(has_battery):
                res = power_check.summarize(
                    "windows", {"ac": 0.0, "dc": 0.0}, None, has_battery=has_battery
                )
                self.assertIsNone(res["keeps_awake"])
                self.assertTrue(any("may be a laptop" in f for f in res["findings"]))

    def test_a_hibernate_timeout_counts_as_sleeping(self):
        res = power_check.summarize(
            "windows",
            {"ac": 0.0, "dc": 0.0},
            {"ac": "do nothing", "dc": "do nothing"},
            hibernate_after={"ac": 0.0, "dc": 960.0},
            has_battery=True,
        )
        self.assertIs(res["keeps_awake"], False)
        self.assertIn("hibernate timeout on battery: 960 minutes idle", res["findings"])

    def test_no_hibernate_timeout_keeps_awake(self):
        res = power_check.summarize(
            "windows",
            {"ac": 0.0, "dc": 0.0},
            {"ac": "do nothing", "dc": "do nothing"},
            hibernate_after={"ac": 0.0, "dc": 0.0},
            has_battery=True,
        )
        self.assertIs(res["keeps_awake"], True)

    def test_unreadable_hibernate_timeout_is_unknown(self):
        res = power_check.summarize(
            "windows",
            {"ac": 0.0, "dc": 0.0},
            {"ac": "do nothing", "dc": "do nothing"},
            hibernate_after={"ac": None, "dc": None},
            has_battery=True,
        )
        self.assertIsNone(res["keeps_awake"])
        self.assertIn("hibernate timeout on power: could not be read", res["findings"])

    def test_laptop_with_sleep_timeouts_does_not(self):
        res = power_check.summarize(
            "windows", {"ac": 30.0, "dc": 15.0}, {"ac": "do nothing", "dc": "sleep"}
        )
        self.assertIs(res["keeps_awake"], False)
        self.assertIn("sleep timeout on power: 30 minutes idle", res["findings"])
        self.assertIn("lid close on battery: sleep", res["findings"])

    def test_never_sleeps_but_lid_close_sleeps(self):
        res = power_check.summarize(
            "windows", {"ac": 0.0, "dc": 0.0}, {"ac": "sleep", "dc": "sleep"}
        )
        self.assertIs(res["keeps_awake"], False)

    def test_unreadable_values_make_the_answer_unknown_not_yes(self):
        res = power_check.summarize("windows", {"ac": None, "dc": 0.0}, None, has_battery=False)
        self.assertIsNone(res["keeps_awake"])
        self.assertIn("sleep timeout on power: could not be read", res["findings"])

    def test_unreadable_lid_value_makes_the_answer_unknown(self):
        res = power_check.summarize(
            "windows", {"ac": 0.0, "dc": 0.0}, {"ac": "do nothing", "dc": None}
        )
        self.assertIsNone(res["keeps_awake"])

    def test_macos_never_sleeping_is_still_unknown_because_the_lid_is_not_readable(self):
        res = power_check.summarize("macos", {"any": 0})
        self.assertIsNone(res["keeps_awake"])
        self.assertTrue(any("MacBook" in f for f in res["findings"]))

    def test_macos_with_a_sleep_timer_does_not_keep_awake(self):
        self.assertIs(power_check.summarize("macos", {"any": 10})["keeps_awake"], False)

    def test_caveat_names_the_desktop_toggle(self):
        self.assertIn("Keep computer awake", power_check.summarize("macos", {"any": 0})["caveat"])


class MainTests(unittest.TestCase):
    def patched(self, name, outputs, battery=True):
        return (
            mock.patch.object(power_check, "platform_name", return_value=name),
            mock.patch.object(power_check, "run_command", side_effect=outputs),
            mock.patch.object(power_check, "battery_present", return_value=battery),
        )

    def laptop_outputs(self):
        return [
            fixture("windows_sleep_real.txt"),
            fixture("windows_hibernate_real.txt"),
            fixture("windows_lid_qh_real.txt"),
        ]

    def test_this_laptop_is_reported_as_not_keeping_awake(self):
        p1, p2, p3 = self.patched("windows", self.laptop_outputs())
        with p1, p2, p3:
            code, out, err = run_main("--json")
        data = json.loads(out)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(data["sleep_after_minutes"], {"ac": 0.0, "dc": 0.0})
        self.assertEqual(data["hibernate_after_minutes"], {"ac": 0.0, "dc": 960.0})
        self.assertEqual(data["lid_close_action"], {"ac": "sleep", "dc": "sleep"})
        self.assertIs(data["keeps_awake"], False)

    def test_windows_laptop_synthetic_report(self):
        p1, p2, p3 = self.patched(
            "windows",
            [
                fixture("windows_sleep_laptop_synthetic.txt"),
                fixture("windows_hibernate_real.txt"),
                fixture("windows_lid_laptop_synthetic.txt"),
            ],
        )
        with p1, p2, p3:
            code, out, _ = run_main("--json")
        data = json.loads(out)
        self.assertEqual(data["sleep_after_minutes"], {"ac": 30.0, "dc": 15.0})
        self.assertEqual(data["lid_close_action"], {"ac": "do nothing", "dc": "sleep"})
        self.assertIs(data["keeps_awake"], False)

    def test_text_report_for_a_machine_that_really_keeps_awake(self):
        never = fixture("windows_sleep_real.txt")  # both indexes 0, reused as a zero hibernate
        p1, p2, p3 = self.patched(
            "windows",
            [never, never, fixture("windows_lid_query_hidden_real.txt")],
            battery=False,
        )
        with p1, p2, p3:
            code, out, _ = run_main()
        self.assertEqual(code, 0)
        self.assertIn("os: windows", out)
        self.assertIn("sleep timeout on power: never", out)
        self.assertIn("keeps awake unattended: yes", out)
        self.assertIn("caveat:", out)
        out.encode("ascii")

    def test_the_hidden_settings_are_queried_with_qh(self):
        p1, p2, p3 = self.patched("windows", self.laptop_outputs())
        with p1, p2 as run, p3:
            run_main()
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([c[:2] for c in commands], [["powercfg", "/qh"]] * 3)
        self.assertEqual([c[-1] for c in commands], ["STANDBYIDLE", "HIBERNATEIDLE", "LIDACTION"])

    def test_query_is_the_fallback_when_qh_fails(self):
        outputs = [power_check.PowerError("no /qh")] + self.laptop_outputs()
        p1, p2, p3 = self.patched("windows", outputs)
        with p1, p2 as run, p3:
            code, out, _ = run_main("--json")
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args_list[1].args[0][:2], ["powercfg", "/query"])
        self.assertEqual(json.loads(out)["sleep_after_minutes"], {"ac": 0.0, "dc": 0.0})

    def test_a_failing_lid_query_is_tolerated_and_unknown(self):
        err = power_check.PowerError("no")
        outputs = [fixture("windows_sleep_real.txt"), fixture("windows_sleep_real.txt"), err, err]
        p1, p2, p3 = self.patched("windows", outputs, battery=True)
        with p1, p2, p3:
            code, out, _ = run_main("--json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertIsNone(data["lid_close_action"])
        self.assertIsNone(data["keeps_awake"])

    def test_unlabelled_powercfg_output_reports_unreadable_lid(self):
        outputs = [text.replace("Current AC Power Setting Index", "Indice secteur")
                   .replace("Current DC Power Setting Index", "Indice batterie")
                   for text in self.laptop_outputs()]
        p1, p2, p3 = self.patched("windows", outputs)
        with p1, p2, p3:
            code, out, err = run_main("--json")
        data = json.loads(out)
        self.assertEqual((code, err), (0, ""))
        self.assertIsNone(data["keeps_awake"])
        self.assertIsNone(data["lid_close_action"])
        self.assertIn("lid close: could not be read", data["findings"])
        self.assertFalse(any("no setting found" in line for line in data["findings"]))

    def test_unreadable_lid_does_not_claim_a_desktop_keeps_awake(self):
        never = fixture("windows_sleep_real.txt")
        for lid in ("", "Parametres illisibles", fixture("windows_lid_qh_real.txt")
                    .replace("Current AC Power Setting Index", "Unlabelled AC")
                    .replace("Current DC Power Setting Index", "Unlabelled DC")):
            with self.subTest(lid=lid):
                p1, p2, p3 = self.patched("windows", [never, never, lid], battery=False)
                with p1, p2, p3:
                    code, out, _ = run_main()
                self.assertEqual(code, 0)
                self.assertIn("lid close: could not be read", out)
                self.assertIn("keeps awake unattended: unknown", out)
                self.assertNotIn("no setting found", out)

    def test_failing_lid_query_on_a_desktop_is_unreadable_not_absent(self):
        never = fixture("windows_sleep_real.txt")
        failure = power_check.PowerError("cannot query")
        p1, p2, p3 = self.patched("windows", [never, never, failure, failure], battery=False)
        with p1, p2, p3:
            code, out, err = run_main("--json")
        data = json.loads(out)
        self.assertEqual((code, err), (0, ""))
        self.assertIsNone(data["keeps_awake"])
        self.assertIn("lid close: could not be read", data["findings"])

    def test_scheme_only_output_preserves_missing_setting_behavior(self):
        never = fixture("windows_sleep_real.txt")
        scheme = fixture("windows_lid_query_hidden_real.txt")
        for lid in (scheme, scheme.splitlines()[0] + "\n"):
            with self.subTest(lid=lid):
                p1, p2, p3 = self.patched("windows", [never, never, lid], battery=False)
                with p1, p2, p3:
                    code, out, _ = run_main("--json")
                data = json.loads(out)
                self.assertEqual(code, 0)
                self.assertIs(data["keeps_awake"], True)
                self.assertIn("lid close: no setting found (no battery, so likely a desktop)",
                              data["findings"])

    def test_unrecognized_lid_indexes_are_unknown_not_absent(self):
        never = fixture("windows_sleep_real.txt")
        lid = fixture("windows_lid_qh_real.txt").replace("0x00000001", "0x00000063")
        p1, p2, p3 = self.patched("windows", [never, never, lid], battery=False)
        with p1, p2, p3:
            _, out, _ = run_main("--json")
        data = json.loads(out)
        self.assertIsNone(data["keeps_awake"])
        self.assertIn("lid close: could not be read", data["findings"])

    def test_a_failing_hibernate_query_is_unknown_not_an_error(self):
        err = power_check.PowerError("no")
        outputs = [fixture("windows_sleep_real.txt"), err, err, fixture("windows_lid_qh_real.txt")]
        p1, p2, p3 = self.patched("windows", outputs)
        with p1, p2, p3:
            code, out, _ = run_main("--json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["hibernate_after_minutes"], {"ac": None, "dc": None})

    def test_a_failing_sleep_query_is_a_clean_error(self):
        err = power_check.PowerError("powercfg failed (exit 1)")
        p1, p2, p3 = self.patched("windows", [err, err])
        with p1, p2, p3:
            code, _, stderr = run_main()
        self.assertEqual(code, 2)
        self.assertTrue(stderr.startswith("error: powercfg failed"))

    def test_macos_report(self):
        p1, p2, p3 = self.patched("macos", [fixture("pmset_synthetic.txt")])
        with p1, p2, p3:
            code, out, _ = run_main("--json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["sleep_after_minutes"], {"any": 10})
        self.assertIs(data["keeps_awake"], False)

    def test_unsupported_os_is_a_clean_error(self):
        p1, p2, p3 = self.patched(None, [])
        with p1, p2, p3:
            code, _, err = run_main()
        self.assertEqual(code, 2)
        self.assertIn("unsupported OS", err)
        self.assertNotIn("Traceback", err)


class RunCommandTests(unittest.TestCase):
    def test_nonzero_exit_is_a_power_error(self):
        done = subprocess.CompletedProcess(["powercfg"], 1, stdout="", stderr="bad args")
        with mock.patch.object(subprocess, "run", return_value=done):
            with self.assertRaises(power_check.PowerError) as ctx:
                power_check.run_command(["powercfg", "/query"])
        self.assertIn("exit 1", str(ctx.exception))

    def test_missing_program_is_a_power_error(self):
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("powercfg")):
            with self.assertRaises(power_check.PowerError):
                power_check.run_command(["powercfg"])

    def test_success_returns_stdout(self):
        done = subprocess.CompletedProcess(["x"], 0, stdout="hello", stderr="")
        with mock.patch.object(subprocess, "run", return_value=done):
            self.assertEqual(power_check.run_command(["x"]), "hello")


if __name__ == "__main__":
    unittest.main()
