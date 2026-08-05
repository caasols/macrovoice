"""Build the synthetic Superwhisper meta.json document. Pure; no filesystem access.

The schema was reconstructed by enumerating every key macrowhisper actually reads
(a grep over all `metaJson[...]` / `json[...]` accesses in the macrowhisper source),
because no real Superwhisper sample was available to copy. Deriving it from the
consumer is arguably better than copying a sample anyway: it captures exactly what
is load-bearing and nothing that is not.

The fields split in two, and the split is the important part.

GROUP A, which the source app must provide, and which this module writes:

    result             REQUIRED. The validation gate.
                       RecordingReferenceResolver.swift:34-53
    modeName           optional. Feeds triggerModes. TriggerEvaluator.swift:22
    datetime           optional. Placeholder use.
    duration           optional. Placeholder use.
    segments           optional. Feeds {{segments}}. VoiceInk exposes none, so omitted.
    llmResult          optional. DELIBERATELY OMITTED, see below.
    languageModelName  optional. DELIBERATELY OMITTED, see below.

GROUP B, which macrowhisper injects itself at runtime, and which this module must
NOT write: frontAppName, frontAppBundleId, frontAppPid, frontAppUrl, frontApp,
selectedText, clipboardContext, clipboardStacking, appContext, appVocabulary,
actionResult, actionResults.

Why languageModelName stays absent: setting it to a non-empty string flips
macrowhisper's gate to require a non-empty `llmResult` instead of `result`. VoiceInk's
Custom Command exposes only the FINAL text, with no way to tell whether it was
AI-enhanced, so there is no honest way to populate both fields. Putting the final
text in `result` and leaving the LLM fields absent makes the gate validate on
`result`, which is exactly what we can guarantee.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

__all__ = ["build_meta", "serialize_meta"]


def _format_datetime(when: datetime) -> str:
    """ISO-8601 in UTC with a trailing Z, second precision.

    A naive datetime is assumed to be UTC rather than local, so the output is
    deterministic regardless of the machine's timezone. macrowhisper only ever
    surfaces this through placeholders, so precision beyond seconds buys nothing.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_meta(
    transcript: str,
    mode_name: Optional[str] = None,
    when: Optional[datetime] = None,
    duration: float = 0.0,
) -> Dict[str, Any]:
    """Return a fresh meta.json document for one dictation.

    `mode_name` is omitted from the document entirely when None or empty, rather
    than written as null. TriggerEvaluator.swift:22 reads it with `as? String`,
    which a null would fail anyway, so a null key would be pure noise.

    The transcript is stored verbatim. No trimming, no normalization, no cleanup:
    macrowhisper owns text transformation via its own templating and smart-insertion
    engines, and second-guessing it here would produce surprising double-processing.
    """
    meta: Dict[str, Any] = {
        "result": transcript,
        "datetime": _format_datetime(when if when is not None else datetime.now(timezone.utc)),
        "duration": duration,
    }
    if mode_name:
        meta["modeName"] = mode_name
    return meta


def serialize_meta(meta: Dict[str, Any]) -> str:
    """Serialize to a single-line JSON string, ready to be written as UTF-8.

    `ensure_ascii=False` keeps non-ASCII readable in the file; Swift's
    JSONSerialization reads raw UTF-8 without complaint. Control characters,
    including NUL, are escaped by json.dumps rather than stripped, so user content
    is never silently altered.

    Single-line output is not a macrowhisper requirement. It is a debugging
    affordance: the file stays greppable, and a truncated write is obvious on sight.
    """
    return json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
