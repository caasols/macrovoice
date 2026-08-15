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
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_harness import is_valid_recording_meta_json  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.watch = Path(self._tmp.name) / "macrovoice"

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

    def spawn_cli_with_open_stdin(self, *args, transcript=None, timeout=8):
        """Launch the CLI with stdin held OPEN and SILENT, then wait for exit.

        This is the only shape that reproduces B5. subprocess.run(input=...)
        closes stdin, so run_cli above can never trigger it. Here the pipe is
        opened and then neither written to nor closed, which is exactly what a
        launchd job, a cron entry, a CI wrapper or a backgrounded shell does.

        Fails the test on timeout rather than returning, because the regression
        is a HANG: without a hard deadline it would stall the whole suite
        instead of failing one test.
        """
        env = dict(os.environ)
        env.pop("VOICEINK_TRANSCRIPT", None)
        if transcript is not None:
            env["VOICEINK_TRANSCRIPT"] = transcript

        proc = subprocess.Popen(
            [
                sys.executable, "-m", "macrovoice",
                "--watch", str(self.watch), "--gap", "0.01", *args,
            ],
            stdin=subprocess.PIPE,  # opened, never written, never closed
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(REPO_ROOT),
            env=env,
            text=True,
        )
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            self.fail(
                f"macrovoice did not exit within {timeout}s while stdin was held "
                "open. B5 has regressed: stdin is being read before the env var "
                "is consulted. That blocks in front of stage(), so the transcript "
                "never reaches the spool and VoiceInk's 10s kill loses it silently."
            )
        finally:
            proc.stdin.close()
            proc.stdout.close()
            proc.stderr.close()
        return proc.returncode

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
                    str(blocked / "macrovoice"),
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


