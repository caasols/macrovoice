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


if __name__ == "__main__":
    unittest.main()
