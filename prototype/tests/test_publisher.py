"""Publisher: atomic staging, monotonic naming, spooling, drain locking, burst spacing.

This is the highest-risk unit in the bridge, because the failure mode it defends
against is SILENT. macrowhisper's burst protection
(RecordingsFolderWatcher.swift:327-345) marks every folder processed and runs NONE
of them when more than one appears in a single filesystem event. Nothing errors.
The dictation simply vanishes.

Two properties carry the design:

  P1 (atomicity)   `recordings/<id>/meta.json` exists and passes macrowhisper's gate
                   at the instant `<id>` first becomes visible. This keeps us on the
                   fast path at RecordingsFolderWatcher.swift:457-462 and out of the
                   17-second WAV-less cancellation window at :38/:1928.

  P2 (spacing)     Never more than one new directory per filesystem event.
"""

import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_harness import is_valid_recording_meta_json  # noqa: E402
from macrovoice.meta import build_meta  # noqa: E402
from macrovoice.publisher import Publisher, generate_recording_name  # noqa: E402


class TestRecordingNames(unittest.TestCase):
    """macrowhisper compares directory names as STRINGS at
    RecordingsFolderWatcher.swift:349 (`dirName < mostRecentExisting`) to decide
    whether an appearing folder is older than what it has already seen. So names
    must be fixed-width and zero-padded; a bare nanosecond timestamp is not, and
    would sort wrongly across a digit-count boundary."""

    def test_names_are_fixed_width(self):
        widths = {len(generate_recording_name(ns)) for ns in (1, 10**9, 1_785_974_400_123_456_789)}
        self.assertEqual(len(widths), 1, f"names vary in width: {widths}")

    def test_string_order_matches_time_order(self):
        earlier = generate_recording_name(1_785_974_400_000_000_000)
        later = generate_recording_name(1_785_974_400_000_000_001)
        self.assertLess(earlier, later)

    def test_small_timestamp_sorts_before_large_as_string(self):
        # The exact case bare `date +%s%N` gets wrong.
        self.assertLess(generate_recording_name(999), generate_recording_name(1_000_000_000))

    def test_counter_disambiguates_same_nanosecond(self):
        a = generate_recording_name(12345, counter=0)
        b = generate_recording_name(12345, counter=1)
        self.assertNotEqual(a, b)
        self.assertLess(a, b)

    def test_ten_thousand_sequential_names_strictly_increase(self):
        names = [generate_recording_name(10**18 + i) for i in range(10_000)]
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(set(names)), 10_000)

    def test_name_contains_no_path_separators(self):
        name = generate_recording_name(1_785_974_400_123_456_789, counter=7)
        self.assertNotIn("/", name)
        self.assertNotIn("\x00", name)


class PublisherTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.watch = Path(self._tmp.name) / "mw-bridge"
        # min_gap_s deliberately tiny so tests stay fast; spacing behavior is
        # asserted relative to whatever gap is configured, not to a wall-clock value.
        self.pub = Publisher(self.watch, min_gap_s=0.05)

    def tearDown(self):
        self._tmp.cleanup()

    @property
    def recordings(self):
        return self.watch / "recordings"

    def published_names(self):
        if not self.recordings.exists():
            return []
        return sorted(p.name for p in self.recordings.iterdir() if p.is_dir())

    def read_result(self, folder: Path) -> str:
        return json.loads((folder / "meta.json").read_text(encoding="utf-8"))["result"]


class TestLayout(PublisherTestCase):
    def test_creates_watch_layout_on_demand(self):
        self.pub.publish(build_meta("hello"))
        self.assertTrue(self.recordings.is_dir())

    def test_recordings_is_the_only_thing_macrowhisper_sees(self):
        # Staging and spool must live OUTSIDE recordings/, or macrowhisper would
        # detect them as recordings.
        self.pub.publish(build_meta("hello"))
        for helper in (".staging", ".spool"):
            self.assertFalse((self.recordings / helper).exists())
            self.assertTrue((self.watch / helper).exists())

    def test_staging_is_empty_after_publish(self):
        self.pub.publish(build_meta("hello"))
        self.assertEqual(list((self.watch / ".staging").iterdir()), [])

    def test_spool_is_empty_after_successful_drain(self):
        self.pub.publish(build_meta("hello"))
        self.assertEqual(list((self.watch / ".spool").iterdir()), [])


