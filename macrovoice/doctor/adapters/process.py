"""One subprocess wrapper, with a timeout, shared by every adapter.

Every external call in doctor goes through here. A hung subprocess must become
UNKNOWN, never a hang: a diagnostic that can block forever is worse than no
diagnostic. That is B5's lesson applied to a different surface.
"""

import subprocess
from typing import Callable, List, NamedTuple, Optional


class CommandResult(NamedTuple):
    returncode: Optional[int]   # None when the command could not be run at all
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_command(args: List[str], timeout: float) -> CommandResult:
    try:
        completed = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return CommandResult(None, "", "timed out after %.1fs" % timeout, True)
    except OSError as exc:
        return CommandResult(None, "", str(exc), False)
    return CommandResult(completed.returncode, completed.stdout, completed.stderr, False)


Runner = Callable[[List[str], float], CommandResult]
