"""Proves test_harness.is_valid_recording_meta_json matches macrowhisper's Swift gate.

Source of truth:
  upstream/macrowhisper/src/macrowhisper/Utils/RecordingReferenceResolver.swift:34-53
  (duplicated at Watcher/RecordingsFolderWatcher.swift:478-518)

The Swift is:

    if let languageModelName = json["languageModelName"] as? String, !languageModelName.isEmpty {
        guard let llmResult = json["llmResult"], !(llmResult is NSNull) else { return false }
        guard let s = llmResult as? String, !s.isEmpty else { return false }
        return true
    }
    guard let result = json["result"], !(result is NSNull) else { return false }
    guard let s = result as? String, !s.isEmpty else { return false }
    return true

Three subtleties the port must preserve, each asserted below:
  1. `as? String` on languageModelName FAILS for a non-string, falling through to the
     `result` branch rather than taking the llmResult branch.
  2. NSNull (JSON null) is rejected explicitly, and is distinct from a missing key.
  3. `as? String` on the result value fails for numbers/bools/containers -> false.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_harness import is_valid_recording_meta_json as valid  # noqa: E402


class TestResultBranch(unittest.TestCase):
    """languageModelName absent or empty -> gate validates on `result`."""

    def test_valid_result(self):
        self.assertTrue(valid({"result": "hello world"}))

    def test_result_single_char(self):
        self.assertTrue(valid({"result": "x"}))

    def test_result_whitespace_only_is_valid(self):
        # Swift checks isEmpty, NOT isBlank. A space is non-empty, so it passes.
        # macrowhisper would happily act on this; our adapter declines to create it.
        self.assertTrue(valid({"result": " "}))

    def test_empty_result(self):
        self.assertFalse(valid({"result": ""}))

    def test_missing_result(self):
        self.assertFalse(valid({}))

    def test_null_result(self):
        # guard let result = json["result"], !(result is NSNull)
        self.assertFalse(valid({"result": None}))

    def test_numeric_result_fails_cast(self):
        # `result as? String` fails -> false
        self.assertFalse(valid({"result": 42}))

    def test_bool_result_fails_cast(self):
        self.assertFalse(valid({"result": True}))

    def test_list_result_fails_cast(self):
        self.assertFalse(valid({"result": ["hello"]}))

    def test_dict_result_fails_cast(self):
        self.assertFalse(valid({"result": {"text": "hello"}}))


class TestLanguageModelNameGating(unittest.TestCase):
    """Subtlety 1: how languageModelName selects the branch."""

    def test_empty_lmn_falls_through_to_result(self):
        self.assertTrue(valid({"languageModelName": "", "result": "hi"}))
        self.assertFalse(valid({"languageModelName": "", "result": ""}))

    def test_missing_lmn_falls_through_to_result(self):
        self.assertTrue(valid({"result": "hi"}))

    def test_null_lmn_falls_through_to_result(self):
        # `nil as? String` fails the cast -> result branch
        self.assertTrue(valid({"languageModelName": None, "result": "hi"}))

    def test_nonstring_lmn_falls_through_to_result(self):
        # THE subtle one: `5 as? String` fails, so we take the result branch,
        # NOT the llmResult branch. A naive truthiness port gets this wrong.
        self.assertTrue(valid({"languageModelName": 5, "result": "hi"}))
        self.assertFalse(valid({"languageModelName": 5, "llmResult": "enhanced"}))

    def test_nonstring_lmn_with_no_result_is_invalid(self):
        self.assertFalse(valid({"languageModelName": 5}))


class TestLlmResultBranch(unittest.TestCase):
    """languageModelName set and non-empty -> gate validates on `llmResult`."""

    def test_valid_llm_result(self):
        self.assertTrue(valid({"languageModelName": "gpt-4", "llmResult": "enhanced text"}))

    def test_empty_llm_result(self):
        self.assertFalse(valid({"languageModelName": "gpt-4", "llmResult": ""}))

    def test_missing_llm_result(self):
        self.assertFalse(valid({"languageModelName": "gpt-4"}))

    def test_null_llm_result(self):
        self.assertFalse(valid({"languageModelName": "gpt-4", "llmResult": None}))

    def test_numeric_llm_result_fails_cast(self):
        self.assertFalse(valid({"languageModelName": "gpt-4", "llmResult": 42}))

    def test_llm_branch_ignores_result_entirely(self):
        # Once the llmResult branch is taken, a perfectly good `result` does not save it.
        # This is exactly why the bridge leaves languageModelName absent.
        self.assertFalse(
            valid({"languageModelName": "gpt-4", "llmResult": "", "result": "good text"})
        )
        self.assertTrue(
            valid({"languageModelName": "gpt-4", "llmResult": "enhanced", "result": ""})
        )


class TestBridgeShapedDocuments(unittest.TestCase):
    """The exact shape vi2meta emits must pass."""

    def test_minimal_bridge_document(self):
        self.assertTrue(
            valid(
                {
                    "result": "hello world",
                    "modeName": "bridge",
                    "datetime": "2026-08-05T02:14:33Z",
                    "duration": 0,
                }
            )
        )

    def test_bridge_document_without_mode(self):
        self.assertTrue(
            valid({"result": "hello world", "datetime": "2026-08-05T02:14:33Z", "duration": 0})
        )

    def test_unicode_result(self):
        self.assertTrue(valid({"result": "héllo wörld 🧠 مرحبا"}))


if __name__ == "__main__":
    unittest.main()
