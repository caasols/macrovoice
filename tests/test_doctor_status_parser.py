"""The macrowhisper --status parser.

Liveness comes from the OUTPUT, never the exit code (main.swift:1115-1122):
--status exits 0 whether or not anything is running, and prints the literal
line "macrowhisper is not running." when it is not. The project's older note
that --status "cannot be used as a liveness check" is true of the exit code
and false of the output.

The parser must be TOLERANT. The failure we must never have is doctor reporting
simEsc as fine because macrowhisper reworded a line, so an unrecognised field
stays None and surfaces as UNKNOWN.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from macrovoice.doctor.adapters.macrowhisper import parse_status  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "doctor" / "status"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestRunningDaemon(unittest.TestCase):
    def setUp(self):
        self.snap = parse_status(fixture("running-2.1.1.txt"))

    def test_running_and_recognized(self):
        self.assertTrue(self.snap.running)
        self.assertTrue(self.snap.recognized)

    def test_version(self):
        self.assertEqual(self.snap.version, "2.1.1")

    def test_watcher_is_armed_with_pending_count(self):
        self.assertTrue(self.snap.watcher_present)
        self.assertTrue(self.snap.watcher_armed)
        self.assertEqual(self.snap.watcher_pending, 0)

    def test_watcher_age_is_parsed_into_seconds(self):
        self.assertEqual(self.snap.watcher_started_ago_s, 6 * 3600)

    def test_folders(self):
        self.assertEqual(self.snap.watch_folder, "/Users/caraujo/mw-bridge")
        self.assertEqual(
            self.snap.recordings_folder, "/Users/caraujo/mw-bridge/recordings"
        )
        self.assertTrue(self.snap.recordings_folder_exists)

    def test_settings_that_matter(self):
        self.assertEqual(self.snap.active_action, "autoPaste")
        self.assertEqual(self.snap.move_to, ".delete")
        self.assertIs(self.snap.sim_esc, False)

    def test_config_path(self):
        self.assertEqual(
            self.snap.config_path, "/Users/caraujo/.config/macrowhisper/macrowhisper.json"
        )


class TestNotRunning(unittest.TestCase):
    def test_sentinel_is_recognised_as_not_running(self):
        snap = parse_status(fixture("not-running.txt"))
        self.assertFalse(snap.running)
        self.assertTrue(snap.recognized)
        self.assertIsNone(snap.sim_esc)


class TestTolerance(unittest.TestCase):
    def test_a_reworded_field_becomes_none_not_a_wrong_answer(self):
        snap = parse_status(fixture("reworded.txt"))
        self.assertTrue(snap.running)
        self.assertTrue(snap.recognized)
        self.assertEqual(snap.version, "2.9.0")
        # The whole point: simEsc was renamed, so we do NOT know its value.
        # Reporting False here would tell the user their work is safe when it
        # may not be.
        self.assertIsNone(snap.sim_esc)

    def test_unknown_lines_are_ignored(self):
        snap = parse_status(
            "Macrowhisper version: 2.1.1\nSome future line: whatever\nsimEsc: no\n"
        )
        self.assertIs(snap.sim_esc, False)

    def test_output_that_is_not_macrowhisper_is_not_recognized(self):
        snap = parse_status("zsh: command not found: macrowhisper\n")
        self.assertFalse(snap.recognized)
        self.assertFalse(snap.running)

    def test_empty_output_is_not_recognized(self):
        snap = parse_status("")
        self.assertFalse(snap.recognized)


if __name__ == "__main__":
    unittest.main()
