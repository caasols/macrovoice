"""The published macrowhisper.sample.json is a shipped artifact. Treat it as one.

It had no tests at all until 2026-08-15, despite being the file the README tells
every new user to copy and the file doctor's repair hint points at. A mistake in
it does not fail loudly at our end: macrowhisper simply fails to decode the
config, and the user sees the bridge "not working".

Nothing here needs macrowhisper installed. The invariants are read out of
AppConfiguration.swift and asserted against the JSON, which is what makes them
cheap enough to run on every commit.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from macrovoice.doctor.registry import (  # noqa: E402
    ACTION_CATEGORIES,
    DEFAULT_CLIPBOARD_BUFFER_S,
    SAMPLE_CONFIG_NAME,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_PATH = REPO_ROOT / SAMPLE_CONFIG_NAME


class SampleConfigTest(unittest.TestCase):
    """Structure and the settings that cause harm when wrong."""

    @classmethod
    def setUpClass(cls):
        cls.raw = SAMPLE_PATH.read_text(encoding="utf-8")
        cls.config = json.loads(cls.raw)

    def test_the_sample_is_the_file_doctor_tells_users_to_copy(self):
        # Binds this suite to doctor's fix hint, so the two can never name
        # different files again. They did: the hint said macrovoice.sample.json
        # while the repo shipped macrowhisper.sample.json.
        self.assertTrue(SAMPLE_PATH.is_file(), "%s is missing" % SAMPLE_PATH)

    def test_it_is_valid_json(self):
        json.loads(self.raw)  # setUpClass already proved it; this names the failure

    def test_every_action_category_maps_names_to_OBJECTS(self):
        """The invariant that a stray comment key breaks.

        AppConfiguration.swift:980 decodes each category as [String: Action]:

            urls = try container.decodeIfPresent([String: Url].self, forKey: .urls) ?? [:]

        Every VALUE must therefore decode as an action object. A "_comment" key
        holding an array of strings does not, and because the decode is not
        optional per-entry, it fails the WHOLE AppConfiguration decode and the
        config stops loading entirely. Unknown keys INSIDE an action object are
        ignored by Swift's Codable, which is why markerLog's own _comment is
        fine and a category-level one is not.

        Caught for real on 2026-08-15, while adding the preset searches: a
        _comment was nearly shipped as a key of "urls".
        """
        for category in ACTION_CATEGORIES:
            bucket = self.config.get(category)
            if bucket is None:
                continue
            self.assertIsInstance(bucket, dict, "%s must be an object" % category)
            for name, action in bucket.items():
                self.assertIsInstance(
                    action, dict,
                    "%s.%s must be an object, got %s. A non-object here (a "
                    "'_comment' array is the easy mistake) fails macrowhisper's "
                    "[String: Action] decode and takes the whole config down. "
                    "Put comments at the top level or inside an action instead."
                    % (category, name, type(action).__name__),
                )

    def test_every_action_defines_an_action_string(self):
        for category in ACTION_CATEGORIES:
            for name, action in (self.config.get(category) or {}).items():
                self.assertIn("action", action, "%s.%s has no 'action'" % (category, name))
                self.assertIsInstance(action["action"], str)
                self.assertTrue(action["action"].strip(), "%s.%s is empty" % (category, name))

    def test_the_active_action_actually_exists(self):
        """doctor's mw.action check exists because macrowhisper accepts
        dictations and does nothing when activeAction names something undefined.
        The file we hand people must not ship in that state."""
        active = self.config.get("defaults", {}).get("activeAction")
        self.assertTrue(active, "defaults.activeAction is not set")
        defined = {
            name
            for category in ACTION_CATEGORIES
            for name in (self.config.get(category) or {})
        }
        self.assertIn(
            active, defined,
            "defaults.activeAction is %r, which is not defined in any of %s"
            % (active, ", ".join(ACTION_CATEGORIES)),
        )

    def test_simesc_is_off(self):
        """The one setting that destroys user work. macrowhisper defaults it to
        TRUE and posts a literal Escape into the focused app before pasting
        (Accessibility.swift:477-494). Measured 2026-08-08: it closed a
        ProtonMail draft. If this ever flips in the sample we ship, we are
        handing people the data-loss default."""
        self.assertIs(self.config.get("defaults", {}).get("simEsc"), False)

    def test_clipboard_buffer_is_raised_above_the_default(self):
        """Under the bridge the recording folder appears when dictation ENDS,
        not when it starts, so the stock 5s buffer loses the pre-recording
        clipboard for any dictation longer than five seconds."""
        value = self.config.get("defaults", {}).get("clipboardBuffer")
        self.assertIsInstance(value, (int, float))
        self.assertGreater(float(value), DEFAULT_CLIPBOARD_BUFFER_S)

    def test_the_watch_dir_is_not_a_real_superwhisper_folder(self):
        """Pointing the bridge at ~/superwhisper would interleave synthetic
        recordings with genuine ones if Superwhisper is ever installed."""
        watch = self.config.get("defaults", {}).get("watch", "")
        self.assertNotIn("superwhisper", watch.lower())

    def test_voice_triggers_list_the_ask_form_as_well_as_the_bare_word(self):
        """Triggers are prefix-anchored (TriggerEvaluator.swift:205) and each
        '|' alternative is anchored independently, so a bare 'youtube' never
        matches the natural 'ask youtube best pizza'. Every triggered url action
        we ship must carry both forms, or it silently does nothing for the
        phrasing people actually use.

        Measured 2026-08-15, which turned this from prudence into a requirement.
        The two forms rescue DIFFERENT real cases:

          plain dictation    -> 'Google, what is the best pizza place in Madrid?'
                                bare 'google' matches; 'ask google' does NOT
          AI enhancement on  -> 'Ask Google: "Best pizza place in Madrid."'
                                'ask google' matches; bare 'google' does NOT

        (gpt-5.5, VoiceInk's Default prompt.) Drop either alternative and one of
        those two everyday cases falls through to the default action silently.
        """
        for name, action in (self.config.get("urls") or {}).items():
            trigger = action.get("triggerVoice")
            if not trigger:
                continue
            alternatives = trigger.split("|")
            bare = [a for a in alternatives if " " not in a]
            asked = [a for a in alternatives if a.startswith("ask ")]
            self.assertTrue(bare, "urls.%s has no bare-word trigger: %r" % (name, trigger))
            self.assertTrue(
                asked,
                "urls.%s has no 'ask ...' trigger: %r. Prefix anchoring means the "
                "bare word alone never matches 'ask %s something'."
                % (name, trigger, name),
            )


def placeholder_quoting(command):
    """Every ``{{placeholder}}`` in a command string, with the quote enclosing it.

    Returns a list of ``(placeholder, quote)`` where quote is ``'"'``, ``"'"`` or
    ``None`` for unquoted. A tiny scanner rather than a regex, because the answer
    depends on the quote state accumulated to the left of the placeholder, which
    is exactly what a regex cannot carry.
    """
    found = []
    state = None
    i = 0
    while i < len(command):
        char = command[i]
        if command.startswith("{{", i):
            end = command.find("}}", i)
            found.append((command[i : end + 2] if end != -1 else command[i:], state))
            i = len(command) if end == -1 else end + 2
            continue
        if state != "'" and char == "\\":  # escapes, except inside single quotes
            i += 2
            continue
        if state is None and char in ("'", '"'):
            state = char
        elif state == char:
            state = None
        i += 1
    return found


class ShellQuotingTest(unittest.TestCase):
    """Placeholders in shell and AppleScript actions MUST sit in double quotes.

    macrowhisper escapes a placeholder for the action type it is in, and for
    shell that is escapeShellCharacters (ShellUtils.swift:4-11), which backslashes
    exactly four characters: backslash, double quote, backtick and dollar. It does
    NOT escape the single quote, because the escaping is written for a
    DOUBLE-quoted context. Upstream's own samples all use double quotes.

    Put the placeholder in single quotes instead and a dictation containing an
    apostrophe, which is most English dictation, ends the quoted string early.
    Measured 2026-08-17 by replaying that escaping against /bin/sh:

        don't forget                       -> rc=2, action never runs, dictation lost
        don't stop; touch X; it's fine     -> the touch EXECUTED
        don't stop, it's fine              -> logged as 3 lines, apostrophes gone

    and every escaped character leaks a literal backslash into the output, which
    is why quotes used to look backslashed in fired.log. In double quotes all of
    those pass through byte-faithfully and nothing executes.

    Unquoted is not acceptable either: the shell would word-split and glob it.

    AppleScript is the same shape one step removed (escapeAppleScriptString
    escapes backslash and double quote only), so it carries the same requirement.
    """

    QUOTED_CATEGORIES = ("scriptsShell", "scriptsAS")

    @classmethod
    def setUpClass(cls):
        cls.config = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))

    def test_every_placeholder_in_a_script_action_is_double_quoted(self):
        checked = 0
        for category in self.QUOTED_CATEGORIES:
            for name, action in (self.config.get(category) or {}).items():
                command = action.get("action") or ""
                for placeholder, quote in placeholder_quoting(command):
                    checked += 1
                    self.assertEqual(
                        quote,
                        '"',
                        "%s.%s puts %s in %s. Placeholders in script actions must be "
                        "double-quoted: macrowhisper's escaping does not escape the "
                        "single quote, so an apostrophe in a dictation breaks out of "
                        "the string and can execute the rest of it.\n  action: %s"
                        % (
                            category,
                            name,
                            placeholder,
                            "single quotes" if quote == "'" else "no quotes",
                            command,
                        ),
                    )
        self.assertGreater(
            checked, 0, "no script placeholders found; this test is asserting nothing"
        )

    def test_the_scanner_reads_quote_state_the_way_a_shell_does(self):
        """The assertion above is only as good as the scanner under it."""
        cases = [
            ("""printf '%s' '{{a}}'""", "'"),
            ('''printf "%s" "{{a}}"''', '"'),
            ("""echo {{a}}""", None),
            # A double quote inside single quotes does not open anything.
            ("""echo '"' '{{a}}'""", "'"),
            # ...and a single quote inside double quotes does not either.
            ('''echo "it's" "{{a}}"''', '"'),
            # An escaped double quote outside quotes must not open a string.
            ('''echo \\" "{{a}}"''', '"'),
        ]
        for command, expected in cases:
            found = placeholder_quoting(command)
            self.assertEqual(len(found), 1, command)
            self.assertEqual(found[0][1], expected, command)


if __name__ == "__main__":
    unittest.main()
