"""Rendering and exit codes. The wording IS the deliverable here, so it is
golden-file tested like any other output."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from macrovoice.doctor.model import Check, Finding, Result, Severity  # noqa: E402
from macrovoice.doctor.report import exit_code, render  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "doctor" / "report"


def result(id_, finding, severity=Severity.FAIL, title=None):
    check = Check(
        id=id_,
        title=title or id_,
        severity=severity,
        inspect=lambda ctx: finding,
    )
    return Result(check, finding)


class TestExitCode(unittest.TestCase):
    def test_all_ok_is_zero(self):
        self.assertEqual(exit_code([result("a", Finding.ok())]), 0)

    def test_a_fail_problem_is_one(self):
        self.assertEqual(exit_code([result("a", Finding.problem("bad"))]), 1)

    def test_a_warn_problem_alone_is_zero(self):
        results = [result("a", Finding.problem("meh"), severity=Severity.WARN)]
        self.assertEqual(exit_code(results), 0)

    def test_a_fail_unknown_is_two(self):
        self.assertEqual(exit_code([result("a", Finding.unknown("cannot tell"))]), 2)

    def test_a_problem_outranks_an_unknown(self):
        results = [
            result("a", Finding.problem("bad")),
            result("b", Finding.unknown("cannot tell")),
        ]
        self.assertEqual(exit_code(results), 1)


class TestRender(unittest.TestCase):
    def test_matches_the_golden_file(self):
        results = [
            result("pre.python", Finding.ok("python3 is 3.9.6"), title="python3 is 3.9 or newer"),
            result(
                "pre.macrowhisper",
                Finding.problem(
                    "not found on PATH", "brew install ognistik/formulae/macrowhisper"
                ),
                title="macrowhisper is installed",
            ),
            result(
                "mw.simesc",
                Finding.unknown(blocked_by="mw.running"),
                title="simEsc is off",
            ),
            result(
                "bridge.spool",
                Finding.problem("2 transcripts waiting in the spool"),
                severity=Severity.WARN,
                title="the spool is empty",
            ),
        ]
        self.assertEqual(render(results), (GOLDEN / "mixed.txt").read_text(encoding="utf-8"))

    def test_no_emoji_and_no_em_dashes_anywhere(self):
        results = [result("pre.python", Finding.ok("fine"))]
        rendered = render(results)
        self.assertNotIn("—", rendered)  # an em-dash, which house rules forbid
        self.assertTrue(all(ord(ch) < 0x2500 for ch in rendered))


if __name__ == "__main__":
    unittest.main()
