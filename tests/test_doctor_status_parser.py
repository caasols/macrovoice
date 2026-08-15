"""The macrowhisper --status parser.

Liveness comes from the OUTPUT, never the exit code (main.swift:1115-1122):
--status exits 0 whether or not anything is running, and prints the literal
line "macrowhisper is not running." when it is not. The project's older note
that --status "cannot be used as a liveness check" is true of the exit code
and false of the output.

The parser must be TOLERANT. The failure we must never have is doctor reporting
simEsc as fine because macrowhisper reworded a line, so an unrecognised field
stays None and surfaces as UNKNOWN.

The fixtures are real captured `--status` output with one edit: the home
directory's username is replaced with `exampleuser`, because this repository is
public and those were the only tracked files carrying the machine's username.
Everything else is byte-identical to what macrowhisper printed.
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
        self.assertEqual(self.snap.watch_folder, "/Users/exampleuser/macrovoice")
        self.assertEqual(
            self.snap.recordings_folder, "/Users/exampleuser/macrovoice/recordings"
        )
        self.assertTrue(self.snap.recordings_folder_exists)

    def test_settings_that_matter(self):
        self.assertEqual(self.snap.active_action, "autoPaste")
        self.assertEqual(self.snap.move_to, ".delete")
        self.assertIs(self.snap.sim_esc, False)

    def test_config_path(self):
        self.assertEqual(
            self.snap.config_path, "/Users/exampleuser/.config/macrowhisper/macrowhisper.json"
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


class TestWatcherArmedParsing(unittest.TestCase):
    """Test watcher_armed parsing from RecordingsFolderWatcher.swift:206-214.

    These tests verify that watcher_armed is parsed from the exact output
    strings, not by substring containment. The regression test covers the bug
    where "armed" in "no (not armed)" was True.
    """

    def test_armed_from_yes_string(self):
        """yes (armed, ...) from RecordingsFolderWatcher.swift:214"""
        snap = parse_status(
            "Macrowhisper version: 2.1.1\n"
            "Recordings watcher: yes (armed, started 6h ago, last event 6h ago, pending 0)\n"
        )
        self.assertIs(snap.watcher_armed, True)

    def test_not_armed_from_no_not_armed_string_regression(self):
        """no (not armed) from RecordingsFolderWatcher.swift:211 - regression for substring containment bug.

        This is the critical case: "armed" appears in the string as "not armed",
        so substring containment would incorrectly return True. We must parse
        the exact strings from the source.
        """
        snap = parse_status(
            "Macrowhisper version: 2.1.1\n"
            "Recordings watcher: no (not armed)\n"
        )
        self.assertIs(snap.watcher_armed, False)

    def test_not_armed_from_folder_missing_string(self):
        """no (folder missing) from RecordingsFolderWatcher.swift:207"""
        snap = parse_status(
            "Macrowhisper version: 2.1.1\n"
            "Recordings watcher: no (folder missing)\n"
        )
        self.assertIs(snap.watcher_armed, False)

    def test_unrecognized_watcher_string_becomes_none(self):
        """Unrecognized watcher string stays None, never guesses."""
        snap = parse_status(
            "Macrowhisper version: 2.1.1\n"
            "Recordings watcher: maybe (some future state)\n"
        )
        self.assertIsNone(snap.watcher_armed)


class TestJustNowAge(unittest.TestCase):
    """describeStatusAge (RecordingsFolderWatcher.swift:226-243) reports ages
    under 5 seconds as "just now", with no digits and no "ago" suffix. The age
    regex alone does not match that, so it needs its own handling."""

    def test_started_just_now_is_zero_seconds_not_none(self):
        snap = parse_status(
            "Macrowhisper version: 2.1.1\n"
            "Recordings watcher: yes (armed, started just now, last event just now, pending 0)\n"
        )
        self.assertEqual(snap.watcher_started_ago_s, 0)

    def test_never_stays_unparsed(self):
        snap = parse_status(
            "Macrowhisper version: 2.1.1\n"
            "Recordings watcher: yes (armed, started never, last event never, pending 0)\n"
        )
        self.assertIsNone(snap.watcher_started_ago_s)

    def test_hour_unit_age_is_parsed_into_seconds(self):
        snap = parse_status(
            "Macrowhisper version: 2.1.1\n"
            "Recordings watcher: yes (armed, started 6h ago, last event 6h ago, pending 0)\n"
        )
        self.assertEqual(snap.watcher_started_ago_s, 6 * 3600)


if __name__ == "__main__":
    unittest.main()
