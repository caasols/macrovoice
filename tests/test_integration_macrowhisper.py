"""Integration tests against a REAL macrowhisper install.

These cover the two claims that matter most and are hardest to test: that stock
macrowhisper actually acts on what macrovoice publishes, and that it keeps doing
so when several dictations land at once. Both were originally proven by hand,
once, which left the project's central claim with no regression test behind it.
A change to the meta schema, the publish mechanism or macrowhisper itself would
go unnoticed until someone repeated the ritual by hand.

These tests re-run those checks automatically. They are OPT-IN and skipped by
default, because they launch a real daemon:

    MACROVOICE_INTEGRATION=1 python3 -m unittest discover -s tests -t tests

Safety, since this drives a system-wide tool:
  * A temporary watch directory and config are used. `~/mw-bridge` and
    `~/.config/macrowhisper/` are never read or written.
  * `macrowhisper --config <path>` PERSISTS that path for future runs, so the
    saved path is captured before and restored after, even on failure.
  * The action is a shell append, never a paste, so no Accessibility permission
    and no focused text field are needed, and nothing is typed into whatever the
    user happens to have open.
  * The daemon is started per-test and killed in cleanup.
"""

import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent

from macrovoice.publisher import DEFAULT_MIN_GAP_S as DEFAULT_GAP_S  # noqa: E402
from macrovoice.listener import NOT_RUNNING_SENTINEL  # noqa: E402

MACROWHISPER = shutil.which("macrowhisper")
ENABLED = os.environ.get("MACROVOICE_INTEGRATION") == "1"

SKIP_REASON = (
    "opt-in: set MACROVOICE_INTEGRATION=1"
    if MACROWHISPER
    else "macrowhisper is not installed on PATH"
)

# Generous because a real daemon and a real filesystem watcher are involved.
# Detection is normally well under a second; these are ceilings, not expectations.
READY_TIMEOUT_S = 20.0
FIRE_TIMEOUT_S = 20.0


def _get_config_path():
    """The path macrowhisper will use on its next start, or '' if unavailable."""
    out = subprocess.run(
        [MACROWHISPER, "--get-config"], capture_output=True, text=True, timeout=15
    ).stdout.strip()
    return out.split(":", 1)[1].strip() if ":" in out else ""


def _is_default_config(text):
    return "default config path" in text


def _temp_roots():
    """Every directory prefix that means 'this is scratch space, not a real config'.

    macOS resolves /tmp to /private/tmp and /var to /private/var, and TMPDIR is
    per-user under /var/folders, so each root is recorded in both spellings.
    """
    roots = set()
    for root in (tempfile.gettempdir(), "/tmp", "/var/folders"):
        if not root:
            continue
        normalised = os.path.normpath(root)
        roots.add(normalised)
        roots.add(os.path.normpath("/private" + normalised))
        # ...and the reverse, for a TMPDIR already expressed under /private.
        if normalised.startswith("/private/"):
            roots.add(normalised[len("/private") :])
    return roots


def looks_like_leaked_temp_path(path):
    """True if `path` sits inside a temp directory, i.e. a leak from a prior run.

    This exists because the obvious guard does not work. Teardown used to ask
    "did the config path change during this run", which cannot catch a leak that
    happened in an EARLIER run: the next run reads the leaked temp path, records
    it as the value to protect, restores it faithfully, and passes. The guard
    compares the leaked value with itself. That is precisely how the 2026-08-06
    leak survived two days while every dictation silently vanished.

    So the question has to be "is this path sane", not "did it change".

    Conservative on purpose. A false positive resets a path the user chose
    deliberately, which is a silent change to their setup, so matching is by path
    COMPONENT and never by substring: `~/tmpconfig/macrowhisper.json` is a
    perfectly ordinary place to keep a config and must not be touched.
    """
    if not path:
        return False
    candidate = str(path).strip()
    # Only absolute paths can be judged. Relative paths and CLI prose such as
    # "using default config path" are handled by the caller, not here.
    if not candidate.startswith("/"):
        return False
    resolved = os.path.normpath(candidate)
    for root in _temp_roots():
        if resolved == root or resolved.startswith(root.rstrip("/") + os.sep):
            return True
    return False


