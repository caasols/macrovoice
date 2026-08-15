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
from macrovoice.doctor.adapters.voiceink import Mode as VoiceInkMode  # noqa: E402
from macrovoice.doctor.model import Context, Outcome  # noqa: E402

STATUS_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "doctor" / "status"
REPO_ROOT = Path(__file__).resolve().parent.parent

_MISSING = object()


class FakeBridge:
    def __init__(self, **overrides):
        self._snapshot = BridgeSnapshot(
            watch_root=Path("/tmp/w"),
            watch_exists=overrides.get("watch_exists", True),
            recordings_exists=overrides.get("recordings_exists", True),
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
        self._config_path = overrides.get("config_path", _MISSING)

    def available(self):
        return self._available

    def status(self, refresh=False):
        return self._status

    def saved_config_path(self):
        return self._saved

    def config_path(self):
        from macrovoice.doctor.adapters.macrowhisper import ConfigPath

        if self._config_path is not _MISSING:
            return self._config_path
        # Default: behave like the old saved_config_path override, persisted.
        return ConfigPath(self._saved, True) if self._saved else None

    def validate_config(self):
        return self._valid

    def service_installed(self):
        return self._service

    def read_config(self, path):
        return self._config

    def accessibility_state(self):
        return self._access


class FakeVoiceInk:
    """Stands in for the read-only VoiceInk adapter.

    `modes=None` and `shortcut=None` mean "could not read", which the checks must
    turn into UNKNOWN rather than into an accusation.
    """

    def __init__(self, modes=(), shortcut=True, running=False):
        self._modes = modes
        self._shortcut = shortcut
        self._running = running

    def modes(self):
        return self._modes

    def has_shortcut(self, mode_id):
        return self._shortcut

    def is_running(self):
        return self._running


def vi_mode(name="macrovoice", command="/repo/macrovoice.sh --mode macrovoice",
            output_mode="customCommand", is_default=False, is_enabled=True,
            mode_id="B-1"):
    return VoiceInkMode(
        id=mode_id, name=name, output_mode=output_mode,
        is_default=is_default, is_enabled=is_enabled, command=command,
    )


def paste_mode(name="Dictation", is_default=True):
    return VoiceInkMode(
        id="D-1", name=name, output_mode="paste",
        is_default=is_default, is_enabled=True, command=None,
    )


def context(mw=None, bridge=None, watch_root="/tmp/w", vi=None, home=None):
    return Context(
        watch_root=Path(watch_root),
        mw=mw or FakeMacrowhisper(),
        bridge=bridge or FakeBridge(),
        vi=vi if vi is not None else FakeVoiceInk(),
        home=Path(home) if home is not None else None,
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


class TestTheLegacyWatchHint(unittest.TestCase):
    """B4's legacy hint.

    The rename to `~/macrovoice` is non-breaking, so an unmigrated user keeps a
    WORKING bridge on `~/mw-bridge` indefinitely. That is deliberate, and it is
    also exactly how a deprecation becomes permanent: nothing ever tells them.
    This check is the telling.

    It must never be fatal. A user on the legacy path has a healthy bridge, and
    turning a healthy machine red would reproduce the "the bridge is broken"
    shape that twelve of the thirteen setup traps already have.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, name):
        (self.home / name).mkdir()
        return self.home / name

    def check(self, watch_root):
        return registry._check_legacy_watch(
            context(watch_root=str(watch_root), home=str(self.home))
        )

    def test_the_current_name_is_ok(self):
        target = self.make("macrovoice")
        self.assertIs(self.check(target).outcome, Outcome.OK)

    def test_the_legacy_directory_in_use_is_reported(self):
        target = self.make("mw-bridge")
        finding = self.check(target)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("mw-bridge", finding.detail)

    def test_the_report_gives_the_migration_and_names_both_paths(self):
        target = self.make("mw-bridge")
        finding = self.check(target)
        self.assertIn(str(self.home / "mw-bridge"), finding.fix_hint)
        self.assertIn(str(self.home / "macrovoice"), finding.fix_hint)
        # The migration is worthless without the config edit: moving the
        # directory alone points macrowhisper at a path that no longer exists.
        self.assertIn("defaults.watch", finding.fix_hint)
        self.assertIn("stop-service", finding.fix_hint)

    def test_a_leftover_legacy_directory_is_reported_even_when_unused(self):
        # The half-migrated state: `cp -r` rather than `mv`. macrovoice now uses
        # the new directory, and the old one is still sitting there looking
        # authoritative to anyone who reads macrowhisper's config.
        target = self.make("macrovoice")
        self.make("mw-bridge")
        finding = self.check(target)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn(str(self.home / "mw-bridge"), finding.detail)
        self.assertIn(str(self.home / "macrovoice"), finding.detail)

    def test_a_custom_watch_root_is_not_nagged_about(self):
        # Somebody who deliberately runs `--watch ~/somewhere-else` is not on a
        # legacy default and has nothing to migrate.
        target = self.make("somewhere-else")
        self.assertIs(self.check(target).outcome, Outcome.OK)

    def test_a_custom_watch_root_is_still_told_about_a_leftover(self):
        target = self.make("somewhere-else")
        self.make("mw-bridge")
        self.assertIs(self.check(target).outcome, Outcome.PROBLEM)

    def test_a_directory_merely_NAMED_mw_bridge_elsewhere_is_not_the_legacy_one(self):
        # Only `~/mw-bridge` is the legacy default. `/projects/mw-bridge` is
        # somebody's own choice.
        other = self.home / "projects"
        (other / "mw-bridge").mkdir(parents=True)
        self.assertIs(self.check(other / "mw-bridge").outcome, Outcome.OK)

    def test_it_is_a_warning_and_never_changes_the_exit_code(self):
        from macrovoice.doctor.model import Severity
        from macrovoice.doctor.report import exit_code
        from macrovoice.doctor.runner import run

        check = next(c for c in registry.CHECKS if c.id == "bridge.legacywatch")
        self.assertIs(check.severity, Severity.WARN)

        target = self.make("mw-bridge")
        results = run(
            (check,), context(watch_root=str(target), home=str(self.home))
        )
        self.assertIs(results[0].finding.outcome, Outcome.PROBLEM)
        self.assertEqual(exit_code(results), 0)

    def test_it_renders_under_bridge_layout_as_a_warning(self):
        from macrovoice.doctor.report import render
        from macrovoice.doctor.runner import run

        check = next(c for c in registry.CHECKS if c.id == "bridge.legacywatch")
        target = self.make("mw-bridge")
        text = render(run((check,), context(watch_root=str(target), home=str(self.home))))
        self.assertIn("Bridge layout", text)
        self.assertIn("warning", text)
        self.assertIn("1 warning", text)

    def test_the_default_home_is_the_real_one(self):
        # Context.home defaults to None so every existing construction site
        # keeps working, exactly as Context.vi does. None must mean "the real
        # home", not "no home".
        ctx = Context(
            watch_root=Path.home() / "mw-bridge",
            mw=FakeMacrowhisper(),
            bridge=FakeBridge(),
            vi=FakeVoiceInk(),
        )
        self.assertIs(registry._check_legacy_watch(ctx).outcome, Outcome.PROBLEM)


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


class TestConfigPathCheck(unittest.TestCase):
    def test_a_persisted_temp_path_is_the_hijack_and_is_a_problem(self):
        from macrovoice.doctor.adapters.macrowhisper import ConfigPath

        ctx = context(mw=FakeMacrowhisper(
            config_path=ConfigPath("/var/folders/ab/T/tmp123/macrowhisper.json", True)))
        finding = registry._check_config_path(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("--set-config", finding.fix_hint)

    def test_a_persisted_sane_path_is_ok(self):
        from macrovoice.doctor.adapters.macrowhisper import ConfigPath

        ctx = context(mw=FakeMacrowhisper(
            config_path=ConfigPath("/Users/x/.config/macrowhisper/macrowhisper.json", True)))
        self.assertIs(registry._check_config_path(ctx).outcome, Outcome.OK)

    def test_a_fresh_install_using_the_default_is_ok_not_unknown(self):
        # The regression test for the audit's HIGH finding: a brand-new
        # macrowhisper has persisted nothing, and that is a healthy state.
        from macrovoice.doctor.adapters.macrowhisper import ConfigPath

        ctx = context(mw=FakeMacrowhisper(
            config_path=ConfigPath("/Users/x/.config/macrowhisper/macrowhisper.json", False)))
        finding = registry._check_config_path(ctx)
        self.assertIs(finding.outcome, Outcome.OK)
        self.assertIn("default", finding.detail)

    def test_a_temp_default_path_is_not_treated_as_the_hijack(self):
        # Only a PERSISTED path can be the hijack. A default that happens to sit
        # under a temp root is not something the user chose.
        from macrovoice.doctor.adapters.macrowhisper import ConfigPath

        ctx = context(mw=FakeMacrowhisper(
            config_path=ConfigPath("/var/folders/ab/T/tmp123/macrowhisper.json", False)))
        self.assertIsNot(registry._check_config_path(ctx).outcome, Outcome.PROBLEM)

    def test_no_readable_output_is_unknown(self):
        ctx = context(mw=FakeMacrowhisper(config_path=None))
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

    def test_the_unknown_names_every_cause_and_how_to_refresh_it(self):
        # The message must not read as a doctor defect. There are THREE ways
        # the startup line can be absent, and the check cannot tell them apart
        # from the inside, so it must name the set rather than assert one:
        #   1. rotated past  (5MB, one backup: Logger.swift:18, :22)
        #   2. deleted or truncated under the running daemon, which leaves the
        #      file present, small and backup-less. Measured live 2026-08-15.
        #   3. unreadable
        # Naming only rotation made a healthy machine's exit 2 look like a bug
        # in doctor, which is the exact misdiagnosis this whole tool exists to
        # prevent. The remedy is the same for all three.
        ctx = context(mw=FakeMacrowhisper(accessibility=(None, None)))
        finding = registry._check_accessibility(ctx)
        self.assertIs(finding.outcome, Outcome.UNKNOWN)
        self.assertIn("rotated", finding.detail)
        self.assertIn("deleted", finding.detail)
        self.assertIn("unreadable", finding.detail)
        self.assertIn("--restart-service", finding.detail)

    def test_the_unknown_disclaims_being_a_permission_failure(self):
        # UNKNOWN is FAIL-severity and pins the exit code at 2, so the one
        # thing the text must not do is let a reader conclude the permission
        # was denied. Denial is a separate outcome with a separate message.
        ctx = context(mw=FakeMacrowhisper(accessibility=(None, None)))
        detail = registry._check_accessibility(ctx).detail
        self.assertIn("not a permission failure", detail)

    def test_granted_is_ok_no_matter_how_old_the_line_is(self):
        # Regression for the false FAIL-severity UNKNOWN found live: a grant
        # line logged hours or days ago (the daemon has been up since) must
        # still read as OK, since it is the only such line the process ever
        # logs.
        ctx = context(
            mw=FakeMacrowhisper(accessibility=(True, datetime.now() - timedelta(days=2)))
        )
        self.assertIs(registry._check_accessibility(ctx).outcome, Outcome.OK)


class TestTheCatalogueMatchesWhatWePublish(unittest.TestCase):
    """The README states a check count, and a stated number drifts.

    It did, immediately: stage 2 was planned as five vi.* checks, shipped as six
    after a live run split one of them, and the README went out saying
    "Twenty-five" because the arithmetic was done against the plan rather than
    against the table. Caught by running doctor, not by review.

    So the number is asserted against `CHECKS` rather than remembered. Update the
    README when this fails; do not update this test to match the README.
    """

    def test_the_readme_names_the_real_number_of_checks(self):
        words = {
            20: "Twenty", 21: "Twenty-one", 22: "Twenty-two", 23: "Twenty-three",
            24: "Twenty-four", 25: "Twenty-five", 26: "Twenty-six",
            27: "Twenty-seven", 28: "Twenty-eight", 29: "Twenty-nine", 30: "Thirty",
        }
        count = len(registry.CHECKS)
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        expected = "%s checks across both apps" % words.get(count, str(count))
        self.assertIn(
            expected, readme,
            "doctor has %d checks but the README does not say so. Expected the "
            "phrase %r." % (count, expected),
        )

    def test_every_check_id_is_unique(self):
        ids = [c.id for c in registry.CHECKS]
        self.assertEqual(len(ids), len(set(ids)), "duplicate check id")

    def test_every_dependency_names_a_check_that_exists(self):
        # runner turns a blocked dependency into UNKNOWN. A dependency on an id
        # that does not exist would block a check forever, silently.
        ids = {c.id for c in registry.CHECKS}
        for check in registry.CHECKS:
            for dep in check.depends_on:
                self.assertIn(dep, ids, "%s depends on unknown %s" % (check.id, dep))

    def test_every_check_id_falls_into_a_rendered_group(self):
        # report.py groups by id prefix; anything else lands in "Other", which is
        # a silent way for a new check to look like a mistake.
        from macrovoice.doctor.report import GROUPS

        prefixes = tuple(prefix for prefix, _ in GROUPS)
        for check in registry.CHECKS:
            self.assertTrue(
                check.id.startswith(prefixes),
                "%s has no group in report.GROUPS" % check.id,
            )


class TestVoiceInkChecks(unittest.TestCase):
    """Stage 2, the read-only VoiceInk half.

    These cover the traps that make the bridge look broken while nothing is
    actually wrong with it. Across a full day of live testing the bridge failed
    zero times and thirteen setup mistakes did, twelve of which present to the
    user as "the bridge does not work". The most common is `vi.reachable`.
    """

    def script(self, tmpdir, name="macrovoice.sh", executable=True):
        path = Path(tmpdir) / name
        path.write_text("#!/bin/zsh\n")
        os.chmod(str(path), 0o755 if executable else 0o644)
        return path

    # vi.mode -----------------------------------------------------------------

    def test_no_bridge_mode_at_all_is_a_problem(self):
        ctx = context(vi=FakeVoiceInk(modes=(paste_mode(),)))
        finding = registry._check_vi_mode(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("macrovoice.sh", finding.detail)

    def test_a_bridge_mode_is_ok_and_names_it(self):
        ctx = context(vi=FakeVoiceInk(modes=(paste_mode(), vi_mode())))
        finding = registry._check_vi_mode(ctx)
        self.assertIs(finding.outcome, Outcome.OK)
        self.assertIn("macrovoice", finding.detail)

    def test_an_unreadable_store_is_unknown_not_a_problem(self):
        # Accusing a machine we could not read is the failure mode doctor exists
        # to avoid: it reads as "the bridge is broken" when nothing is.
        ctx = context(vi=FakeVoiceInk(modes=None))
        self.assertIs(registry._check_vi_mode(ctx).outcome, Outcome.UNKNOWN)

    def test_no_adapter_at_all_is_unknown(self):
        ctx = context(vi=None)
        ctx = Context(watch_root=ctx.watch_root, mw=ctx.mw, bridge=ctx.bridge, vi=None)
        self.assertIs(registry._check_vi_mode(ctx).outcome, Outcome.UNKNOWN)

    # vi.outputmode -----------------------------------------------------------

    def test_a_bridge_mode_left_on_paste_is_a_problem(self):
        # The command is stored but VoiceInk never invokes it, so the text just
        # pastes and the user concludes the bridge does not work.
        ctx = context(vi=FakeVoiceInk(modes=(vi_mode(output_mode="paste"),)))
        finding = registry._check_vi_outputmode(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("customCommand", finding.detail)

    def test_custom_command_output_is_ok(self):
        ctx = context(vi=FakeVoiceInk(modes=(vi_mode(),)))
        self.assertIs(registry._check_vi_outputmode(ctx).outcome, Outcome.OK)

    # vi.command --------------------------------------------------------------

    def test_a_command_pointing_at_a_missing_script_is_a_problem(self):
        ctx = context(vi=FakeVoiceInk(modes=(vi_mode(command="/gone/macrovoice.sh --mode x"),)))
        finding = registry._check_vi_command(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("does not exist", finding.detail)

    def test_a_relative_command_path_is_a_problem(self):
        # VoiceInk runs the command with cwd=/, so a relative path cannot work.
        ctx = context(vi=FakeVoiceInk(modes=(vi_mode(command="macrovoice.sh --mode x"),)))
        finding = registry._check_vi_command(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("absolute", finding.detail)

    def test_a_non_executable_script_is_a_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.script(tmp, executable=False)
            ctx = context(
                vi=FakeVoiceInk(modes=(vi_mode(command="%s --mode x" % path),)),
                bridge=FakeBridge(script=path),
            )
            finding = registry._check_vi_command(ctx)
            self.assertIs(finding.outcome, Outcome.PROBLEM)
            self.assertIn("not executable", finding.detail)

    def test_a_command_pointing_at_a_different_checkout_still_passes_vi_command(self):
        """The split, found by running the thing rather than by review.

        A Mode pointing at a DIFFERENT but working copy is not a broken bridge:
        it dictates fine. Failing it here would pin exit 1 on a healthy machine
        belonging to anyone with two copies, which is the false-alarm shape this
        command has already been bitten by twice. vi.checkout says it at WARN.
        """
        with tempfile.TemporaryDirectory() as tmp:
            other = self.script(tmp, name="macrovoice.sh")
            mine = self.script(tmp, name="the-real-one.sh")
            ctx = context(
                vi=FakeVoiceInk(modes=(vi_mode(command="%s --mode x" % other),)),
                bridge=FakeBridge(script=mine),
            )
            self.assertIs(registry._check_vi_command(ctx).outcome, Outcome.OK)

    def test_a_command_pointing_at_this_checkout_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.script(tmp)
            ctx = context(
                vi=FakeVoiceInk(modes=(vi_mode(command="%s --mode x" % path),)),
                bridge=FakeBridge(script=path),
            )
            self.assertIs(registry._check_vi_command(ctx).outcome, Outcome.OK)

    def test_a_quoted_path_with_spaces_is_parsed_not_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "my folder"
            folder.mkdir()
            path = self.script(folder)
            ctx = context(
                vi=FakeVoiceInk(modes=(vi_mode(command='"%s" --mode x' % path),)),
                bridge=FakeBridge(script=path),
            )
            self.assertIs(registry._check_vi_command(ctx).outcome, Outcome.OK)

    # vi.checkout -------------------------------------------------------------

    def test_a_different_checkout_is_a_warning_naming_both_paths(self):
        """The 2026-08-09 hazard: the folder was renamed, a stale copy survived
        at the old path, VoiceInk kept running it, and every edit went to a
        checkout nothing was using. Invisible to every other check."""
        with tempfile.TemporaryDirectory() as tmp:
            other = self.script(tmp, name="macrovoice.sh")
            mine = self.script(tmp, name="the-real-one.sh")
            ctx = context(
                vi=FakeVoiceInk(modes=(vi_mode(command="%s --mode x" % other),)),
                bridge=FakeBridge(script=mine),
            )
            finding = registry._check_vi_checkout(ctx)
            self.assertIs(finding.outcome, Outcome.PROBLEM)
            self.assertIn(str(other), finding.detail)
            self.assertIn(str(mine), finding.detail)

    def test_the_same_checkout_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.script(tmp)
            ctx = context(
                vi=FakeVoiceInk(modes=(vi_mode(command="%s --mode x" % path),)),
                bridge=FakeBridge(script=path),
            )
            self.assertIs(registry._check_vi_checkout(ctx).outcome, Outcome.OK)

    def test_a_missing_script_defers_to_vi_command_rather_than_double_reporting(self):
        # vi.command already fails loudly for this. Two alarms for one cause is
        # what makes a bare machine look like a wall of unrelated breakage.
        ctx = context(vi=FakeVoiceInk(modes=(vi_mode(command="/gone/macrovoice.sh"),)))
        self.assertIs(registry._check_vi_checkout(ctx).outcome, Outcome.UNKNOWN)

    # vi.reachable ------------------------------------------------------------

    def test_a_mode_that_is_neither_default_nor_bound_is_a_problem(self):
        """Friction trap 1, the single most common false 'the bridge is broken'.

        VoiceInk resolves the Mode per dictation: a Mode-specific shortcut wins,
        otherwise the default is used. A Mode that is neither never runs, so
        every dictation goes through the normal paste Mode and the command is
        never called, which looks exactly like the bridge premise being wrong.
        """
        ctx = context(vi=FakeVoiceInk(modes=(vi_mode(),), shortcut=False))
        finding = registry._check_vi_reachable(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("never runs", finding.detail)

    def test_a_shortcut_bound_mode_is_ok(self):
        ctx = context(vi=FakeVoiceInk(modes=(vi_mode(),), shortcut=True))
        self.assertIs(registry._check_vi_reachable(ctx).outcome, Outcome.OK)

    def test_a_default_mode_is_reachable_without_a_shortcut(self):
        ctx = context(vi=FakeVoiceInk(modes=(vi_mode(is_default=True),), shortcut=False))
        self.assertIs(registry._check_vi_reachable(ctx).outcome, Outcome.OK)

    def test_an_unreadable_shortcut_is_unknown_not_a_problem(self):
        ctx = context(vi=FakeVoiceInk(modes=(vi_mode(),), shortcut=None))
        self.assertIs(registry._check_vi_reachable(ctx).outcome, Outcome.UNKNOWN)

    # vi.escapehatch ----------------------------------------------------------

    def test_the_bridge_mode_holding_default_is_a_warning(self):
        """G4, checked continuously instead of remembered once.

        If the bridge Mode is the default, every ordinary dictation depends on
        macrowhisper being alive. When it is not, nothing pastes and there is no
        way to dictate normally.
        """
        ctx = context(vi=FakeVoiceInk(
            modes=(paste_mode(is_default=False), vi_mode(is_default=True))))
        finding = registry._check_vi_escapehatch(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("macrowhisper", finding.detail)

    def test_a_non_bridge_default_is_ok(self):
        ctx = context(vi=FakeVoiceInk(modes=(paste_mode(is_default=True), vi_mode())))
        finding = registry._check_vi_escapehatch(ctx)
        self.assertIs(finding.outcome, Outcome.OK)
        self.assertIn("Dictation", finding.detail)

    def test_no_default_mode_at_all_is_a_warning(self):
        # VoiceInk then falls back to list order, which is not something a user
        # can reason about.
        ctx = context(vi=FakeVoiceInk(
            modes=(paste_mode(is_default=False), vi_mode(is_default=False))))
        self.assertIs(registry._check_vi_escapehatch(ctx).outcome, Outcome.PROBLEM)

    # unparseable commands and unreadable stores ------------------------------

    def test_an_unbalanced_quote_in_the_command_is_reported_not_raised(self):
        # shlex.split raises ValueError on an unterminated quote. A check that
        # throws is worse than one that says what is wrong.
        broken = '/repo/macrovoice.sh --mode "unterminated'
        ctx = context(vi=FakeVoiceInk(modes=(vi_mode(command=broken),)))
        finding = registry._check_vi_command(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("unparseable", finding.detail)

    def test_vi_checkout_treats_an_unparseable_command_as_unknown(self):
        broken = '/repo/macrovoice.sh --mode "unterminated'
        ctx = context(vi=FakeVoiceInk(modes=(vi_mode(command=broken),)))
        self.assertIs(registry._check_vi_checkout(ctx).outcome, Outcome.UNKNOWN)

    def test_vi_checkout_passes_through_the_no_bridge_mode_finding(self):
        ctx = context(vi=FakeVoiceInk(modes=(paste_mode(),)))
        self.assertIs(registry._check_vi_checkout(ctx).outcome, Outcome.PROBLEM)

    def test_escapehatch_with_no_adapter_is_unknown(self):
        ctx = context()
        ctx = Context(watch_root=ctx.watch_root, mw=ctx.mw, bridge=ctx.bridge, vi=None)
        self.assertIs(registry._check_vi_escapehatch(ctx).outcome, Outcome.UNKNOWN)

    def test_escapehatch_with_an_unreadable_store_is_unknown(self):
        ctx = context(vi=FakeVoiceInk(modes=None))
        self.assertIs(registry._check_vi_escapehatch(ctx).outcome, Outcome.UNKNOWN)

    def test_an_empty_quoted_first_token_is_caught_as_not_absolute(self):
        # shlex.split('"" macrovoice.sh') is ['', 'macrovoice.sh'], so argv[0]
        # can be empty even though argv never is. It must not crash.
        ctx = context(vi=FakeVoiceInk(modes=(vi_mode(command='"" macrovoice.sh'),)))
        finding = registry._check_vi_command(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("absolute", finding.detail)

    # the staleness caveat ----------------------------------------------------

    def test_every_vi_fix_hint_mentions_quitting_voiceink(self):
        """VoiceInk reads its Mode store once at launch and never re-reads, and
        cfprefsd caches on its behalf, so an export taken while it runs can be
        stale. Observed 2026-08-15. Without this line a user who has just fixed
        the Mode in the UI is told it is still broken."""
        vi = FakeVoiceInk(modes=(vi_mode(output_mode="paste", command="/gone/x.sh"),),
                          shortcut=False, running=True)
        ctx = context(vi=vi)
        for check in (registry._check_vi_mode, registry._check_vi_outputmode,
                      registry._check_vi_command, registry._check_vi_reachable):
            finding = check(ctx)
            if finding.outcome is Outcome.PROBLEM:
                self.assertIn("quit VoiceInk", finding.fix_hint or "",
                              "%s must name the staleness caveat" % check.__name__)


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
            self.assertIn(str(script.parent / registry.SAMPLE_CONFIG_NAME), finding.fix_hint)

    def test_the_sample_config_the_hint_names_actually_exists_in_the_repo(self):
        # The hint tells the user to `cp <repo>/<sample> <their config path>`,
        # and on a fresh install it is the FIRST thing doctor ever asks them to
        # do. It shipped naming macrovoice.sample.json while the repo ships
        # macrowhisper.sample.json, so following it returned "No such file or
        # directory" and the user's very first repair attempt failed.
        #
        # The test above cannot catch that class of defect: it builds its
        # expected string from the same constant the code uses, so the two agree
        # even when both are wrong. Only checking the name against the actual
        # filesystem does. Note this asserts against the REAL repo root, not a
        # temp dir, which is the whole point.
        self.assertTrue(
            (REPO_ROOT / registry.SAMPLE_CONFIG_NAME).is_file(),
            "doctor's fix hint names %r, which does not exist in the repo. "
            "Following the hint would fail. Repo root: %s"
            % (registry.SAMPLE_CONFIG_NAME, REPO_ROOT),
        )

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


class TestUncoveredCheckPaths(unittest.TestCase):
    """The UNKNOWN and PROBLEM branches the suite never reached.

    UNKNOWN is the feature's central idea: a check that cannot tell must say so
    rather than guess. Five of its paths had no test.
    """

    def test_watch_match_with_an_unreadable_config_is_unknown(self):
        ctx = context(mw=FakeMacrowhisper(config=None))
        self.assertIs(registry._check_watch_match(ctx).outcome, Outcome.UNKNOWN)

    def test_watch_match_with_no_watch_key_is_a_problem(self):
        ctx = context(mw=FakeMacrowhisper(config={"defaults": {}}))
        finding = registry._check_watch_match(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("watch", finding.detail)

    def test_armed_is_unknown_when_status_did_not_report_the_watcher(self):
        status = StatusSnapshot(running=True, recognized=True, watcher_armed=None)
        ctx = context(mw=FakeMacrowhisper(status=status))
        self.assertIs(registry._check_armed(ctx).outcome, Outcome.UNKNOWN)

    def test_moveto_is_unknown_when_status_did_not_report_it(self):
        status = StatusSnapshot(running=True, recognized=True, move_to=None)
        ctx = context(mw=FakeMacrowhisper(status=status))
        self.assertIs(registry._check_moveto(ctx).outcome, Outcome.UNKNOWN)

    def test_action_with_no_active_action_is_a_problem(self):
        status = StatusSnapshot(running=True, recognized=True, active_action="(none)")
        ctx = context(mw=FakeMacrowhisper(status=status, config={"inserts": {}}))
        finding = registry._check_action(ctx)
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertIn("--action", finding.fix_hint)

    def test_action_is_unknown_when_the_config_cannot_be_read(self):
        status = StatusSnapshot(running=True, recognized=True, active_action="autoPaste")
        ctx = context(mw=FakeMacrowhisper(status=status, config=None))
        self.assertIs(registry._check_action(ctx).outcome, Outcome.UNKNOWN)

    def test_clipboard_buffer_is_unknown_when_the_config_cannot_be_read(self):
        ctx = context(mw=FakeMacrowhisper(config=None))
        self.assertIs(registry._check_clipboard_buffer(ctx).outcome, Outcome.UNKNOWN)

    def test_clipboard_buffer_unset_is_a_problem(self):
        ctx = context(mw=FakeMacrowhisper(config={"defaults": {}}))
        self.assertIs(registry._check_clipboard_buffer(ctx).outcome, Outcome.PROBLEM)


class TestVersionTuple(unittest.TestCase):
    """The comparison that decides whether 3.10 is newer than 3.9."""

    def test_ordinary_versions(self):
        self.assertEqual(registry._version_tuple("3.9.6"), (3, 9, 6))
        self.assertEqual(registry._version_tuple("3.14.6"), (3, 14, 6))

    def test_ten_is_newer_than_nine(self):
        self.assertGreater(registry._version_tuple("3.10.0"), registry._version_tuple("3.9.6"))

    def test_a_prerelease_suffix_stops_at_the_digits(self):
        self.assertEqual(registry._version_tuple("3.13.0rc1"), (3, 13, 0))

    def test_a_short_version(self):
        self.assertEqual(registry._version_tuple("3"), (3,))

    def test_an_empty_string_is_empty(self):
        self.assertEqual(registry._version_tuple(""), ())


if __name__ == "__main__":
    unittest.main()
