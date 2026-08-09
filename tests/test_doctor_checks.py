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
from macrovoice.doctor.adapters.macrowhisper import (  # noqa: E402
    StatusSnapshot,
    parse_status,
)
from macrovoice.doctor.model import Context, Outcome  # noqa: E402

STATUS_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "doctor" / "status"


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

    def test_moveto_reported_as_none_is_a_problem_not_ok(self):
        # Regression: --status never emits an empty string. SocketCommunication
        # .swift:3259 prints moveTo.isEmpty ? "(none)" : moveTo, so a stock,
        # never-configured macrowhisper reports "(none)" here. Reporting OK on
        # this is the exact state every new user is in.
        status = StatusSnapshot(running=True, recognized=True, move_to="(none)")
        finding = registry._check_moveto(context(mw=FakeMacrowhisper(status=status)))
        self.assertIs(finding.outcome, Outcome.PROBLEM)

    def test_a_real_moveto_value_is_ok(self):
        status = StatusSnapshot(running=True, recognized=True, move_to=".delete")
        finding = registry._check_moveto(context(mw=FakeMacrowhisper(status=status)))
        self.assertIs(finding.outcome, Outcome.OK)


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

    def test_the_running_daemons_live_value_wins_over_the_file(self):
        # Friction trap 5: editing the config while the daemon runs can be
        # silently discarded. --status reports the daemon's actual in-memory
        # belief, which must win over a file that may no longer be true.
        status = StatusSnapshot(running=True, recognized=True, watch_folder="/tmp/w")
        ctx = context(
            mw=FakeMacrowhisper(
                status=status, config={"defaults": {"watch": "/somewhere/else"}}
            ),
            watch_root="/tmp/w",
        )
        finding = registry._check_watch_match(ctx)
        self.assertIs(finding.outcome, Outcome.OK)
        self.assertIn("daemon", finding.detail)

    def test_a_mismatch_against_the_live_daemon_value_is_a_problem(self):
        status = StatusSnapshot(running=True, recognized=True, watch_folder="/somewhere/else")
        ctx = context(
            mw=FakeMacrowhisper(
                status=status, config={"defaults": {"watch": "/tmp/w"}}
            ),
            watch_root="/tmp/w",
        )
        finding = registry._check_watch_match(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("/somewhere/else", finding.detail)

    def test_falls_back_to_the_file_when_the_daemon_is_not_running(self):
        # status.watch_folder is only ever set on the running branch of
        # parse_status, so None here means "daemon down or unreadable",
        # which is exactly when the file is the only source available.
        status = StatusSnapshot(running=False, recognized=True)
        ctx = context(
            mw=FakeMacrowhisper(
                status=status, config={"defaults": {"watch": "/tmp/w"}}
            ),
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
    """No freshness comparison: the Accessibility line is logged once,
    unconditionally, at true process startup (main.swift:1905), so the
    newest line in the tail always describes the running process. The
    watcher's arm time is a different thing entirely and re-arms on wake
    (main.swift:672-683), which is why this check must not compare against
    it. See the docstring on _check_accessibility for the full history."""

    def test_denied_is_a_problem_naming_the_restart(self):
        ctx = context(mw=FakeMacrowhisper(accessibility=(False, datetime(2026, 8, 9, 11, 0, 0))))
        finding = registry._check_accessibility(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("--restart-service", finding.fix_hint)

    def test_no_accessibility_line_is_unknown(self):
        ctx = context(mw=FakeMacrowhisper(accessibility=(None, None)))
        self.assertIs(registry._check_accessibility(ctx).outcome, Outcome.UNKNOWN)

    def test_granted_is_ok_no_matter_how_old_the_line_is(self):
        # Regression for the false FAIL-severity UNKNOWN found live: a grant
        # line logged hours or days ago (the daemon has been up since) must
        # still read as OK, since it is the only such line the process ever
        # logs.
        ctx = context(
            mw=FakeMacrowhisper(accessibility=(True, datetime.now() - timedelta(days=2)))
        )
        self.assertIs(registry._check_accessibility(ctx).outcome, Outcome.OK)


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


class TestDefaultConfigFixture(unittest.TestCase):
    """Drives checks through parse_status() on a fixture built from
    SocketCommunication.swift:3246-3270 and the compiled Defaults.defaultValues()
    (AppConfiguration.swift:376-407), representing a stock, never configured
    daemon. This is the shape of input the real daemon can produce, which a
    hand-built StatusSnapshot(move_to="") cannot: --status never emits an
    empty string, only "(none)" (SocketCommunication.swift:3259). That gap
    between the fixture corpus and the real output is exactly how the C1
    false OK on moveTo survived 130 tests, so these checks are driven off the
    parser, never a hand-constructed snapshot.

    The assertions below are the honest answer for THIS state, not a
    preselected list of problems: defaultValues() also sets activeAction to
    "autoPaste" and defaultConfig() seeds a matching "autoPaste" insert
    (AppConfiguration.swift:381, :1007-1035), so mw.action is genuinely OK
    on a stock daemon. Asserting a problem there would itself be a false
    report, the exact failure mode this feature exists to avoid.
    """

    def setUp(self):
        text = (STATUS_FIXTURES / "default-config.txt").read_text(encoding="utf-8")
        self.status = parse_status(text)

    def test_the_fixture_parses_as_a_running_recognized_daemon(self):
        self.assertTrue(self.status.recognized)
        self.assertTrue(self.status.running)

    def test_moveto_is_a_problem_not_ok(self):
        ctx = context(mw=FakeMacrowhisper(status=self.status))
        self.assertIs(registry._check_moveto(ctx).outcome, Outcome.PROBLEM)

    def test_simesc_is_a_problem_not_ok(self):
        ctx = context(mw=FakeMacrowhisper(status=self.status))
        self.assertIs(registry._check_simesc(ctx).outcome, Outcome.PROBLEM)

    def test_active_action_is_ok_when_autopaste_is_defined(self):
        # A stock config really does define the "autoPaste" it names as
        # active (AppConfiguration.swift:1007-1035's defaultConfig()), so
        # this must read OK, not PROBLEM.
        ctx = context(
            mw=FakeMacrowhisper(status=self.status, config={"inserts": {"autoPaste": {}}})
        )
        self.assertIs(registry._check_action(ctx).outcome, Outcome.OK)

    def test_missing_recordings_folder_is_a_problem_not_ok(self):
        ctx = context(mw=FakeMacrowhisper(status=self.status))
        self.assertIs(registry._check_folders(ctx).outcome, Outcome.PROBLEM)

    def test_stock_watch_path_mismatches_a_real_bridge_root_and_is_a_problem(self):
        # The stock watch path is ~/Documents/superwhisper, the exact
        # directory this project's README tells users never to point the
        # bridge at. Checked against any real bridge watch root, that is a
        # mismatch macrowhisper would otherwise silently sit on.
        ctx = context(mw=FakeMacrowhisper(status=self.status), watch_root="/tmp/w")
        finding = registry._check_watch_match(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("Documents/superwhisper", finding.detail)


if __name__ == "__main__":
    unittest.main()
