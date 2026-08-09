"""Value types for doctor. Pure, so these tests need no Mac state at all."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from macrovoice.doctor.model import (  # noqa: E402
    Check,
    Context,
    Finding,
    Outcome,
    Result,
    Severity,
)


class TestFinding(unittest.TestCase):
    def test_ok_carries_no_blocker(self):
        finding = Finding.ok("python3 is 3.9.6")
        self.assertIs(finding.outcome, Outcome.OK)
        self.assertEqual(finding.detail, "python3 is 3.9.6")
        self.assertIsNone(finding.blocked_by)

    def test_problem_keeps_the_fix_hint(self):
        finding = Finding.problem("not on PATH", "brew install ognistik/formulae/macrowhisper")
        self.assertIs(finding.outcome, Outcome.PROBLEM)
        self.assertEqual(finding.fix_hint, "brew install ognistik/formulae/macrowhisper")

    def test_unknown_records_the_blocking_check(self):
        finding = Finding.unknown(blocked_by="mw.running")
        self.assertIs(finding.outcome, Outcome.UNKNOWN)
        self.assertEqual(finding.blocked_by, "mw.running")

    def test_unknown_without_a_blocker_is_allowed(self):
        # A check can be unable to tell on its own, with nothing blocking it:
        # an unrecognised --status output, for instance.
        finding = Finding.unknown("could not parse macrowhisper --status")
        self.assertIsNone(finding.blocked_by)

    def test_findings_are_immutable(self):
        finding = Finding.ok()
        with self.assertRaises(Exception):
            finding.detail = "mutated"


class TestCheck(unittest.TestCase):
    def test_depends_on_defaults_to_empty(self):
        check = Check(
            id="pre.python",
            title="python3 is 3.9 or newer",
            severity=Severity.FAIL,
            inspect=lambda ctx: Finding.ok(),
        )
        self.assertEqual(check.depends_on, ())

    def test_inspect_receives_the_context(self):
        seen = []
        check = Check(
            id="x",
            title="x",
            severity=Severity.WARN,
            inspect=lambda ctx: seen.append(ctx) or Finding.ok(),
        )
        ctx = Context(watch_root=Path("/tmp/w"), mw=object(), bridge=object())
        result = Result(check, check.inspect(ctx))
        self.assertIs(seen[0], ctx)
        self.assertIs(result.finding.outcome, Outcome.OK)


if __name__ == "__main__":
    unittest.main()