class TestAtomicity(PublisherTestCase):
    """Property P1."""

    def test_published_folder_contains_valid_meta_json(self):
        self.pub.publish(build_meta("hello world"))
        (folder,) = list(self.recordings.iterdir())
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        self.assertTrue(is_valid_recording_meta_json(meta))
        self.assertEqual(meta["result"], "hello world")

    def test_meta_json_is_present_the_instant_the_folder_appears(self):
        """A watcher thread polls recordings/ as fast as it can and records, for
        every directory it observes, whether meta.json was already inside. If the
        publisher ever exposes an empty directory, this catches it.

        This simulates exactly what RecordingsFolderWatcher does on its
        DispatchSource event, and is the reason we rename a fully-built directory
        rather than mkdir-then-write.
        """
        self.recordings.mkdir(parents=True, exist_ok=True)
        observations = []
        stop = threading.Event()

        def watch():
            seen = set()
            while not stop.is_set():
                try:
                    for entry in self.recordings.iterdir():
                        if entry.name not in seen:
                            seen.add(entry.name)
                            observations.append((entry.name, (entry / "meta.json").exists()))
                except FileNotFoundError:
                    pass

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            for i in range(20):
                self.pub.publish(build_meta(f"transcript {i}"))
        finally:
            stop.set()
            watcher.join(timeout=5)

        self.assertGreater(len(observations), 0, "watcher observed nothing; test is not proving anything")
        empty = [name for name, had_meta in observations if not had_meta]
        self.assertEqual(empty, [], f"directories became visible without meta.json: {empty}")

    def test_unicode_survives_the_filesystem_round_trip(self):
        text = "héllo wörld 🧠 مرحبا 日本語"
        self.pub.publish(build_meta(text))
        (folder,) = list(self.recordings.iterdir())
        self.assertEqual(self.read_result(folder), text)


class TestSpoolRecovery(PublisherTestCase):
    def test_stage_puts_a_complete_folder_in_spool(self):
        spooled = self.pub.stage(build_meta("hello"))
        self.assertTrue(spooled.exists())
        self.assertTrue((spooled / "meta.json").exists())
        self.assertEqual(self.published_names(), [], "stage must not publish")

    def test_drain_publishes_what_stage_spooled(self):
        self.pub.stage(build_meta("hello"))
        published = self.pub.drain()
        self.assertEqual(len(published), 1)
        self.assertEqual(len(self.published_names()), 1)

    def test_drain_on_empty_spool_is_a_noop(self):
        self.assertEqual(self.pub.drain(), [])

    def test_leftovers_from_a_previous_run_are_drained(self):
        # Simulate a process killed mid-drain: folders sitting in spool.
        for i in range(3):
            self.pub.stage(build_meta(f"orphan {i}"))
        self.assertEqual(self.published_names(), [])

        published = Publisher(self.watch, min_gap_s=0.01).drain()
        self.assertEqual(len(published), 3)
        results = {self.read_result(self.recordings / n) for n in self.published_names()}
        self.assertEqual(results, {"orphan 0", "orphan 1", "orphan 2"})

    def test_drain_is_ordered_oldest_first(self):
        for i in range(4):
            self.pub.stage(build_meta(f"msg {i}"))
        self.pub.drain()
        names = self.published_names()
        results = [self.read_result(self.recordings / n) for n in names]
        self.assertEqual(results, ["msg 0", "msg 1", "msg 2", "msg 3"])


