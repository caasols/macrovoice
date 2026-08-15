"""`macrovoice doctor`: inspect the whole bridge and report what is wrong.

Stage 1 is READ-ONLY. It never creates a directory, never edits a config, never
touches VoiceInk. Repairs arrive in stage 3.

The exit code deliberately departs from cli.py's always-exit-0 policy: 0 when
healthy, 1 when a fatal problem remains, 2 when a fatal check could not be
determined. A check that cannot fail is useless in a script.
"""

import argparse
import sys
from pathlib import Path

from .adapters.bridge import BridgeState
from .adapters.macrowhisper import Macrowhisper
from .adapters.voiceink import VoiceInk
from ..watch import DEFAULT_WATCH, ENV_WATCH, LEGACY_ENV_WATCH, resolve_watch_default
from .model import Context
from .registry import CHECKS
from .report import exit_code, render
from .runner import run


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="macrovoice doctor",
        description=(
            "Inspect the VoiceInk to macrowhisper bridge and report what is wrong, "
            "in the order a user hits it."
        ),
    )
    parser.add_argument(
        "--watch",
        # Must resolve exactly as cli.py does, or doctor inspects a directory
        # the delivery path is not using and reports on the wrong bridge. That
        # is why both call the same function. See macrovoice/watch.py.
        default=resolve_watch_default(),
        help=(
            "macrowhisper watch root (default: $%s, else $%s, else %s, falling "
            "back to ~/mw-bridge when only that legacy directory exists)"
            % (ENV_WATCH, LEGACY_ENV_WATCH, DEFAULT_WATCH)
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Read-only. This release is read-only regardless; the flag pins that "
            "contract so a script written now keeps it when repairs land."
        ),
    )
    return parser.parse_args(argv)


def doctor_main(argv) -> int:
    args = _parse_args(argv)
    watch_root = Path(args.watch).expanduser()
    ctx = Context(
        watch_root=watch_root,
        mw=Macrowhisper(),
        bridge=BridgeState(watch_root),
        vi=VoiceInk(),
    )
    results = run(CHECKS, ctx)
    sys.stdout.write(render(results))
    return exit_code(results)