class TestTheDefaultWatchRoot(unittest.TestCase):
    """B4, proved through the shipped command with NO `--watch` given.

    This is the only shape that matches how VoiceInk actually invokes us: the
    Mode's command is `macrovoice.sh --mode <name>`, with no watch flag, so
    `DEFAULT_WATCH` is not a fallback here, it is the production path. A unit
    test of the resolver alone would not catch argparse being wired to the wrong
    thing.

    HOME is redirected to a temp directory so nothing here can see, create or
    publish into the developer's real `~/mw-bridge`.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def run_with_home(self, *args, transcript="hello", **env_overrides):
        env = dict(os.environ)
        env.pop("VOICEINK_TRANSCRIPT", None)
        env.pop("MACROVOICE_WATCH", None)
        env.pop("MW_BRIDGE_WATCH", None)
        env["HOME"] = str(self.home)
        env["VOICEINK_TRANSCRIPT"] = transcript
        env.update(env_overrides)
        return subprocess.run(
            # --no-liveness-check so the test never shells out to the real
            # macrowhisper, and never depends on whether it happens to be up.
            [
                sys.executable, "-m", "macrovoice",
                "--gap", "0.01", "--no-liveness-check", *args,
            ],
            input="",
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=env,
            timeout=30,
        )

    def published_under(self, name):
        recordings = self.home / name / "recordings"
        if not recordings.exists():
            return []
        return sorted(p for p in recordings.iterdir() if p.is_dir())

    def test_a_fresh_machine_publishes_into_macrovoice(self):
        result = self.run_with_home(transcript="fresh install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.published_under("macrovoice")), 1, result.stderr)

    def test_an_unmigrated_install_still_publishes_into_mw_bridge(self):
        """THE regression test for B4, end to end.

        If this fails, upgrading silently redirects an existing user's
        dictations into a directory macrowhisper is not watching, and
        macrowhisper destroys them rather than delaying them.
        """
        (self.home / "mw-bridge").mkdir()
        result = self.run_with_home(transcript="legacy install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.published_under("mw-bridge")), 1, result.stderr)
        self.assertFalse(
            (self.home / "macrovoice").exists(),
            "the new directory must not be created behind an unmigrated user's back",
        )

    def test_the_new_environment_variable_is_honoured(self):
        target = self.home / "elsewhere"
        result = self.run_with_home(MACROVOICE_WATCH=str(target))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.published_under("elsewhere")), 1, result.stderr)

    def test_the_legacy_environment_variable_is_still_honoured(self):
        target = self.home / "scripted"
        result = self.run_with_home(MW_BRIDGE_WATCH=str(target))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.published_under("scripted")), 1, result.stderr)

    def test_the_legacy_environment_variable_is_honoured_silently(self):
        # Ruled: support both, say nothing. A deprecation notice on the delivery
        # path is noise the user cannot act on mid-dictation.
        target = self.home / "scripted"
        result = self.run_with_home(MW_BRIDGE_WATCH=str(target))
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "")
        log = (target / "macrovoice.log").read_text(encoding="utf-8")
        self.assertNotIn("MW_BRIDGE_WATCH", log)
        self.assertNotIn("deprecat", log.lower())
        self.assertNotIn("legacy", log.lower())

    def test_an_explicit_watch_flag_outranks_an_existing_legacy_directory(self):
        (self.home / "mw-bridge").mkdir()
        target = self.home / "explicit"
        result = self.run_with_home("--watch", str(target))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.published_under("explicit")), 1, result.stderr)
        self.assertEqual(self.published_under("mw-bridge"), [])

    def test_help_names_both_environment_variables(self):
        # The published interface. If the legacy name is ever removed, this test
        # is the reminder that the README and this help text must change too.
        result = self.run_with_home("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MACROVOICE_WATCH", result.stdout)
        self.assertIn("MW_BRIDGE_WATCH", result.stdout)
        self.assertIn("~/macrovoice", result.stdout)

    def test_help_does_not_leak_a_resolved_machine_path(self):
        # The default is computed per run, so printing it would both leak the
        # user's home directory and print a value that changes with the
        # filesystem. The help text states the rule instead.
        (self.home / "mw-bridge").mkdir()
        result = self.run_with_home("--help")
        self.assertNotIn(str(self.home / "mw-bridge"), result.stdout)


class TestLivenessCheck(CliTestCase):
    """G3 end to end, through the real CLI, with a fake macrowhisper on PATH.

    A fake binary rather than a patched function, because the thing under test
    is that the shipped command shells out and reads the answer correctly. The
    real macrowhisper is never involved and the user's daemon is never touched.
    """

    SENTINEL = "macrowhisper is not running."
    HEALTHY = (
        "Macrowhisper version: 2.1.1\\n"
        "Recordings watcher: yes (armed, started 1h ago, last event never, pending 0)\\n"
    )

    def fake_macrowhisper(self, prints, exit_code=0):
        """Put a stub `macrowhisper` first on PATH and return the env to use."""
        bindir = Path(self._tmp.name) / "bin"
        bindir.mkdir(exist_ok=True)
        stub = bindir / "macrowhisper"
        stub.write_text('#!/bin/sh\nprintf "%s"\nexit %d\n' % (prints, exit_code))
        os.chmod(str(stub), 0o755)
        return {"PATH": "%s:%s" % (bindir, os.environ.get("PATH", ""))}

    def spooled(self):
        spool = self.watch / ".spool"
        return sorted(p.name for p in spool.iterdir()) if spool.exists() else []

    def test_a_dead_daemon_spools_instead_of_publishing(self):
        env = self.fake_macrowhisper(self.SENTINEL)
        result = self.run_cli(transcript="during the outage", extra_env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.published(), [],
                         "published into a folder the arm race will destroy")
        self.assertEqual(len(self.spooled()), 1)

    def test_it_says_so_loudly_in_the_log(self):
        # Silent deferral would just be a quieter version of the bug.
        env = self.fake_macrowhisper(self.SENTINEL)
        self.run_cli(transcript="during the outage", extra_env=env)
        log = (self.watch / "macrovoice.log").read_text(encoding="utf-8")
        self.assertIn("macrowhisper is not running", log)
        self.assertIn("spooled", log.lower())

    def test_a_live_daemon_publishes_normally(self):
        env = self.fake_macrowhisper(self.HEALTHY)
        self.run_cli(transcript="delivered", extra_env=env)
        self.assertEqual(len(self.published()), 1)

    def test_a_later_run_delivers_what_the_outage_deferred(self):
        dead = self.fake_macrowhisper(self.SENTINEL)
        self.run_cli(transcript="waited it out", extra_env=dead)
        self.assertEqual(self.published(), [])

        alive = self.fake_macrowhisper(self.HEALTHY)
        self.run_cli("--drain-only", extra_env=alive)
        self.assertEqual(self.sole_meta()["result"], "waited it out")

    def test_drain_only_also_refuses_to_publish_into_the_void(self):
        # --drain-only is the recovery path, and recovery into an unwatched
        # folder would destroy exactly the transcripts it was run to rescue.
        dead = self.fake_macrowhisper(self.SENTINEL)
        self.run_cli(transcript="rescue me", extra_env=dead)
        self.assertEqual(len(self.spooled()), 1)

        result = self.run_cli("--drain-only", extra_env=dead)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.published(), [], "drain-only published into the void")
        self.assertEqual(len(self.spooled()), 1, "and it must still be recoverable")
        log = (self.watch / "macrovoice.log").read_text(encoding="utf-8")
        self.assertIn("macrowhisper is not running", log)

    def test_the_flag_turns_the_probe_off(self):
        env = self.fake_macrowhisper(self.SENTINEL)
        self.run_cli("--no-liveness-check", transcript="published anyway", extra_env=env)
        self.assertEqual(len(self.published()), 1)

    def test_no_macrowhisper_on_path_publishes_rather_than_stalling(self):
        # Fail open. A missing binary says nothing about the daemon, and
        # deferring here would stop delivery for anyone whose PATH differs
        # under VoiceInk's login shell.
        bindir = Path(self._tmp.name) / "emptybin"
        bindir.mkdir(exist_ok=True)
        result = self.run_cli(transcript="still delivered", extra_env={"PATH": str(bindir)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.published()), 1)

    def test_a_hanging_macrowhisper_does_not_cost_the_dictation(self):
        # The B5 lesson on a new surface: a probe that blocks must not become a
        # hang on the delivery path. VoiceInk kills the command at 10 seconds.
        bindir = Path(self._tmp.name) / "slowbin"
        bindir.mkdir(exist_ok=True)
        stub = bindir / "macrowhisper"
        stub.write_text("#!/bin/sh\nsleep 30\n")
        os.chmod(str(stub), 0o755)
        env = {"PATH": "%s:%s" % (bindir, os.environ.get("PATH", ""))}
        started = time.monotonic()
        result = self.run_cli(transcript="not lost to a hang", extra_env=env, timeout=20)
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, 10.0,
                        "the probe must not push the run past VoiceInk's 10s kill")
        self.assertEqual(len(self.published()), 1, "a slow probe cost the delivery")

    def test_the_transcript_is_never_lost_even_when_deferred(self):
        env = self.fake_macrowhisper(self.SENTINEL)
        self.run_cli(transcript="precious words", extra_env=env)
        spool = self.watch / ".spool"
        folders = list(spool.iterdir())
        self.assertEqual(len(folders), 1)
        meta = json.loads((folders[0] / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["result"], "precious words")


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


class TestOpenStdinDoesNotBlock(CliTestCase):
    """B5: reading stdin before consulting the env var could hang forever.

    Diagnosed 2026-08-08, reproduced 2026-08-09. cli.py read stdin
    unconditionally whenever it was not a tty, BEFORE anyone asked whether it
    was needed, so a parent that opened a pipe and never wrote or closed it made
    read() wait forever. The block sits in front of stage(), which inverts its
    severity: every other failure in this tool is survivable because spooling
    happens first, and this one is not. VoiceInk's 10s kill
    (TranscriptionDelivery.swift:115) then fires and the words are gone with
    nothing logged anywhere.
    """

    def test_env_var_set_does_not_block_on_an_open_stdin(self):
        code = self.spawn_cli_with_open_stdin(transcript="env wins, stdin never closes")

        self.assertEqual(code, 0, "the delivery path must always exit 0")
        self.assertEqual(self.sole_meta()["result"], "env wins, stdin never closes")

    def test_stdin_is_not_read_at_all_when_the_env_var_wins(self):
        # Both channels carry text, and they disagree. The env var must win, per
        # the precedence documented in transcript.resolve_transcript. This pins
        # the rule while we are changing the code around it.
        result = self.run_cli(transcript="from the env var", stdin_text="from stdin")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.sole_meta()["result"], "from the env var")

    def test_a_whitespace_only_env_var_does_not_reach_for_stdin(self):
        # The case a naive short-circuit gets wrong. A whitespace-only env var
        # means VoiceInk delivered something unpublishable, NOT that the channel
        # was unavailable, so stdin must not be consulted and nothing is
        # published. Held open here so a regression shows up as a hang too.
        code = self.spawn_cli_with_open_stdin(transcript="   ")

        self.assertEqual(code, 0)
        self.assertEqual(self.published(), [], "whitespace must publish nothing")

    def test_drain_only_does_not_block_on_an_open_stdin(self):
        # --drain-only returns at cli.py:95-98, before any stdin handling, so it
        # is safe today. This test keeps it that way: it reads no transcript, so
        # it must never wait on one.
        code = self.spawn_cli_with_open_stdin("--drain-only")

        self.assertEqual(code, 0)
        self.assertEqual(self.published(), [])


if __name__ == "__main__":
    unittest.main()
