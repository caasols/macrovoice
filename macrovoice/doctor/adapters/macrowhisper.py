"""Adapter for the macrowhisper CLI, plus the parser for its --status output.

LIVENESS COMES FROM THE OUTPUT, NEVER THE EXIT CODE. main.swift:1115-1122:

    if args.contains("-s") || args.contains("--status") {
        if let response = socketCommunication.sendCommand(.status) { print(response) }
        else { print("macrowhisper is not running.") }
        exit(0)
    }

The parser is deliberately tolerant: unknown lines are ignored, and any field it
cannot find stays None, which surfaces to the user as UNKNOWN. The failure mode
we must never have is doctor reporting simEsc as safe because a line was
reworded upstream.
"""

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .process import CommandResult, run_command

NOT_RUNNING_SENTINEL = "macrowhisper is not running."
VERSION_KEY = "Macrowhisper version"

_AGE = re.compile(r"started\s+(\d+)\s*([smhd])\s+ago")
_PENDING = re.compile(r"pending\s+(\d+)")
_EXISTS = re.compile(r"\(exists:\s*(yes|no)\)")
_WATCHER_ARMED = re.compile(r"^(yes|no)\b")
_AGE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


@dataclass(frozen=True)
class StatusSnapshot:
    """Parsed `macrowhisper --status`. Every optional field is None when the
    output did not carry it, which is distinct from carrying a false value."""

    running: bool
    recognized: bool
    raw: str = ""
    version: Optional[str] = None
    config_path: Optional[str] = None
    watch_folder: Optional[str] = None
    recordings_folder: Optional[str] = None
    recordings_folder_exists: Optional[bool] = None
    watcher_present: Optional[bool] = None
    watcher_armed: Optional[bool] = None
    watcher_pending: Optional[int] = None
    watcher_started_ago_s: Optional[int] = None
    active_action: Optional[str] = None
    move_to: Optional[str] = None
    sim_esc: Optional[bool] = None


def yes_no(value: Optional[str]) -> Optional[bool]:
    """"yes"/"no" to a bool, and anything else to None. Never guesses."""
    if value is None:
        return None
    text = value.strip().lower()
    if text in ("yes", "true"):
        return True
    if text in ("no", "false"):
        return False
    return None


def _parse_watcher_armed(value: Optional[str]) -> Optional[bool]:
    """Parse watcher armed status from RecordingsFolderWatcher.swift:206-214.

    Matches three possible strings:
    - "yes (armed, ...)" returns True
    - "no (not armed)" returns False
    - "no (folder missing)" returns False
    - Any other string returns None (never guesses)
    """
    if not value:
        return None
    match = _WATCHER_ARMED.match(value)
    if not match:
        return None
    return yes_no(match.group(1))


def parse_status(text: str) -> StatusSnapshot:
    if NOT_RUNNING_SENTINEL in text:
        return StatusSnapshot(running=False, recognized=True, raw=text)

    fields = {}
    for line in text.splitlines():
        if ": " not in line:
            continue
        key, _, value = line.partition(": ")
        fields[key.strip()] = value.strip()

    if VERSION_KEY not in fields:
        # Neither the sentinel nor a version line: this is not macrowhisper's
        # output at all. Say so rather than reporting a daemon-shaped nothing.
        return StatusSnapshot(running=False, recognized=False, raw=text)

    watcher = fields.get("Recordings watcher", "")
    pending = _PENDING.search(watcher)
    age = _AGE.search(watcher)

    recordings = fields.get("Recordings folder")
    exists = None
    if recordings is not None:
        found = _EXISTS.search(recordings)
        if found:
            exists = found.group(1) == "yes"
            recordings = _EXISTS.sub("", recordings).strip()

    return StatusSnapshot(
        running=True,
        recognized=True,
        raw=text,
        version=fields.get(VERSION_KEY),
        config_path=fields.get("Config file"),
        watch_folder=fields.get("Superwhisper folder"),
        recordings_folder=recordings,
        recordings_folder_exists=exists,
        watcher_present=yes_no(watcher.split(" ")[0]) if watcher else None,
        watcher_armed=_parse_watcher_armed(watcher),
        watcher_pending=int(pending.group(1)) if pending else None,
        watcher_started_ago_s=(
            int(age.group(1)) * _AGE_UNITS[age.group(2)] if age else None
        ),
        active_action=fields.get("Active action"),
        move_to=fields.get("moveTo"),
        sim_esc=yes_no(fields.get("simEsc")),
    )