def resolve_original_config(raw):
    """Decide what to treat as the user's config, from `--get-config` output.

    Returns `(path, was_default, leaked)`. `leaked` is the rejected temp path, or
    None. Pure, so the decision that protects the user's install is testable
    without a macrowhisper anywhere near it.

    A path inside a temp directory is never adopted as "the original". Adopting
    it is what made the 2026-08-06 leak self-perpetuating and invisible. Falling
    back to the default config path is always safe: it is where macrowhisper
    would have looked with no configuration at all.
    """
    path = raw.split(":", 1)[1].strip() if ":" in raw else ""
    was_default = _is_default_config(raw)
    if looks_like_leaked_temp_path(path):
        return "", True, path
    return path, was_default, None


# Captured ONCE, at import, before any test can touch it. Restoring from a
# per-test snapshot is not good enough: if a test leaks, every later test
# snapshots the leaked value and faithfully restores the wrong thing.
if MACROWHISPER and ENABLED:
    _ORIGINAL_RAW = subprocess.run(
        [MACROWHISPER, "--get-config"], capture_output=True, text=True, timeout=15
    ).stdout.strip()
    _ORIGINAL_PATH, _ORIGINAL_WAS_DEFAULT, _LEAKED = resolve_original_config(_ORIGINAL_RAW)
    if _LEAKED:
        print(
            "\nWARNING: macrowhisper's saved config path is a temp directory:\n"
            f"  {_LEAKED}\n"
            "A previous integration run leaked it. Your daemon has been reading a\n"
            "throwaway config, so its watch directory is probably wrong and\n"
            "dictations may have been silently discarded. Resetting to the default\n"
            "instead of preserving the leak.\n",
            file=sys.stderr,
        )
else:
    _ORIGINAL_RAW, _ORIGINAL_PATH, _ORIGINAL_WAS_DEFAULT, _LEAKED = "", "", True, None


def _restore_original_config():
    """Put the user's config path back. Safe to call repeatedly."""
    if _ORIGINAL_WAS_DEFAULT or not _ORIGINAL_PATH:
        subprocess.run([MACROWHISPER, "--reset-config"], capture_output=True, timeout=15)
    else:
        subprocess.run(
            [MACROWHISPER, "--set-config", _ORIGINAL_PATH],
            capture_output=True,
            timeout=15,
        )


_SAFETY_NET_ARMED = False


def _arm_interrupt_safety_net():
    """Restore the config path even when the run is KILLED rather than finished.

    `addCleanup` covers a passing test and `tearDownModule` covers a failing run.
    Neither runs on a signal, and this file's own docstring has listed "crash,
    SIGTERM, kill before tearDownModule" as a leak cause since 2026-08-06 while
    defending against it only after the fact, by detecting the leak on the NEXT
    run. That backstop is good and stays; it is a poor primary defence because
    "the next run" can be days away.

    It bit for real on 2026-08-15: a run was interrupted partway through, leaving
    macrowhisper's persisted path at a temp config that was then deleted. Because
    macrowhisper defaults `simEsc` to TRUE and `moveTo` to empty, the machine was
    left posting an Escape into whatever app had focus, which is the one setting
    this project treats as destructive. `doctor` caught it, which is what it is
    for, but nothing should have needed catching.

    Armed lazily, from `_start_daemon`, so the blast radius is exactly the moment
    the risk is created: a default run that skips these tests never installs a
    handler and never shells out to macrowhisper.

    Both mechanisms are used because neither covers the other's cases. `atexit`
    handles a normal unwind, `sys.exit`, an unhandled exception, and a
    KeyboardInterrupt that propagates; it does NOT run when the process is
    terminated by a signal. The handlers cover that. Double-restoring is
    harmless: `_restore_original_config` is idempotent by construction.
    """
    global _SAFETY_NET_ARMED
    if _SAFETY_NET_ARMED or not (MACROWHISPER and ENABLED):
        return
    _SAFETY_NET_ARMED = True

    atexit.register(_restore_original_config)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            previous = signal.getsignal(sig)
        except (ValueError, OSError, AttributeError):  # pragma: no cover
            continue

        def _handler(signum, frame, _previous=previous):
            # Restore FIRST. Whatever we delegate to may not come back.
            _restore_original_config()
            if callable(_previous):
                # unittest installs its own SIGINT handler for graceful stop, and
                # Python's default_int_handler raises KeyboardInterrupt. Chaining
                # preserves both rather than replacing them.
                _previous(signum, frame)
                return
            # SIG_DFL or SIG_IGN: an int, not a function. Re-raise with the
            # default disposition so the process dies the way the caller asked.
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):  # pragma: no cover - not on the main thread
            pass


