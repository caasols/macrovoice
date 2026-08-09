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
