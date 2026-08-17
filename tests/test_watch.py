"""Where the watch root comes from when nobody passed `--watch`.

The whole point of this module is that the rename from `~/mw-bridge` to
`~/macrovoice` is NOT breaking. A user who upgrades without migrating must keep
publishing into the directory macrowhisper is already watching, because the
failure mode is not a delay: macrowhisper marks pre-existing folders processed
when its watcher next arms, so a dictation published into an unwatched directory
is destroyed.

Every test here builds a REAL temporary home directory and creates or omits the
real directories. None of them assert a constant against itself, which is the
mistake that locked in the bug fixed by 5e3367b.
"""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from macrovoice import watch  # noqa: E402


class WatchDefaultTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def resolve(self, **environ):
        return watch.resolve_watch_default(environ=environ, home=self.home)

    def make(self, name):
        (self.home / name).mkdir()


class TestTheEnvironmentWins(WatchDefaultTestCase):
    def test_macrovoice_watch_is_read(self):
        self.assertEqual(
            self.resolve(MACROVOICE_WATCH="/tmp/explicit"), Path("/tmp/explicit")
        )

    def test_the_legacy_name_is_still_honoured(self):
        # Ruled 2026-08-15: support both, remove nothing. Anyone scripting the
        # tool against the old name keeps working.
        self.assertEqual(
            self.resolve(MW_BRIDGE_WATCH="/tmp/legacy"), Path("/tmp/legacy")
        )

    def test_the_new_name_wins_when_both_are_set(self):
        self.assertEqual(
            self.resolve(MACROVOICE_WATCH="/tmp/new", MW_BRIDGE_WATCH="/tmp/old"),
            Path("/tmp/new"),
        )

    def test_a_tilde_in_the_environment_is_expanded(self):
        # The caller used to do this. It has to keep happening somewhere, or a
        # documented `MW_BRIDGE_WATCH=~/elsewhere` starts creating a literal
        # directory named "~".
        self.assertEqual(
            watch.resolve_watch_default(
                environ={"MACROVOICE_WATCH": "~/elsewhere"}, home=self.home
            ),
            self.home / "elsewhere",
        )

    def test_an_empty_value_counts_as_unset(self):
        # Honouring "" literally resolves the watch root to the working
        # directory, which under VoiceInk is `/`.
        self.make("macrovoice")
        self.assertEqual(
            self.resolve(MACROVOICE_WATCH="", MW_BRIDGE_WATCH=""),
            self.home / "macrovoice",
        )

    def test_an_empty_new_name_falls_through_to_the_legacy_name(self):
        self.assertEqual(
            self.resolve(MACROVOICE_WATCH="", MW_BRIDGE_WATCH="/tmp/legacy"),
            Path("/tmp/legacy"),
        )


class TestTheDirectoryFallback(WatchDefaultTestCase):
    def test_a_fresh_machine_gets_the_new_name(self):
        self.assertEqual(self.resolve(), self.home / "macrovoice")

    def test_an_existing_macrovoice_directory_is_used(self):
        self.make("macrovoice")
        self.assertEqual(self.resolve(), self.home / "macrovoice")

    def test_an_unmigrated_install_keeps_using_mw_bridge(self):
        """THE regression test for B4.

        This is the entire reason the rename is non-breaking. If a later change
        makes `~/macrovoice` unconditional, this fails, and it should.
        """
        self.make("mw-bridge")
        self.assertEqual(self.resolve(), self.home / "mw-bridge")

    def test_the_new_name_wins_when_both_directories_exist(self):
        # Unknowable from the filesystem alone; only macrowhisper's
        # defaults.watch decides. Preferring the new name makes the fallback
        # self-retiring, and doctor's mw.watchmatch and bridge.legacywatch both
        # name the ambiguity rather than leaving it silent.
        self.make("macrovoice")
        self.make("mw-bridge")
        self.assertEqual(self.resolve(), self.home / "macrovoice")

    def test_the_fallback_retires_itself_after_a_migration(self):
        # Before: only the legacy directory. After `mv`: only the new one.
        self.make("mw-bridge")
        self.assertEqual(self.resolve(), self.home / "mw-bridge")
        (self.home / "mw-bridge").rename(self.home / "macrovoice")
        self.assertEqual(self.resolve(), self.home / "macrovoice")

    def test_a_legacy_FILE_is_not_mistaken_for_the_directory(self):
        # `is_dir`, not `exists`. A stray file named mw-bridge must not divert
        # delivery to a path that can never hold recordings/.
        (self.home / "mw-bridge").write_text("not a directory")
        self.assertEqual(self.resolve(), self.home / "macrovoice")

    def test_the_environment_beats_an_existing_legacy_directory(self):
        self.make("mw-bridge")
        self.assertEqual(
            self.resolve(MACROVOICE_WATCH="/tmp/explicit"), Path("/tmp/explicit")
        )


