"""macrovoice command line entry point.

Invoked by VoiceInk as the Custom Command for a Mode. VoiceInk runs
`/bin/zsh -lc <command>` with VOICEINK_TRANSCRIPT in the environment and the same
text on stdin, then kills the process after 10 seconds
(TranscriptionDelivery.swift:115, CustomCommandDeliveryRunner.swift:78-96).

Exit-code policy, which is deliberate and load-bearing: this program always exits 0
on the delivery path. By the time it runs, VoiceInk has already routed around its own
paste (TranscriptionDelivery.swift:43-46), so the transcript exists nowhere else. A
non-zero exit produces a user-facing VoiceInk error notification without recovering
the text, which is alarm without remedy. Instead, failures are logged and the
transcript is spooled so a later invocation can still publish it.
"""

import argparse
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .meta import build_meta
from .publisher import DEFAULT_MIN_GAP_S, Publisher
from .listener import is_listening
from .transcript import env_supplies_transcript, resolve_transcript
from .watch import DEFAULT_WATCH, ENV_WATCH, LEGACY_ENV_WATCH, resolve_watch_default

LOG_NAME = "macrovoice.log"
# G3. Names the cause and the remedy, because the user's words are on disk and
# recoverable and they cannot tell that from silence.
_NOT_RUNNING = (
    "macrowhisper is not running, so nothing would have watched for this: %s. "
    "It is safe in the spool and the next run publishes it. "
    "Start it with: macrowhisper --start-service"
)


def _log(watch_root: Path, message: str) -> None:
    """Append one timestamped line. Never raises: logging must not break delivery."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        watch_root.mkdir(parents=True, exist_ok=True)
        with open(watch_root / LOG_NAME, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        print(f"{stamp} {message}", file=sys.stderr)


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="macrovoice",
        description=(
            "Bridge a VoiceInk Custom Command invocation into a Superwhisper-shaped "
            "meta.json that stock macrowhisper watches."
        ),
    )
    parser.add_argument(
        "--mode",
        default=None,
        help=(
            "Mode name to record as modeName, feeding macrowhisper's triggerModes. "
            "VoiceInk does NOT expose the mode to the command, so a per-Mode wrapper "
            "must pass it here explicitly."
        ),
    )
    parser.add_argument(
        "--watch",
        # Resolved per invocation, not at import, which is what lets an
        # unmigrated `~/mw-bridge` keep working. See macrovoice/watch.py.
        default=resolve_watch_default(),
        help=(
            f"macrowhisper watch root (default: ${ENV_WATCH}, else ${LEGACY_ENV_WATCH}, "
            f"else {DEFAULT_WATCH}, falling back to ~/mw-bridge when only that "
            "legacy directory exists)"
        ),
    )
    parser.add_argument(
        "--gap",
        type=float,
        default=DEFAULT_MIN_GAP_S,
        help=(
            "Minimum seconds between publishes. Defends macrowhisper's burst "
            f"protection, which silently drops all folders appearing at once (default: {DEFAULT_MIN_GAP_S})"
        ),
    )
    parser.add_argument(
        "--drain-only",
        action="store_true",
        help="Publish anything left in the spool and exit. Reads no transcript.",
    )
    parser.add_argument(
        "--no-liveness-check",
        action="store_true",
        help=(
            "Publish even when macrowhisper is provably not running. Off by "
            "default: publishing into an unwatched folder does not delay the "
            "dictation, it destroys it, because macrowhisper drops every folder "
            "that already exists when its watcher next arms."
        ),
    )
    parser.add_argument(
        "--log-transcript",
        action="store_true",
        help="Log transcript text. Off by default: the log would otherwise become a "
        "plaintext record of everything dictated.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "doctor":
        # Lazy import: a dictation must never load doctor's code. Everything
        # below this line is the delivery path and is unchanged, including the
        # always-exit-0 policy in this module's docstring. doctor deliberately
        # does NOT exit 0, because a check that cannot fail is useless.
        from .doctor import doctor_main

        return doctor_main(argv[1:])
    args = _parse_args(argv)
    watch_root = Path(args.watch).expanduser()

    try:
        # G3. The probe answers False ONLY on macrowhisper's own "not running"
        # sentence; everything else publishes. Deferring on uncertainty would
        # stop delivery on a working setup, which is worse than the loss it
        # prevents. See macrovoice/listener.py.
        listener = None if args.no_liveness_check else is_listening
        publisher = Publisher(watch_root, min_gap_s=args.gap, listener=listener)

        if args.drain_only:
            published = publisher.drain()
            if publisher.listener_said_down:
                _log(watch_root, _NOT_RUNNING % ("drain-only: nothing published"))
            else:
                _log(watch_root, f"drain-only: published {len(published)}")
            return 0

        if env_supplies_transcript(os.environ) or sys.stdin.isatty():
            # The env var already carries the words, so stdin cannot change the
            # answer. Reading it anyway is what made B5 reachable: an open pipe
            # that never reaches EOF blocked here forever, in front of stage(),
            # so the transcript never reached the spool.
            stdin_text = ""
        else:
            # No env var: stdin genuinely IS the only source of the words, so
            # this read must be allowed to block, and that is deliberate.
            # VoiceInk always closes the pipe. A deadline here would have no
            # basis to choose, and a truncated read would be worse than a hang:
            # it would publish half a dictation as though it were whole.
            # Decided 2026-08-09. Do not "fix" this into a timeout.
            stdin_text = sys.stdin.read()
        transcript = resolve_transcript(os.environ, stdin_text)

        if transcript is None:
            _log(watch_root, "skipped: no publishable transcript (empty or whitespace only)")
            return 0

        meta = build_meta(transcript, mode_name=args.mode)
        outcome = publisher.publish(meta)

        detail = f"text={transcript!r}" if args.log_transcript else f"chars={len(transcript)}"
        if publisher.listener_said_down:
            # Loud on purpose. A silent deferral is just a quieter version of the
            # bug this exists to fix, and the user needs to know their words are
            # waiting rather than delivered.
            _log(
                watch_root,
                _NOT_RUNNING % (f"spooled {outcome.spooled.name} {detail}"),
            )
        elif outcome.deferred:
            _log(
                watch_root,
                f"spooled (deferred, will publish on a later run) {outcome.spooled.name} "
                f"{detail} drained={len(outcome.published)}",
            )
        else:
            _log(watch_root, f"published {outcome.spooled.name} {detail}")
        return 0

    except Exception:
        # Last-resort net. Losing a transcript is worse than a messy log, and a
        # non-zero exit would not recover it anyway.
        _log(watch_root, f"ERROR (exiting 0 anyway): {traceback.format_exc().strip()}")
        return 0
