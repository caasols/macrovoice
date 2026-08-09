"""Our own side of the bridge: the watch directory, the spool, the log, the
wrapper script, and the interpreter the wrapper will actually get.

Stage 1 is read-only. Nothing here creates a directory, which is why the
missing-watch-root test asserts the root still does not exist afterwards.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

from .process import run_command

LOG_NAME = "macrovoice.log"
LOG_TAIL_BYTES = 65536
ENV_PYTHON_PROBE = (
    "import sys, platform; print(sys.executable); print(platform.python_version())"
)
ENV_PYTHON_TIMEOUT_S = 10.0
MAX_REPORTED_ERRORS = 5


@dataclass(frozen=True)
class BridgeSnapshot:
    watch_root: Path
    watch_exists: bool
    recordings_exists: bool
    recordings_count: int
    spool_count: int
    staging_count: int


def _count(directory: Path) -> int:
    try:
        return sum(1 for _ in directory.iterdir())
    except OSError:
        return 0


class BridgeState:
    def __init__(self, watch_root, repo_root=None, runner=None):
        self.watch_root = Path(watch_root).expanduser()
        # bridge.py -> adapters -> doctor -> macrovoice -> repo root
        self.repo_root = (
            Path(repo_root) if repo_root else Path(__file__).resolve().parents[3]
        )
        self._runner = runner if runner is not None else run_command

    def snapshot(self) -> BridgeSnapshot:
        recordings = self.watch_root / "recordings"
        return BridgeSnapshot(
            watch_root=self.watch_root,
            watch_exists=self.watch_root.is_dir(),
            recordings_exists=recordings.is_dir(),
            recordings_count=_count(recordings),
            spool_count=_count(self.watch_root / ".spool"),
            staging_count=_count(self.watch_root / ".staging"),
        )

    def script_path(self) -> Path:
        return self.repo_root / "macrovoice.sh"

    def env_python(self) -> Tuple[Optional[str], Optional[str]]:
        """The interpreter `/usr/bin/env python3` resolves to, which is what
        macrovoice.sh execs. Deliberately NOT sys.version_info: doctor may be
        run under a different interpreter than the wrapper will get."""
        result = self._runner(
            ["/usr/bin/env", "python3", "-c", ENV_PYTHON_PROBE], ENV_PYTHON_TIMEOUT_S
        )
        if result.timed_out or result.returncode is None or result.returncode != 0:
            return None, None
        lines = result.stdout.strip().splitlines()
        if len(lines) < 2:
            return None, None
        return lines[0].strip(), lines[1].strip()

    def recent_log_errors(self, within_hours: int = 24) -> Tuple[str, ...]:
        path = self.watch_root / LOG_NAME
        try:
            with open(path, "rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - LOG_TAIL_BYTES))
                tail = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return ()

        cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
        lines = tail.splitlines()
        found = []

        i = 0
        while i < len(lines):
            line = lines[i]

            # Check if this line starts with a timestamp (header line).
            stamp = line.split(" ", 1)[0] if line else ""
            try:
                when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                # Not a header line, skip it.
                i += 1
                continue

            # It is a header line. Check if it contains ERROR and is recent.
            if " ERROR" not in line or when < cutoff:
                i += 1
                continue

            # Found a matching error entry. Collect continuation lines.
            entry = line
            i += 1
            continuation_lines = []

            while i < len(lines):
                next_line = lines[i]
                # Check if this is a new header (starts with timestamp).
                next_stamp = next_line.split(" ", 1)[0] if next_line else ""
                try:
                    datetime.strptime(next_stamp, "%Y-%m-%dT%H:%M:%SZ")
                    # It is a new header, stop collecting continuation lines.
                    break
                except ValueError:
                    # Not a header, it is a continuation line.
                    continuation_lines.append(next_line)
                    i += 1

            # If there are continuation lines, append the last non-empty one.
            if continuation_lines:
                # Find the last non-empty line.
                for cont_line in reversed(continuation_lines):
                    if cont_line.strip():
                        entry = entry + " ... " + cont_line.strip()
                        break

            found.append(entry)

        return tuple(found[-MAX_REPORTED_ERRORS:])
