"""Adapters, driven through an injected fake runner so no daemon is involved."""

import os
import sys
import traceback
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from macrovoice.doctor.adapters.macrowhisper import ConfigPath, Macrowhisper  # noqa: E402
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


class TestConfigPathShapes(unittest.TestCase):
    """--get-config prints two different lines (main.swift:899-906):

        Saved config path: <path>          when one has been persisted
        Using default config path: <path>  when none has

    A fresh macrowhisper always prints the second. Reading only the first made
    six of doctor's twenty checks dead on a brand-new install.
    """

    def test_a_persisted_path_is_reported_as_persisted(self):
        runner = FakeRunner(
            {"--get-config": ok("Saved config path: /Users/x/.config/macrowhisper/macrowhisper.json\n")}
        )
        found = Macrowhisper(runner=runner).config_path()
        self.assertEqual(found.path, "/Users/x/.config/macrowhisper/macrowhisper.json")
        self.assertTrue(found.persisted)

    def test_a_default_path_is_reported_as_not_persisted(self):
        runner = FakeRunner(
            {"--get-config": ok("Using default config path: /Users/x/.config/macrowhisper/macrowhisper.json\n")}
        )
        found = Macrowhisper(runner=runner).config_path()
        self.assertEqual(found.path, "/Users/x/.config/macrowhisper/macrowhisper.json")
        self.assertFalse(found.persisted)

    def test_unrecognised_output_is_none(self):
        runner = FakeRunner({"--get-config": ok("something else entirely\n")})
        self.assertIsNone(Macrowhisper(runner=runner).config_path())

    def test_a_timeout_is_none(self):
        runner = FakeRunner({"--get-config": CommandResult(None, "", "", True)})
        self.assertIsNone(Macrowhisper(runner=runner).config_path())

    def test_saved_config_path_still_returns_the_path_for_both_shapes(self):
        for line in (
            "Saved config path: /a/b.json\n",
            "Using default config path: /a/b.json\n",
        ):
            runner = FakeRunner({"--get-config": ok(line)})
            self.assertEqual(Macrowhisper(runner=runner).saved_config_path(), "/a/b.json")

    def test_config_path_is_cached_and_invalidate_clears_it(self):
        runner = FakeRunner({"--get-config": ok("Saved config path: /a/b.json\n")})
        mw = Macrowhisper(runner=runner)
        mw.config_path()
        mw.config_path()
        self.assertEqual(len(runner.calls), 1)
        mw.invalidate()
        mw.config_path()
        self.assertEqual(len(runner.calls), 2)


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


class TestAdapterDegradation(unittest.TestCase):
    """Every command promises the same thing: a timeout or an unrunnable binary
    becomes an UNKNOWN-shaped result, never a hang and never a guess. Only
    status() and config_path() had that test; this closes it for
    validate_config() and service_installed() too."""

    def timing_out(self, flag):
        return FakeRunner({flag: CommandResult(None, "", "", True)})

    def test_validate_config_degrades(self):
        valid, _ = Macrowhisper(runner=self.timing_out("--validate-config")).validate_config()
        self.assertIsNone(valid)

    def test_service_installed_degrades(self):
        self.assertIsNone(
            Macrowhisper(runner=self.timing_out("--service-status")).service_installed()
        )

    def test_service_installed_is_none_when_the_line_is_absent(self):
        runner = FakeRunner({"--service-status": ok("Service Status:\n  Running: Yes\n")})
        self.assertIsNone(Macrowhisper(runner=runner).service_installed())


