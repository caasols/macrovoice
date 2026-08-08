"""End-to-end CLI tests, driven through real subprocesses.

These deliberately do NOT import the CLI. VoiceInk invokes us as
`/bin/zsh -lc <command>` with VOICEINK_TRANSCRIPT in the environment and the
transcript on stdin (CustomCommandDeliveryRunner.swift:78-96). Testing through
subprocess is the only way to prove that contract actually holds, including the
exit-code policy, which an in-process test would silently bypass.
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_harness import is_valid_recording_meta_json  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.watch = Path(self._tmp.name) / "mw-bridge"

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args, transcript=None, stdin_text="", extra_env=None, timeout=30):
        env = dict(os.environ)
        env.pop("VOICEINK_TRANSCRIPT", None)
        if transcript is not None:
            env["VOICEINK_TRANSCRIPT"] = transcript
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, "-m", "macrovoice", "--watch", str(self.watch), "--gap", "0.01", *args],
            input=stdin_text,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=env,
            timeout=timeout,
        )

    @property
    def recordings(self):
        return self.watch / "recordings"

    def published(self):
        if not self.recordings.exists():
            return []
        return sorted(p for p in self.recordings.iterdir() if p.is_dir())

    def sole_meta(self):
        folders = self.published()
        self.assertEqual(len(folders), 1, f"expected exactly one recording, got {folders}")
        return json.loads((folders[0] / "meta.json").read_text(encoding="utf-8"))


class TestDeliveryPaths(CliTestCase):
    def test_env_transcript_publishes(self):
        result = self.run_cli(transcript="hello from env")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sole_meta()["result"], "hello from env")

    def test_stdin_transcript_publishes(self):
        result = self.run_cli(stdin_text="hello from stdin")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sole_meta()["result"], "hello from stdin")

    def test_both_channels_prefers_env(self):
        self.run_cli(transcript="from env", stdin_text="from stdin")
        self.assertEqual(self.sole_meta()["result"], "from env")

    def test_published_document_passes_the_macrowhisper_gate(self):
        self.run_cli(transcript="hello world")
        self.assertTrue(is_valid_recording_meta_json(self.sole_meta()))

    def test_mode_flag_lands_in_meta(self):
        # The only way modeName can ever be populated: VoiceInk exposes no mode to
        # the command, so a per-Mode wrapper must pass it as argv.
        self.run_cli("--mode", "email", transcript="hello")
        self.assertEqual(self.sole_meta()["modeName"], "email")

    def test_without_mode_flag_the_key_is_absent(self):
        self.run_cli(transcript="hello")
        self.assertNotIn("modeName", self.sole_meta())

    def test_unicode_survives_the_full_subprocess_round_trip(self):
        text = "héllo wörld 🧠 مرحبا 日本語 élève"
        self.run_cli(transcript=text)
        self.assertEqual(self.sole_meta()["result"], text)

    def test_multiline_transcript_survives(self):
        text = "first line\nsecond line\nthird line"
        self.run_cli(transcript=text)
        self.assertEqual(self.sole_meta()["result"], text)

    def test_quotes_and_shell_metachars_survive(self):
        text = "she said \"hi\"; rm -rf / && echo $(whoami) `id`"
        self.run_cli(transcript=text)
        self.assertEqual(self.sole_meta()["result"], text)


class TestNothingToPublish(CliTestCase):
    def test_empty_transcript_publishes_nothing_and_exits_zero(self):
        result = self.run_cli(transcript="", stdin_text="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.published(), [])

    def test_whitespace_only_transcript_publishes_nothing(self):
        result = self.run_cli(transcript="   \n\t  ")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.published(), [])

    def test_no_channels_at_all_exits_zero(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)


class TestExitCodePolicy(CliTestCase):
    """The adapter must ALWAYS exit 0 on the delivery path.

    A non-zero exit surfaces to the user as a VoiceInk error notification
    (CustomCommandDeliveryError.nonZeroExit) WITHOUT recovering the transcript, so
    it adds alarm without adding value. VoiceInk has already suppressed its own
    paste by the time we run (TranscriptionDelivery.swift:43-46).
    """

    def test_unwritable_watch_root_still_exits_zero(self):
        blocked = Path(self._tmp.name) / "blocked"
        blocked.mkdir()
        blocked.chmod(0o500)  # r-x: cannot create the layout inside
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "macrovoice",
                    "--watch",
                    str(blocked / "mw-bridge"),
                    "--gap",
                    "0.01",
                ],
                input="",
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
                env={**os.environ, "VOICEINK_TRANSCRIPT": "hello"},
                timeout=30,
            )
            self.assertEqual(
                result.returncode, 0, f"must exit 0 even when publishing fails: {result.stderr}"
            )
        finally:
            blocked.chmod(0o700)

    def test_completes_well_within_voiceinks_ten_second_kill(self):
        import time

        start = time.monotonic()
        self.run_cli(transcript="hello")
        self.assertLess(time.monotonic() - start, 5.0)


class TestLogging(CliTestCase):
    def test_writes_a_log_line(self):
        self.run_cli(transcript="hello world")
        log = (self.watch / "macrovoice.log").read_text(encoding="utf-8")
        self.assertIn("published", log)

    def test_log_records_length_not_content_by_default(self):
        # A dictation log is a plaintext record of everything the user says. Default
        # to metadata only; --log-transcript opts in.
        secret = "my bank password is hunter2"
        self.run_cli(transcript=secret)
        log = (self.watch / "macrovoice.log").read_text(encoding="utf-8")
        self.assertNotIn(secret, log)
        self.assertIn(str(len(secret)), log)

    def test_log_transcript_flag_opts_in(self):
        self.run_cli("--log-transcript", transcript="visible text")
        log = (self.watch / "macrovoice.log").read_text(encoding="utf-8")
        self.assertIn("visible text", log)

    def test_log_appends_across_invocations(self):
        self.run_cli(transcript="first")
        self.run_cli(transcript="second")
        lines = [
            line
            for line in (self.watch / "macrovoice.log").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertGreaterEqual(len(lines), 2)


class TestSequentialDictations(CliTestCase):
    def test_five_back_to_back_dictations_all_survive(self):
        """The realistic burst case: a user dictating repeatedly. Every transcript
        must reach recordings/, and each must be in its own folder."""
        expected = {f"dictation number {i}" for i in range(5)}
        for text in sorted(expected):
            self.run_cli(transcript=text)

        # Flush anything still spooled by a deferred final call.
        subprocess.run(
            [sys.executable, "-m", "macrovoice", "--watch", str(self.watch), "--gap", "0.01", "--drain-only"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=30,
        )

        found = {
            json.loads((f / "meta.json").read_text(encoding="utf-8"))["result"]
            for f in self.published()
        }
        self.assertEqual(found, expected)

    def test_a_deferred_publish_says_so_in_the_log(self):
        """When another process holds the drain lock, we spool and say we spooled.

        The log is the only forensic trail a user has when a dictation does not
        appear: the real macrovoice.log from 2026-08-05 is how the concurrency
        bug was diagnosed at all. A deferred publish that logged "published"
        would actively mislead, pointing the investigation at macrowhisper when
        the folder had never left the spool.
        """
        import fcntl

        lock_path = self.watch / ".drain.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder = open(lock_path, "w")
        self.addCleanup(holder.close)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

        self.run_cli(transcript="held up by the lock")

        log = (self.watch / "macrovoice.log").read_text(encoding="utf-8")
        self.assertIn("spooled (deferred", log)
        self.assertIn("drained=0", log)
        self.assertNotIn("published 17", log)
        # The transcript is safe in the spool, not lost and not published.
        self.assertEqual(self.published(), [])
        self.assertEqual(len(list((self.watch / ".spool").iterdir())), 1)

    def test_drain_only_publishes_nothing_new(self):
        result = subprocess.run(
            [sys.executable, "-m", "macrovoice", "--watch", str(self.watch), "--drain-only"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.published(), [])


if __name__ == "__main__":
    unittest.main()
