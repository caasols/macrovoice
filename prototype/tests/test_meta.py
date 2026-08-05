"""meta.json construction: schema correctness and escaping fidelity.

Every generated document is asserted against the ported macrowhisper gate
(test_harness.is_valid_recording_meta_json), and every transcript is asserted to
survive a full serialize -> json.loads round trip byte for byte.

Escaping is where a shell implementation of this adapter would quietly corrupt
user data, which is the reason the design chose Python. These tests are the proof.
"""

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_harness import is_valid_recording_meta_json  # noqa: E402
from macrovoice.meta import build_meta, serialize_meta  # noqa: E402

# The content matrix from the design doc, section 7. Each entry is a transcript that
# has historically broken naive JSON emitters.
TRICKY_TRANSCRIPTS = {
    "plain_ascii": "hello world",
    "double_quotes": 'she said "hello" to me',
    "single_quotes": "it's a test, y'all",
    "backslash": "path is C:\\Users\\test",
    "literal_escaped_quote": 'a backslash-quote sequence: \\" here',
    "newline": "line one\nline two",
    "crlf": "line one\r\nline two",
    "tab": "column one\tcolumn two",
    "vertical_tab_and_formfeed": "a\x0bb\x0cc",
    "nul_byte": "before\x00after",
    "control_chars": "bell\x07 backspace\x08 escape\x1b",
    "emoji": "great idea 🧠💡",
    "astral_plane": "\U0001f9e0\U0001f4a1\U0001f680",
    "combining_accents": "e\u0301le\u0300ve",  # e + combining acute, e + combining grave
    "precomposed_accents": "élève",
    "rtl": "مرحبا بالعالم",
    "cjk": "日本語のテスト",
    "mixed_scripts": "héllo wörld 🧠 مرحبا 日本語",
    "json_lookalike": '{"result": "not really json"}',
    "json_array_lookalike": "[1, 2, 3]",
    "trigger_word_prefix": "google best pizza in Milan",
    "leading_trailing_space": "   padded   ",
    "only_punctuation": "!?.,;:",
    "single_char": "x",
    "single_space": " ",
    "long_5k": "word " * 1000,
    "long_100k": "x" * 100_000,
    "html_like": "<script>alert('xss')</script>",
    "template_placeholder_lookalike": "{{swResult}} and {{selectedText}}",
    "shell_metachars": "rm -rf / ; echo $(whoami) `id` && echo done",
    "percent_and_ampersand": "100% & more",
}


