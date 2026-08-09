"""The check table: the specification of what doctor knows.

Ordered the way a user meets the traps, which is also roughly dependency order.
Each entry is data. Adding a trap is one function and one row.

Every trap number below refers to docs/product/2026-08-08-first-run-friction.md.
"""

import os
from pathlib import Path

from .model import Check, Finding, Severity

VOICEINK_APP_PATHS = (
    Path("/Applications/VoiceInk.app"),
    Path("~/Applications/VoiceInk.app").expanduser(),
)
MIN_PYTHON = (3, 9)
BREW_INSTALL = "brew install ognistik/formulae/macrowhisper"


def _version_tuple(text):
    parts = []
    for piece in text.split("."):
        digits = ""
        for char in piece:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _check_env_python(ctx):
    """The interpreter macrovoice.sh will actually get, not the one running us."""
    executable, version = ctx.bridge.env_python()
    if version is None:
        return Finding.unknown("could not run /usr/bin/env python3")
    if _version_tuple(version) < MIN_PYTHON:
        return Finding.problem(
            "/usr/bin/env python3 is %s at %s" % (version, executable),
            "macrovoice needs Python %d.%d or newer" % MIN_PYTHON,
        )
    return Finding.ok("/usr/bin/env python3 is %s" % version)


def _check_macrowhisper_installed(ctx):
    if ctx.mw.available():
        return Finding.ok()
    return Finding.problem("macrowhisper is not on PATH", BREW_INSTALL)


def _check_voiceink_installed(ctx):
    for path in VOICEINK_APP_PATHS:
        if path.exists():
            return Finding.ok("%s" % path)
    return Finding.problem(
        "VoiceInk.app not found in /Applications or ~/Applications",
        "install VoiceInk 2.0 or later, which is where the Custom Command output mode arrived",
    )


def _check_watch_dirs(ctx):
    snapshot = ctx.bridge.snapshot()
    if not snapshot.watch_exists:
        return Finding.problem(
            "watch root %s does not exist" % snapshot.watch_root,
            "mkdir -p %s/recordings" % snapshot.watch_root,
        )
    if not snapshot.recordings_exists:
        return Finding.problem(
            "%s exists but recordings/ does not" % snapshot.watch_root,
            "mkdir -p %s/recordings" % snapshot.watch_root,
        )
    return Finding.ok("%s" % snapshot.watch_root)


def _check_script(ctx):
    script = ctx.bridge.script_path()
    if not script.exists():
        return Finding.problem("%s does not exist" % script)
    if not os.access(str(script), os.X_OK):
        return Finding.problem("%s is not executable" % script, "chmod +x %s" % script)
    return Finding.ok("%s" % script)


def _check_spool(ctx):
    """A non-empty spool means publishing has been failing and transcripts are
    waiting. They are not lost: the design keeps them recoverable."""
    snapshot = ctx.bridge.snapshot()
    waiting = snapshot.spool_count + snapshot.staging_count
    if waiting:
        return Finding.problem(
            "%d transcript(s) waiting in the spool" % waiting,
            "macrovoice --drain-only --watch %s" % snapshot.watch_root,
        )
    return Finding.ok()


def _check_log_errors(ctx):
    errors = ctx.bridge.recent_log_errors()
    if errors:
        return Finding.problem(
            "%d error(s) in macrovoice.log in the last 24h, newest: %s"
            % (len(errors), errors[-1])
        )
    return Finding.ok()


CHECKS = (
    Check(
        id="pre.python",
        title="python3 is 3.9 or newer",
        severity=Severity.FAIL,
        inspect=_check_env_python,
    ),
    Check(
        id="pre.macrowhisper",
        title="macrowhisper is installed",
        severity=Severity.FAIL,
        inspect=_check_macrowhisper_installed,
    ),
    Check(
        id="pre.voiceink",
        title="VoiceInk is installed",
        severity=Severity.FAIL,
        inspect=_check_voiceink_installed,
    ),
    Check(
        id="bridge.watch",
        title="the watch directory exists",
        severity=Severity.FAIL,
        inspect=_check_watch_dirs,
    ),
    Check(
        id="bridge.script",
        title="macrovoice.sh is executable",
        severity=Severity.FAIL,
        inspect=_check_script,
    ),
    Check(
        id="bridge.spool",
        title="the spool is empty",
        severity=Severity.WARN,
        inspect=_check_spool,
        depends_on=("bridge.watch",),
    ),
    Check(
        id="bridge.log",
        title="no recent errors in macrovoice.log",
        severity=Severity.WARN,
        inspect=_check_log_errors,
        depends_on=("bridge.watch",),
    ),
)
