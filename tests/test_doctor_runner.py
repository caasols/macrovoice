"""The runner: ordering, blocking, and containment.

The central behaviour is that a check whose dependency is not OK reports
UNKNOWN naming the blocker, and is NEVER counted as a failure. A bare machine
must print one problem and a run of unknowns, not sixteen alarms.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from macrovoice.doctor.model import Check, Context, Finding, Outcome, Severity  # noqa: E402
from macrovoice.doctor.runner import order_checks, run  # noqa: E402


def check(id_, inspect, depends_on=()):
    return Check(
        id=id_, title=id_, severity=Severity.FAIL, inspect=inspect, depends_on=depends_on
    )


def ctx():
    return Context(watch_root=Path("/tmp/w"), mw=object(), bridge=object())


class TestOrdering(unittest.TestCase):
    def test_dependencies_run_first(self):
        checks = [
            check("b", lambda c: Finding.ok(), depends_on=("a",)),
            check("a", lambda c: Finding.ok()),
        ]
        self.assertEqual([c.id for c in order_checks(checks)], ["a", "b"])

    def test_declaration_order_is_preserved_among_equals(self):
        checks = [check("a", lambda c: Finding.ok()), check("b", lambda c: Finding.ok())]
        self.assertEqual([c.id for c in order_checks(checks)], ["a", "b"])

    def test_a_cycle_is_a_table_bug_and_raises(self):
        checks = [
            check("a", lambda c: Finding.ok(), depends_on=("b",)),
            check("b", lambda c: Finding.ok(), depends_on=("a",)),
        ]
        with self.assertRaises(ValueError):
            order_checks(checks)

    def test_an_unknown_dependency_is_a_table_bug_and_raises(self):
        checks = [check("a", lambda c: Finding.ok(), depends_on=("nope",))]
        with self.assertRaises(ValueError):
            order_checks(checks)

    def test_a_duplicate_check_id_is_a_table_bug_and_raises(self):
        # Two rows sharing an id would make one check silently shadow the other.
        checks = [check("a", lambda c: Finding.ok()), check("a", lambda c: Finding.ok())]
        with self.assertRaises(ValueError):
            order_checks(checks)


class TestBlocking(unittest.TestCase):
    def test_a_dependent_of_a_problem_is_unknown_not_a_failure(self):
        checks = [
            check("a", lambda c: Finding.problem("down")),
            check("b", lambda c: Finding.ok(), depends_on=("a",)),
        ]
        results = {r.check.id: r.finding for r in run(checks, ctx())}
        self.assertIs(results["b"].outcome, Outcome.UNKNOWN)
        self.assertEqual(results["b"].blocked_by, "a")

    def test_blocking_is_transitive(self):
        checks = [
            check("a", lambda c: Finding.problem("down")),
            check("b", lambda c: Finding.ok(), depends_on=("a",)),
            check("c", lambda c: Finding.ok(), depends_on=("b",)),
        ]
        results = {r.check.id: r.finding for r in run(checks, ctx())}
        self.assertIs(results["c"].outcome, Outcome.UNKNOWN)
        self.assertEqual(results["c"].blocked_by, "b")

    def test_a_blocked_check_never_runs(self):
        ran = []
        checks = [
            check("a", lambda c: Finding.problem("down")),
            check("b", lambda c: ran.append(1) or Finding.ok(), depends_on=("a",)),
        ]
        run(checks, ctx())
        self.assertEqual(ran, [])

    def test_an_unknown_dependency_also_blocks(self):
        checks = [
            check("a", lambda c: Finding.unknown("cannot tell")),
            check("b", lambda c: Finding.ok(), depends_on=("a",)),
        ]
        results = {r.check.id: r.finding for r in run(checks, ctx())}
        self.assertIs(results["b"].outcome, Outcome.UNKNOWN)


class TestContainment(unittest.TestCase):
    def test_a_raising_check_becomes_unknown_and_does_not_stop_the_run(self):
        def boom(c):
            raise RuntimeError("kaboom")

        checks = [check("a", boom), check("b", lambda c: Finding.ok())]
        results = {r.check.id: r.finding for r in run(checks, ctx())}
        self.assertIs(results["a"].outcome, Outcome.UNKNOWN)
        self.assertIn("RuntimeError", results["a"].detail)
        self.assertIs(results["b"].outcome, Outcome.OK)


if __name__ == "__main__":
    unittest.main()
