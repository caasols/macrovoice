"""Where the watch root comes from when nobody passed `--watch`.

One owner for the resolution, shared by `cli.py` and `doctor/`. Two copies of the
same constant in two files is how they drift, and this one is user-facing.

Deliberately cheap: `cli.py` is the delivery path and imports this
unconditionally, so nothing here may reach beyond `os` and `pathlib`. No
subprocess, no config parsing, no import of the rest of the package.

WHY THIS IS A FUNCTION AND NOT A CONSTANT
-----------------------------------------
The directory was renamed from `~/mw-bridge` to `~/macrovoice`. Done as a plain
constant change that rename is BREAKING, and the break is silent: it moves where
macrovoice publishes without moving the user's data and without touching
macrowhisper's persisted `defaults.watch`. An unmigrated install would then
publish into a directory nothing is watching, and that does not delay the
dictation, it destroys it, because macrowhisper marks every pre-existing folder
processed when its watcher next arms
(RecordingsFolderWatcher.swift, the startup arm race).

Resolving at call time instead means an existing `~/mw-bridge` keeps being used,
so upgrading changes nothing until the user chooses to migrate, and the fallback
retires itself the moment `~/mw-bridge` stops existing.
"""

import os
from pathlib import Path

DEFAULT_WATCH = "~/macrovoice"
LEGACY_WATCH = "~/mw-bridge"

ENV_WATCH = "MACROVOICE_WATCH"
# Kept, and not deprecated in any user-visible way. Ruled 2026-08-15: read the
# new name first, fall back to this, REMOVE NOTHING, and say nothing when the old
# one is used. It is non-breaking for anyone scripting the tool, it needed no
# decision to ship, and it leaves the removal as a separate choice later rather
# than bundling a second breaking change into a rename. Do not re-litigate this
# into a deprecation warning without asking; a warning on the delivery path buys
# the user nothing they can act on mid-dictation.
LEGACY_ENV_WATCH = "MW_BRIDGE_WATCH"


def resolve_watch_default(environ=None, home=None) -> Path:
    """The default watch root, as an absolute expanded path.

    Order, first match wins:

      1. ``$MACROVOICE_WATCH``
      2. ``$MW_BRIDGE_WATCH``, the legacy name
      3. ``~/macrovoice`` if it exists
      4. ``~/mw-bridge`` if it exists and ``~/macrovoice`` does not
      5. ``~/macrovoice``, the new default

    An explicit ``--watch`` outranks all of these; argparse never calls this when
    the flag is given.

    Rule 3 beats rule 4 when BOTH directories exist. That case is genuinely
    unknowable here, since only macrowhisper's `defaults.watch` decides where a
    transcript must land, so the tie is broken towards the new name to keep the
    fallback self-retiring. It is not silent: doctor's `mw.watchmatch` fails
    loudly on a real mismatch and `bridge.legacywatch` names the leftover.

    `environ` and `home` are injectable so the behaviour can be tested against a
    real temporary home rather than against these constants.
    """
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)

    for name in (ENV_WATCH, LEGACY_ENV_WATCH):
        value = environ.get(name)
        if value:
            # Empty counts as unset for both names. Honouring "" literally would
            # resolve the watch root to the working directory, which under
            # VoiceInk is `/`.
            return _expand(value, home)

    # `is_dir`, not `exists`: a stray FILE named mw-bridge must not divert
    # delivery to a path that can never hold recordings/.
    if not (home / "macrovoice").is_dir() and (home / "mw-bridge").is_dir():
        return _expand(LEGACY_WATCH, home)
    return _expand(DEFAULT_WATCH, home)


def _expand(value: str, home: Path) -> Path:
    """Expand a leading `~` against `home` rather than the real one.

    `Path.expanduser()` would consult `$HOME` and ignore the injected home, which
    makes every test here silently exercise the developer's own directory.
    """
    path = Path(value)
    if path.parts and path.parts[0] == "~":
        return home.joinpath(*path.parts[1:])
    return path.expanduser()
