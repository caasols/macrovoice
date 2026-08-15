"""Is anything actually watching? The last silent-loss path on the delivery side.

G3. `macrovoice` published into `recordings/` whether or not macrowhisper was
listening. If it was down, the folders simply sat there, and when it next started
its recordings watcher marked every folder that already existed as processed and
dropped them all. A dictation made during the outage vanished with no error in
any log, on either side.

The exposure is narrower than it first looks, and worth stating precisely so
nobody over-corrects: the launchd agent carries `KeepAlive => SuccessfulExit
false` and `RunAtLoad => true`, so a CRASH is restarted within seconds. Only a
clean stop stays down: `--stop-service`, an upgrade, a logout. The restart window
is still exposed, and that window is where transcripts died.

THE RULE, and it is the load-bearing decision here: DEFER ONLY ON PROOF OF DEATH.

    sentinel seen    -> False -> caller keeps it spooled
    anything else    -> True  -> publish
    cannot tell      -> None  -> publish

`macrowhisper --status` EXITS 0 whether or not a daemon is listening
(main.swift:1115-1122), so the return code proves nothing and only the literal
sentence below is definitive. Its absence is NOT proof of life. Failing open is
therefore deliberate: a check that deferred whenever it was unsure could stop
delivery on a working setup, growing the spool while the user dictated into
nothing and nothing said so. That is a worse failure than the one being fixed,
and it is the shape this project has already been bitten by twice in doctor.

Nothing here raises. This runs between `stage()` and `drain()`, and while the
transcript is safely spooled by then, an exception would still abort the drain
and defer delivery for no reason.

This module deliberately does NOT live under `doctor/`. `cli.py` lazy-imports
doctor so a dictation never loads it, and the sentinel is shared the other way
round: doctor imports it from here.
"""

import subprocess
from typing import Optional

__all__ = ["NOT_RUNNING_SENTINEL", "DEFAULT_TIMEOUT_S", "is_listening"]

# The literal line macrowhisper prints when nothing is on the socket
# (main.swift:1115-1122). Matched as a substring, since it is printed alone but
# a future version could wrap it.
NOT_RUNNING_SENTINEL = "macrowhisper is not running."

MACROWHISPER = "macrowhisper"
# What a real status reply always opens with. Used only to tell "confirmed
# alive" from "output I do not recognise", so that True actually means
# something. Drift here is harmless by construction: an unrecognised reply
# becomes None, and None publishes exactly as True does.
VERSION_KEY = "Macrowhisper version"
# Small on purpose. VoiceInk kills the command at 10s and the drain budget is
# 6s, so a generous timeout here would eat the budget that actually publishes.
# Measured 2026-08-15: a warm --status answers in about 10ms, 170ms cold.
DEFAULT_TIMEOUT_S = 1.5


def _run(args, timeout):
    completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return completed.returncode, completed.stdout


def is_listening(timeout_s: float = DEFAULT_TIMEOUT_S, runner=None) -> Optional[bool]:
    """False only when macrowhisper says it is not running. True or None otherwise.

    `runner` is injectable so the whole decision is testable without a daemon.
    """
    runner = runner or _run
    try:
        returncode, stdout = runner([MACROWHISPER, "--status"], timeout_s)
    except Exception:
        # Timeout, missing binary, or anything else: we could not tell. Publish.
        return None

    text = stdout or ""
    if NOT_RUNNING_SENTINEL in text:
        return False
    if returncode == 0 and VERSION_KEY in text:
        return True
    # Neither the sentinel nor a reply we recognise: a non-zero exit (--status
    # exits 0 in both real cases, so that is already anomalous), silence, or
    # something reworded upstream. None of it is proof of death, and True would
    # be a claim we cannot support, so say we do not know and publish anyway.
    return None
