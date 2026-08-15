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
from unittest import mock  # noqa: E402

import macrovoice.publisher as publisher_module  # noqa: E402
from macrovoice.publisher import (  # noqa: E402
    Publisher,
    generate_recording_name,
    successor_name,
)


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


class TestUnbeatableCeilingDoesNotWedgeThePublisher(unittest.TestCase):
    """A name in recordings/ that no clock-derived name can exceed.

    Found 2026-08-08 by reading _next_publish_name for coverage. The original
    loop was `while candidate <= ceiling: mint again; sleep(1ms)`, which assumes
    the clock will eventually overtake the ceiling. Two reachable cases break
    that assumption permanently:

      1. A future-dated folder. A clock that jumps forward and is then corrected
         (NTP, a restored VM snapshot, a DST or timezone bug, dual boot) leaves a
         folder stamped ahead of real time. macrowhisper's `moveTo` defaults to
         keeping folders, so it persists indefinitely.
      2. A folder whose name starts with a letter, e.g. `zz-old-backup`. Every
         letter sorts above every digit, so NO numeric name can ever beat it.

    In both cases the loop never terminates. The transcript is safe, because
    stage() already spooled it, but drain() hangs until VoiceInk's 10-second kill
    and the spool never empties again. Nothing errors. The bridge just stops
    working, permanently, which is precisely the silent-failure class this whole
    project exists to eliminate.

    Each test carries a hard timeout, because the failure mode is a hang: without
    one, a regression would stall the suite instead of failing it.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.watch = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _publisher(self):
        pub = Publisher(self.watch, min_gap_s=0.0)
        pub._ensure_layout()
        return pub

    def _call_with_timeout(self, fn, seconds=5.0):
        """Run fn() in a thread so a hang fails the test instead of stalling it."""
        box = {}

        def run():
            try:
                box["value"] = fn()
            except BaseException as exc:  # noqa: BLE001 - reported below
                box["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(seconds)
        if thread.is_alive():
            self.fail(
                f"{fn.__name__} did not return within {seconds}s: the unbeatable "
                "ceiling has wedged the publisher again"
            )
        if "error" in box:
            raise box["error"]
        return box["value"]

    def test_future_dated_ceiling_still_yields_a_greater_name(self):
        pub = self._publisher()
        future = generate_recording_name(time.time_ns() + 10**18)  # ~31 years ahead
        (pub.recordings_dir / future).mkdir()

        name = self._call_with_timeout(pub._next_publish_name)

        self.assertIsNotNone(name, "a parseable ceiling must always be beatable")
        self.assertGreater(
            name, future, "the minted name must exceed the ceiling or macrowhisper drops it"
        )

    def test_a_publish_over_a_future_ceiling_actually_lands(self):
        """The property that matters, end to end: the transcript reaches recordings/."""
        pub = self._publisher()
        future = generate_recording_name(time.time_ns() + 10**18)
        (pub.recordings_dir / future).mkdir()

        outcome = self._call_with_timeout(lambda: pub.publish({"result": "hello"}))

        self.assertEqual(len(outcome.published), 1)
        self.assertFalse(outcome.deferred)
        published = outcome.published[0]
        self.assertGreater(published.name, future)
        self.assertTrue((published / "meta.json").exists())

    def test_non_numeric_ceiling_leaves_the_transcript_spooled(self):
        """No numeric name can exceed a letter, so publishing is impossible.

        The only honest options are to hang, to publish something macrowhisper
        will silently discard, or to keep it spooled. Spooled is correct: the
        words survive, and a later run recovers once the folder is gone.
        """
        pub = self._publisher()
        (pub.recordings_dir / "zz-old-backup").mkdir()

        outcome = self._call_with_timeout(lambda: pub.publish({"result": "hello"}))

        self.assertEqual(outcome.published, [], "must not publish a name that would be dropped")
        self.assertTrue(outcome.deferred)
        self.assertTrue(
            (pub.spool_dir / outcome.spooled.name).exists(),
            "the transcript must remain in the spool, never discarded",
        )

    def test_it_recovers_once_the_unbeatable_folder_is_gone(self):
        pub = self._publisher()
        blocker = pub.recordings_dir / "zz-old-backup"
        blocker.mkdir()
        first = self._call_with_timeout(lambda: pub.publish({"result": "one"}))
        self.assertTrue(first.deferred)

        blocker.rmdir()
        second = self._call_with_timeout(lambda: pub.publish({"result": "two"}))

        # Both the backlog entry and the new one drain.
        self.assertEqual(len(second.published), 2, "the spooled backlog must drain on recovery")


class TestStagingNameCollisionAcrossProcesses(unittest.TestCase):
    """stage() must never raise, because it runs BEFORE the spool.

    The whole design rests on "once a transcript is in the spool it cannot be
    lost". An exception inside stage() happens before that point, so the
    transcript is lost outright: cli.py catches it, logs, and exits 0 by policy,
    and the user never learns their words are gone.

    This was a real defect, found 2026-08-08 in roughly 1 run in 8 of the
    five-concurrent integration test:

        FileExistsError: [Errno 17] File exists: .../.staging/1786151901562436000-000

    The cause is that every VoiceInk dictation is a fresh PROCESS, so every
    Publisher starts at counter 0. Two landing in the same nanosecond mint an
    identical staging name. The old guard was check-then-act:

        while staged.exists(): ...      # both processes see "no"
        staged.mkdir(parents=True)      # one wins, the other raises

    The existing 12-thread concurrency test could never catch this: threads share
    one Publisher and therefore one counter, so they never collide. Only separate
    processes do.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.watch = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_two_fresh_publishers_in_the_same_nanosecond(self):
        """Simulates two processes: two Publishers, both at counter 0, one clock."""
        first = Publisher(self.watch)
        second = Publisher(self.watch)
        first._ensure_layout()
        second._ensure_layout()

        with mock.patch.object(publisher_module.time, "time_ns", return_value=1786151901562436000):
            a = first.stage({"result": "first dictation"})
            b = second.stage({"result": "second dictation"})

        self.assertNotEqual(a.name, b.name, "two dictations must not share a folder")
        for path, expected in ((a, "first dictation"), (b, "second dictation")):
            self.assertTrue(path.exists())
            payload = json.loads((path / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["result"], expected)

    def test_the_directory_appearing_mid_call_is_survived(self):
        """The actual race: another process wins between our check and our mkdir.

        Reproduced by making the first mkdir fail exactly as the kernel would.
        Under the old check-then-act code this raised straight out of stage().
        """
        pub = Publisher(self.watch)
        pub._ensure_layout()

        real_mkdir = Path.mkdir
        state = {"raised": False}

        def racy_mkdir(self, *args, **kwargs):
            if not state["raised"] and self.parent.name == ".staging":
                state["raised"] = True
                raise FileExistsError(17, "File exists", str(self))
            return real_mkdir(self, *args, **kwargs)

        with mock.patch.object(Path, "mkdir", racy_mkdir):
            spooled = pub.stage({"result": "must survive the race"})

        self.assertTrue(state["raised"], "the race was not actually simulated")
        self.assertTrue(spooled.exists())
        payload = json.loads((spooled / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["result"], "must survive the race")

    def test_no_transcript_is_lost_across_many_separate_publishers(self):
        """Each Publisher stands in for one macrovoice process, all sharing a clock."""
        transcripts = {f"dictation {i}" for i in range(25)}
        with mock.patch.object(publisher_module.time, "time_ns", return_value=1786151901562436000):
            for text in sorted(transcripts):
                pub = Publisher(self.watch)  # fresh process => counter back to 0
                pub._ensure_layout()
                pub.stage({"result": text})

        recovered = {
            json.loads((folder / "meta.json").read_text(encoding="utf-8"))["result"]
            for folder in (self.watch / ".spool").iterdir()
        }
        self.assertEqual(recovered, transcripts, "a transcript was lost before the spool")


class TestLivenessCheck(PublisherTestCase):
    """G3: do not publish into a watch directory nobody is watching.

    macrowhisper's recordings watcher marks every folder that ALREADY EXISTS
    when it arms as processed and drops it. So a folder published while the
    daemon is down is not merely late, it is destroyed by the next startup, with
    no error on either side. Deferring keeps it in `.spool/`, which the arm race
    cannot reach because it is not inside `recordings/`.

    The rule is defer only on PROOF of death. See macrovoice/listener.py.
    """

    def test_no_listener_configured_publishes_exactly_as_before(self):
        # The default must be a no-op, so every pre-existing caller and test is
        # unaffected by the seam existing.
        pub = Publisher(self.watch, min_gap_s=0.05)
        pub.publish(build_meta("unchanged"))
        self.assertEqual(len(self.published_names()), 1)

    def test_a_dead_daemon_defers_instead_of_publishing(self):
        pub = Publisher(self.watch, min_gap_s=0.05, listener=lambda: False)
        outcome = pub.publish(build_meta("nobody is watching"))
        self.assertTrue(outcome.deferred)
        self.assertEqual(self.published_names(), [],
                         "published into a directory nothing is watching")

    def test_the_transcript_survives_in_the_spool(self):
        pub = Publisher(self.watch, min_gap_s=0.05, listener=lambda: False)
        pub.publish(build_meta("keep me"))
        spooled = list((self.watch / ".spool").iterdir())
        self.assertEqual(len(spooled), 1)
        self.assertEqual(self.read_result(spooled[0]), "keep me")

    def test_a_later_run_delivers_it_once_the_daemon_is_back(self):
        """The whole point: deferral must be recoverable, not a quieter loss."""
        down = Publisher(self.watch, min_gap_s=0.05, listener=lambda: False)
        down.publish(build_meta("during the outage"))
        self.assertEqual(self.published_names(), [])

        up = Publisher(self.watch, min_gap_s=0.05, listener=lambda: True)
        up.drain()
        published = self.published_names()
        self.assertEqual(len(published), 1)
        self.assertEqual(self.read_result(self.recordings / published[0]),
                         "during the outage")

    def test_a_live_daemon_publishes(self):
        pub = Publisher(self.watch, min_gap_s=0.05, listener=lambda: True)
        outcome = pub.publish(build_meta("someone is home"))
        self.assertFalse(outcome.deferred)
        self.assertEqual(len(self.published_names()), 1)

    def test_an_undetermined_listener_publishes(self):
        # None means "could not tell". Deferring on uncertainty would stop
        # delivery on a working setup, which is worse than the bug being fixed.
        pub = Publisher(self.watch, min_gap_s=0.05, listener=lambda: None)
        outcome = pub.publish(build_meta("cannot tell"))
        self.assertFalse(outcome.deferred)
        self.assertEqual(len(self.published_names()), 1)

    def test_a_listener_that_raises_publishes_rather_than_blocking_delivery(self):
        # listener.is_listening swallows everything, but Publisher must not
        # depend on that: a raising probe must never cost a delivery.
        def boom():
            raise RuntimeError("probe exploded")

        pub = Publisher(self.watch, min_gap_s=0.05, listener=boom)
        outcome = pub.publish(build_meta("still delivered"))
        self.assertFalse(outcome.deferred)
        self.assertEqual(len(self.published_names()), 1)

    def test_the_probe_runs_once_per_drain_not_once_per_folder(self):
        calls = []

        def counting():
            calls.append(1)
            return True

        pub = Publisher(self.watch, min_gap_s=0.0, listener=counting)
        for i in range(3):
            pub.stage(build_meta("queued %d" % i))
        pub.drain()
        self.assertEqual(len(calls), 1,
                         "one subprocess per drain, not one per spooled folder")

    def test_staging_still_happens_when_the_daemon_is_down(self):
        # stage() runs BEFORE the probe and is unconditional. If the probe ever
        # gated staging, an outage would lose the transcript outright, which is
        # the opposite of the fix.
        pub = Publisher(self.watch, min_gap_s=0.05, listener=lambda: False)
        spooled = pub.stage(build_meta("staged regardless"))
        self.assertTrue(spooled.exists())
        self.assertEqual(self.read_result(spooled), "staged regardless")


class TestSuccessorName(unittest.TestCase):
    """The pure helper that replaces spinning on the clock."""

    def test_increments_the_counter(self):
        self.assertEqual(
            successor_name("0000000000000000005-000"), "0000000000000000005-001"
        )

    def test_rolls_into_the_next_nanosecond_when_the_counter_is_exhausted(self):
        self.assertEqual(
            successor_name("0000000000000000005-999"), "0000000000000000006-000"
        )

    def test_result_always_sorts_above_its_input(self):
        for name in (
            "0000000000000000000-000",
            "1786148563285682000-042",
            "0000000000000000005-999",
        ):
            self.assertGreater(successor_name(name), name)

    def test_unparseable_names_are_refused_rather_than_guessed(self):
        for name in ("zz-old-backup", "", "not-a-name", "123-456", "0000000000000000005"):
            self.assertIsNone(successor_name(name), f"{name!r} should be unparseable")

    def test_the_maximum_representable_name_has_no_successor(self):
        self.assertIsNone(successor_name(f"{10**19 - 1:019d}-999"))


class TestSpooledFolders(unittest.TestCase):
    def test_missing_spool_directory_is_empty_not_an_error(self):
        with TemporaryDirectory() as tmp:
            pub = Publisher(Path(tmp))
            self.assertFalse(pub.spool_dir.exists())
            self.assertEqual(pub._spooled_folders(), [])


class TestDrainBudget(unittest.TestCase):
    def test_drain_stops_at_the_deadline_and_leaves_the_rest_spooled(self):
        """The budget exists so drain can never approach VoiceInk's 10s kill."""
        with TemporaryDirectory() as tmp:
            pub = Publisher(Path(tmp), min_gap_s=0.0, drain_budget_s=0.0)
            for i in range(3):
                pub.stage({"result": f"queued {i}"})

            published = pub.drain()

            self.assertEqual(published, [], "a zero budget must publish nothing")
            self.assertEqual(
                len(pub._spooled_folders()), 3, "everything must stay spooled, not vanish"
            )


class TestGapSurvivesClockAnomalies(unittest.TestCase):
    """A `.last-publish` timestamp in the future must not stall delivery.

    Measured 2026-08-09: with that file set a year ahead, every publish deferred
    and the spool grew without bound. Transcripts were never lost, because the
    spool holds them, but they were never delivered either and nothing surfaced
    it. Same hazard class as the `_next_publish_name` spin fixed 2026-08-08.
    """

    def _publisher(self, root, gap):
        # Constructing the Publisher creates the watch directories, so this must
        # happen before writing .last-publish into them.
        return Publisher(root, min_gap_s=gap)

    def _slept_for(self, publisher, offset_s, gap):
        """Return the durations time.sleep was asked for, deterministically."""
        stamp = time.time() + offset_s
        (publisher.watch_root / ".last-publish").write_text("%.6f" % stamp)
        slept = []
        with mock.patch("macrovoice.publisher.time.sleep", slept.append):
            granted = publisher._wait_for_gap(time.monotonic() + 30.0)
        return granted, slept

    def test_a_year_in_the_future_does_not_stall_publication(self):
        with TemporaryDirectory() as tmp:
            publisher = self._publisher(Path(tmp), 0.5)
            granted, slept = self._slept_for(publisher, 365 * 86400, 0.5)
            self.assertTrue(granted)
            self.assertTrue(slept, "expected a bounded wait, not no wait at all")
            self.assertLessEqual(slept[0], 0.5 + 1e-6)

    def test_slight_future_skew_still_waits_the_full_gap(self):
        # Ordinary clock jitter between two processes. The fix must NOT treat
        # this as unusable and skip the gap: the gap is what defends against
        # macrowhisper's burst protection silently dropping dictations.
        with TemporaryDirectory() as tmp:
            publisher = self._publisher(Path(tmp), 0.5)
            granted, slept = self._slept_for(publisher, 0.05, 0.5)
            self.assertTrue(granted)
            self.assertTrue(slept)
            self.assertGreater(slept[0], 0.4)

    def test_a_normal_recent_publish_waits_only_the_remainder(self):
        with TemporaryDirectory() as tmp:
            publisher = self._publisher(Path(tmp), 0.5)
            granted, slept = self._slept_for(publisher, -0.1, 0.5)
            self.assertTrue(granted)
            self.assertTrue(slept)
            self.assertLess(slept[0], 0.45)

    def test_end_to_end_a_future_stamp_still_publishes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            publisher = self._publisher(root, 0.01)
            publisher.publish(build_meta("first", mode_name="t"))
            (root / ".last-publish").write_text("%.6f" % (time.time() + 365 * 86400))
            outcome = Publisher(root, min_gap_s=0.01).publish(
                build_meta("second", mode_name="t")
            )
            self.assertFalse(
                outcome.deferred, "a future .last-publish must not defer forever"
            )


if __name__ == "__main__":
    unittest.main()
