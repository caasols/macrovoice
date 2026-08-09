"""Rendering, and the exit-code policy.

Pure: takes results, returns a string. No colour, no emoji, no em-dashes, and
no timestamps, which is what makes the output golden-file testable.

The exit code deliberately breaks cli.py's always-exit-0 rule. That rule exists
because a non-zero exit on the delivery path alarms the user without recovering
their words. A check has the opposite job. Do not "fix" this into exit 0.
"""

from typing import List

from .model import Outcome, Result, Severity

GROUPS = (
    ("pre.", "Prerequisites"),
    ("bridge.", "Bridge layout"),
    ("mw.", "macrowhisper"),
    ("vi.", "VoiceInk"),
    ("proof.", "Proof"),
)

_MARKERS = {
    (Outcome.OK, Severity.FAIL): "ok",
    (Outcome.OK, Severity.WARN): "ok",
    (Outcome.OK, Severity.INFO): "ok",
    (Outcome.PROBLEM, Severity.FAIL): "PROBLEM",
    (Outcome.PROBLEM, Severity.WARN): "warning",
    (Outcome.PROBLEM, Severity.INFO): "note",
    (Outcome.UNKNOWN, Severity.FAIL): "unknown",
    (Outcome.UNKNOWN, Severity.WARN): "unknown",
    (Outcome.UNKNOWN, Severity.INFO): "unknown",
}

_MARKER_WIDTH = 7
_INDENT = "  "
_DETAIL_INDENT = " " * (len(_INDENT) + _MARKER_WIDTH + 2)


def _group_of(check_id: str) -> str:
    for prefix, title in GROUPS:
        if check_id.startswith(prefix):
            return title
    return "Other"


def render(results: List[Result]) -> str:
    lines = ["macrovoice doctor", ""]

    for _, title in GROUPS + (("", "Other"),):
        in_group = [r for r in results if _group_of(r.check.id) == title]
        if not in_group:
            continue
        lines.append(title)
        for item in in_group:
            marker = _MARKERS[(item.finding.outcome, item.check.severity)]
            lines.append("%s%-*s %s" % (_INDENT, _MARKER_WIDTH, marker, item.check.title))
            if item.finding.detail:
                lines.append(_DETAIL_INDENT + item.finding.detail)
            if item.finding.blocked_by:
                lines.append(_DETAIL_INDENT + "blocked by " + item.finding.blocked_by)
            if item.finding.fix_hint:
                lines.append(_DETAIL_INDENT + "fix: " + item.finding.fix_hint)
        lines.append("")

    problems = sum(
        1
        for r in results
        if r.finding.outcome is Outcome.PROBLEM and r.check.severity is Severity.FAIL
    )
    warnings = sum(
        1
        for r in results
        if r.finding.outcome is Outcome.PROBLEM and r.check.severity is Severity.WARN
    )
    unknowns = sum(1 for r in results if r.finding.outcome is Outcome.UNKNOWN)
    oks = sum(1 for r in results if r.finding.outcome is Outcome.OK)

    lines.append(
        "%d problem%s, %d warning%s, %d unknown, %d ok"
        % (
            problems,
            "" if problems == 1 else "s",
            warnings,
            "" if warnings == 1 else "s",
            unknowns,
            oks,
        )
    )
    return "\n".join(lines) + "\n"


def exit_code(results: List[Result]) -> int:
    if any(
        r.finding.outcome is Outcome.PROBLEM and r.check.severity is Severity.FAIL
        for r in results
    ):
        return 1
    if any(
        r.finding.outcome is Outcome.UNKNOWN and r.check.severity is Severity.FAIL
        for r in results
    ):
        return 2
    return 0
