"""The VoiceInk adapter, against fixtures. No VoiceInk, no `defaults`, no Mac state.

Every test drives a fake runner, so the whole read path is exercised without the
app being installed, let alone running.

The shape being pinned is not obvious and is the thing most likely to break under
a VoiceInk update, so it is asserted directly rather than assumed:

  * `modeConfigurationsV2` is a **bytes** value holding UTF-8 JSON. It is NOT a
    plist array. A reader that treats it as one gets a TypeError, not a wrong
    answer, which is why `test_the_modes_key_is_json_inside_bytes` exists.
  * the keyboard shortcut is a **separate top-level key**, `Shortcut_mode_<UUID>`,
    not a field of the Mode (design doc 4.3). This is what makes a saved Mode
    inert, friction trap 1, and it cannot be seen by reading the Mode alone.

Confirmed against a live VoiceInk 2.1 install on 2026-08-15.
"""

import json
import plistlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from macrovoice.doctor.adapters.process import CommandResult  # noqa: E402
from macrovoice.doctor.adapters.voiceink import (  # noqa: E402
    BUNDLE_ID,
    Mode,
    VoiceInk,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "doctor" / "voiceink"
BRIDGE_ID = "B0000000-0000-0000-0000-0000000000B1"


def runner_for(fixture=None, *, stdout=None, returncode=0, timed_out=False):
    """A fake run_command that replays a fixture instead of shelling out."""
    if stdout is None:
        stdout = (FIXTURES / fixture).read_text(encoding="utf-8") if fixture else ""

    def run(args, timeout):
        return CommandResult(
            None if timed_out else returncode, stdout, "", timed_out
        )

    return run


def adapter(fixture="healthy.plist", **kw):
    return VoiceInk(runner=runner_for(fixture, **kw))


class TestTheStoredShape(unittest.TestCase):
    def test_the_modes_key_is_json_inside_bytes(self):
        """Pins the trap. If VoiceInk ever stores a real plist array here, this
        fails loudly instead of the adapter silently returning nothing."""
        raw = plistlib.loads((FIXTURES / "healthy.plist").read_bytes())
        self.assertIsInstance(raw["modeConfigurationsV2"], bytes)
        self.assertIsInstance(json.loads(raw["modeConfigurationsV2"]), list)

    def test_the_shortcut_is_a_separate_top_level_key(self):
        raw = plistlib.loads((FIXTURES / "healthy.plist").read_bytes())
        self.assertIn("Shortcut_mode_" + BRIDGE_ID, raw)
        modes = json.loads(raw["modeConfigurationsV2"])
        bridge = [m for m in modes if m["id"] == BRIDGE_ID][0]
        self.assertNotIn("shortcut", bridge, "the Mode itself must not carry it")

    def test_the_bundle_id_lives_in_one_constant(self):
        self.assertEqual(BUNDLE_ID, "com.prakashjoshipax.VoiceInk")


class TestModes(unittest.TestCase):
    def test_it_reads_every_mode(self):
        modes = adapter().modes()
        self.assertEqual([m.name for m in modes], ["Dictation", "macrovoice"])

    def test_it_reads_the_fields_the_checks_need(self):
        bridge = [m for m in adapter().modes() if m.name == "macrovoice"][0]
        self.assertEqual(bridge.id, BRIDGE_ID)
        self.assertEqual(bridge.output_mode, "customCommand")
        self.assertIs(bridge.is_default, False)
        self.assertIs(bridge.is_enabled, True)
        self.assertEqual(
            bridge.command,
            "/Users/example/macrovoice/macrovoice.sh --mode macrovoice",
        )

    def test_a_mode_with_no_custom_command_has_no_command(self):
        dictation = [m for m in adapter().modes() if m.name == "Dictation"][0]
        self.assertIsNone(dictation.command)

    def test_modes_are_returned_as_an_immutable_record(self):
        # Checks receive these; nothing downstream should be able to mutate the
        # snapshot it is reasoning about.
        bridge = adapter().modes()[1]
        with self.assertRaises(Exception):
            bridge.name = "changed"

    def test_the_result_is_cached_so_five_checks_shell_out_once(self):
        calls = []

        def counting(args, timeout):
            calls.append(args)
            return CommandResult(0, (FIXTURES / "healthy.plist").read_text(), "", False)

        vi = VoiceInk(runner=counting)
        vi.modes()
        vi.modes()
        vi.has_shortcut(BRIDGE_ID)
        self.assertEqual(len(calls), 1, "defaults export should run once, not per check")


class TestModesFailsSafe(unittest.TestCase):
    """Every failure is None, never an exception and never a guess. A check that
    cannot read VoiceInk must report UNKNOWN, and UNKNOWN is what None becomes."""

    def test_a_non_zero_exit_is_none(self):
        self.assertIsNone(adapter(returncode=1).modes())

    def test_a_timeout_is_none(self):
        self.assertIsNone(adapter(timed_out=True).modes())

    def test_empty_output_is_none(self):
        self.assertIsNone(adapter(stdout="").modes())

    def test_a_malformed_plist_is_none(self):
        self.assertIsNone(adapter(stdout="this is not a plist").modes())

    def test_a_plist_without_the_modes_key_is_none(self):
        blob = plistlib.dumps({"unrelated": "value"}, fmt=plistlib.FMT_XML).decode()
        self.assertIsNone(adapter(stdout=blob).modes())

    def test_malformed_json_inside_the_blob_is_none(self):
        blob = plistlib.dumps(
            {"modeConfigurationsV2": b"{not json"}, fmt=plistlib.FMT_XML
        ).decode()
        self.assertIsNone(adapter(stdout=blob).modes())

    def test_json_that_is_not_a_list_is_none(self):
        blob = plistlib.dumps(
            {"modeConfigurationsV2": b'{"not": "a list"}'}, fmt=plistlib.FMT_XML
        ).decode()
        self.assertIsNone(adapter(stdout=blob).modes())

    def test_a_plist_that_is_not_a_dictionary_is_none(self):
        # `defaults export` always yields a dict, but a truncated or substituted
        # payload need not, and indexing a list by string key would raise.
        blob = plistlib.dumps([1, 2, 3], fmt=plistlib.FMT_XML).decode()
        self.assertIsNone(adapter(stdout=blob).modes())

    def test_a_non_dict_entry_is_skipped_not_fatal(self):
        payload = json.dumps([{"id": "a", "name": "ok"}, "garbage"]).encode()
        blob = plistlib.dumps(
            {"modeConfigurationsV2": payload}, fmt=plistlib.FMT_XML
        ).decode()
        modes = adapter(stdout=blob).modes()
        self.assertEqual([m.name for m in modes], ["ok"])


class TestShortcut(unittest.TestCase):
    def test_a_bound_mode_reports_true(self):
        self.assertIs(adapter("healthy.plist").has_shortcut(BRIDGE_ID), True)

    def test_an_unbound_mode_reports_false(self):
        self.assertIs(adapter("inert-mode.plist").has_shortcut(BRIDGE_ID), False)

    def test_an_unknown_mode_id_reports_false(self):
        self.assertIs(adapter("healthy.plist").has_shortcut("no-such-id"), False)

    def test_an_unreadable_store_is_unknown_not_false(self):
        # False would mean "definitely not bound", which would make doctor report
        # the inert-Mode trap on a machine it simply could not read.
        self.assertIsNone(adapter(returncode=1).has_shortcut(BRIDGE_ID))


class TestIsRunning(unittest.TestCase):
    def test_a_matching_process_is_running(self):
        self.assertIs(VoiceInk(runner=runner_for(stdout="92712\n")).is_running(), True)

    def test_no_match_is_not_running(self):
        # pgrep exits 1 with no output when nothing matches.
        self.assertIs(
            VoiceInk(runner=runner_for(stdout="", returncode=1)).is_running(), False
        )

    def test_pgrep_failing_outright_is_unknown(self):
        self.assertIsNone(VoiceInk(runner=runner_for(timed_out=True)).is_running())


if __name__ == "__main__":
    unittest.main()
