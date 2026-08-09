"""Adapters, driven through an injected fake runner so no daemon is involved."""

import sys
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from macrovoice.doctor.adapters.macrowhisper import Macrowhisper  # noqa: E402
from macrovoice.doctor.adapters.process import CommandResult  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "doctor" / "status"


class FakeRunner:
    """Maps a CLI flag to a canned CommandResult, and records the calls."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append((tuple(args), timeout))
        for flag, result in self.responses.items():
            if flag in args:
                return result
        return CommandResult(1, "", "unexpected call", False)


def ok(stdout):
    return CommandResult(0, stdout, "", False)


class TestStatus(unittest.TestCase):
    def test_status_is_parsed(self):
        runner = FakeRunner({"--status": ok((FIXTURES / "running-2.1.1.txt").read_text())})
        mw = Macrowhisper(runner=runner)
        self.assertTrue(mw.status().running)
        self.assertEqual(mw.status().version, "2.1.1")

    def test_status_is_cached_so_thirteen_checks_cost_one_call(self):
        runner = FakeRunner({"--status": ok((FIXTURES / "running-2.1.1.txt").read_text())})
        mw = Macrowhisper(runner=runner)
        mw.status()
        mw.status()
        self.assertEqual(len(runner.calls), 1)

    def test_invalidate_forces_a_refetch(self):
        runner = FakeRunner({"--status": ok((FIXTURES / "running-2.1.1.txt").read_text())})
        mw = Macrowhisper(runner=runner)
        mw.status()
        mw.invalidate()
        mw.status()
        self.assertEqual(len(runner.calls), 2)

    def test_a_timeout_is_unrecognised_rather_than_a_hang_or_a_lie(self):
        runner = FakeRunner({"--status": CommandResult(None, "", "", True)})
        snap = Macrowhisper(runner=runner).status()
        self.assertFalse(snap.recognized)
        self.assertFalse(snap.running)


class TestConfigCommands(unittest.TestCase):
    def test_saved_config_path(self):
        runner = FakeRunner(
            {"--get-config": ok("Saved config path: /Users/x/.config/macrowhisper/macrowhisper.json\n")}
        )
        self.assertEqual(
            Macrowhisper(runner=runner).saved_config_path(),
            "/Users/x/.config/macrowhisper/macrowhisper.json",
        )

    def test_saved_config_path_is_none_when_the_line_is_absent(self):
        runner = FakeRunner({"--get-config": ok("something else entirely\n")})
        self.assertIsNone(Macrowhisper(runner=runner).saved_config_path())

    def test_validate_config_reports_valid(self):
        runner = FakeRunner({"--validate-config": ok("Configuration is valid: /x.json\n")})
        valid, detail = Macrowhisper(runner=runner).validate_config()
        self.assertTrue(valid)
        self.assertIn("valid", detail)

    def test_validate_config_reports_invalid(self):
        runner = FakeRunner({"--validate-config": ok("Invalid: defaults.watch must be a string\n")})
        valid, detail = Macrowhisper(runner=runner).validate_config()
        self.assertFalse(valid)

    def test_service_installed(self):
        runner = FakeRunner({"--service-status": ok("Service Status:\n  Installed: Yes\n  Running: Yes\n")})
        self.assertTrue(Macrowhisper(runner=runner).service_installed())

    def test_read_config_returns_none_on_broken_json(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "macrowhisper.json"
            path.write_text("{ not json", encoding="utf-8")
            self.assertIsNone(Macrowhisper().read_config(str(path)))

    def test_read_config_returns_none_on_none_path(self):
        self.assertIsNone(Macrowhisper().read_config(None))


class TestAccessibilityLog(unittest.TestCase):
    """The line is emitted at daemon STARTUP (friction trap 4), so the newest
    one describes the current daemon. Reading it as live state is the mistake."""

    def write_log(self, tmp, body):
        path = Path(tmp) / "macrowhisper.log"
        path.write_text(body, encoding="utf-8")
        return path

    def test_newest_line_wins(self):
        with TemporaryDirectory() as tmp:
            path = self.write_log(
                tmp,
                "[2026-08-08 02:15:27] [WARNING] Accessibility permissions were not granted"
                " - some features may be limited\n"
                "[2026-08-09 11:02:53] [DEBUG] Accessibility permissions already granted\n",
            )
            granted, when = Macrowhisper(log_path=str(path)).accessibility_state()
            self.assertTrue(granted)
            self.assertEqual(when, datetime(2026, 8, 9, 11, 2, 53))

    def test_denied_is_reported(self):
        with TemporaryDirectory() as tmp:
            path = self.write_log(
                tmp,
                "[2026-08-09 11:02:53] [WARNING] Accessibility permissions were not granted"
                " - some features may be limited\n",
            )
            granted, _ = Macrowhisper(log_path=str(path)).accessibility_state()
            self.assertFalse(granted)

    def test_missing_log_is_unknown_not_denied(self):
        granted, when = Macrowhisper(log_path="/nonexistent/macrowhisper.log").accessibility_state()
        self.assertIsNone(granted)
        self.assertIsNone(when)


if __name__ == "__main__":
    unittest.main()
