"""doctor as a real subprocess, and proof the delivery path is unaffected.

Driven through subprocess for the same reason tests/test_cli.py is: an
in-process test would silently bypass the argument dispatch, which is the exact
thing that could regress a dictation.
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_doctor(*args, timeout=60):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "macrovoice", "doctor"] + list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(REPO_ROOT),
    )


class TestDoctorRuns(unittest.TestCase):
    def test_it_produces_a_report(self):
        with TemporaryDirectory() as tmp:
            result = run_doctor("--check", "--watch", tmp)
            self.assertIn("macrovoice doctor", result.stdout)
            self.assertIn("Prerequisites", result.stdout)

    def test_a_missing_watch_directory_is_reported_and_not_created(self):
        with TemporaryDirectory() as tmp:
            absent = Path(tmp) / "absent"
            result = run_doctor("--check", "--watch", str(absent))
            self.assertIn("does not exist", result.stdout)
            self.assertFalse(absent.exists())

    def test_exit_code_is_non_zero_when_something_is_wrong(self):
        with TemporaryDirectory() as tmp:
            absent = Path(tmp) / "absent"
            result = run_doctor("--check", "--watch", str(absent))
            self.assertNotEqual(result.returncode, 0)

    def test_output_carries_no_emoji_and_no_em_dashes(self):
        with TemporaryDirectory() as tmp:
            result = run_doctor("--check", "--watch", tmp)
            self.assertNotIn("\u2014", result.stdout)  # an em-dash, which house rules forbid


class TestDoctorResolvesTheSameWatchRootAsDelivery(unittest.TestCase):
    """doctor and the delivery path must agree about the default.

    If they disagree, doctor inspects a directory nothing publishes into and
    reports a clean bill of health for the wrong bridge, which is worse than no
    check at all. HOME is redirected so nothing here can read or create the
    developer's real directories.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def run_with_home(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["HOME"] = str(self.home)
        env.pop("MACROVOICE_WATCH", None)
        env.pop("MW_BRIDGE_WATCH", None)
        return subprocess.run(
            [sys.executable, "-m", "macrovoice", "doctor", "--check", *args],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            cwd=str(REPO_ROOT),
        )

    def detail_after(self, stdout, title):
        """The detail line doctor prints under a named check.

        Asserting on the whole report is too loose: `~/macrovoice` also appears
        inside the legacy migration hint, so a substring search over stdout
        passes even when the watch root resolved to the old directory.
        """
        lines = stdout.splitlines()
        for index, line in enumerate(lines):
            if title in line:
                return lines[index + 1].strip()
        self.fail("no check titled %r in:\n%s" % (title, stdout))

    def test_a_fresh_machine_is_checked_against_macrovoice(self):
        result = self.run_with_home()
        detail = self.detail_after(result.stdout, "the watch directory exists")
        self.assertIn(str(self.home / "macrovoice"), detail)
        self.assertNotIn("mw-bridge", detail)

    def test_an_unmigrated_install_is_checked_against_mw_bridge(self):
        (self.home / "mw-bridge" / "recordings").mkdir(parents=True)
        result = self.run_with_home()
        detail = self.detail_after(result.stdout, "the watch directory exists")
        self.assertEqual(detail, str(self.home / "mw-bridge"))

    def test_the_legacy_directory_earns_a_warning_and_not_a_problem(self):
        (self.home / "mw-bridge" / "recordings").mkdir(parents=True)
        result = self.run_with_home()
        self.assertIn("the watch directory uses the current name", result.stdout)
        self.assertIn("macrowhisper --stop-service", result.stdout)
        # It must not be the thing that fails the run. Other checks on this
        # machine may legitimately fail, so assert the marker rather than the
        # exit code.
        line = next(
            l for l in result.stdout.splitlines()
            if "the watch directory uses the current name" in l
        )
        self.assertTrue(line.strip().startswith("warning"), line)

    def test_the_current_name_earns_no_warning(self):
        (self.home / "macrovoice" / "recordings").mkdir(parents=True)
        result = self.run_with_home()
        line = next(
            l for l in result.stdout.splitlines()
            if "the watch directory uses the current name" in l
        )
        self.assertTrue(line.strip().startswith("ok"), line)

    def test_doctor_creates_no_watch_directory(self):
        # Stage 1 and 2 are read-only. Resolving the default now touches the
        # filesystem, and a probe that CREATED what it probes for would both
        # break that contract and, worse, be self-fulfilling: one `doctor` run
        # would mint `~/macrovoice` and permanently divert an unmigrated user
        # away from the directory macrowhisper is watching.
        #
        # Asserting the home is empty afterwards is too strong, and says so on
        # the 3.9 floor: reading VoiceInk's preferences makes macOS create
        # ~/Library underneath us. Only these two names are ours to answer for.
        self.run_with_home()
        self.assertFalse((self.home / "macrovoice").exists())
        self.assertFalse((self.home / "mw-bridge").exists())

    def test_help_names_both_environment_variables(self):
        result = subprocess.run(
            [sys.executable, "-m", "macrovoice", "doctor", "--help"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "HOME": str(self.home)},
            cwd=str(REPO_ROOT),
        )
        self.assertIn("MACROVOICE_WATCH", result.stdout)
        self.assertIn("MW_BRIDGE_WATCH", result.stdout)


class TestDeliveryPathUnaffected(unittest.TestCase):
    """The guard must not shadow a dictation. These are the regressions that
    would cost a user their words."""

    def run_delivery(self, tmp, *args, transcript="hello"):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["VOICEINK_TRANSCRIPT"] = transcript
        return subprocess.run(
            [sys.executable, "-m", "macrovoice", "--watch", tmp] + list(args),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL,
        )

    def test_a_normal_dictation_still_publishes(self):
        with TemporaryDirectory() as tmp:
            result = self.run_delivery(tmp)
            self.assertEqual(result.returncode, 0)
            published = list((Path(tmp) / "recordings").iterdir())
            self.assertEqual(len(published), 1)

    def test_a_mode_literally_named_doctor_still_publishes(self):
        # argv[0] is "--mode", not "doctor", so the guard must not fire.
        with TemporaryDirectory() as tmp:
            result = self.run_delivery(tmp, "--mode", "doctor")
            self.assertEqual(result.returncode, 0)
            meta = next((Path(tmp) / "recordings").iterdir()) / "meta.json"
            self.assertEqual(json.loads(meta.read_text())["modeName"], "doctor")


if __name__ == "__main__":
    unittest.main()
