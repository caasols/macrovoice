"""Regenerate the VoiceInk defaults fixtures.

The fixtures are SCRUBBED stand-ins, not a dump of anyone's real install: no real
UUIDs, no real home directory, no real shortcut binding. What they preserve is
the SHAPE, which is the only part the adapter depends on and the only part that
can break:

  * `modeConfigurationsV2` is a BYTES value holding UTF-8 JSON, not a plist array.
    Verified against a live install on 2026-08-15.
  * the keyboard shortcut is a SEPARATE top-level key, `Shortcut_mode_<UUID>`,
    not a field of the Mode (design doc section 4.3). A Mode with neither a
    shortcut nor isDefault is inert, which is friction trap 1.
  * unrelated keys are present and must be ignored.

Run from the repo root:  python3 tests/fixtures/doctor/voiceink/make_fixtures.py
"""

import json
import plistlib
from pathlib import Path

HERE = Path(__file__).resolve().parent

BRIDGE_ID = "B0000000-0000-0000-0000-0000000000B1"
SHORTCUT = b'{"kind":"modifierOnly","keyCode":65535,"modifierFlagsRawValue":655360}'


def mode(**over):
    base = {
        "id": "10000000-0000-0000-0000-000000000001",
        "name": "Dictation",
        "outputMode": "paste",
        "isDefault": True,
        "isEnabled": True,
        "selectedLanguage": "auto",
        "icon": "mic",
    }
    base.update(over)
    return base


def bridge_mode(command="/Users/example/macrovoice/macrovoice.sh --mode macrovoice", **over):
    fields = {
        "id": BRIDGE_ID, "name": "macrovoice", "outputMode": "customCommand",
        "isDefault": False, "icon": "doc", "customCommand": {"command": command},
    }
    fields.update(over)  # callers override, rather than colliding with, the defaults
    return mode(**fields)


def write(name, modes, extra=None):
    plist = {"modeConfigurationsV2": json.dumps(modes).encode("utf-8"),
             "someUnrelatedKey": "ignored"}
    plist.update(extra or {})
    path = HERE / name
    path.write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML))
    return path


FIXTURES = {
    # The healthy layout the README recommends: everyday Mode holds default,
    # bridge Mode is shortcut-bound.
    "healthy.plist": (
        [mode(), bridge_mode()],
        {"Shortcut_mode_" + BRIDGE_ID: SHORTCUT},
    ),
    # Friction trap 1: saved but neither default nor shortcut-bound, so it never
    # runs and every dictation silently goes through the paste Mode instead.
    "inert-mode.plist": ([mode(), bridge_mode()], {}),
    # The bridge Mode holds default, so there is no non-bridge escape hatch.
    "no-escapehatch.plist": (
        [mode(isDefault=False), bridge_mode(isDefault=True)],
        {"Shortcut_mode_" + BRIDGE_ID: SHORTCUT},
    ),
    # Output reverted to paste: the command is stored but never invoked.
    "wrong-outputmode.plist": (
        [mode(), bridge_mode(outputMode="paste")],
        {"Shortcut_mode_" + BRIDGE_ID: SHORTCUT},
    ),
    # No bridge Mode at all.
    "no-bridge-mode.plist": ([mode()], {}),
}


if __name__ == "__main__":
    for name, (modes, extra) in FIXTURES.items():
        print("wrote", write(name, modes, extra))
