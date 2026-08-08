"""Test oracle: a faithful Python port of macrowhisper's recording validation gate.

This exists so `macrovoice`'s output can be proven correct WITHOUT macrowhisper installed
and running. Every meta.json the adapter generates is asserted against this function.

Ported verbatim (branch for branch) from:
    upstream/macrowhisper/src/macrowhisper/Utils/RecordingReferenceResolver.swift:34-53

The identical logic is duplicated upstream at:
    upstream/macrowhisper/src/macrowhisper/Watcher/RecordingsFolderWatcher.swift:478-518

If macrowhisper changes that gate, this port MUST be updated in lockstep, and
tests/test_harness_port.py is what will catch the drift.
"""

from typing import Any, Mapping

__all__ = ["is_valid_recording_meta_json"]


def _as_string(value: Any) -> "str | None":
    """Model Swift's `value as? String`.

    Returns the string, or None when the cast would fail. Note that Python's
    `isinstance(True, int)` quirk is not a concern here because we test for `str`
    specifically, and `bool` is not a `str`.
    """
    return value if isinstance(value, str) else None


def is_valid_recording_meta_json(json: Mapping[str, Any]) -> bool:
    """Return True when macrowhisper would consider this recording ready to process.

    Mirrors the Swift exactly:

        if let languageModelName = json["languageModelName"] as? String,
           !languageModelName.isEmpty {
            guard let llmResult = json["llmResult"], !(llmResult is NSNull) else { return false }
            guard let s = llmResult as? String, !s.isEmpty else { return false }
            return true
        }
        guard let result = json["result"], !(result is NSNull) else { return false }
        guard let s = result as? String, !s.isEmpty else { return false }
        return true

    Three subtleties that a naive port gets wrong:

    1. `json["languageModelName"] as? String` FAILS the cast for a non-string value,
       so `{"languageModelName": 5}` falls through to the `result` branch. It does
       NOT take the llmResult branch. A truthiness check would get this backwards.
    2. JSON `null` (Swift `NSNull`) is rejected explicitly and is distinct from a
       missing key, though both ultimately return False here.
    3. The emptiness test is Swift's `isEmpty`, not a whitespace check. A result of
       `" "` is VALID to macrowhisper. macrovoice independently declines to publish
       whitespace-only transcripts, but that is the adapter's policy, not this gate's.
    """
    language_model_name = _as_string(json.get("languageModelName"))
    if language_model_name is not None and language_model_name != "":
        if "llmResult" not in json:
            return False
        llm_result = json["llmResult"]
        if llm_result is None:  # NSNull
            return False
        llm_result_string = _as_string(llm_result)
        if llm_result_string is None or llm_result_string == "":
            return False
        return True

    if "result" not in json:
        return False
    result = json["result"]
    if result is None:  # NSNull
        return False
    result_string = _as_string(result)
    if result_string is None or result_string == "":
        return False
    return True