class TestBurstProtection(PublisherTestCase):
    """Property P2, the whole reason this class exists."""

    def test_consecutive_publishes_are_spaced(self):
        gap = 0.15
        pub = Publisher(self.watch, min_gap_s=gap)
        times = []
        for i in range(4):
            pub.publish(build_meta(f"msg {i}"))
            times.append(time.monotonic())
        deltas = [b - a for a, b in zip(times, times[1:])]
        for d in deltas:
            self.assertGreaterEqual(
                d, gap * 0.9, f"publishes {d:.3f}s apart, below the {gap}s gap"
            )

    def test_spacing_holds_across_separate_publisher_instances(self):
        """Separate processes must also respect the gap, so the timestamp has to be
        persisted on disk rather than held in memory."""
        gap = 0.15
        Publisher(self.watch, min_gap_s=gap).publish(build_meta("first"))
        start = time.monotonic()
        Publisher(self.watch, min_gap_s=gap).publish(build_meta("second"))
        self.assertGreaterEqual(time.monotonic() - start, gap * 0.9)

    def test_concurrent_publishes_lose_nothing(self):
        """N threads publish at once. Every transcript must end up on disk exactly
        once. This is the data-loss guarantee."""
        n = 12
        pub = Publisher(self.watch, min_gap_s=0.01)
        errors = []

        def worker(i):
            try:
                pub.publish(build_meta(f"concurrent {i:02d}"))
            except Exception as exc:  # pragma: no cover - surfaced via assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [])
        Publisher(self.watch, min_gap_s=0.01).drain()  # flush anything deferred
        results = {self.read_result(self.recordings / name) for name in self.published_names()}
        self.assertEqual(results, {f"concurrent {i:02d}" for i in range(n)})

    def test_deferred_reports_on_OUR_folder_not_just_any_folder(self):
        """`deferred` must mean "the transcript I just spooled is not yet in
        recordings/", not "nothing at all got published".

        The distinguishing case: an older folder is already spooled, the drain
        budget only stretches far enough to publish that one, and ours stays behind.
        Reporting deferred=False there would be a lie, and the CLI logs it.
        """
        # Budget deliberately smaller than the gap: the first folder publishes
        # immediately (no prior publish to wait behind), then the budget runs out
        # before the gap for the second has elapsed.
        pub = Publisher(self.watch, min_gap_s=0.3, drain_budget_s=0.15)
        pub.stage(build_meta("older, already waiting"))

        outcome = pub.publish(build_meta("mine, should be deferred"))

        self.assertEqual(len(outcome.published), 1)
        self.assertEqual(self.read_result(outcome.published[0]), "older, already waiting")
        self.assertTrue(outcome.deferred, "our folder was not published, so deferred must be True")
        self.assertTrue(outcome.spooled.exists(), "our folder must still be recoverable in spool")

    def test_not_deferred_when_our_folder_is_published(self):
        outcome = Publisher(self.watch, min_gap_s=0.01).publish(build_meta("mine"))
        self.assertFalse(outcome.deferred)
        self.assertEqual(len(outcome.published), 1)
        # The published name deliberately differs from the spool name: spool names
        # encode ARRIVAL order for FIFO draining, published names encode PUBLICATION
        # order so macrowhisper never sees a backwards name. Identity is therefore
        # asserted on content, not on the name.
        self.assertEqual(self.read_result(outcome.published[0]), "mine")
        self.assertNotEqual(outcome.published[0].name, outcome.spooled.name)

    def test_lock_contention_defers_instead_of_blocking(self):
        pub = Publisher(self.watch, min_gap_s=0.01)
        pub._ensure_layout()
        with open(pub.lock_path, "w") as handle:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
            outcome = pub.publish(build_meta("deferred one"))
            fcntl.flock(handle, fcntl.LOCK_UN)

        self.assertTrue(outcome.deferred)
        self.assertEqual(outcome.published, [])
        self.assertTrue(outcome.spooled.exists(), "a deferred transcript must still be spooled")

        # The next invocation, holding no contention, picks it up.
        Publisher(self.watch, min_gap_s=0.01).drain()
        self.assertEqual(len(self.published_names()), 1)