class TestAccessibilityLogEdges(unittest.TestCase):
    def write(self, tmp, body):
        path = Path(tmp) / "macrowhisper.log"
        path.write_text(body, encoding="utf-8")
        return path

    def test_no_matching_line_is_unknown(self):
        with TemporaryDirectory() as tmp:
            path = self.write(tmp, "[2026-08-09 11:02:53] [DEBUG] Something unrelated\n")
            self.assertEqual(
                Macrowhisper(log_path=str(path)).accessibility_state(), (None, None)
            )

    def test_a_malformed_timestamp_still_reports_the_grant(self):
        with TemporaryDirectory() as tmp:
            path = self.write(tmp, "[not-a-date] Accessibility permissions already granted\n")
            granted, when = Macrowhisper(log_path=str(path)).accessibility_state()
            self.assertTrue(granted)
            self.assertIsNone(when)

    def test_a_line_beyond_the_old_tail_window_is_still_found(self):
        # Regression for the window bug: macrowhisper writes its Accessibility
        # line once, at startup, so a daemon that restarts and then logs
        # heavily pushes that line far back in the live log. A 256KB tail read
        # missed it, which would have let the PREVIOUS process's verdict
        # through from the rotated log once the fallback existed.
        with TemporaryDirectory() as tmp:
            filler = "[2026-08-10 02:00:00] [DEBUG] noise\n" * 12000
            self.assertGreater(len(filler.encode("utf-8")), 262144)
            path = self.write(
                tmp,
                "[2026-08-10 01:23:42] [DEBUG] Accessibility permissions already granted\n"
                + filler,
            )
            granted, when = Macrowhisper(log_path=str(path)).accessibility_state()
            self.assertTrue(granted)
            self.assertEqual(when, datetime(2026, 8, 10, 1, 23, 42))

    def test_a_rotated_log_supplies_the_line_when_the_live_log_has_none(self):
        # The live defect, 2026-08-11: macrowhisper rotates at 5MB and keeps
        # one backup (Logger.swift:18, :22), and writes the Accessibility line
        # only at startup, so a rotation leaves the live log without one.
        with TemporaryDirectory() as tmp:
            path = self.write(tmp, "[2026-08-10 19:00:00] [DEBUG] nothing relevant here\n")
            rotated = Path(tmp) / "macrowhisper.log.2026-08-10 18-14-18"
            rotated.write_text(
                "[2026-08-10 01:23:42] [DEBUG] Accessibility permissions already granted\n"
                "[2026-08-10 01:23:43] [DEBUG] later noise\n",
                encoding="utf-8",
            )
            granted, when = Macrowhisper(log_path=str(path)).accessibility_state()
            self.assertTrue(granted)
            self.assertEqual(when, datetime(2026, 8, 10, 1, 23, 42))

    def test_a_rotated_denial_is_reported_not_swallowed(self):
        with TemporaryDirectory() as tmp:
            path = self.write(tmp, "[2026-08-10 19:00:00] [DEBUG] nothing relevant here\n")
            rotated = Path(tmp) / "macrowhisper.log.2026-08-10 18-14-18"
            rotated.write_text(
                "[2026-08-10 01:23:42] [WARNING] Accessibility permissions were not granted"
                " - some features may be limited\n",
                encoding="utf-8",
            )
            granted, _ = Macrowhisper(log_path=str(path)).accessibility_state()
            self.assertIs(granted, False)

    def test_the_newest_rotated_log_wins(self):
        with TemporaryDirectory() as tmp:
            path = self.write(tmp, "[2026-08-10 19:00:00] [DEBUG] nothing relevant here\n")
            # Filenames deliberately sort opposite to mtimes, so a name-based
            # selection would fail this test.
            name_first = Path(tmp) / "macrowhisper.log.2026-08-01 00-00-00"
            name_first.write_text(
                "[2026-08-01 00:00:00] [DEBUG] Accessibility permissions already granted\n",
                encoding="utf-8",
            )
            name_last = Path(tmp) / "macrowhisper.log.2026-08-10 18-14-18"
            name_last.write_text(
                "[2026-08-10 01:23:42] [WARNING] Accessibility permissions were not granted"
                " - some features may be limited\n",
                encoding="utf-8",
            )
            os.utime(name_first, (2000000, 2000000))
            os.utime(name_last, (1000000, 1000000))
            granted, _ = Macrowhisper(log_path=str(path)).accessibility_state()
            self.assertTrue(granted)

    def test_an_unstatable_rotated_candidate_does_not_crash_the_check(self):
        # A broken symlink matches the glob but cannot be stat-ed. It must sort
        # last and never raise: doctor is read-only and a check that throws is
        # worse than one that says unknown.
        with TemporaryDirectory() as tmp:
            path = self.write(tmp, "[2026-08-10 19:00:00] [DEBUG] nothing relevant here\n")
            good = Path(tmp) / "macrowhisper.log.2026-08-10 18-14-18"
            good.write_text(
                "[2026-08-10 01:23:42] [DEBUG] Accessibility permissions already granted\n",
                encoding="utf-8",
            )
            (Path(tmp) / "macrowhisper.log.broken").symlink_to(Path(tmp) / "nonexistent")
            granted, _ = Macrowhisper(log_path=str(path)).accessibility_state()
            self.assertTrue(granted)

    def test_an_unreadable_rotated_log_is_unknown_not_a_crash(self):
        with TemporaryDirectory() as tmp:
            path = self.write(tmp, "[2026-08-10 19:00:00] [DEBUG] nothing relevant here\n")
            only = Path(tmp) / "macrowhisper.log.broken"
            only.symlink_to(Path(tmp) / "nonexistent")
            self.assertEqual(
                Macrowhisper(log_path=str(path)).accessibility_state(), (None, None)
            )

    def test_no_rotated_log_at_all_is_unknown(self):
        with TemporaryDirectory() as tmp:
            path = self.write(tmp, "[2026-08-10 19:00:00] [DEBUG] nothing relevant here\n")
            self.assertEqual(
                Macrowhisper(log_path=str(path)).accessibility_state(), (None, None)
            )

    def test_the_prompted_grant_wording_is_recognized(self):
        # Accessibility.swift:59 logs "Accessibility permissions granted" when
        # the prompt was shown and the user granted it, distinct from :51's
        # "already granted" wording. Missing this pattern would fall through
        # to the rotated log, so a rotated denial must be here to prove the
        # live log's own wording is what wins.
        with TemporaryDirectory() as tmp:
            path = self.write(
                tmp,
                "[2026-08-10 19:00:00] [INFO] Accessibility permissions granted\n",
            )
            rotated = Path(tmp) / "macrowhisper.log.2026-08-10 18-14-18"
            rotated.write_text(
                "[2026-08-10 01:23:42] [WARNING] Accessibility permissions were not granted"
                " - some features may be limited\n",
                encoding="utf-8",
            )
            granted, _ = Macrowhisper(log_path=str(path)).accessibility_state()
            self.assertIs(granted, True)

    def test_the_live_log_takes_precedence_over_the_rotated_backup(self):
        # Even when both files have a matching line, the live log must win:
        # an implementation that consulted the backup first would still pass
        # every other test in this class.
        with TemporaryDirectory() as tmp:
            path = self.write(
                tmp,
                "[2026-08-10 19:00:00] [DEBUG] Accessibility permissions already granted\n",
            )
            rotated = Path(tmp) / "macrowhisper.log.2026-08-01 00-00-00"
            rotated.write_text(
                "[2026-08-01 00:00:00] [WARNING] Accessibility permissions were not granted"
                " - some features may be limited\n",
                encoding="utf-8",
            )
            granted, when = Macrowhisper(log_path=str(path)).accessibility_state()
            self.assertIs(granted, True)
            self.assertEqual(when, datetime(2026, 8, 10, 19, 0, 0))


class TestBridgeEdges(unittest.TestCase):
    def test_env_python_with_short_output_is_none(self):
        runner = FakeRunner({"python3": ok("only-one-line\n")})
        self.assertEqual(BridgeState("/tmp/w", runner=runner).env_python(), (None, None))

    @unittest.skipIf(os.geteuid() == 0, "root bypasses mode bits")
    def test_recent_log_errors_errors_on_an_unreadable_log_is_empty(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "macrovoice.log"
            log.write_text("x", encoding="utf-8")
            log.chmod(0o000)
            try:
                self.assertEqual(BridgeState(root).recent_log_errors(), ())
            finally:
                log.chmod(0o644)

    def test_a_tail_beginning_mid_entry_skips_the_orphan_line(self):
        # The tail window can open inside a multi-line traceback, so the first
        # physical line may be a continuation with no timestamp.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "macrovoice.log").write_text(
                "    File \"x.py\", line 1, in <module>\n"
                "ValueError: orphaned\n",
                encoding="utf-8",
            )
            self.assertEqual(BridgeState(root).recent_log_errors(), ())


if __name__ == "__main__":
    unittest.main()
