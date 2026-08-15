"""`macrovoice doctor`: inspect the whole bridge and report what is wrong.

Stage 1 is READ-ONLY. It never creates a directory, never edits a config, never
touches VoiceInk. Repairs arrive in stage 3.

The exit code deliberately departs from cli.py's always-exit-0 policy: 0 when
healthy, 1 when a fatal problem remains, 2 when a fatal check could not be
determined. A check that cannot fail is useless in a script.
"""

import argparse
import os
import sys
from pathlib import Path

from .adapters.bridge import BridgeState
from .adapters.macrowhisper import Macrowhisper
from .adapters.voiceink import VoiceInk
from .model import Context
from .registry import CHECKS
from .report import exit_code, render
from .runner import run

DEFAULT_WATCH = "~/mw-bridge"


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
        default=os.environ.get("MW_BRIDGE_WATCH", DEFAULT_WATCH),
        help="macrowhisper watch root (default: $MW_BRIDGE_WATCH or %s)" % DEFAULT_WATCH,
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