class TestSchemaShape(unittest.TestCase):
    def test_minimal_document_has_result(self):
        meta = build_meta("hello world")
        self.assertEqual(meta["result"], "hello world")

    def test_passes_the_macrowhisper_gate(self):
        self.assertTrue(is_valid_recording_meta_json(build_meta("hello world")))

    def test_language_model_name_is_absent(self):
        # Setting it flips macrowhisper's gate to require a non-empty llmResult,
        # which the bridge can never honestly supply. See design section 5.
        meta = build_meta("hello world")
        self.assertNotIn("languageModelName", meta)
        self.assertNotIn("llmResult", meta)

    def test_mode_name_included_when_given(self):
        self.assertEqual(build_meta("hi", mode_name="email")["modeName"], "email")

    def test_mode_name_omitted_entirely_when_none(self):
        # Omitted, not null. TriggerEvaluator.swift:22 does `as? String`, and a null
        # would fail that cast anyway, so a null key is pure noise.
        self.assertNotIn("modeName", build_meta("hi"))

    def test_mode_name_omitted_when_empty_string(self):
        self.assertNotIn("modeName", build_meta("hi", mode_name=""))

    def test_datetime_is_iso8601_utc_with_z(self):
        when = datetime(2026, 8, 5, 2, 14, 33, tzinfo=timezone.utc)
        self.assertEqual(build_meta("hi", when=when)["datetime"], "2026-08-05T02:14:33Z")

    def test_datetime_defaults_to_now(self):
        meta = build_meta("hi")
        self.assertRegex(meta["datetime"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_naive_datetime_is_treated_as_utc(self):
        when = datetime(2026, 8, 5, 2, 14, 33)
        self.assertEqual(build_meta("hi", when=when)["datetime"], "2026-08-05T02:14:33Z")

    def test_duration_defaults_to_zero(self):
        self.assertEqual(build_meta("hi")["duration"], 0.0)

    def test_duration_passthrough(self):
        self.assertEqual(build_meta("hi", duration=12.5)["duration"], 12.5)

    def test_no_group_b_fields_written(self):
        """macrowhisper injects these itself at runtime; the bridge must not write them.

        Enumerated by grepping every metaJson[...] read in the macrowhisper source.
        """
        group_b = {
            "selectedText",
            "clipboardContext",
            "clipboardStacking",
            "frontApp",
            "frontAppName",
            "frontAppBundleId",
            "frontAppPid",
            "frontAppUrl",
            "appContext",
            "appVocabulary",
            "actionResult",
            "actionResults",
        }
        meta = build_meta("hello", mode_name="bridge")
        self.assertEqual(group_b & set(meta.keys()), set())

    def test_exact_key_set(self):
        self.assertEqual(
            set(build_meta("hi", mode_name="bridge").keys()),
            {"result", "modeName", "datetime", "duration"},
        )


class TestSerializationRoundTrip(unittest.TestCase):
    """The core property: what goes in comes out, exactly, for every tricky input."""

    def test_all_tricky_transcripts_round_trip(self):
        for name, transcript in TRICKY_TRANSCRIPTS.items():
            with self.subTest(transcript=name):
                payload = serialize_meta(build_meta(transcript, mode_name="bridge"))
                reparsed = json.loads(payload)
                self.assertEqual(
                    reparsed["result"],
                    transcript,
                    f"transcript {name!r} did not survive the round trip",
                )

    def test_all_tricky_transcripts_pass_the_gate(self):
        for name, transcript in TRICKY_TRANSCRIPTS.items():
            with self.subTest(transcript=name):
                reparsed = json.loads(serialize_meta(build_meta(transcript)))
                self.assertTrue(
                    is_valid_recording_meta_json(reparsed),
                    f"transcript {name!r} produced a document macrowhisper would reject",
                )

    def test_output_is_valid_utf8_encodable(self):
        for name, transcript in TRICKY_TRANSCRIPTS.items():
            with self.subTest(transcript=name):
                serialize_meta(build_meta(transcript)).encode("utf-8")

    def test_output_is_a_single_line(self):
        # Not required by macrowhisper, but it keeps the file trivially greppable
        # and makes partial-write detection obvious by eye during debugging.
        payload = serialize_meta(build_meta("line one\nline two"))
        self.assertNotIn("\n", payload)

    def test_non_ascii_is_not_escaped_to_surrogates(self):
        # ensure_ascii=False keeps the file human-readable. JSONSerialization in
        # Swift handles raw UTF-8 fine.
        payload = serialize_meta(build_meta("日本語"))
        self.assertIn("日本語", payload)

    def test_nul_byte_is_preserved_not_stripped(self):
        # json.dumps escapes NUL as \u0000, which Swift's JSONSerialization accepts.
        # Preserving is the honest choice: we do not silently alter user content.
        payload = serialize_meta(build_meta("before\x00after"))
        self.assertIn("\\u0000", payload)
        self.assertEqual(json.loads(payload)["result"], "before\x00after")

    def test_very_long_transcript_round_trips(self):
        transcript = "x" * 100_000
        reparsed = json.loads(serialize_meta(build_meta(transcript)))
        self.assertEqual(len(reparsed["result"]), 100_000)


class TestPurity(unittest.TestCase):
    def test_build_meta_does_not_mutate_across_calls(self):
        first = build_meta("one", mode_name="a")
        second = build_meta("two", mode_name="b")
        self.assertEqual(first["result"], "one")
        self.assertEqual(first["modeName"], "a")
        self.assertEqual(second["result"], "two")

    def test_returned_dict_is_independent(self):
        meta = build_meta("hello")
        meta["result"] = "mutated"
        self.assertEqual(build_meta("hello")["result"], "hello")


if __name__ == "__main__":
    unittest.main()
