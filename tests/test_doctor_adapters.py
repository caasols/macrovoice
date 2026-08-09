"""Adapters, driven through an injected fake runner so no daemon is involved."""

import sys
import traceback
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from macrovoice.doctor.adapters.macrowhisper import Macrowhisper  # noqa: E402
from macrovoice.doctor.adapters.process import CommandResult  # noqa: E402
from macrovoice.doctor.adapters.bridge import BridgeState  # noqa: E402

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


class TestBridgeState(unittest.TestCase):
    def test_missing_watch_root_is_reported_not_created(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "absent"
            snap = BridgeState(root).snapshot()
            self.assertFalse(snap.watch_exists)
            self.assertFalse(snap.recordings_exists)
            self.assertFalse(root.exists())  # doctor --check must never create it

    def test_counts_spool_and_staging(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "recordings").mkdir()
            (root / ".spool" / "a").mkdir(parents=True)
            (root / ".spool" / "b").mkdir()
            (root / ".staging").mkdir()
            snap = BridgeState(root).snapshot()
            self.assertTrue(snap.watch_exists)
            self.assertTrue(snap.recordings_exists)
            self.assertEqual(snap.spool_count, 2)
            self.assertEqual(snap.staging_count, 0)

    def test_script_path_points_at_the_repo_root_wrapper(self):
        state = BridgeState("/tmp/w")
        self.assertEqual(state.script_path().name, "macrovoice.sh")
        self.assertTrue(state.script_path().exists())

    def test_env_python_reports_the_interpreter_the_wrapper_will_get(self):
        version_out = "/opt/homebrew/opt/python/bin/python3.14\n3.14.6\n"
        runner = FakeRunner({"python3": ok(version_out)})
        executable, version = BridgeState("/tmp/w", runner=runner).env_python()
        self.assertEqual(version, "3.14.6")
        self.assertTrue(executable.endswith("python3.14"))

    def test_env_python_is_none_when_it_cannot_be_run(self):
        runner = FakeRunner({"python3": CommandResult(None, "", "boom", False)})
        self.assertEqual(BridgeState("/tmp/w", runner=runner).env_python(), (None, None))

    def test_recent_log_errors_ignores_old_ones(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "macrovoice.log").write_text(
                "2020-01-01T00:00:00Z ERROR (exiting 0 anyway): ancient\n"
                "2026-08-09T15:25:13Z published 1786289113229804000-000 chars=73\n",
                encoding="utf-8",
            )
            self.assertEqual(BridgeState(root).recent_log_errors(within_hours=24), ())

    def test_recent_log_errors_reports_fresh_ones(self):
        from datetime import datetime as dt, timedelta, timezone

        stamp = (dt.now(timezone.utc) - timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "macrovoice.log").write_text(
                "%s ERROR (exiting 0 anyway): Traceback\n" % stamp, encoding="utf-8"
            )
            errors = BridgeState(root).recent_log_errors(within_hours=24)
            self.assertEqual(len(errors), 1)

    def test_multiline_traceback_entry_keeps_exception_line(self):
        from datetime import datetime as dt, timedelta, timezone

        # Create a realistic multi-line traceback entry.
        stamp = (dt.now(timezone.utc) - timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        try:
            raise ValueError("boom")
        except ValueError:
            body = traceback.format_exc().strip()

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "macrovoice.log").write_text(
                "%s ERROR (exiting 0 anyway): %s\n" % (stamp, body),
                encoding="utf-8",
            )
            errors = BridgeState(root).recent_log_errors(within_hours=24)
            # A multi-line body should be returned as a single entry.
            self.assertEqual(len(errors), 1)
            # The returned entry should contain both the header and the exception.
            self.assertIn("ERROR (exiting 0 anyway)", errors[0])
            self.assertIn("ValueError: boom", errors[0])

    def test_multiline_entry_not_absorbed_by_following_non_error_line(self):
        from datetime import datetime as dt, timedelta, timezone

        stamp = (dt.now(timezone.utc) - timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        try:
            raise ValueError("boom")
        except ValueError:
            body = traceback.format_exc().strip()

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_content = (
                "%s ERROR (exiting 0 anyway): %s\n"
                "%s published 1786289113229804000-000 chars=73\n"
            ) % (stamp, body, stamp)
            (root / "macrovoice.log").write_text(log_content, encoding="utf-8")
            errors = BridgeState(root).recent_log_errors(within_hours=24)
            # Should have exactly one error (the multi-line entry).
            self.assertEqual(len(errors), 1)
            # The published line should not be part of the error.
            self.assertNotIn("published", errors[0])


if __name__ == "__main__":
    unittest.main()
