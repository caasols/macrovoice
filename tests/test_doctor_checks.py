"""Every check, against fake adapters. No Mac state, no daemon, no VoiceInk."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from macrovoice.doctor import registry  # noqa: E402
from macrovoice.doctor.adapters.bridge import BridgeSnapshot  # noqa: E402
from macrovoice.doctor.adapters.macrowhisper import StatusSnapshot  # noqa: E402
from macrovoice.doctor.model import Context, Outcome  # noqa: E402


class FakeBridge:
    def __init__(self, **overrides):
        self._snapshot = BridgeSnapshot(
            watch_root=Path("/tmp/w"),
            watch_exists=overrides.get("watch_exists", True),
            recordings_exists=overrides.get("recordings_exists", True),
            recordings_count=overrides.get("recordings_count", 0),
            spool_count=overrides.get("spool_count", 0),
            staging_count=overrides.get("staging_count", 0),
        )
        self._script = overrides.get("script", Path("/tmp/w/macrovoice.sh"))
        self._python = overrides.get("python", ("/usr/bin/python3", "3.9.6"))
        self._errors = overrides.get("errors", ())

    def snapshot(self):
        return self._snapshot

    def script_path(self):
        return self._script

    def env_python(self):
        return self._python

    def recent_log_errors(self, within_hours=24):
        return self._errors


class FakeMacrowhisper:
    def __init__(self, **overrides):
        self._available = overrides.get("available", True)
        self._status = overrides.get("status", StatusSnapshot(running=True, recognized=True))
        self._saved = overrides.get("saved_config", "/Users/x/.config/macrowhisper/macrowhisper.json")
        self._valid = overrides.get("validate", (True, "Configuration is valid"))
        self._service = overrides.get("service_installed", True)
        self._config = overrides.get("config", {})
        self._access = overrides.get("accessibility", (True, None))

    def available(self):
        return self._available

    def status(self, refresh=False):
        return self._status

    def saved_config_path(self):
        return self._saved

    def validate_config(self):
        return self._valid

    def service_installed(self):
        return self._service

    def read_config(self, path):
        return self._config

    def accessibility_state(self):
        return self._access


def context(mw=None, bridge=None, watch_root="/tmp/w"):
    return Context(
        watch_root=Path(watch_root),
        mw=mw or FakeMacrowhisper(),
        bridge=bridge or FakeBridge(),
    )


class TestPrerequisites(unittest.TestCase):
    def test_env_python_too_old_is_a_problem(self):
        ctx = context(bridge=FakeBridge(python=("/usr/bin/python3", "3.8.10")))
        self.assertIs(registry._check_env_python(ctx).outcome, Outcome.PROBLEM)

    def test_env_python_current_is_ok(self):
        self.assertIs(registry._check_env_python(context()).outcome, Outcome.OK)

    def test_env_python_unavailable_is_unknown(self):
        ctx = context(bridge=FakeBridge(python=(None, None)))
        self.assertIs(registry._check_env_python(ctx).outcome, Outcome.UNKNOWN)

    def test_missing_macrowhisper_names_the_install_command(self):
        ctx = context(mw=FakeMacrowhisper(available=False))
        finding = registry._check_macrowhisper_installed(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("brew install", finding.fix_hint)

    def test_voiceink_found_is_ok(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            voiceink_path = Path(tmpdir) / "VoiceInk.app"
            voiceink_path.mkdir()
            with mock.patch.object(registry, "VOICEINK_APP_PATHS", (voiceink_path,)):
                finding = registry._check_voiceink_installed(context())
                self.assertIs(finding.outcome, Outcome.OK)
                self.assertIn(str(voiceink_path), finding.detail)

    def test_voiceink_not_found_is_a_problem(self):
        with mock.patch.object(registry, "VOICEINK_APP_PATHS", (Path("/nonexistent/VoiceInk.app"),)):
            finding = registry._check_voiceink_installed(context())
            self.assertIs(finding.outcome, Outcome.PROBLEM)
            self.assertIn("VoiceInk.app not found", finding.detail)


class TestBridgeLayout(unittest.TestCase):
    def test_missing_watch_root_is_a_problem(self):
        ctx = context(bridge=FakeBridge(watch_exists=False, recordings_exists=False))
        self.assertIs(registry._check_watch_dirs(ctx).outcome, Outcome.PROBLEM)

    def test_watch_without_recordings_is_a_problem(self):
        ctx = context(bridge=FakeBridge(recordings_exists=False))
        finding = registry._check_watch_dirs(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("recordings", finding.detail)

    def test_both_present_is_ok(self):
        self.assertIs(registry._check_watch_dirs(context()).outcome, Outcome.OK)

    def test_script_does_not_exist_is_a_problem(self):
        ctx = context(bridge=FakeBridge(script=Path("/nonexistent/macrovoice.sh")))
        finding = registry._check_script(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("does not exist", finding.detail)

    def test_script_not_executable_is_a_problem(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            script_path = Path(f.name)
        try:
            os.chmod(str(script_path), 0o644)
            ctx = context(bridge=FakeBridge(script=script_path))
            finding = registry._check_script(ctx)
            self.assertIs(finding.outcome, Outcome.PROBLEM)
            self.assertIn("not executable", finding.detail)
            self.assertIn("chmod +x", finding.fix_hint)
            self.assertIn(str(script_path), finding.fix_hint)
        finally:
            script_path.unlink()

    def test_script_executable_is_ok(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            script_path = Path(f.name)
        try:
            os.chmod(str(script_path), 0o755)
            ctx = context(bridge=FakeBridge(script=script_path))
            finding = registry._check_script(ctx)
            self.assertIs(finding.outcome, Outcome.OK)
        finally:
            script_path.unlink()

    def test_a_non_empty_spool_is_a_problem(self):
        ctx = context(bridge=FakeBridge(spool_count=2))
        finding = registry._check_spool(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("2", finding.detail)
        self.assertIn("spool plus staging", finding.detail)

    def test_spool_drain_command_names_the_script_path(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".sh") as f:
            script_path = Path(f.name)
        try:
            os.chmod(str(script_path), 0o755)
            ctx = context(bridge=FakeBridge(spool_count=1, script=script_path))
            finding = registry._check_spool(ctx)
            self.assertIs(finding.outcome, Outcome.PROBLEM)
            self.assertIn(str(script_path), finding.fix_hint)
            self.assertIn("--drain-only", finding.fix_hint)
        finally:
            script_path.unlink()

    def test_recent_errors_are_reported(self):
        ctx = context(bridge=FakeBridge(errors=("2026-08-09T10:00:00Z ERROR boom",)))
        self.assertIs(registry._check_log_errors(ctx).outcome, Outcome.PROBLEM)


class TestTableIntegrity(unittest.TestCase):
    def test_the_table_orders_without_cycles_or_dangling_dependencies(self):
        from macrovoice.doctor.runner import order_checks

        order_checks(registry.CHECKS)  # raises on a table bug

    def test_ids_are_unique(self):
        ids = [c.id for c in registry.CHECKS]
        self.assertEqual(len(ids), len(set(ids)))


from datetime import datetime, timedelta  # noqa: E402


class TestMacrowhisperRuntime(unittest.TestCase):
    def test_not_running_is_a_problem_with_the_start_command(self):
        ctx = context(
            mw=FakeMacrowhisper(status=StatusSnapshot(running=False, recognized=True))
        )
        finding = registry._check_running(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("--start-service", finding.fix_hint)

    def test_unrecognised_output_is_unknown_not_a_failure(self):
        ctx = context(
            mw=FakeMacrowhisper(status=StatusSnapshot(running=False, recognized=False))
        )
        self.assertIs(registry._check_running(ctx).outcome, Outcome.UNKNOWN)

    def test_unarmed_watcher_names_the_startup_race(self):
        status = StatusSnapshot(
            running=True, recognized=True, watcher_present=True, watcher_armed=False
        )
        finding = registry._check_armed(context(mw=FakeMacrowhisper(status=status)))
        self.assertIs(finding.outcome, Outcome.PROBLEM)


class TestDangerousSettings(unittest.TestCase):
    def test_simesc_on_is_a_problem_and_says_it_destroys_work(self):
        status = StatusSnapshot(running=True, recognized=True, sim_esc=True)
        finding = registry._check_simesc(context(mw=FakeMacrowhisper(status=status)))
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("Escape", finding.detail)

    def test_simesc_off_is_ok(self):
        status = StatusSnapshot(running=True, recognized=True, sim_esc=False)
        self.assertIs(
            registry._check_simesc(context(mw=FakeMacrowhisper(status=status))).outcome,
            Outcome.OK,
        )

    def test_simesc_absent_from_the_output_is_unknown_never_ok(self):
        # This is the whole reason the parser is tolerant. Reporting OK here
        # would tell the user their work is safe when we do not know.
        status = StatusSnapshot(running=True, recognized=True, sim_esc=None)
        self.assertIs(
            registry._check_simesc(context(mw=FakeMacrowhisper(status=status))).outcome,
            Outcome.UNKNOWN,
        )

    def test_empty_moveto_is_a_problem_about_plaintext_accumulating(self):
        status = StatusSnapshot(running=True, recognized=True, move_to="")
        finding = registry._check_moveto(context(mw=FakeMacrowhisper(status=status)))
        self.assertIs(finding.outcome, Outcome.PROBLEM)


class TestConfigPath(unittest.TestCase):
    def test_a_temp_config_path_is_the_hijack_and_is_caught(self):
        ctx = context(
            mw=FakeMacrowhisper(saved_config="/var/folders/ab/T/tmp123/macrowhisper.json")
        )
        finding = registry._check_config_path(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("--set-config", finding.fix_hint)

    def test_a_normal_path_is_ok(self):
        self.assertIs(registry._check_config_path(context()).outcome, Outcome.OK)

    def test_no_path_at_all_is_unknown(self):
        ctx = context(mw=FakeMacrowhisper(saved_config=None))
        self.assertIs(registry._check_config_path(ctx).outcome, Outcome.UNKNOWN)


class TestWatchMatch(unittest.TestCase):
    def test_mismatch_is_a_problem(self):
        ctx = context(
            mw=FakeMacrowhisper(config={"defaults": {"watch": "/somewhere/else"}}),
            watch_root="/tmp/w",
        )
        self.assertIs(registry._check_watch_match(ctx).outcome, Outcome.PROBLEM)

    def test_match_is_ok(self):
        ctx = context(
            mw=FakeMacrowhisper(config={"defaults": {"watch": "/tmp/w"}}),
            watch_root="/tmp/w",
        )
        self.assertIs(registry._check_watch_match(ctx).outcome, Outcome.OK)


class TestActiveAction(unittest.TestCase):
    def test_an_action_that_is_not_defined_is_a_problem(self):
        status = StatusSnapshot(running=True, recognized=True, active_action="ghost")
        ctx = context(
            mw=FakeMacrowhisper(status=status, config={"inserts": {"autoPaste": {}}})
        )
        self.assertIs(registry._check_action(ctx).outcome, Outcome.PROBLEM)

    def test_a_defined_action_is_ok(self):
        status = StatusSnapshot(running=True, recognized=True, active_action="autoPaste")
        ctx = context(
            mw=FakeMacrowhisper(status=status, config={"inserts": {"autoPaste": {}}})
        )
        self.assertIs(registry._check_action(ctx).outcome, Outcome.OK)

    def test_a_malformed_category_does_not_false_positive_on_substring_match(self):
        # A category is supposed to be a mapping of action name to config. If it
        # is a string instead, "in" does substring matching: "co" in "corrupted"
        # is True. That must not read as the action being defined.
        status = StatusSnapshot(running=True, recognized=True, active_action="co")
        ctx = context(
            mw=FakeMacrowhisper(status=status, config={"inserts": "corrupted"})
        )
        self.assertIsNot(registry._check_action(ctx).outcome, Outcome.OK)


class TestMultipleDependencies(unittest.TestCase):
    def test_the_first_declared_blocker_wins_when_both_dependencies_fail(self):
        # mw.action is the first check with more than one dependency
        # (mw.running, mw.configexists). With macrowhisper unavailable, both
        # dependencies end up not-OK; the runner must report the first one in
        # declaration order as the blocker, not the second.
        from macrovoice.doctor.runner import run

        ctx = context(mw=FakeMacrowhisper(available=False))
        results = run(registry.CHECKS, ctx)
        findings = {result.check.id: result.finding for result in results}
        self.assertIs(findings["mw.action"].outcome, Outcome.UNKNOWN)
        self.assertEqual(findings["mw.action"].blocked_by, "mw.running")


class TestClipboardBuffer(unittest.TestCase):
    def test_the_default_five_seconds_is_a_warning(self):
        ctx = context(mw=FakeMacrowhisper(config={"defaults": {"clipboardBuffer": 5.0}}))
        self.assertIs(registry._check_clipboard_buffer(ctx).outcome, Outcome.PROBLEM)

    def test_sixty_is_ok(self):
        ctx = context(mw=FakeMacrowhisper(config={"defaults": {"clipboardBuffer": 60.0}}))
        self.assertIs(registry._check_clipboard_buffer(ctx).outcome, Outcome.OK)

    def test_a_non_numeric_value_is_a_problem_not_a_crash(self):
        # float("not-a-number") raises. That must surface as domain-shaped
        # copy, not the runner's generic "check raised" fallback.
        ctx = context(
            mw=FakeMacrowhisper(config={"defaults": {"clipboardBuffer": "lots"}})
        )
        finding = registry._check_clipboard_buffer(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("lots", finding.detail)

    def test_a_boolean_value_does_not_silently_convert_to_a_number(self):
        ctx = context(mw=FakeMacrowhisper(config={"defaults": {"clipboardBuffer": True}}))
        self.assertIsNot(registry._check_clipboard_buffer(ctx).outcome, Outcome.OK)


class TestAccessibility(unittest.TestCase):
    def test_denied_is_a_problem_naming_the_restart(self):
        ctx = context(mw=FakeMacrowhisper(accessibility=(False, datetime(2026, 8, 9, 11, 0, 0))))
        finding = registry._check_accessibility(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("--restart-service", finding.fix_hint)

    def test_granted_recently_is_ok(self):
        status = StatusSnapshot(
            running=True, recognized=True, watcher_started_ago_s=600
        )
        ctx = context(
            mw=FakeMacrowhisper(
                status=status, accessibility=(True, datetime.now() - timedelta(minutes=9))
            )
        )
        self.assertIs(registry._check_accessibility(ctx).outcome, Outcome.OK)

    def test_a_line_older_than_the_daemon_is_unknown_not_ok(self):
        # Trap 4: the line is logged at startup. If it predates the running
        # daemon it describes a previous one, so we do not actually know.
        status = StatusSnapshot(running=True, recognized=True, watcher_started_ago_s=600)
        ctx = context(
            mw=FakeMacrowhisper(
                status=status, accessibility=(True, datetime.now() - timedelta(hours=5))
            )
        )
        self.assertIs(registry._check_accessibility(ctx).outcome, Outcome.UNKNOWN)

    def test_unknown_daemon_start_time_is_unknown_never_ok(self):
        # Without the daemon's start time we cannot tell whether a stale-looking
        # grant line is fresh. Falling through to OK here would be a false OK on
        # a FAIL-severity check about whether pasting works at all.
        ctx = context(
            mw=FakeMacrowhisper(
                accessibility=(True, datetime.now() - timedelta(hours=5))
            )
        )
        self.assertIs(registry._check_accessibility(ctx).outcome, Outcome.UNKNOWN)

    def test_regression_grant_line_within_the_reported_hours_precision_is_ok(self):
        # Regression for the false UNKNOWN found live: macrowhisper's status
        # text truncates to whole units ("started 6h ago" means anywhere in
        # [6h, 7h)), so a grant line 6h30m old with "6h" reported must not be
        # flagged as predating the daemon.
        status = StatusSnapshot(
            running=True,
            recognized=True,
            watcher_started_ago_s=6 * 3600,
            watcher_started_ago_unit="h",
        )
        ctx = context(
            mw=FakeMacrowhisper(
                status=status,
                accessibility=(True, datetime.now() - timedelta(hours=6, minutes=30)),
            )
        )
        self.assertIs(registry._check_accessibility(ctx).outcome, Outcome.OK)

    def test_freshly_restarted_daemon_reporting_just_now_is_ok(self):
        status = StatusSnapshot(
            running=True,
            recognized=True,
            watcher_started_ago_s=0,
            watcher_started_ago_unit="s",
        )
        ctx = context(
            mw=FakeMacrowhisper(
                status=status,
                accessibility=(True, datetime.now() - timedelta(seconds=2)),
            )
        )
        self.assertIs(registry._check_accessibility(ctx).outcome, Outcome.OK)

    def test_a_line_older_than_the_widened_tolerance_is_still_unknown(self):
        # The tolerance widens to match reported precision, it does not
        # disappear: a line genuinely from a previous daemon must still be
        # reported UNKNOWN, not OK.
        status = StatusSnapshot(
            running=True,
            recognized=True,
            watcher_started_ago_s=6 * 3600,
            watcher_started_ago_unit="h",
        )
        ctx = context(
            mw=FakeMacrowhisper(
                status=status,
                accessibility=(True, datetime.now() - timedelta(days=2)),
            )
        )
        self.assertIs(registry._check_accessibility(ctx).outcome, Outcome.UNKNOWN)


class TestConfigExists(unittest.TestCase):
    def test_missing_config_names_a_copy_command_from_the_repo_sample(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            saved = str(Path(tmpdir) / "does-not-exist" / "macrowhisper.json")
            script = Path(tmpdir) / "macrovoice.sh"
            ctx = context(
                mw=FakeMacrowhisper(saved_config=saved),
                bridge=FakeBridge(script=script),
            )
            finding = registry._check_config_exists(ctx)
            self.assertIs(finding.outcome, Outcome.PROBLEM)
            self.assertIn(saved, finding.fix_hint)
            self.assertIn(str(script.parent / "macrovoice.sample.json"), finding.fix_hint)

    def test_existing_config_is_ok(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as f:
            saved = f.name
        try:
            ctx = context(mw=FakeMacrowhisper(saved_config=saved))
            self.assertIs(registry._check_config_exists(ctx).outcome, Outcome.OK)
        finally:
            os.unlink(saved)

    def test_no_saved_path_is_unknown(self):
        ctx = context(mw=FakeMacrowhisper(saved_config=None))
        self.assertIs(registry._check_config_exists(ctx).outcome, Outcome.UNKNOWN)


class TestConfigValid(unittest.TestCase):
    def test_invalid_config_names_the_validate_command(self):
        ctx = context(
            mw=FakeMacrowhisper(validate=(False, "line one issue\nline two issue"))
        )
        finding = registry._check_config_valid(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("line one issue", finding.detail)
        self.assertIn("--validate-config", finding.fix_hint)

    def test_valid_config_is_ok(self):
        ctx = context(mw=FakeMacrowhisper(validate=(True, "Configuration is valid")))
        self.assertIs(registry._check_config_valid(ctx).outcome, Outcome.OK)

    def test_could_not_run_validate_is_unknown(self):
        ctx = context(mw=FakeMacrowhisper(validate=(None, "")))
        self.assertIs(registry._check_config_valid(ctx).outcome, Outcome.UNKNOWN)


class TestServiceInstalled(unittest.TestCase):
    def test_not_installed_names_the_install_command(self):
        ctx = context(mw=FakeMacrowhisper(service_installed=False))
        finding = registry._check_service(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("--install-service", finding.fix_hint)

    def test_installed_is_ok(self):
        ctx = context(mw=FakeMacrowhisper(service_installed=True))
        self.assertIs(registry._check_service(ctx).outcome, Outcome.OK)

    def test_could_not_run_service_status_is_unknown(self):
        ctx = context(mw=FakeMacrowhisper(service_installed=None))
        self.assertIs(registry._check_service(ctx).outcome, Outcome.UNKNOWN)


class TestFolders(unittest.TestCase):
    def test_missing_folder_with_a_known_path_names_mkdir(self):
        status = StatusSnapshot(
            running=True,
            recognized=True,
            recordings_folder="/tmp/w/recordings",
            recordings_folder_exists=False,
        )
        ctx = context(mw=FakeMacrowhisper(status=status))
        finding = registry._check_folders(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertEqual(finding.fix_hint, "mkdir -p /tmp/w/recordings")

    def test_missing_folder_with_no_reported_path_has_no_dangling_mkdir(self):
        status = StatusSnapshot(
            running=True,
            recognized=True,
            recordings_folder="",
            recordings_folder_exists=False,
        )
        ctx = context(mw=FakeMacrowhisper(status=status))
        finding = registry._check_folders(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertEqual(finding.fix_hint, "")

    def test_folder_present_is_ok(self):
        status = StatusSnapshot(
            running=True,
            recognized=True,
            recordings_folder="/tmp/w/recordings",
            recordings_folder_exists=True,
        )
        ctx = context(mw=FakeMacrowhisper(status=status))
        self.assertIs(registry._check_folders(ctx).outcome, Outcome.OK)

    def test_status_silent_on_the_folder_is_unknown(self):
        status = StatusSnapshot(running=True, recognized=True)
        ctx = context(mw=FakeMacrowhisper(status=status))
        self.assertIs(registry._check_folders(ctx).outcome, Outcome.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