def tearDownModule():
    """Last line of defence, and the one that makes a leak impossible to miss.

    `macrowhisper --config <path>` persists that path for future runs, so a
    test that leaks leaves the user's real macrowhisper pointing at a temp
    directory that no longer exists. Nothing would surface that until they next
    started it and it failed to find its config.

    This happened for real during development: a run left the saved path at a
    deleted /var/folders temp dir. The per-test cleanup could not be made to
    reproduce it, so the guarantee is enforced here instead, unconditionally
    and with a loud failure rather than a silent one.
    """
    if not (MACROWHISPER and ENABLED):
        return
    _restore_original_config()
    now = _get_config_path()

    # The check that actually catches a leak. "Did the path change" cannot: a
    # leak inherited from an earlier run is equal to itself and passes silently.
    # "Is the path sane" holds regardless of what we captured at import.
    if looks_like_leaked_temp_path(now):
        raise RuntimeError(
            "INTEGRATION TESTS LEFT YOUR MACROWHISPER POINTED AT A TEMP CONFIG.\n"
            f"  saved path: {now}\n"
            "That directory is scratch space and will be deleted. Your daemon\n"
            "would silently watch nothing and discard every dictation.\n"
            "Fix with: macrowhisper --reset-config"
        )

    if _ORIGINAL_PATH and now != _ORIGINAL_PATH:
        raise RuntimeError(
            "INTEGRATION TESTS LEAKED YOUR MACROWHISPER CONFIG PATH.\n"
            f"  expected: {_ORIGINAL_PATH}\n"
            f"  actual:   {now}\n"
            f"Fix with: macrowhisper "
            + ("--reset-config" if _ORIGINAL_WAS_DEFAULT else f"--set-config {_ORIGINAL_PATH}")
        )


