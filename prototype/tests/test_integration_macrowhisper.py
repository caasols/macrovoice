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

import json
import os
import shutil
import subprocess
import sys
import time
import unicodedata
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROTOTYPE = Path(__file__).resolve().parent.parent

from macrovoice.publisher import DEFAULT_MIN_GAP_S as DEFAULT_GAP_S  # noqa: E402

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


# Captured ONCE, at import, before any test can touch it. Restoring from a
# per-test snapshot is not good enough: if a test leaks, every later test
# snapshots the leaked value and faithfully restores the wrong thing.
if MACROWHISPER and ENABLED:
    _ORIGINAL_RAW = subprocess.run(
        [MACROWHISPER, "--get-config"], capture_output=True, text=True, timeout=15
    ).stdout.strip()
    _ORIGINAL_PATH = (
        _ORIGINAL_RAW.split(":", 1)[1].strip() if ":" in _ORIGINAL_RAW else ""
    )
    _ORIGINAL_WAS_DEFAULT = _is_default_config(_ORIGINAL_RAW)
else:
    _ORIGINAL_RAW, _ORIGINAL_PATH, _ORIGINAL_WAS_DEFAULT = "", "", True


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

        self.addCleanup(stop)
        self.daemon = proc

        if not _poll(self._watcher_is_armed, READY_TIMEOUT_S):
            self.fail(
                f"macrowhisper's recordings watcher did not arm within {READY_TIMEOUT_S}s.\n"
                f"last status:\n{self._status_text()}\ndaemon:\n{self._daemon_output()}"
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
            cwd=str(PROTOTYPE),
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
                    cwd=str(PROTOTYPE),
                    env=env,
                )
            )
        for p in procs:
            p.wait(timeout=90)

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
        self.assertEqual(
            sorted(lines),
            sorted(f"dictation {i}" for i in range(5)),
            "silent drop regression: macrowhisper did not act on every published "
            f"folder.\npublished on disk: "
            f"{sorted(p.name for p in (self.watch / 'recordings').iterdir())}\n"
            f"daemon:\n{self._daemon_output()}",
        )


if __name__ == "__main__":
    unittest.main()
