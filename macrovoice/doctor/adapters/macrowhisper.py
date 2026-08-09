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

import re
from dataclasses import dataclass
from typing import Optional

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