class TestTheResultIsUsable(WatchDefaultTestCase):
    def test_the_result_is_an_absolute_path(self):
        # cli.py does Path(args.watch).expanduser() on it. An unexpanded "~"
        # reaching the filesystem creates a directory literally named "~".
        self.make("mw-bridge")
        resolved = self.resolve()
        self.assertIsInstance(resolved, Path)
        self.assertTrue(resolved.is_absolute(), resolved)
        self.assertNotIn("~", str(resolved))

    def test_it_reads_the_real_environment_by_default(self):
        # The production call site passes neither argument.
        import os

        marker = str(self.home / "from-real-environ")
        previous = os.environ.get(watch.ENV_WATCH)
        os.environ[watch.ENV_WATCH] = marker
        try:
            self.assertEqual(watch.resolve_watch_default(), Path(marker))
        finally:
            if previous is None:
                del os.environ[watch.ENV_WATCH]
            else:
                os.environ[watch.ENV_WATCH] = previous


class TestTheNamesArePublished(unittest.TestCase):
    """The constants are the contract other modules and the README quote."""

    def test_the_default_is_the_product_name(self):
        self.assertEqual(watch.DEFAULT_WATCH, "~/macrovoice")

    def test_the_legacy_name_is_still_named(self):
        self.assertEqual(watch.LEGACY_WATCH, "~/mw-bridge")

    def test_the_environment_variable_names(self):
        self.assertEqual(watch.ENV_WATCH, "MACROVOICE_WATCH")
        self.assertEqual(watch.LEGACY_ENV_WATCH, "MW_BRIDGE_WATCH")


class TestTheGitignoreDoesNotSwallowThePackage(unittest.TestCase):
    """The trap inside B4's own rewrite list.

    `.gitignore` carries `/mw-bridge/`, guarding against a watch root created
    inside the repo. Renaming that entry to `/macrovoice/` alongside everything
    else looks like finishing the job and would instead ignore the PYTHON
    PACKAGE, making every new module invisible to `git add`.

    This repo's `.gitignore` already fails closed on markdown, so `git status`
    showing nothing is a normal sight here and would not raise an eyebrow. That
    is exactly why this is a test and not a comment.
    """

    def rules(self):
        text = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(
            encoding="utf-8"
        )
        return [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def test_the_package_directory_is_not_ignored(self):
        for rule in self.rules():
            self.assertNotIn(
                rule.strip("!").rstrip("/"),
                ("/macrovoice", "macrovoice"),
                "%r ignores the macrovoice package directory" % rule,
            )

    def test_the_package_is_actually_visible_to_git(self):
        # The rule above is the mechanism; this is the consequence. Asked
        # directly, git is the only authority on what it can see.
        import subprocess

        repo = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            ["git", "check-ignore", "-q", "macrovoice/watch.py"],
            cwd=str(repo), capture_output=True,
        )
        # check-ignore exits 0 when the path IS ignored, 1 when it is not.
        self.assertEqual(
            result.returncode, 1,
            "git is ignoring macrovoice/watch.py, so new package modules would "
            "never be committed",
        )


class TestTheGitignoreCoversAgentWorktrees(unittest.TestCase):
    """The other half of the same blind spot, found 2026-08-17.

    `.claude/worktrees/<branch>/` is an entire nested checkout of this repo,
    created by the agent harness inside the working tree. It was matched by no
    ignore rule, so `git add -A` from the main checkout would have swept it in.
    Nobody noticed for the same reason the package trap above is a test: on this
    repo `git status` showing nothing is normal, so an untracked directory
    sitting in it does not read as an alarm.

    The complement matters as much as the rule: the fix is deliberately NOT a
    blanket `.claude/`, because that would hide a shared settings.json or a
    project skill from `git add` and reintroduce the very trap above, one
    directory over. Both halves are asserted here so a later "simplification" to
    `.claude/` fails loudly.
    """

    REPO = Path(__file__).resolve().parent.parent

    def rules(self):
        text = (self.REPO / ".gitignore").read_text(encoding="utf-8")
        return [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def ignored(self, relpath):
        import subprocess

        result = subprocess.run(
            ["git", "check-ignore", "-q", relpath],
            cwd=str(self.REPO), capture_output=True,
        )
        return result.returncode == 0  # 0 means git IS ignoring it

    def test_a_worktree_checkout_is_ignored(self):
        self.assertTrue(
            self.ignored(".claude/worktrees/some-branch/macrovoice/cli.py"),
            "a nested worktree checkout is visible to `git add -A`",
        )

    def test_local_settings_are_ignored(self):
        self.assertTrue(
            self.ignored(".claude/settings.local.json"),
            "per-machine Claude settings are visible to `git add -A`",
        )

    def test_claude_is_not_ignored_wholesale(self):
        # The complement. A blanket rule would silently swallow anything the repo
        # might legitimately want to publish under .claude/, on a repo where
        # `git status` is not a reliable alarm. Same shape as the package trap.
        for rule in self.rules():
            self.assertNotIn(
                rule.strip("!").rstrip("/"),
                (".claude", "/.claude"),
                "%r ignores all of .claude/, hiding shareable project config" % rule,
            )
        self.assertFalse(
            self.ignored(".claude/settings.json"),
            "a shared .claude/settings.json would be invisible to `git add`",
        )


if __name__ == "__main__":
    unittest.main()
