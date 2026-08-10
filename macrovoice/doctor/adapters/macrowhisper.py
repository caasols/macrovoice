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
from typing import Optional, Tuple

from .process import CommandResult, run_command

NOT_RUNNING_SENTINEL = "macrowhisper is not running."
VERSION_KEY = "Macrowhisper version"

_AGE = re.compile(r"started\s+(\d+)\s*([smhd])\s+ago")
_AGE_JUST_NOW = re.compile(r"started\s+just\s+now")
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


@dataclass(frozen=True)
class ConfigPath:
    """Where macrowhisper reads its config, and whether that was a deliberate choice.

    `--get-config` prints one of two lines (main.swift:899-906): a persisted path,
    or the default it fell back to because none was persisted. Both name a real,
    usable path, but only a PERSISTED one can be the integration-test hijack that
    doctor's mw.configpath check exists to catch.
    """

    path: str
    persisted: bool


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
        return StatusSnapshot(running=False, recognized=True)

    fields = {}
    for line in text.splitlines():
        if ": " not in line:
            continue
        key, _, value = line.partition(": ")
        fields[key.strip()] = value.strip()

    if VERSION_KEY not in fields:
        # Neither the sentinel nor a version line: this is not macrowhisper's
        # output at all. Say so rather than reporting a daemon-shaped nothing.
        return StatusSnapshot(running=False, recognized=False)

    watcher = fields.get("Recordings watcher", "")
    pending = _PENDING.search(watcher)
    age = _AGE.search(watcher)
    # describeStatusAge (RecordingsFolderWatcher.swift:226-243) reports ages
    # under 5 seconds as "just now": no digits, no unit, no "ago" suffix. The
    # digit-based _AGE regex cannot match that, so it needs its own case.
    # "never" (no start recorded) intentionally matches neither and stays None.
    just_now = _AGE_JUST_NOW.search(watcher) if not age else None

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
        version=fields.get(VERSION_KEY),
        config_path=fields.get("Config file"),
        watch_folder=fields.get("Superwhisper folder"),
        recordings_folder=recordings,
        recordings_folder_exists=exists,
        watcher_present=yes_no(watcher.split(" ")[0]) if watcher else None,
        watcher_armed=_parse_watcher_armed(watcher),
        watcher_pending=int(pending.group(1)) if pending else None,
        watcher_started_ago_s=(
            int(age.group(1)) * _AGE_UNITS[age.group(2)]
            if age
            else (0 if just_now else None)
        ),
        active_action=fields.get("Active action"),
        move_to=fields.get("moveTo"),
        sim_esc=yes_no(fields.get("simEsc")),
    )


DEFAULT_TIMEOUT_S = 10.0
DEFAULT_LOG_PATH = "~/Library/Logs/Macrowhisper/macrowhisper.log"
SAVED_CONFIG_PREFIX = "Saved config path:"
DEFAULT_CONFIG_PREFIX = "Using default config path:"
ACCESS_GRANTED = "Accessibility permissions already granted"
ACCESS_DENIED = "Accessibility permissions were not granted"
CONFIG_VALID = "Configuration is valid"

_LOG_STAMP = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")


def _newest_access_line(path):
    """(granted, raw_line) for the NEWEST Accessibility line in `path`, else None.

    Streams the whole file forward rather than tail-seeking a window. The
    window read this replaced was unsound: macrowhisper logs its Accessibility
    line exactly once per process, at startup (main.swift:1905, logging one of
    Accessibility.swift:51 or :62), so a daemon that restarts and then logs
    past the window drops its own line out of view. With the rotated-log
    fallback in place that would surface the PREVIOUS process's verdict as if
    it described the running one.

    Returns None, never raises, when the file is absent or unreadable: doctor
    is read-only and a check must never crash.
    """
    if path is None:
        return None
    found = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if ACCESS_GRANTED in line:
                    found = (True, line)
                elif ACCESS_DENIED in line:
                    found = (False, line)
    except OSError:
        return None
    return found


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
        self._config_path = _UNSET

    def _run(self, *args) -> CommandResult:
        return self._runner([self.binary] + list(args), self.timeout)

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def invalidate(self) -> None:
        """Drop cached reads. Stage 3's second convergence pass needs this."""
        self._status = None
        self._config_path = _UNSET

    def status(self, refresh: bool = False) -> StatusSnapshot:
        if self._status is None or refresh:
            result = self._run("--status")
            if result.timed_out or result.returncode is None:
                self._status = StatusSnapshot(running=False, recognized=False)
            else:
                self._status = parse_status(result.stdout)
        return self._status

    def config_path(self) -> Optional[ConfigPath]:
        if self._config_path is _UNSET:
            result = self._run("--get-config")
            value = None
            if not result.timed_out and result.returncode is not None:
                for line in result.stdout.splitlines():
                    for prefix, persisted in (
                        (SAVED_CONFIG_PREFIX, True),
                        (DEFAULT_CONFIG_PREFIX, False),
                    ):
                        if line.startswith(prefix):
                            text = line[len(prefix):].strip()
                            if text:
                                value = ConfigPath(text, persisted)
                            break
                    if value is not None:
                        break
            self._config_path = value
        return self._config_path

    def saved_config_path(self) -> Optional[str]:
        """The config path macrowhisper will read, persisted or not."""
        found = self.config_path()
        return found.path if found else None

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
        if path is None:
            return None
        try:
            text = Path(path).expanduser().read_text(encoding="utf-8")
            document = json.loads(text)
        except (OSError, ValueError):
            return None
        return document if isinstance(document, dict) else None

    def accessibility_state(self) -> Tuple[Optional[bool], Optional[datetime]]:
        """(granted, when) from the NEWEST Accessibility line, else (None, None).

        macrowhisper logs this once per process, at startup, in the same
        instant it raises the permission prompt. Because every startup emits
        exactly one such line, the newest one describes the CURRENT daemon,
        and there is no freshness comparison to make. There used to be a false
        one here; see the docstring on registry._check_accessibility.

        The whole file is scanned, not a tail window. See _newest_access_line.
        """
        found = _newest_access_line(self._log_path)
        if found is None:
            return None, None
        granted, line = found
        return granted, _log_time(line)


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