def _poll(predicate, timeout_s, interval_s=0.1):
    """Wait for predicate() to return a truthy value. Returns it, or None."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval_s)
    return None


@unittest.skipUnless(ENABLED and MACROWHISPER, SKIP_REASON)
class RealMacrowhisperTestCase(unittest.TestCase):
    """Base: a temp watch dir, a temp config, and a live macrowhisper on top."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        root = Path(self._tmp.name)
        self.watch = root / "mw-bridge"
        (self.watch / "recordings").mkdir(parents=True)
        self.fired_log = self.watch / "fired.log"
        self.config_path = root / "macrowhisper.json"
        self.config_path.write_text(json.dumps(self._config()), encoding="utf-8")

        self._save_and_restore_config_path()
        self._start_daemon()

    def _config(self):
        return {
            "defaults": {
                "watch": str(self.watch),
                "activeAction": "markerLog",
                # .none so a failing test leaves the meta.json behind to inspect.
                "moveTo": ".none",
                "actionDelay": 0.05,
                "history": 0,
                "muteNotifications": True,
            },
            "scriptsShell": {
                "markerLog": {
                    "action": (
                        "printf '%s\\n' '{{swResult}}' >> " + str(self.fired_log)
                    ),
                    "scriptAsync": False,
                }
            },
        }

    def _save_and_restore_config_path(self):
        """macrowhisper --config persists. Put the user's setting back after
        every test, and again in tearDownModule as the enforced backstop."""
        self.addCleanup(_restore_original_config)

    def _start_daemon(self):
        # Arm BEFORE the daemon persists the temp path, not after: the window
        # between the two is exactly what an interrupt exploits.
        _arm_interrupt_safety_net()
        proc = subprocess.Popen(
            [MACROWHISPER, "--config", str(self.config_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        def stop():
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
            finally:
                # Popen does not close the pipes it opened. Leaving them dangling
                # emits ResourceWarnings and leaks a descriptor per daemon, which
                # a longer run would eventually feel.
                if proc.stdout:
                    proc.stdout.close()

        self.addCleanup(stop)
        self.daemon = proc

        if not _poll(self._watcher_is_armed, READY_TIMEOUT_S):
            self.fail(
                f"macrowhisper's recordings watcher did not arm within {READY_TIMEOUT_S}s.\n"
                f"last status:\n{self._status_text()}\ndaemon:\n{self._daemon_output()}"
            )

    def _stop_daemon_and_wait(self):
        """Stop this test's daemon and wait until nothing answers the socket.

        Polling for the sentinel rather than sleeping: process exit and socket
        release are not the same instant, and a test that raced that gap would
        fail intermittently in a way that looks like the liveness check being
        wrong when it is the test being impatient.
        """
        self.daemon.terminate()
        try:
            self.daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            self.daemon.kill()
            self.daemon.wait(timeout=10)

        if not _poll(lambda: NOT_RUNNING_SENTINEL in self._status_text(), 8.0):
            # Measured 2026-08-15 on a machine with the service installed: the
            # launchd agent carries KeepAlive, so the user's own daemon reclaims
            # the shared socket within a second of ours dying and --status keeps
            # answering. There is then no way to observe "not running" without
            # stopping the developer's real macrowhisper, which a test must never
            # do. Skip rather than fail: the condition is the environment, not
            # the code, and a red suite that means "you have macrowhisper
            # installed properly" trains people to ignore red.
            self.skipTest(
                "another macrowhisper holds the socket (almost certainly your "
                "own launchd service, which KeepAlive restarts), so an outage "
                "cannot be simulated without stopping it. Run "
                "`macrowhisper --stop-service` first to exercise this test."
            )

    def _status_text(self):
        return subprocess.run(
            [MACROWHISPER, "--status"], capture_output=True, text=True, timeout=10
        ).stdout

    def _watcher_is_armed(self):
        """Readiness, done properly. Two traps make the obvious check wrong.

        First, `macrowhisper --status` exits 0 even when nothing is running (it
        prints "macrowhisper is not running"), so polling on the return code
        succeeds instantly and proves nothing.

        Second, and worse: the recordings watcher marks every folder that
        already exists when it arms as processed, silently. Publishing into the
        gap between spawn and arm produces no action and no error, so a test
        that races the daemon fails looking exactly like a broken bridge. Wait
        for "armed", and confirm the instance is ours: the CLI talks to whatever
        daemon owns the shared socket, which could be the user's own.
        """
        status = self._status_text()
        return "Recordings watcher: yes (armed" in status and str(
            self.config_path
        ) in status

    def _daemon_output(self):
        if self.daemon.poll() is None:
            return "(daemon still running; output not drained)"
        return (self.daemon.stdout.read() if self.daemon.stdout else "") or "(no output)"

    def fired_lines(self):
        if not self.fired_log.exists():
            return []
        text = self.fired_log.read_text(encoding="utf-8")
        return [l for l in text.split("\n") if l.strip()]

    def wait_for_fires(self, count):
        got = _poll(
            lambda: self.fired_lines() if len(self.fired_lines()) >= count else None,
            FIRE_TIMEOUT_S,
        )
        return got or self.fired_lines()

    def run_macrovoice(self, transcript, *args, gap="0.05"):
        env = dict(os.environ)
        env["VOICEINK_TRANSCRIPT"] = transcript
        return subprocess.run(
            [
                sys.executable, "-m", "macrovoice",
                "--watch", str(self.watch),
                "--gap", gap,
                *args,
            ],
            input="",
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=env,
            timeout=60,
        )


class Gate1SyntheticFolderTest(RealMacrowhisperTestCase):
    """Gate 1: macrowhisper acts on a hand-crafted recording folder.

    No macrovoice in the loop at all. This isolates the downstream half of the
    bridge, so a failure here means macrowhisper's contract changed, not that
    our publisher broke.
    """

    def test_handcrafted_meta_json_fires_the_action(self):
        folder = self.watch / "recordings" / f"{time.time_ns()}-000"
        staging = self.watch / ".handmade"
        staging.mkdir()
        (staging / "meta.json").write_text(
            json.dumps({"result": "gate one lives", "modeName": "test"}),
            encoding="utf-8",
        )
        # Directory-level rename, matching how the bridge publishes: meta.json is
        # already inside the moment the folder becomes visible.
        staging.rename(folder)

        lines = self.wait_for_fires(1)
        self.assertEqual(
            lines, ["gate one lives"],
            f"macrowhisper did not act on a valid synthetic folder.\n"
            f"daemon:\n{self._daemon_output()}",
        )


class Gate3MacrovoiceDrivesMacrowhisperTest(RealMacrowhisperTestCase):
    """Gate 3: the real adapter, end to end, minus VoiceInk itself."""

    def test_macrovoice_publish_fires_the_action(self):
        result = self.run_macrovoice("gate three lives")
        self.assertEqual(result.returncode, 0, result.stderr)

        lines = self.wait_for_fires(1)
        self.assertEqual(
            lines, ["gate three lives"],
            f"macrovoice published but macrowhisper did not act.\n"
            f"daemon:\n{self._daemon_output()}",
        )

    def test_unicode_survives_the_round_trip(self):
        # The escaping matrix is unit-tested; this proves it survives the real
        # JSON write, the real watcher and macrowhisper's own placeholder
        # substitution, which is where a naive shell implementation would break.
        transcript = 'quotes " backslash \\ accent café emoji 🎙 done'
        result = self.run_macrovoice(transcript)
        self.assertEqual(result.returncode, 0, result.stderr)

        lines = self.wait_for_fires(1)
        self.assertEqual(len(lines), 1, f"expected one fire, got {lines}")
        got = lines[0]

        # Shell-type actions get shell-safe escaping applied by macrowhisper
        # (Placeholders.swift), so the quote arrives backslashed. Assert on the
        # parts that must survive verbatim rather than on the whole string.
        self.assertIn("🎙", got)
        self.assertIn("backslash", got)
        # Accented text needs normalising first; see the dedicated test below.
        self.assertIn("café", unicodedata.normalize("NFC", got))

    def test_macrowhisper_returns_accented_text_as_nfd(self):
        """Measured behaviour, not a preference: the far end denormalises.

        macrovoice writes `meta.json` in NFC, byte-faithful to what it was
        given. What comes back out of macrowhisper's shell action is NFD, so
        `café` arrives as `e` + U+0301 rather than U+00E9. Visually identical,
        different bytes, and unequal under a naive string comparison.

        Pinned here because it is invisible until it bites: an action that
        greps, diffs or dedupes dictated text would silently miss matches on
        accented words. It also localises the blame correctly if this surfaces
        later, since the meta.json assertion proves the bridge is not the one
        changing the form.

        Scope: this measures the SHELL action path only. Insert/autoPaste
        actions take a different route through Placeholders.swift and are not
        covered until Gate 4 runs with a human at the Mac.
        """
        result = self.run_macrovoice("café")
        self.assertEqual(result.returncode, 0, result.stderr)

        lines = self.wait_for_fires(1)
        self.assertEqual(len(lines), 1, f"expected one fire, got {lines}")
        got = lines[0]

        published = sorted((self.watch / "recordings").iterdir())
        written = json.loads(
            (published[0] / "meta.json").read_text(encoding="utf-8")
        )["result"]

        self.assertTrue(
            unicodedata.is_normalized("NFC", written),
            f"the bridge itself should write NFC, got {written!r}",
        )
        self.assertEqual(
            unicodedata.normalize("NFC", got), "café",
            "text must survive the round trip up to normalisation",
        )
        self.assertTrue(
            unicodedata.is_normalized("NFD", got),
            "macrowhisper's shell path returned something other than NFD. If "
            "this now returns NFC, the downstream denormalisation was fixed "
            "upstream, and this test should be updated to match.",
        )


class ConcurrentPublishRegressionTest(RealMacrowhisperTestCase):
    """The regression that matters most: five at once, five fired.

    This is the older-name-rejection hazard (RecordingsFolderWatcher.swift:350),
    which the code audit did NOT predict and only appeared when the experiment
    ran. Before the fix, 5 concurrent dictations produced 5 folders on disk and 4
    actions, losing one silently. It is the single behaviour most likely to
    regress unnoticed, because it needs a real watcher to reproduce.
    """

    def test_five_concurrent_dictations_all_fire(self):
        procs = []
        for i in range(5):
            env = dict(os.environ)
            env["VOICEINK_TRANSCRIPT"] = f"dictation {i}"
            procs.append(
                subprocess.Popen(
                    [
                        sys.executable, "-m", "macrovoice",
                        "--watch", str(self.watch),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=str(REPO_ROOT),
                    env=env,
                )
            )
        for p in procs:
            p.wait(timeout=90)
            # Close the pipes Popen opened, or each publisher leaks two
            # descriptors and the run emits ResourceWarnings.
            for stream in (p.stdout, p.stderr):
                if stream:
                    stream.close()

        # Anything the gap deferred is still spooled; drain it before asserting.
        # The gap MUST match what the publishers used. Draining with a smaller
        # one republishes the backlog in a tight burst and macrowhisper silently
        # drops it, which is precisely the failure the gap exists to prevent.
        # Getting this wrong cost a debugging round: an early version of this
        # test drained at 0.05s, published three folders 50ms apart, and lost
        # two of them. It is the sharpest available demonstration of why
        # erring small on --gap is the dangerous direction.
        self.run_macrovoice("", "--drain-only", gap=str(DEFAULT_GAP_S))

        lines = self.wait_for_fires(5)

        # Distinguish the two very different failures this test can see:
        #   - published but not acted on  -> a macrowhisper silent drop
        #   - never published             -> macrovoice ran out of drain budget
        # The original message showed only the recordings dir, which cannot tell
        # them apart and sent one debugging round down the wrong path.
        spooled = sorted(p.name for p in (self.watch / ".spool").iterdir())
        published = sorted(p.name for p in (self.watch / "recordings").iterdir())
        adapter_log = (self.watch / "macrovoice.log")
        adapter = adapter_log.read_text(encoding="utf-8") if adapter_log.exists() else "(none)"

        self.assertEqual(
            sorted(lines),
            sorted(f"dictation {i}" for i in range(5)),
            "silent drop regression: macrowhisper did not act on every published "
            f"folder.\npublished on disk ({len(published)}): {published}\n"
            f"STILL SPOOLED ({len(spooled)}): {spooled}\n"
            f"macrovoice.log:\n{adapter}\n"
            f"daemon:\n{self._daemon_output()}",
        )


class XmlPlaceholderFallbackTest(RealMacrowhisperTestCase):
    """F5: `{{xml:tag}}` extracts, but `{{swResult}}` keeps the raw markup.

    This asymmetry is specific to the bridge and cannot be hit upstream. In
    Placeholders.swift:1759-1780, processAllPlaceholders branches on which field
    carries the text:

        if llmResult non-empty:                       <- Superwhisper with an LLM
            let (cleaned, tags) = processXmlPlaceholders(...)
            updatedMetaJson["llmResult"] = cleaned     <- cleaned text written BACK
        else if result non-empty:                     <- ALWAYS the bridge
            let (_, tags) = processXmlPlaceholders(...)
            ...                                        <- cleaned text DISCARDED

    The bridge deliberately never sets llmResult (setting it would flip
    macrowhisper's validation gate to require a non-empty llmResult, which we
    cannot honestly supply), so the second branch is the only one we ever take.
    The tag content still reaches {{xml:note}}, but {{swResult}} is read from
    metaJson["result"] (RecordingsFolderWatcher.swift:868,
    `llmResult ?? result ?? ""`), which was never updated, so the markup
    survives into it.

    Verified against the source AND live here, because it was previously a
    code-reasoned claim about someone else's code, and this project's own
    history is a list of code-reasoned claims that turned out wrong.
    """

    TAG_TEXT = "buy milk"
    TRANSCRIPT = "before <note>%s</note> after" % TAG_TEXT

    def _config(self):
        config = super()._config()
        # Both placeholders in ONE action. The extraction path only runs at all
        # when the action requests a tag (processXmlPlaceholders returns early
        # on an empty requestedTags), so {{swResult}} alone would prove nothing.
        config["scriptsShell"]["markerLog"]["action"] = (
            "printf 'TAG=%s|SW=%s\\n' '{{xml:note}}' '{{swResult}}' >> "
            + str(self.fired_log)
        )
        return config

    def test_xml_tag_is_extracted_but_swresult_keeps_the_markup(self):
        result = self.run_macrovoice(self.TRANSCRIPT)
        self.assertEqual(result.returncode, 0, result.stderr)

        lines = self.wait_for_fires(1)
        self.assertTrue(lines, "macrowhisper never fired.\n%s" % self._daemon_output())
        line = lines[0]

        tag_part, _, sw_part = line.partition("|SW=")
        tag = tag_part.replace("TAG=", "", 1)

        self.assertEqual(
            tag, self.TAG_TEXT,
            "the tag content should be extracted cleanly into {{xml:note}}. got: %r" % line,
        )
        self.assertIn(
            "<note>", sw_part,
            "REGRESSION OR UPSTREAM FIX: {{swResult}} no longer carries the raw "
            "markup. If macrowhisper started writing the cleaned text back to "
            "`result` as well as `llmResult`, this trap is gone and the README "
            "section documenting it must be removed. got: %r" % line,
        )
        self.assertIn(self.TAG_TEXT, sw_part, "got: %r" % line)


class PresetSearchTriggerTest(RealMacrowhisperTestCase):
    """N3: the preset searches we ship must actually route to their own action.

    The trigger strings come from the SHIPPED macrowhisper.sample.json rather
    than being restated here, so this fails if the file drifts. Each url action
    is rebuilt as a SHELL action carrying the same triggerVoice, because the real
    ones open a browser and a test suite must not. What is under test is
    macrowhisper's trigger SELECTION, which is the subtle part; the URL strings
    themselves are covered by tests/test_sample_config.py.

    The subtlety being pinned: triggers are prefix-anchored per alternative
    (TriggerEvaluator.swift:205 builds "^(?i)" + escaped pattern), so a bare
    "youtube" alternative does NOT match "ask youtube best pizza". Shipping only
    the bare word would leave every preset silently falling through to the
    default action, which is the failure shape this project keeps meeting.
    """

    SAMPLE_URLS = json.loads(
        (REPO_ROOT / "macrowhisper.sample.json").read_text(encoding="utf-8")
    )["urls"]

    def _config(self):
        config = super()._config()
        for name, action in self.SAMPLE_URLS.items():
            config["scriptsShell"][name] = {
                "action": (
                    "printf 'ROUTED=" + name + "|TEXT=%s\\n' "
                    "'{{swResult}}' >> " + str(self.fired_log)
                ),
                "triggerVoice": action["triggerVoice"],
                "scriptAsync": False,
            }
        return config

    def test_each_preset_trigger_routes_to_its_own_action(self):
        expected = []
        for index, name in enumerate(self.SAMPLE_URLS):
            phrase = "ask %s best pizza in madrid" % name
            result = self.run_macrovoice(phrase)
            self.assertEqual(result.returncode, 0, result.stderr)
            expected.append(name)
            lines = self.wait_for_fires(index + 1)
            self.assertGreaterEqual(
                len(lines), index + 1,
                "'%s' produced no action.\nlines so far: %s\ndaemon:\n%s"
                % (phrase, lines, self._daemon_output()),
            )

        lines = self.wait_for_fires(len(expected))
        routed = [l.split("|")[0].replace("ROUTED=", "", 1) for l in lines]
        self.assertEqual(
            routed, expected,
            "each 'ask <name> ...' must route to that name's action. got:\n%s"
            % "\n".join(lines),
        )

    def test_the_trigger_phrase_is_stripped_from_the_text(self):
        """macrowhisper removes the matched trigger and capitalises the rest, so
        the search query must not contain the words 'ask youtube'. If it did,
        every preset would search for its own trigger phrase."""
        self.run_macrovoice("ask youtube best pizza in madrid")
        lines = self.wait_for_fires(1)
        self.assertTrue(lines, self._daemon_output())
        text = lines[0].split("TEXT=", 1)[1]
        self.assertNotIn("ask youtube", text.lower(), "trigger not stripped: %r" % lines[0])
        self.assertIn("best pizza in madrid", text.lower(), "got: %r" % lines[0])


class LivenessAgainstARealDaemonTest(RealMacrowhisperTestCase):
    """G3's one remaining unproven link: that a REAL stopped macrowhisper says so.

    Everything else about the liveness check is covered without a daemon:
    listener.py's three states, Publisher's deferral, and the CLI end to end with
    a stub binary on PATH. What a stub cannot prove is that macrowhisper's actual
    output contains the sentinel we match on, which is the one thing that would
    silently break if upstream reworded it. The check would fail open forever and
    look like it worked.

    A NOTE ON WHAT THIS CHECK ACTUALLY ANSWERS, worth knowing before trusting it:
    `macrowhisper --status` talks to whatever daemon owns the shared socket, so
    the probe answers "is a macrowhisper listening", not "is the macrowhisper
    watching MY directory listening". For the real deployment, one daemon and one
    watch root, those are the same question. They come apart only under this test
    suite, which is why this test kills its own daemon rather than reasoning
    about watch roots.
    """

    def test_the_real_sentinel_is_what_listener_matches_on(self):
        from macrovoice.listener import NOT_RUNNING_SENTINEL, is_listening

        # Alive: the daemon started in setUp owns the socket.
        self.assertIs(is_listening(), True, self._status_text())

        self._stop_daemon_and_wait()

        status = self._status_text()
        self.assertIn(
            NOT_RUNNING_SENTINEL, status,
            "macrowhisper no longer prints the sentence the liveness check "
            "matches on. The check now fails open permanently and silently.\n"
            "got:\n%s" % status,
        )
        self.assertIs(is_listening(), False)

    def test_a_dictation_during_an_outage_is_spooled_and_then_delivered(self):
        """The whole point of G3, against a real daemon.

        Without the check, the folder published during the outage is not merely
        late: the watcher marks every folder that already exists when it arms as
        processed, so the restart that should deliver it destroys it instead.
        """
        self._stop_daemon_and_wait()

        result = self.run_macrovoice("spoken into the void")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            list((self.watch / "recordings").iterdir()), [],
            "published into a folder the next arm would have discarded",
        )
        spooled = list((self.watch / ".spool").iterdir())
        self.assertEqual(len(spooled), 1, "the transcript is not in the spool either")

        self._start_daemon()
        drain = self.run_macrovoice("", "--drain-only")
        self.assertEqual(drain.returncode, 0, drain.stderr)

        lines = self.wait_for_fires(1)
        self.assertEqual(
            lines, ["spoken into the void"],
            "the deferred transcript never arrived after the daemon came back.\n"
            "daemon:\n%s" % self._daemon_output(),
        )


if __name__ == "__main__":
    unittest.main()
