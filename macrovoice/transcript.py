"""Resolve the dictation transcript from what VoiceInk hands us.

VoiceInk's Custom Command delivers the final transcript two ways, both carrying the
same text (CustomCommandDeliveryRunner.swift:5-16):

    environment: ["VOICEINK_TRANSCRIPT": transcript]
    standardInput: transcript

That single string is the ENTIRE input surface. There is no mode name, no front app,
no raw-versus-enhanced split. A repo-wide grep for `VOICEINK_` confirms no other
variable reaches this code path. This is why `--mode` must be supplied as an argv
parameter by a per-Mode wrapper rather than discovered at runtime.
"""

from typing import Mapping, Optional

__all__ = ["ENV_VAR", "env_supplies_transcript", "resolve_transcript"]

ENV_VAR = "VOICEINK_TRANSCRIPT"


def env_supplies_transcript(env: Mapping[str, str]) -> bool:
    """True when VOICEINK_TRANSCRIPT is set to a non-empty string.

    Callers use this to decide whether stdin needs reading AT ALL. It is the
    same truthiness test resolve_transcript applies below, exposed so the rule
    lives in one module rather than being restated by every caller.

    Note this is deliberately True for a WHITESPACE-ONLY value. VoiceInk did
    deliver something; resolve_transcript will decline to publish it, and stdin
    must not be consulted, because that would publish text VoiceInk never meant
    as the transcript.
    """
    return bool(env.get(ENV_VAR))


def resolve_transcript(env: Mapping[str, str], stdin_text: str) -> Optional[str]:
    """Return the transcript to publish, or None when there is nothing publishable.

    Resolution order:
      1. `VOICEINK_TRANSCRIPT` if set to a non-empty string.
      2. Otherwise stdin.

    An *empty* env var falls through to stdin, on the theory that the channel was
    unavailable. A *whitespace-only* env var does NOT fall through: VoiceInk did
    deliver something, it just is not publishable, and reaching for stdin in that
    case risks publishing text VoiceInk never meant as the transcript.

    The returned value is never stripped. Emptiness is tested against a stripped
    copy, but the caller receives exactly what VoiceInk sent, so that interior
    newlines, tabs and unicode survive intact. macrowhisper's smartSpacing handles
    stray edge whitespace at insertion time.
    """
    env_value = env.get(ENV_VAR)

    if env_value:
        return env_value if env_value.strip() else None

    if stdin_text and stdin_text.strip():
        return stdin_text

    return None
