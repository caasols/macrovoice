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
from .transcript import resolve_transcript

DEFAULT_WATCH = "~/mw-bridge"
LOG_NAME = "macrovoice.log"


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
        default=os.environ.get("MW_BRIDGE_WATCH", DEFAULT_WATCH),
        help=f"macrowhisper watch root (default: $MW_BRIDGE_WATCH or {DEFAULT_WATCH})",
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
        "--log-transcript",
        action="store_true",
        help="Log transcript text. Off by default: the log would otherwise become a "
        "plaintext record of everything dictated.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = _parse_args(argv)
    watch_root = Path(args.watch).expanduser()

    try:
        publisher = Publisher(watch_root, min_gap_s=args.gap)

        if args.drain_only:
            published = publisher.drain()
            _log(watch_root, f"drain-only: published {len(published)}")
            return 0

        stdin_text = "" if sys.stdin.isatty() else sys.stdin.read()
        transcript = resolve_transcript(os.environ, stdin_text)

        if transcript is None:
            _log(watch_root, "skipped: no publishable transcript (empty or whitespace only)")
            return 0

        meta = build_meta(transcript, mode_name=args.mode)
        outcome = publisher.publish(meta)

        detail = f"text={transcript!r}" if args.log_transcript else f"chars={len(transcript)}"
        if outcome.deferred:
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


if __name__ == "__main__":
    raise SystemExit(main())
