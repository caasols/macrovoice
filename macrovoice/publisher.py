"""Publish synthetic recording folders into macrowhisper's watch directory.

Everything here exists to satisfy two properties that come straight from reading
macrowhisper's watcher. Both are non-obvious, and getting either wrong produces a
SILENT failure, which is why this module is more careful than its size suggests.

P1, ATOMICITY. `recordings/<id>/meta.json` must exist and be complete at the instant
`<id>` first becomes visible.

    RecordingsFolderWatcher.swift:457-462 takes a fast path when meta.json already
    exists and is complete: it processes immediately and skips clipboard monitoring,
    the meta.json-creation watch, and the audio watcher entirely. If instead the
    directory shows up empty, macrowhisper starts an audio-file watcher and a
    RECORDING_TIMEOUT_SECONDS = 17.0 timer (:38), and at :1928-1935 any unprocessed
    recording still lacking a .wav file is logged as TIMEOUT CANCELLATION. Bridge
    folders never contain a .wav, so an empty-directory window is a real hazard.

    We therefore build the folder under .staging/ and rename() the whole DIRECTORY
    into place. rename(2) on one filesystem is atomic, so the directory can never be
    observed without its meta.json. This is why .staging/, .spool/ and recordings/
    all live under the same watch root: same filesystem, so rename stays atomic.

P2, ONE AT A TIME. Never let two directories appear in a single filesystem event.

    RecordingsFolderWatcher.swift:327-345: when newSubdirectories.count > 1,
    macrowhisper marks them ALL as processed and executes NONE, and additionally
    cancels any pending auto-return and scheduled action. handleFolderChangeEvent
    runs directly off the DispatchSource handler with no debounce, so "simultaneous"
    means "within one coalesced kernel event", not "within the same second".

    Nothing errors when this happens. The dictation is simply swallowed. We defend
    with a drain lock plus a minimum gap between publishes, persisted on disk so the
    gap holds across separate processes.

The spool is what makes P2 safe to enforce. VoiceInk kills the command at 10 seconds
(TranscriptionDelivery.swift:115), so a design that blocked until it could publish
would eventually be killed mid-wait and lose the transcript. Instead every invocation
spools first, which is fast and unconditional, and only then tries to publish. Once a
transcript is in the spool it cannot be lost: some later invocation will drain it.
"""

import errno
import fcntl
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .meta import serialize_meta

__all__ = ["Publisher", "PublishOutcome", "generate_recording_name", "successor_name"]

# Nanoseconds since the epoch need 19 digits until the year 2286. Zero-padding to a
# fixed width is what keeps lexicographic order equal to chronological order.
_NS_WIDTH = 19
_COUNTER_WIDTH = 3
_NAME_RE = re.compile(rf"^(\d{{{_NS_WIDTH}}})-(\d{{{_COUNTER_WIDTH}}})$")

DEFAULT_MIN_GAP_S = 1.0
DEFAULT_DRAIN_BUDGET_S = 6.0  # comfortably under VoiceInk's 10s kill

# Retries for minting a free staging name. Each attempt draws a fresh nanosecond,
# so realistically one or two suffice; the bound only exists so a frozen clock
# fails loudly instead of spinning forever.
_STAGE_ATTEMPTS = 10_000


def generate_recording_name(now_ns: int, counter: int = 0) -> str:
    """Return a fixed-width, lexicographically monotonic recording folder name.

    macrowhisper compares folder names as STRINGS at RecordingsFolderWatcher.swift:349
    (`dirName < mostRecentExisting`) to decide whether an appearing folder predates
    what it has already seen. A bare `date +%s%N`, as the original spike plan
    proposed, is variable-width and therefore sorts incorrectly across any digit-count
    boundary. Zero-padding removes that whole class of bug.

    The counter disambiguates two folders created within the same nanosecond, which
    is reachable when several threads publish at once.
    """
    return f"{now_ns:0{_NS_WIDTH}d}-{counter:0{_COUNTER_WIDTH}d}"


