"""Value types for `macrovoice doctor`.

Pure: no filesystem, no subprocess, no imports from the rest of the package. That
is what lets the entire check catalogue be tested with fake adapters and no Mac
state.

THREE outcomes, not two, and this is load-bearing. If macrowhisper is not
running, `simEsc` is not failing, it is UNKNOWABLE. Reporting it as a failure
would reproduce, inside the tool built to cure the problem, the exact shape that
makes twelve of the thirteen setup traps look like "the bridge is broken".
See docs/product/2026-08-08-first-run-friction.md.
"""

import enum
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple


class Outcome(enum.Enum):
    OK = "ok"
    PROBLEM = "problem"
    UNKNOWN = "unknown"


class Severity(enum.Enum):
    FAIL = "fail"      # the bridge cannot work
    WARN = "warn"      # it works, but this will bite
    INFO = "info"      # worth saying, never a failure


@dataclass(frozen=True)
class Finding:
    """What one check observed. `detail` is one line; `fix_hint` is what to do
    about it, when there is something to do. Usually that is a single
    runnable command, but not always: some findings have no single command
    that fixes them, so `fix_hint` is a short instruction or sentence instead.
    Empty when there is nothing to suggest.
    """

    outcome: Outcome
    detail: str = ""
    fix_hint: str = ""
    blocked_by: Optional[str] = None

    @classmethod
    def ok(cls, detail: str = "") -> "Finding":
        return cls(Outcome.OK, detail)

    @classmethod
    def problem(cls, detail: str, fix_hint: str = "") -> "Finding":
        return cls(Outcome.PROBLEM, detail, fix_hint)

    @classmethod
    def unknown(cls, detail: str = "", blocked_by: Optional[str] = None) -> "Finding":
        return cls(Outcome.UNKNOWN, detail, "", blocked_by)


@dataclass(frozen=True)
class Check:
    """One row of the table in registry.py.

    `repair` and `repair_prompt` are deliberately absent: repairs arrive in
    stage 3, and dead fields age worse than a later edit.
    """

    id: str
    title: str
    severity: Severity
    inspect: Callable[["Context"], Finding]
    depends_on: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Result:
    check: Check
    finding: Finding


@dataclass(frozen=True)
class Context:
    """Everything a check is allowed to touch. Adapters are injected, never
    constructed inside a check, which is what keeps the catalogue testable."""

    watch_root: Path
    mw: object
    bridge: object
    # Defaulted so every existing construction site keeps working. A check that
    # needs it must handle None, which is what a caller building a
    # macrowhisper-only Context passes; the vi.* checks report UNKNOWN in that
    # case rather than assuming VoiceInk is absent.
    vi: object = None
    # The user's home directory, injectable for the same reason the adapters
    # are: `bridge.legacywatch` has to reason about `~/mw-bridge` and
    # `~/macrovoice` by name, and a check that reads the real home cannot be
    # tested without touching the developer's own machine. None means the real
    # one; see `home_dir` below.
    home: Optional[Path] = None

    def home_dir(self) -> Path:
        return Path.home() if self.home is None else Path(self.home)
