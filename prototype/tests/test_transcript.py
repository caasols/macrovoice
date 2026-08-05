"""Transcript resolution: VOICEINK_TRANSCRIPT env, with stdin fallback.

Contract comes from VoiceInk's CustomCommandDeliveryContext
(upstream/voiceink/VoiceInk/Transcription/Engine/CustomCommandDeliveryRunner.swift:5-16):

    var standardInput: String { transcript }
    var environment: [String: String] { ["VOICEINK_TRANSCRIPT": transcript] }

So in practice VoiceInk sends the SAME text both ways. We prefer the env var and
fall back to stdin, so the adapter still works if either channel is unavailable
(e.g. a user testing by hand, or a future VoiceInk change).
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vi2meta.transcript import resolve_transcript  # noqa: E402


class TestEnvPreferred(unittest.TestCase):
    def test_env_used_when_present(self):
        self.assertEqual(resolve_transcript({"VOICEINK_TRANSCRIPT": "hello"}, ""), "hello")

    def test_env_wins_over_stdin(self):
        self.assertEqual(
            resolve_transcript({"VOICEINK_TRANSCRIPT": "from env"}, "from stdin"), "from env"
        )

    def test_other_env_vars_ignored(self):
        self.assertEqual(
            resolve_transcript({"VOICEINK_FULL_PROMPT": "wrong hook", "PATH": "/bin"}, "stdin"),
            "stdin",
        )


class TestStdinFallback(unittest.TestCase):
    def test_stdin_used_when_env_absent(self):
        self.assertEqual(resolve_transcript({}, "hello from stdin"), "hello from stdin")

    def test_stdin_used_when_env_empty_string(self):
        # An empty env var is indistinguishable from "not set" for our purposes,
        # so fall through rather than publishing nothing.
        self.assertEqual(resolve_transcript({"VOICEINK_TRANSCRIPT": ""}, "stdin text"), "stdin text")


class TestNothingPublishable(unittest.TestCase):
    def test_both_empty_returns_none(self):
        self.assertIsNone(resolve_transcript({}, ""))

    def test_both_absent_returns_none(self):
        self.assertIsNone(resolve_transcript({"VOICEINK_TRANSCRIPT": ""}, ""))

    def test_whitespace_only_env_returns_none(self):
        self.assertIsNone(resolve_transcript({"VOICEINK_TRANSCRIPT": "   "}, ""))

    def test_whitespace_only_stdin_returns_none(self):
        self.assertIsNone(resolve_transcript({}, "\n\t  \r\n"))

    def test_whitespace_env_does_not_fall_through_to_stdin(self):
        # A whitespace-only env var means VoiceInk delivered whitespace. That is
        # "nothing publishable", not "channel unavailable", so we do NOT reach for
        # stdin. Distinguishing these two matters: falling through would publish
        # stdin content that VoiceInk did not intend as the transcript.
        self.assertIsNone(resolve_transcript({"VOICEINK_TRANSCRIPT": "   "}, "stdin text"))


class TestContentPreservation(unittest.TestCase):
    """The returned value must be byte-for-byte what VoiceInk sent. We test for
    emptiness after stripping, but we never return a stripped value."""

    def test_leading_and_trailing_whitespace_preserved(self):
        self.assertEqual(resolve_transcript({"VOICEINK_TRANSCRIPT": "  hi  "}, ""), "  hi  ")

    def test_interior_newlines_preserved(self):
        text = "line one\nline two\nline three"
        self.assertEqual(resolve_transcript({"VOICEINK_TRANSCRIPT": text}, ""), text)

    def test_crlf_preserved(self):
        text = "line one\r\nline two"
        self.assertEqual(resolve_transcript({"VOICEINK_TRANSCRIPT": text}, ""), text)

    def test_tabs_preserved(self):
        self.assertEqual(resolve_transcript({"VOICEINK_TRANSCRIPT": "a\tb"}, ""), "a\tb")

    def test_unicode_preserved(self):
        text = "héllo wörld 🧠 مرحبا 日本語"
        self.assertEqual(resolve_transcript({"VOICEINK_TRANSCRIPT": text}, ""), text)

    def test_trailing_newline_from_stdin_preserved(self):
        # Shell pipelines routinely append a newline. We keep it; macrowhisper's
        # smartSpacing handles trailing whitespace at insert time.
        self.assertEqual(resolve_transcript({}, "hello\n"), "hello\n")


if __name__ == "__main__":
    unittest.main()