class TestNeverPublishesAnOlderName(PublisherTestCase):
    """macrowhisper silently drops a folder whose name sorts BEFORE the newest one
    already in recordings/.

        RecordingsFolderWatcher.swift:350
            if let mostRecentExisting = mostRecentExistingDir, dirName < mostRecentExisting {
                markAsProcessed(recordingPath: fullPath)
                logInfo("New recording ... is older than existing recordings.
                         Marked as processed to prevent cloud sync interference.")

    It is meant to stop cloud-sync from replaying old recordings. For the bridge it
    is a live data-loss bug: names are assigned when a transcript ARRIVES, but the
    folder is published later, so a concurrent drain can publish a newer name first
    and strand an older one forever.

    Observed for real on 2026-08-05: 5 concurrent dictations, all 5 folders reached
    recordings/, only 4 fired an action. The 5th was dropped by this exact branch.

    The fix: the name that lands in recordings/ is generated at PUBLISH time and is
    always greater than anything already there. Spool names keep arrival order for
    FIFO draining, but they are not the published identity.
    """

    def max_published_name(self):
        names = self.published_names()
        return max(names) if names else ""

    def test_published_name_always_exceeds_existing_maximum(self):
        self.pub.publish(build_meta("first"))
        first_max = self.max_published_name()

        # Stage a folder whose SPOOL name is deliberately older than what is already
        # published, exactly the situation a concurrent drain creates.
        stale = self.pub.spool_dir / generate_recording_name(1)
        stale.mkdir(parents=True)
        (stale / "meta.json").write_text('{"result":"stale arrival","duration":0}', encoding="utf-8")

        published = self.pub.drain()

        self.assertEqual(len(published), 1)
        self.assertGreater(
            published[0].name,
            first_max,
            "published a name macrowhisper would reject as 'older than existing'",
        )
        self.assertEqual(self.read_result(published[0]), "stale arrival")

    def test_every_published_name_is_strictly_increasing(self):
        for i in range(8):
            self.pub.stage(build_meta(f"msg {i}"))
        self.pub.drain()
        names = self.published_names()
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(set(names)), len(names))

    def test_fifo_order_is_preserved_despite_renaming(self):
        """Renaming at publish time must not reorder the transcripts."""
        for i in range(5):
            self.pub.stage(build_meta(f"msg {i}"))
        self.pub.drain()
        results = [self.read_result(self.recordings / n) for n in self.published_names()]
        self.assertEqual(results, [f"msg {i}" for i in range(5)])

    def test_concurrent_publishes_never_produce_a_backwards_name(self):
        n = 10
        pub = Publisher(self.watch, min_gap_s=0.01)
        threads = [
            threading.Thread(target=lambda i=i: pub.publish(build_meta(f"concurrent {i:02d}")))
            for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        Publisher(self.watch, min_gap_s=0.01).drain()

        names = self.published_names()
        self.assertEqual(len(names), n)
        self.assertEqual(names, sorted(names), "a folder was published with a backwards name")


class TestTimeBudget(PublisherTestCase):
    """VoiceInk kills the command at 10s (TranscriptionDelivery.swift:115), so the
    drain must be bounded well below that and leave the rest spooled."""

    def test_drain_respects_its_time_budget(self):
        pub = Publisher(self.watch, min_gap_s=0.2, drain_budget_s=0.5)
        for i in range(20):
            pub.stage(build_meta(f"msg {i}"))
        start = time.monotonic()
        published = pub.drain()
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 2.0, "drain overran its budget")
        self.assertLess(len(published), 20, "expected the budget to stop the drain early")
        self.assertGreater(len(published), 0)
        remaining = list((self.watch / ".spool").iterdir())
        self.assertEqual(len(remaining), 20 - len(published), "unpublished folders must stay spooled")

    def test_publish_returns_quickly_under_contention(self):
        pub = Publisher(self.watch, min_gap_s=5.0)
        pub._ensure_layout()
        with open(pub.lock_path, "w") as handle:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
            start = time.monotonic()
            pub.publish(build_meta("fast path"))
            elapsed = time.monotonic() - start
            fcntl.flock(handle, fcntl.LOCK_UN)
        self.assertLess(elapsed, 1.0, "a deferred publish must not block on the lock")


if __name__ == "__main__":
    unittest.main()
