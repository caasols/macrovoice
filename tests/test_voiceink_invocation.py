"""The seam VoiceInk actually runs through: `/bin/zsh -lc <script>`.

Every other test invokes `python3 -m macrovoice` directly, which skips the two
things standing between VoiceInk and the adapter: the `.sh` wrappers, and the
login shell VoiceInk starts them in.

That gap matters because it is exactly where a failure would be hardest to
diagnose. VoiceInk runs the command as `/bin/zsh -lc <command>`
(CustomCommandDeliveryRunner.swift:78-96) and kills it after 10 seconds
(TranscriptionDelivery.swift:115). If the wrapper cannot locate its own package,
or a login shell's startup files interfere, the symptom is a dictation that
silently goes nowhere, which looks identical to every other silent-drop path.

The `-l` matters more than it looks. A login shell sources the user's zsh
startup files, so the wrapper runs in an environment shaped by whatever is in
`~/.zshrc`: PATH rewrites, `set -e`, conda/pyenv shims, anything. These tests run
through a real login shell for that reason; a plain `zsh -c` would pass while the
real invocation failed.

These are hermetic: no macrowhisper, no VoiceInk, no network. Every path is a
temp directory, so the real `~/mw-bridge` is never touched.
"""

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

PROTOTYPE = Path(__file__).resolve().parent.parent
MACROVOICE_SH = PROTOTYPE / "macrovoice.sh"
PROBE_SH = PROTOTYPE / "probe.sh"

# VoiceInk's hard limit. Not configurable, and the transcript exists nowhere else
# by the time the command runs, so overrunning it loses the dictation outright.
VOICEINK_TIMEOUT_S = 10.0


def run_as_voiceink(script_command, transcript, watch, cwd=None, timeout=VOICEINK_TIMEOUT_S):
    """Invoke exactly as VoiceInk does: login zsh, env var set, text on stdin."""
    env = dict(os.environ)
    env["VOICEINK_TRANSCRIPT"] = transcript
    env["MW_BRIDGE_WATCH"] = str(watch)
    env.pop("PYTHONPATH", None)  # the wrapper must set this up itself
    return subprocess.run(
        ["/bin/zsh", "-lc", script_command],
        input=transcript,
        capture_output=True,
        text=True,
        env=env,
        # VoiceInk's working directory is not documented and must not matter.
        cwd=cwd or "/",
        timeout=timeout,
    )


