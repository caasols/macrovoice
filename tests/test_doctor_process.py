"""run_command's failure contract, driven through REAL subprocesses.

Every other adapter test injects a fake runner, so this is the only place the
real one is exercised. The contract is load-bearing and is stated in
process.py's own docstring: a hung subprocess must become an UNKNOWN-shaped
result, never a hang, because a diagnostic that can block forever is worse than
no diagnostic. macrovoice has already lost a transcript to an unbounded read
(B5, closed 2026-08-09), so this is not theoretical.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from macrovoice.doctor.adapters.process import run_command  # noqa: E402


class TestRunCommand(unittest.TestCase):
    def test_a_successful_command_reports_ok_with_its_output(self):
        result = run_command(["/bin/echo", "hello"], 10.0)
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout.strip(), "hello")
        self.assertFalse(result.timed_out)

    def test_a_failing_command_is_not_ok_but_does_not_raise(self):
        result = run_command(["/bin/sh", "-c", "exit 3"], 10.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 3)
        self.assertFalse(result.timed_out)

    def test_a_hanging_command_times_out_instead_of_hanging(self):
        result = run_command(["/bin/sleep", "5"], 0.2)
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.returncode)
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.stderr)

    def test_a_missing_binary_is_reported_rather_than_raised(self):
        result = run_command(["/nonexistent/definitely-not-a-binary"], 10.0)
        self.assertIsNone(result.returncode)
        self.assertFalse(result.ok)
        self.assertFalse(result.timed_out)
        self.assertTrue(result.stderr, "the OSError message should reach the caller")


if __name__ == "__main__":
    unittest.main()
