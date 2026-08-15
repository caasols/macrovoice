"""The liveness check, on the delivery path. No macrowhisper needed.

G3. `macrovoice` used to publish into `recordings/` whether or not anything was
watching. If macrowhisper was down the folders sat there, and when it next
started the arm race marked every pre-existing folder as processed and dropped
them all, so a dictation made during the outage was lost with no error anywhere.

THE RULE THESE TESTS PIN, and it is the load-bearing decision: defer only on
PROOF OF DEATH, publish on everything else.

  sentinel seen   -> False -> defer, keep it spooled
  any other output-> True  -> publish
  cannot tell     -> None  -> publish

Failing OPEN is deliberate. `macrowhisper --status` exits 0 whether or not a
daemon is listening (main.swift:1115-1122), so the exit code proves nothing and
only the literal sentence is definitive. Its ABSENCE is not proof of life. A
check that deferred on uncertainty could silently stop delivering on a perfectly
working setup, which is a worse failure than the one G3 fixes: the spool would
grow while the user dictated into nothing, and no error would say so.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from macrovoice.listener import (  # noqa: E402
    NOT_RUNNING_SENTINEL,
    is_listening,
)


def fake_runner(stdout="", returncode=0, raises=None):
    def run(args, timeout):
        if raises:
            raise raises
        return returncode, stdout

    return run


RUNNING = (
    "Macrowhisper version: 2.1.1\n"
    "Recordings watcher: yes (armed, started 2h ago, last event never, pending 0)\n"
    "Superwhisper folder: /Users/x/mw-bridge\n"
)


class TestProofOfDeath(unittest.TestCase):
    def test_the_sentinel_means_not_listening(self):
        self.assertIs(is_listening(runner=fake_runner(NOT_RUNNING_SENTINEL)), False)

    def test_the_sentinel_is_found_among_other_output(self):
        noisy = "some preamble\n%s\ntrailing\n" % NOT_RUNNING_SENTINEL
        self.assertIs(is_listening(runner=fake_runner(noisy)), False)

    def test_the_sentinel_text_is_exactly_what_macrowhisper_prints(self):
        # main.swift:1115-1122 prints this literal line. A drifted copy would
        # silently stop matching, and the check would fail open forever while
        # looking like it worked.
        self.assertEqual(NOT_RUNNING_SENTINEL, "macrowhisper is not running.")


class TestFailOpen(unittest.TestCase):
    """Everything that is not proof of death must publish."""

    def test_a_running_daemon_is_listening(self):
        self.assertIs(is_listening(runner=fake_runner(RUNNING)), True)

    def test_a_non_zero_exit_still_publishes(self):
        # --status exits 0 either way, so a non-zero exit is anomalous and tells
        # us nothing about the daemon. Publishing is the safe reading.
        self.assertIsNone(is_listening(runner=fake_runner("", returncode=3)))

    def test_empty_output_is_unknown_not_dead(self):
        self.assertIsNone(is_listening(runner=fake_runner("")))

    def test_unrecognisable_output_is_unknown_not_dead(self):
        self.assertIsNone(is_listening(runner=fake_runner("what even is this")))

    def test_a_timeout_is_unknown_not_dead(self):
        self.assertIsNone(is_listening(runner=fake_runner(raises=TimeoutError())))

    def test_a_missing_binary_is_unknown_not_dead(self):
        self.assertIsNone(is_listening(runner=fake_runner(raises=OSError("no such file"))))

    def test_an_unexpected_exception_is_unknown_not_a_crash(self):
        # This runs on the delivery path, where an exception before the spool
        # loses the transcript. Nothing here may propagate.
        self.assertIsNone(is_listening(runner=fake_runner(raises=RuntimeError("boom"))))


class TestItNeverBlocksForLong(unittest.TestCase):
    def test_the_timeout_is_passed_through_to_the_runner(self):
        seen = {}

        def run(args, timeout):
            seen["timeout"] = timeout
            seen["args"] = args
            return 0, RUNNING

        is_listening(timeout_s=1.5, runner=run)
        self.assertEqual(seen["timeout"], 1.5)
        self.assertIn("--status", seen["args"])

    def test_the_default_timeout_is_small_enough_for_the_delivery_path(self):
        # VoiceInk kills the command at 10s and the drain budget is 6s. A
        # generous timeout here would eat the budget that publishes transcripts.
        from macrovoice.listener import DEFAULT_TIMEOUT_S

        self.assertLessEqual(DEFAULT_TIMEOUT_S, 2.0)


class TestSingleSourceOfTruth(unittest.TestCase):
    def test_doctor_uses_the_same_sentinel_object(self):
        """doctor cannot be imported on the delivery path, so the sentinel lives
        here and doctor imports it. Two copies of a string that must match
        someone else's source exactly is a drift waiting to happen."""
        from macrovoice.doctor.adapters.macrowhisper import (
            NOT_RUNNING_SENTINEL as doctor_sentinel,
        )

        self.assertIs(doctor_sentinel, NOT_RUNNING_SENTINEL)


class TestTheDeliveryPathStaysLight(unittest.TestCase):
    def test_importing_the_listener_does_not_drag_in_doctor(self):
        """cli.py lazy-imports doctor so a dictation never pays for it. A
        liveness check that imported doctor's adapter would undo that."""
        import subprocess

        code = (
            "import sys; import macrovoice.listener; "
            "print(any(m.startswith('macrovoice.doctor') for m in sys.modules))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        self.assertEqual(out.stdout.strip(), "False", out.stderr)


if __name__ == "__main__":
    unittest.main()