class MacrovoiceWrapperTest(unittest.TestCase):
    """`macrovoice.sh`, the line the user pastes into VoiceInk."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.watch = Path(self._tmp.name) / "mw-bridge"

    def published(self):
        recordings = self.watch / "recordings"
        if not recordings.exists():
            return []
        return sorted(p for p in recordings.iterdir() if p.is_dir())

    def sole_meta(self):
        folders = self.published()
        self.assertEqual(len(folders), 1, f"expected one folder, got {folders}")
        return json.loads((folders[0] / "meta.json").read_text(encoding="utf-8"))

    def test_wrapper_publishes_through_a_login_shell(self):
        result = run_as_voiceink(
            f"{MACROVOICE_SH} --watch {self.watch} --gap 0.01",
            "wrapper works",
            self.watch,
        )
        self.assertEqual(result.returncode, 0, f"stderr:\n{result.stderr}")
        self.assertEqual(self.sole_meta()["result"], "wrapper works")

    def test_mode_reaches_meta_json(self):
        # The only way modeName can be set: VoiceInk does not expose the Mode,
        # so a per-Mode wrapper line has to pass it. If this breaks, macrowhisper's
        # triggerModes silently never match and the user sees no error.
        result = run_as_voiceink(
            f"{MACROVOICE_SH} --mode email --watch {self.watch} --gap 0.01",
            "moded",
            self.watch,
        )
        self.assertEqual(result.returncode, 0, f"stderr:\n{result.stderr}")
        self.assertEqual(self.sole_meta().get("modeName"), "email")

    def test_wrapper_works_from_an_unrelated_cwd(self):
        # VoiceInk's working directory is undocumented. The wrapper resolves its
        # own location via ${0:A:h} precisely so this cannot matter; assert it.
        result = run_as_voiceink(
            f"{MACROVOICE_SH} --watch {self.watch} --gap 0.01",
            "cwd independent",
            self.watch,
            cwd="/tmp",
        )
        self.assertEqual(result.returncode, 0, f"stderr:\n{result.stderr}")
        self.assertEqual(self.sole_meta()["result"], "cwd independent")

    def test_exits_zero_even_when_the_watch_root_is_unwritable(self):
        # The exit-code policy is load-bearing: VoiceInk has already suppressed
        # its own paste, so a non-zero exit shows the user an error without
        # recovering their words. Verify it survives the wrapper too, not just
        # the Python entry point.
        blocked = Path(self._tmp.name) / "blocked"
        blocked.mkdir()
        blocked.chmod(0o500)
        self.addCleanup(blocked.chmod, 0o700)
        result = run_as_voiceink(
            f"{MACROVOICE_SH} --watch {blocked / 'nope'} --gap 0.01",
            "unwritable",
            blocked / "nope",
        )
        self.assertEqual(
            result.returncode, 0,
            f"must exit 0 even on failure.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_finishes_well_inside_voiceinks_kill_deadline(self):
        # Not a micro-benchmark: the assertion is that a normal dictation has
        # generous headroom against a hard limit we do not control. The default
        # --gap alone is 1.0s, so the margin is worth pinning.
        start = time.monotonic()
        result = run_as_voiceink(
            f"{MACROVOICE_SH} --watch {self.watch}",   # default gap, as in real use
            "timing check",
            self.watch,
        )
        elapsed = time.monotonic() - start
        self.assertEqual(result.returncode, 0, f"stderr:\n{result.stderr}")
        self.assertLess(
            elapsed, VOICEINK_TIMEOUT_S / 2,
            f"took {elapsed:.2f}s of a {VOICEINK_TIMEOUT_S}s budget. Still passing, "
            "but the headroom is gone and a slower Mac would start losing dictations.",
        )


class ProbeWrapperTest(unittest.TestCase):
    """`probe.sh`, the Gate 2 instrument.

    Worth testing precisely because it is the FIRST thing that runs against live
    VoiceInk. A broken probe would produce an empty log, which reads as "VoiceInk
    never called the command" and would send the investigation the wrong way.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.watch = Path(self._tmp.name) / "mw-bridge"

    def test_probe_logs_an_invocation_without_touching_the_real_log(self):
        result = run_as_voiceink(str(PROBE_SH), "probe me", self.watch)
        self.assertEqual(result.returncode, 0, f"stderr:\n{result.stderr}")

        log = self.watch / "probe.log"
        self.assertTrue(log.exists(), "probe.sh wrote no log")
        text = log.read_text(encoding="utf-8")

        self.assertEqual(text.count("INVOCATION"), 1, "expected exactly one block")
        self.assertIn(">>>probe me<<<", text)
        self.assertIn("--- stdin ---", text)
        self.assertIn("identical", text)  # env and stdin agree

    def test_probe_appends_rather_than_truncates(self):
        # Gate 2 asks for 3 to 4 dictations. If the log truncated per run, only
        # the last would survive and the "exactly one INVOCATION per dictation"
        # check, the whole point of Gate 2, could never be made.
        for i in range(3):
            run_as_voiceink(str(PROBE_SH), f"dictation {i}", self.watch)
        text = (self.watch / "probe.log").read_text(encoding="utf-8")
        self.assertEqual(text.count("INVOCATION"), 3)
        for i in range(3):
            self.assertIn(f">>>dictation {i}<<<", text)

    def test_probe_survives_a_transcript_that_would_break_a_naive_script(self):
        nasty = 'quotes " backslash \\ $HOME `id` ; rm -rf / accent café emoji 🎙'
        result = run_as_voiceink(str(PROBE_SH), nasty, self.watch)
        self.assertEqual(result.returncode, 0, f"stderr:\n{result.stderr}")
        text = (self.watch / "probe.log").read_text(encoding="utf-8")
        # Substitution or execution of any of these would be a real bug: the
        # probe runs on the user's machine with their transcript as input.
        self.assertIn("`id`", text)
        self.assertIn("$HOME", text)
        self.assertIn("rm -rf /", text)
        self.assertIn("🎙", text)


if __name__ == "__main__":
    unittest.main()
