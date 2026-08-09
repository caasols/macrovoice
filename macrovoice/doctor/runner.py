"""Ordering and execution of the check table.

Two rules carry the design:

1. A check whose dependency is not OK is UNKNOWN, naming the blocker, and is
   never counted as a failure. Fifteen alarms caused by one missing install is
   the exact "everything looks broken" shape this tool exists to remove.
2. A check that raises becomes UNKNOWN and the run continues. A diagnostic that
   dies on its own bug tells the user nothing at all.
"""

from typing import Dict, List

from .model import Check, Context, Finding, Outcome, Result


def order_checks(checks) -> List[Check]:
    """Kahn's algorithm, preserving declaration order among independent checks
    so the report reads in the order a user meets the traps.

    Raises ValueError on an unknown dependency or a cycle. Both are bugs in the
    table, not conditions on the user's machine, so they must fail loudly.
    """
    by_id = {}
    for check in checks:
        if check.id in by_id:
            raise ValueError("duplicate check id: %s" % check.id)
        by_id[check.id] = check

    for check in checks:
        for dependency in check.depends_on:
            if dependency not in by_id:
                raise ValueError(
                    "check %s depends on unknown check %s" % (check.id, dependency)
                )

    ordered: List[Check] = []
    placed = set()
    remaining = list(checks)
    while remaining:
        progressed = False
        still_waiting = []
        for check in remaining:
            if all(dependency in placed for dependency in check.depends_on):
                ordered.append(check)
                placed.add(check.id)
                progressed = True
            else:
                still_waiting.append(check)
        if not progressed:
            raise ValueError(
                "dependency cycle among: %s"
                % ", ".join(check.id for check in still_waiting)
            )
        remaining = still_waiting
    return ordered


def run(checks, ctx: Context) -> List[Result]:
    ordered = order_checks(checks)
    findings: Dict[str, Finding] = {}

    for check in ordered:
        blocker = None
        for dependency in check.depends_on:
            if findings[dependency].outcome is not Outcome.OK:
                blocker = dependency
                break

        if blocker is not None:
            findings[check.id] = Finding.unknown(blocked_by=blocker)
            continue

        try:
            findings[check.id] = check.inspect(ctx)
        except Exception as exc:  # noqa: BLE001 - containment is the point
            findings[check.id] = Finding.unknown(
                "check raised %s: %s" % (type(exc).__name__, exc)
            )

    return [Result(check, findings[check.id]) for check in ordered]