DEFAULT_TIMEOUT_S = 10.0
DEFAULT_LOG_PATH = "~/Library/Logs/Macrowhisper/macrowhisper.log"
LOG_TAIL_BYTES = 262144
SAVED_CONFIG_PREFIX = "Saved config path:"
ACCESS_GRANTED = "Accessibility permissions already granted"
ACCESS_DENIED = "Accessibility permissions were not granted"
CONFIG_VALID = "Configuration is valid"
ACTION_CATEGORIES = ("inserts", "urls", "shortcuts", "scriptsShell", "scriptsAS")

_LOG_STAMP = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")


class Macrowhisper:
    """Read-only view of a macrowhisper install. Stage 1 never mutates anything.

    `runner` is injectable so the whole class is testable without a daemon.
    """

    def __init__(self, binary="macrowhisper", timeout=DEFAULT_TIMEOUT_S, runner=None,
                 log_path=DEFAULT_LOG_PATH):
        self.binary = binary
        self.timeout = timeout
        self._runner = runner if runner is not None else run_command
        self._log_path = Path(log_path).expanduser()
        self._status = None
        self._saved_config = _UNSET

    def _run(self, *args) -> CommandResult:
        return self._runner([self.binary] + list(args), self.timeout)

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def invalidate(self) -> None:
        """Drop cached reads. Stage 3's second convergence pass needs this."""
        self._status = None
        self._saved_config = _UNSET

    def status(self, refresh: bool = False) -> StatusSnapshot:
        if self._status is None or refresh:
            result = self._run("--status")
            if result.timed_out or result.returncode is None:
                self._status = StatusSnapshot(
                    running=False, recognized=False, raw=result.stderr
                )
            else:
                self._status = parse_status(result.stdout)
        return self._status

    def saved_config_path(self) -> Optional[str]:
        if self._saved_config is _UNSET:
            result = self._run("--get-config")
            value = None
            if not result.timed_out and result.returncode is not None:
                for line in result.stdout.splitlines():
                    if line.startswith(SAVED_CONFIG_PREFIX):
                        value = line[len(SAVED_CONFIG_PREFIX):].strip() or None
                        break
            self._saved_config = value
        return self._saved_config

    def validate_config(self) -> Tuple[Optional[bool], str]:
        result = self._run("--validate-config")
        if result.timed_out or result.returncode is None:
            return None, result.stderr
        return CONFIG_VALID in result.stdout, result.stdout.strip()

    def service_installed(self) -> Optional[bool]:
        result = self._run("--service-status")
        if result.timed_out or result.returncode is None:
            return None
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("Installed:"):
                return yes_no(stripped.partition(":")[2])
        return None

    def read_config(self, path) -> Optional[dict]:
        try:
            text = Path(path).expanduser().read_text(encoding="utf-8")
            document = json.loads(text)
        except (OSError, ValueError):
            return None
        return document if isinstance(document, dict) else None

    def accessibility_state(self) -> Tuple[Optional[bool], Optional[datetime]]:
        """(granted, when) from the NEWEST Accessibility line, else (None, None).

        macrowhisper checks and logs this at startup, in the same instant it
        raises the permission prompt, so the line is stale the moment the user
        clicks Allow and stays stale until the daemon restarts (friction trap
        4). That is exactly why the newest line describes the CURRENT daemon,
        and why a caller must compare it against the daemon's start rather than
        reading it as live state.
        """
        try:
            with open(self._log_path, "rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - LOG_TAIL_BYTES))
                tail = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return None, None

        for line in reversed(tail.splitlines()):
            if ACCESS_GRANTED in line:
                return True, _log_time(line)
            if ACCESS_DENIED in line:
                return False, _log_time(line)
        return None, None


class _Unset:
    pass


_UNSET = _Unset()


def _log_time(line: str) -> Optional[datetime]:
    found = _LOG_STAMP.match(line)
    if not found:
        return None
    try:
        return datetime.strptime(found.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
