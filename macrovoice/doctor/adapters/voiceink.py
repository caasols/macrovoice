"""Read-only view of VoiceInk's Mode store.

Stage 2 never writes. Everything here answers questions about what VoiceInk has
saved, which is enough to catch the traps that make the bridge look broken.

THE STORED SHAPE, which is not guessable and is what this module exists to know:

  * Modes live in the `modeConfigurationsV2` default as a **bytes** value holding
    UTF-8 JSON. It is not a plist array. Reading it as one raises rather than
    returning something wrong, which is at least honest, but the adapter has to
    do the json.loads itself.
  * The keyboard shortcut is NOT a field of the Mode. It is a separate top-level
    key, `Shortcut_mode_<UUID>` (design doc 4.3). This matters more than it
    looks: a Mode that is neither `isDefault` nor shortcut-bound is INERT, and
    that is friction trap 1, the single most common "the bridge does not work"
    report. It cannot be detected by reading the Mode alone.

A CAVEAT THE CHECKS MUST SURFACE, not swallow. VoiceInk reads this store exactly
once, in `ModeManager.init()` (`ModeConfig.swift:288`), and observes nothing
afterwards, while `cfprefsd` caches on the running app's behalf. So a `defaults
export` taken while VoiceInk is running can be STALE. Observed on 2026-08-15: a
provider configured in the UI still read as absent here minutes later. Every
`vi.*` check therefore tells the user to quit VoiceInk and re-run if they have
just changed something, rather than asserting the store is current.

Nothing here raises. Every failure becomes None, which the checks turn into
UNKNOWN. A diagnostic that crashes is worse than one that says it does not know.
"""

import json
import plistlib
from dataclasses import dataclass
from typing import Optional, Tuple

from .process import CommandResult, run_command

__all__ = ["BUNDLE_ID", "Mode", "VoiceInk"]

BUNDLE_ID = "com.prakashjoshipax.VoiceInk"
PROCESS_NAME = "VoiceInk"
MODES_KEY = "modeConfigurationsV2"
SHORTCUT_KEY_PREFIX = "Shortcut_mode_"
DEFAULT_TIMEOUT_S = 10.0

_UNREAD = object()


@dataclass(frozen=True)
class Mode:
    """One VoiceInk Mode, reduced to the fields the checks reason about.

    `command` is None for any Mode that is not a Custom Command Mode, which is
    most of them. Frozen because checks receive these and must not be able to
    mutate the snapshot they are reasoning about.
    """

    id: str
    name: str
    output_mode: Optional[str]
    is_default: bool
    is_enabled: bool
    command: Optional[str]


class VoiceInk:
    """Read-only VoiceInk inspection. `runner` is injectable so the whole read
    path is testable without VoiceInk installed."""

    def __init__(self, bundle_id=BUNDLE_ID, timeout=DEFAULT_TIMEOUT_S, runner=None):
        self._bundle_id = bundle_id
        self._timeout = timeout
        self._runner = runner or run_command
        self._exported = _UNREAD

    # Reading -----------------------------------------------------------------

    def _run(self, *args) -> CommandResult:
        return self._runner(list(args), self._timeout)

    def _export(self) -> Optional[dict]:
        """The whole defaults domain, parsed, or None.

        Cached for the life of this adapter: five checks ask about VoiceInk and
        `defaults export` is not free. Caching the FAILURE too is deliberate, so
        a machine without VoiceInk does not pay for five doomed subprocesses and
        the five checks cannot disagree with each other about what they saw.
        """
        if self._exported is not _UNREAD:
            return self._exported

        self._exported = None
        result = self._run("defaults", "export", self._bundle_id, "-")
        if not result.ok or not result.stdout.strip():
            return None
        try:
            parsed = plistlib.loads(result.stdout.encode("utf-8"))
        except Exception:
            # plistlib raises a variety of types for malformed input, and the
            # only correct response to any of them is the same.
            return None
        if isinstance(parsed, dict):
            self._exported = parsed
        return self._exported

    def modes(self) -> Optional[Tuple[Mode, ...]]:
        """Every saved Mode, or None if the store could not be read."""
        exported = self._export()
        if not exported:
            return None
        raw = exported.get(MODES_KEY)
        if not isinstance(raw, (bytes, bytearray)):
            return None
        try:
            decoded = json.loads(bytes(raw).decode("utf-8"))
        except Exception:
            return None
        if not isinstance(decoded, list):
            return None

        modes = []
        for entry in decoded:
            # A malformed entry is skipped rather than failing the whole read:
            # one bad Mode must not blind every check.
            if not isinstance(entry, dict):
                continue
            command = entry.get("customCommand")
            modes.append(
                Mode(
                    id=str(entry.get("id", "")),
                    name=str(entry.get("name", "")),
                    output_mode=entry.get("outputMode"),
                    is_default=bool(entry.get("isDefault", False)),
                    is_enabled=bool(entry.get("isEnabled", True)),
                    command=command.get("command") if isinstance(command, dict) else None,
                )
            )
        return tuple(modes)

    def has_shortcut(self, mode_id: str) -> Optional[bool]:
        """Whether `mode_id` has its own keyboard binding, or None if unreadable.

        None and False are meaningfully different here. False means "this Mode is
        genuinely unbound", which combined with not being default is the inert
        trap. None means we could not look, and reporting that as False would
        accuse a healthy machine.
        """
        exported = self._export()
        if not exported:
            return None
        return (SHORTCUT_KEY_PREFIX + str(mode_id)) in exported

    def is_running(self) -> Optional[bool]:
        """Whether VoiceInk is up, which is what makes an export possibly stale.

        `pgrep -x` exits 1 with no output when nothing matches, which is a
        successful answer of "no", not a failure. Only a command that could not
        run at all is None.
        """
        result = self._run("pgrep", "-x", PROCESS_NAME)
        if result.timed_out or result.returncode is None:
            return None
        return bool(result.stdout.strip())
