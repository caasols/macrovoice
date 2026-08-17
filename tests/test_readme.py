"""The README states numbers about this suite. Derive them, do not trust them.

Every count the README publishes about itself has drifted at least once. On
2026-08-17 the Tests section claimed 434 tests while the suite had 526, and the
per-file table and the command comment were invalidated by the commit that fixed
the shell-quoting bug, in the same breath as fixing a Troubleshooting row that had
been teaching a bug as expected behaviour. A document that misreports itself is the
same class of defect.

So these are derived from unittest discovery, which is the same mechanism that
produces the real number, rather than restated here where they could agree with
nothing. The cost is deliberate: adding a test now fails this file until the README
is updated. That is the feature. `4e019e7` exists because someone had to go back
and stop a count drifting by hand.

Not pinned here: the coverage percentage. Deriving it needs a full coverage run
with COVERAGE_PROCESS_START and a sitecustomize.py, which is far too expensive for
every commit, and a half-pinned number is worse than an honestly manual one.
"""

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
README = REPO_ROOT / "README.md"

DOCTOR_PREFIX = "test_doctor_"
# The opt-in suite. Its tests are the whole of what a default run skips, which is
# what makes the README's "N skipped" derivable rather than remembered.
OPT_IN_FILE = "test_integration_macrowhisper.py"

NUMBER_WORDS = {
    "One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5,
    "Six": 6, "Seven": 7, "Eight": 8, "Nine": 9, "Ten": 10,
}


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            for leaf in _flatten(item):
                yield leaf
        else:
            yield item


def _will_be_skipped(test):
    """Whether a default run skips this test, without running it.

    unittest.skipUnless evaluates its condition at DECORATION time and sets
    __unittest_skip__ on the class or the method, so the answer is already sitting
    on the object after import. Reading it is a measurement of the real mechanism;
    grepping the file for the word "skipUnless" is not, and matches this very
    module.
    """
    if getattr(test.__class__, "__unittest_skip__", False):
        return True
    method = getattr(test, test._testMethodName, None)
    return bool(getattr(method, "__unittest_skip__", False))


def discovered():
    """{filename: (total tests, tests a default run would skip)}."""
    loader = unittest.TestLoader()
    found = {}
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        suite = loader.discover(
            str(TESTS_DIR), pattern=path.name, top_level_dir=str(TESTS_DIR)
        )
        tests = list(_flatten(suite))
        broken = [t.id() for t in tests if "_FailedTest" in t.id()]
        if broken:
            raise AssertionError(
                "%s failed to import, so its count would be a lie: %s"
                % (path.name, broken)
            )
        found[path.name] = (tests, [t for t in tests if _will_be_skipped(t)])
    return found


def test_counts_by_file():
    """{filename: number of test cases}, straight from unittest discovery."""
    return {name: len(tests) for name, (tests, _) in discovered().items()}


class ReadmeCountsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = README.read_text(encoding="utf-8")
        cls.counts = test_counts_by_file()
        cls.total = sum(cls.counts.values())
        cls.doctor = sum(
            v for k, v in cls.counts.items() if k.startswith(DOCTOR_PREFIX)
        )
        cls.delivery = cls.total - cls.doctor
        cls.table = cls._parse_table(cls.text)

    @staticmethod
    def _parse_table(text):
        """{label: count} for each row of the per-file table.

        The doctor row is a glob with a file count, so it is keyed by its glob and
        carries both numbers.
        """
        rows = {}
        pattern = re.compile(
            r"^\|\s*`tests/(?P<name>[^`]+)`\s*(?:\((?P<files>\d+) files\))?\s*"
            r"\|\s*(?P<count>\d+)\s*\|",
            re.MULTILINE,
        )
        for match in pattern.finditer(text):
            rows[match.group("name")] = (
                int(match.group("count")),
                int(match.group("files")) if match.group("files") else None,
            )
        return rows

    def test_the_table_lists_every_test_file_exactly_once(self):
        listed = set(self.table)
        expected = {
            name for name in self.counts if not name.startswith(DOCTOR_PREFIX)
        }
        expected.add("%s*.py" % DOCTOR_PREFIX)
        self.assertEqual(
            listed,
            expected,
            "the per-file table and tests/ disagree about which files exist.\n"
            "missing from the README: %s\nlisted but absent: %s"
            % (sorted(expected - listed), sorted(listed - expected)),
        )

    def test_the_table_counts_match_discovery(self):
        for name, (claimed, files) in sorted(self.table.items()):
            if name.endswith("*.py"):
                actual = self.doctor
                actual_files = sum(
                    1 for k in self.counts if k.startswith(DOCTOR_PREFIX)
                )
                self.assertEqual(
                    files, actual_files, "the doctor row's file count is wrong"
                )
            else:
                actual = self.counts[name]
            self.assertEqual(
                claimed,
                actual,
                "the table says tests/%s has %d tests; discovery finds %d"
                % (name, claimed, actual),
            )

    def test_the_summary_line_matches_discovery(self):
        match = re.search(
            r"(\d+) tests, (\d+) skipped: (\d+) on the delivery path, "
            r"(\d+) for `doctor`",
            self.text,
        )
        self.assertIsNotNone(
            match,
            "the Tests section's summary sentence is gone or reworded. If you "
            "reword it, update this regex in the same commit.",
        )
        total, skipped, delivery, doctor = (int(g) for g in match.groups())
        self.assertEqual(total, self.total, "summary total")
        self.assertEqual(delivery, self.delivery, "summary delivery-path count")
        self.assertEqual(doctor, self.doctor, "summary doctor count")
        self.assertEqual(skipped, self.counts[OPT_IN_FILE], "summary skip count")

    def test_the_command_comment_matches_discovery(self):
        match = re.search(r"#\s*(\d+) tests, (\d+) skipped", self.text)
        self.assertIsNotNone(match, "the commented count on the discover line is gone")
        total, skipped = (int(g) for g in match.groups())
        self.assertEqual(total, self.total)
        self.assertEqual(skipped, self.counts[OPT_IN_FILE])

    @unittest.skipIf(
        os.environ.get("MACROVOICE_INTEGRATION") == "1",
        "the README's figure describes a DEFAULT run; with the opt-in suite "
        "enabled its tests are not skipped and the invariant does not apply",
    )
    def test_a_default_run_skips_exactly_the_opt_in_suite(self):
        """The invariant that makes the README's skip figure derivable.

        Every skip in a default run comes from the opt-in daemon suite. If another
        file starts skipping, that is worth knowing on purpose: this fails, and
        whoever added it decides whether the README should explain a second reason
        rather than quietly reporting a bigger number.
        """
        found = discovered()
        skipping = {
            name: len(skipped) for name, (_, skipped) in found.items() if skipped
        }
        self.assertEqual(
            skipping,
            {OPT_IN_FILE: self.counts[OPT_IN_FILE]},
            "a default run no longer skips exactly the opt-in suite, so the "
            "README's 'N skipped' needs a second reason spelled out.",
        )


class ReadmeHeadingCountTest(unittest.TestCase):
    """"Five things that will surprise you" must contain five things.

    It said Four while holding five for the length of one commit, which is how
    long it takes.
    """

    def test_the_surprises_heading_counts_its_own_subsections(self):
        text = README.read_text(encoding="utf-8")
        match = re.search(
            r"^## (\w+) things that will surprise you$", text, re.MULTILINE
        )
        self.assertIsNotNone(match, "the surprises heading is gone or reworded")

        word = match.group(1)
        self.assertIn(
            word,
            NUMBER_WORDS,
            "the heading's count is not a number word this test knows: %r" % word,
        )

        after = text[match.end() :]
        end = re.search(r"^## ", after, re.MULTILINE)
        section = after[: end.start()] if end else after
        subsections = re.findall(r"^### ", section, re.MULTILINE)

        self.assertEqual(
            NUMBER_WORDS[word],
            len(subsections),
            "the heading says %s but the section holds %d subsections"
            % (word, len(subsections)),
        )

        anchor = "#%s-things-that-will-surprise-you" % word.lower()
        self.assertIn(
            anchor,
            text,
            "no link points at the renamed heading, so an in-page link is now "
            "broken. Search the file for 'things-that-will-surprise-you'.",
        )


if __name__ == "__main__":
    unittest.main()