def successor_name(name: str) -> Optional[str]:
    """The smallest valid recording name strictly greater than `name`.

    Returns None when `name` is not one of ours, or is already the maximum
    representable name. None means "no name in our format can beat this", which
    the caller must treat as "do not publish", never as "publish anyway".

    This exists so the publisher never has to spin waiting for the wall clock to
    overtake a name. See _next_publish_name for why spinning was unsafe.
    """
    match = _NAME_RE.match(name or "")
    if not match:
        return None
    now_ns, counter = int(match.group(1)), int(match.group(2))
    if counter + 1 < 10**_COUNTER_WIDTH:
        return generate_recording_name(now_ns, counter + 1)
    if now_ns + 1 >= 10**_NS_WIDTH:
        return None
    return generate_recording_name(now_ns + 1, 0)


@dataclass
class PublishOutcome:
    """What one publish() call actually did."""

    spooled: Path
    published: List[Path] = field(default_factory=list)
    deferred: bool = False


class Publisher:
    """Stages, spools and publishes recording folders for one watch root."""

    def __init__(
        self,
        watch_root: Path,
        min_gap_s: float = DEFAULT_MIN_GAP_S,
        drain_budget_s: float = DEFAULT_DRAIN_BUDGET_S,
        listener=None,
    ) -> None:
        self.watch_root = Path(watch_root).expanduser()
        self.min_gap_s = min_gap_s
        self.drain_budget_s = drain_budget_s
        # G3. Optional callable returning True (listening), False (proven not
        # listening) or None (could not tell). None here, the DEFAULT, means no
        # probe at all and unconditional publishing, which is the behaviour every
        # caller had before this existed. Injected rather than imported so this
        # module keeps knowing nothing about macrowhisper's CLI.
        self._listener = listener
        # True only after a drain in which the probe returned a definite False.
        # The CLI reads this to log the right thing: "spooled because nothing is
        # listening" and "spooled because another process holds the lock" are
        # very different messages to a user wondering where their words went.
        self.listener_said_down = False
        self._counter = 0
        # published folder name -> the spool name it came from, so publish() can tell
        # whether ITS transcript made it out rather than just "something did".
        self._published_from: Dict[str, str] = {}

    # Layout -----------------------------------------------------------------

    @property
    def recordings_dir(self) -> Path:
        """The ONLY directory macrowhisper watches."""
        return self.watch_root / "recordings"

    @property
    def staging_dir(self) -> Path:
        return self.watch_root / ".staging"

    @property
    def spool_dir(self) -> Path:
        return self.watch_root / ".spool"

    @property
    def lock_path(self) -> Path:
        return self.watch_root / ".drain.lock"

    @property
    def _last_publish_path(self) -> Path:
        return self.watch_root / ".last-publish"

    def _ensure_layout(self) -> None:
        """Create the watch layout if absent.

        Helper directories are dotted and live beside recordings/ rather than inside
        it, because anything inside recordings/ would be detected as a recording.
        """
        for directory in (self.recordings_dir, self.staging_dir, self.spool_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # Staging ----------------------------------------------------------------

    def _next_name(self) -> str:
        name = generate_recording_name(time.time_ns(), self._counter)
        self._counter = (self._counter + 1) % (10**_COUNTER_WIDTH)
        return name

    def stage(self, meta: Dict[str, Any]) -> Path:
        """Build a complete recording folder and move it into the spool.

        Returns the spooled folder path. Fast, unconditional, and the point after
        which the transcript cannot be lost.
        """
        self._ensure_layout()

        # This method must NEVER raise. It runs before the spool, and the whole
        # design rests on "once a transcript is in the spool it cannot be lost".
        # An exception here means it never got there: cli.py catches it, logs, and
        # exits 0 by policy, so the user's words are simply gone with no error.
        #
        # Every dictation is a fresh PROCESS, so every Publisher starts at counter
        # 0, and two landing in the same nanosecond mint an IDENTICAL name. There
        # are two places that collides, and the original code guarded neither
        # atomically:
        #
        #   staging: `while staged.exists()` then `mkdir()` is check-then-act.
        #            Both processes saw "no", both called mkdir, the loser got
        #            FileExistsError. Observed for real on 2026-08-08, in about
        #            1 run in 8 of the five-concurrent integration test.
        #   spool:   never checked at all. rename() onto an existing non-empty
        #            directory fails with ENOTEMPTY.
        #
        # Both are closed by letting the atomic syscall be the test and retrying
        # with a fresh name. mkdir(2) and rename(2) are atomic; exists() is not.
        payload = serialize_meta(meta)

        for _ in range(_STAGE_ATTEMPTS):
            name = self._next_name()
            staged = self.staging_dir / name
            try:
                staged.mkdir(parents=True)
            except FileExistsError:
                continue  # another process holds this staging name

            meta_path = staged / "meta.json"
            # fsync the file before the directory becomes reachable, so a crash
            # cannot leave a folder holding a truncated meta.json.
            with open(meta_path, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())

            spooled = self.spool_dir / name
            try:
                os.rename(staged, spooled)
            except OSError:
                # Another process already spooled this name. Drop our copy and
                # take a different one; the transcript is still in hand.
                shutil.rmtree(staged, ignore_errors=True)
                continue
            return spooled

        raise RuntimeError(  # pragma: no cover - needs a clock frozen for 10,000 tries
            f"could not mint a free recording name in {_STAGE_ATTEMPTS} attempts; "
            "the system clock appears to be frozen"
        )

    # Draining ---------------------------------------------------------------

    def _max_published_name(self) -> str:
        """Lexicographic maximum of the names already in recordings/.

        String comparison, not numeric, because that is exactly what macrowhisper
        does at RecordingsFolderWatcher.swift:349-350.
        """
        try:
            names = [p.name for p in self.recordings_dir.iterdir() if p.is_dir()]
        except OSError:  # pragma: no cover
            return ""
        return max(names) if names else ""

    def _next_publish_name(self) -> Optional[str]:
        """Mint the name a folder will carry inside recordings/.

        This MUST be generated at publish time rather than at stage time, and MUST
        exceed every name already present. macrowhisper drops any single new folder
        sorting below the newest existing one, treating it as cloud-sync replay:

            RecordingsFolderWatcher.swift:350
                if let mostRecentExisting = mostRecentExistingDir,
                   dirName < mostRecentExisting { markAsProcessed(...) }

        Staging names encode arrival order so the spool drains FIFO. Published names
        encode publication order so macrowhisper accepts them. Conflating the two
        loses transcripts whenever a drain publishes out of arrival order, which is
        routine under concurrency. Verified live on 2026-08-05: 5 concurrent
        dictations, 5 folders published, only 4 actions fired before this fix.

        Returns None when no name in our format can exceed what is already there.
        The caller must then leave the folder spooled: publishing a name below the
        ceiling would have macrowhisper silently discard it, which is the exact
        failure this method exists to prevent.

        This used to spin on the clock: `while candidate <= ceiling: mint; sleep(1ms)`.
        That assumed the clock would eventually overtake the ceiling, and two
        reachable cases break the assumption permanently, hanging the process:

          1. A FUTURE-DATED folder, from a clock that jumped forward and was then
             corrected (NTP, restored VM snapshot, DST or timezone bug, dual boot).
             macrowhisper keeps folders by default, so it persists indefinitely.
          2. A folder whose name starts with a LETTER, e.g. `zz-old-backup`. Every
             letter sorts above every digit, so no numeric name can ever win.

        Either one wedged drain() until VoiceInk's 10-second kill, forever, with the
        spool growing and nothing published. Deriving the successor arithmetically
        is deterministic, instant, and cannot loop.
        """
        candidate = generate_recording_name(time.time_ns(), self._counter)
        self._counter = (self._counter + 1) % (10**_COUNTER_WIDTH)

        ceiling = self._max_published_name()
        if candidate > ceiling:
            return candidate
        # The clock cannot beat the ceiling: step just past it instead of waiting.
        return successor_name(ceiling)

    def _spooled_folders(self) -> List[Path]:
        if not self.spool_dir.exists():
            return []
        # Names are monotonic, so sorting by name is oldest-first.
        return sorted((p for p in self.spool_dir.iterdir() if p.is_dir()), key=lambda p: p.name)

    def _read_last_publish(self) -> float:
        """Last publish time, as a monotonic-comparable wall clock.

        Persisted on disk because the gap must hold across separate processes: each
        VoiceInk dictation runs a brand-new macrovoice, so in-memory state is useless.
        """
        try:
            return float(self._last_publish_path.read_text().strip())
        except (OSError, ValueError):
            return 0.0

    def _write_last_publish(self, when: float) -> None:
        try:
            self._last_publish_path.write_text(f"{when:.6f}")
        except OSError:  # pragma: no cover - non-fatal; spacing degrades, nothing breaks
            pass

    def _wait_for_gap(self, deadline: float) -> bool:
        """Sleep until the minimum gap since the last publish has elapsed.

        Returns False if waiting would exceed the drain deadline, in which case the
        caller must stop draining and leave the rest spooled.

        The wait is capped at min_gap_s. `.last-publish` is read from disk, and a
        value in the future (a clock jump forward then an NTP correction back, a
        restored VM snapshot, a hand-edited file) would otherwise make `remaining`
        arbitrarily large, trip the deadline guard below, and stall every later
        publish permanently. Measured 2026-08-09: transcripts spooled and were
        never delivered, with nothing surfacing it.

        Capping is safe because the gap IS the longest wait the spacing rule can
        ever require. Rejecting a future timestamp instead would misfire on the
        ordinary clock jitter between two processes and skip the gap entirely,
        weakening the defence against macrowhisper's burst protection dropping
        dictations. Do not "simplify" this into a freshness check on the file.
        """
        elapsed = time.time() - self._read_last_publish()
        remaining = min(self.min_gap_s - elapsed, self.min_gap_s)
        if remaining <= 0:
            return True
        if time.monotonic() + remaining > deadline:
            return False
        time.sleep(remaining)
        return True

    def drain(self) -> List[Path]:
        """Publish spooled folders into recordings/, one at a time, with spacing.

        Acquires the drain lock non-blockingly. If another process holds it, returns
        immediately with nothing published: that holder will drain our folders too.

        Bounded by drain_budget_s so this can never approach VoiceInk's 10s kill.
        Anything not published stays in the spool for the next invocation.
        """
        self._ensure_layout()
        deadline = time.monotonic() + self.drain_budget_s
        published: List[Path] = []

        # G3: publishing into a watch directory nobody is watching is not merely
        # late delivery, it is destruction. macrowhisper's recordings watcher
        # marks every folder that already exists when it arms as processed and
        # runs none of them, so a folder published during an outage is dropped by
        # the very restart that should have delivered it.
        #
        # Probed ONCE per drain, before the loop, not per folder: it is a
        # subprocess, and the answer cannot meaningfully change inside one drain.
        #
        # Any failure publishes. See listener.py for why deferring on
        # uncertainty would be the worse bug.
        self.listener_said_down = False
        if self._listener is not None:
            try:
                if self._listener() is False:
                    self.listener_said_down = True
                    return published
            except Exception:
                # listener.is_listening already swallows everything, but this
                # module must not depend on its caller being careful: a probe
                # that raises must never cost a delivery.
                pass

        try:
            lock_handle = open(self.lock_path, "w")
        except OSError:  # pragma: no cover
            return published

        try:
            try:
                fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    return published  # someone else is draining; they will take ours
                raise  # pragma: no cover

            for folder in self._spooled_folders():
                if time.monotonic() >= deadline:
                    break
                if not self._wait_for_gap(deadline):
                    break

                # The name is minted HERE, not at stage time, and is guaranteed to
                # exceed everything already published. See _next_publish_name.
                target_name = self._next_publish_name()
                if target_name is None:
                    # Something in recordings/ outranks every name we can produce,
                    # e.g. a folder starting with a letter. Publishing anyway would
                    # have macrowhisper drop it silently. Stop and stay spooled: the
                    # words survive, and a later run recovers once it is gone.
                    break
                target = self.recordings_dir / target_name
                try:
                    os.rename(folder, target)
                except OSError:  # pragma: no cover - leave it spooled and retry later
                    continue

                self._published_from[target.name] = folder.name
                self._write_last_publish(time.time())
                published.append(target)
        finally:
            try:
                fcntl.flock(lock_handle, fcntl.LOCK_UN)
            finally:
                lock_handle.close()

        return published

    # The one call the CLI makes ---------------------------------------------

    def publish(self, meta: Dict[str, Any]) -> PublishOutcome:
        """Spool this recording, then drain the spool if we can get the lock.

        Always spools first. If the lock is held, returns immediately with
        deferred=True rather than waiting, because waiting risks VoiceInk's 10s kill
        while the lock holder is perfectly capable of publishing our folder for us.
        """
        spooled = self.stage(meta)
        published = self.drain()
        # `deferred` is about OUR folder specifically, not about whether the drain
        # accomplished anything. A drain can publish an older backlog entry and run
        # out of budget before reaching ours; reporting that as not-deferred would
        # misrepresent what happened in the log.
        ours_published = any(self._published_from.get(p.name) == spooled.name for p in published)
        return PublishOutcome(
            spooled=spooled,
            published=published,
            deferred=not ours_published,
        )
