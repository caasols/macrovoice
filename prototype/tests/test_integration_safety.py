"""Unit tests for the integration suite's own safety net.

These are NOT integration tests. They need no macrowhisper, touch no filesystem
and always run, because the thing they protect is exactly what fails when the
integration suite is not running: the user's real macrowhisper install.

Why this file exists. On 2026-08-06 an integration run leaked a temp config path
into macrowhisper's persisted settings. The daemon then watched a directory that
did not exist, and every dictation vanished with no error for two days. The
teardown in test_integration_macrowhisper.py was supposed to prevent exactly
that, and did not, because the leak defeats the guard rather than tripping it:

  1. a run leaks a temp path (crash, SIGTERM, kill before tearDownModule)
  2. the next run asks `macrowhisper --get-config` and is told the temp path
  3. it records that as _ORIGINAL_PATH, the value to protect
  4. teardown restores it faithfully, compares now == _ORIGINAL_PATH, passes

The guard was comparing the leaked value against itself. The in-run cascade was
already modelled (see that file's comment at the capture); the ACROSS-run cascade
was not. So the check cannot be "did the path change", it has to be "is the path
sane", and that is a pure decision worth testing on its own.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_integration_macrowhisper import (  # noqa: E402
    looks_like_leaked_temp_path,
    resolve_original_config,
)


class TestRealUserPathsAreNeverRejected(unittest.TestCase):
    """False positives are worse than false negatives here.

    A false positive resets a config path the user chose on purpose, which is a
    surprising, silent change to their setup. Be conservative.
    """

    def test_the_standard_config_location(self):
        self.assertFalse(
            looks_like_leaked_temp_path("/Users/someone/.config/macrowhisper/macrowhisper.json")
        )

    def test_a_path_under_documents(self):
        self.assertFalse(
            looks_like_leaked_temp_path("/Users/someone/Documents/mw/macrowhisper.json")
        )

    def test_a_directory_merely_named_tmp_something(self):
        """The obvious wrong implementation is `'tmp' in path`.

        A user is entitled to keep their config in ~/tmpconfig, or in a folder
        called `temp-setups`. Substring matching would reset it out from under
        them, which is the false positive this whole class guards against.
        """
        self.assertFalse(looks_like_leaked_temp_path("/Users/someone/tmpconfig/macrowhisper.json"))
        self.assertFalse(
            looks_like_leaked_temp_path("/Users/someone/temp-setups/macrowhisper.json")
        )
        self.assertFalse(
            looks_like_leaked_temp_path("/Users/someone/var/folders-backup/macrowhisper.json")
        )

    def test_a_path_containing_the_word_var(self):
        self.assertFalse(
            looks_like_leaked_temp_path("/Users/someone/varsity/macrowhisper.json")
        )


class TestLeakedTempPathsAreCaught(unittest.TestCase):
    def test_the_macos_temp_dir_that_actually_bit_us(self):
        """The real leaked value from the 2026-08-06 incident."""
        self.assertTrue(
            looks_like_leaked_temp_path(
                "/var/folders/7n/291l5k995c9dj8h57n9x4ll00000gp/T/"
                "tmpiyz__0bv/t1786025443250787000/macrowhisper.json"
            )
        )

    def test_plain_tmp(self):
        self.assertTrue(looks_like_leaked_temp_path("/tmp/whatever/macrowhisper.json"))

    def test_private_tmp(self):
        """macOS resolves /tmp to /private/tmp, so both spellings must be caught."""
        self.assertTrue(looks_like_leaked_temp_path("/private/tmp/x/macrowhisper.json"))

    def test_private_var_folders(self):
        self.assertTrue(
            looks_like_leaked_temp_path("/private/var/folders/ab/cd/T/x/macrowhisper.json")
        )

    def test_the_systems_own_temp_dir(self):
        """Whatever tempfile.gettempdir() reports must be treated as a leak.

        This is the one that keeps the check honest on a machine configured
        differently from this one, e.g. a CI runner with TMPDIR elsewhere.
        """
        import tempfile

        leaked = str(Path(tempfile.gettempdir()) / "run123" / "macrowhisper.json")
        self.assertTrue(looks_like_leaked_temp_path(leaked))


class TestDegenerateInput(unittest.TestCase):
    """`--get-config` output is parsed loosely upstream, so anything can arrive."""

    def test_empty_string_is_not_a_leak(self):
        """Empty means 'could not determine', which is handled elsewhere.

        Reporting it as a leak would trigger a reset on any machine where the
        macrowhisper CLI output format changes, which is worse than doing nothing.
        """
        self.assertFalse(looks_like_leaked_temp_path(""))

    def test_none_is_not_a_leak(self):
        self.assertFalse(looks_like_leaked_temp_path(None))

    def test_the_default_config_sentinel_text_is_not_a_leak(self):
        self.assertFalse(looks_like_leaked_temp_path("using default config path"))

    def test_a_relative_path_is_not_a_leak(self):
        self.assertFalse(looks_like_leaked_temp_path("macrowhisper.json"))


class TestPathNormalisation(unittest.TestCase):
    def test_trailing_whitespace_does_not_hide_a_leak(self):
        self.assertTrue(looks_like_leaked_temp_path("  /tmp/x/macrowhisper.json  "))

    def test_a_traversal_into_temp_is_still_a_leak(self):
        """Normalise before deciding, or `..` smuggles a temp path past the check."""
        self.assertTrue(
            looks_like_leaked_temp_path("/Users/someone/../../tmp/x/macrowhisper.json")
        )


class TestResolveOriginalConfig(unittest.TestCase):
    """The capture decision: what do we agree to protect and restore?"""

    def test_a_real_user_path_is_adopted(self):
        path, was_default, leaked = resolve_original_config(
            "Config path: /Users/someone/.config/macrowhisper/macrowhisper.json"
        )
        self.assertEqual(path, "/Users/someone/.config/macrowhisper/macrowhisper.json")
        self.assertFalse(was_default)
        self.assertIsNone(leaked)

    def test_the_default_sentinel_is_recognised(self):
        path, was_default, leaked = resolve_original_config(
            "Using default config path: /Users/someone/.config/macrowhisper/macrowhisper.json"
        )
        self.assertTrue(was_default)
        self.assertIsNone(leaked)

    def test_a_leaked_temp_path_is_rejected_not_adopted(self):
        """The heart of the 2026-08-06 regression.

        Adopting the leak is what made it self-perpetuating: the run restores
        the temp path, compares it to itself, and reports success while the
        user's daemon quietly watches a directory that no longer exists.
        """
        leaked_raw = (
            "Config path: /var/folders/7n/291l5k995c9dj8h57n9x4ll00000gp/T/"
            "tmpiyz__0bv/t1786025443250787000/macrowhisper.json"
        )
        path, was_default, leaked = resolve_original_config(leaked_raw)

        self.assertEqual(path, "", "must not adopt the leaked path as the original")
        self.assertTrue(was_default, "must fall back to the default config")
        self.assertIn("/var/folders/", leaked, "must report what it rejected")

    def test_unparseable_output_yields_no_path_rather_than_guessing(self):
        path, was_default, leaked = resolve_original_config("something unexpected")
        self.assertEqual(path, "")
        self.assertIsNone(leaked)

    def test_empty_output(self):
        path, was_default, leaked = resolve_original_config("")
        self.assertEqual(path, "")
        self.assertIsNone(leaked)


if __name__ == "__main__":
    unittest.main()
