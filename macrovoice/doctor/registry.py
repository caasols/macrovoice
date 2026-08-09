"""The check table: the specification of what doctor knows.

Ordered the way a user meets the traps, which is also roughly dependency order.
Each entry is data. Adding a trap is one function and one row.
"""

import os
import tempfile
from pathlib import Path

from .model import Check, Finding, Severity

VOICEINK_APP_PATHS = (
    Path("/Applications/VoiceInk.app"),
    Path("~/Applications/VoiceInk.app").expanduser(),
)
MIN_PYTHON = (3, 9)
BREW_INSTALL = "brew install ognistik/formulae/macrowhisper"
DEFAULT_CLIPBOARD_BUFFER_S = 5.0
RECOMMENDED_CLIPBOARD_BUFFER_S = 60.0
# The categories a config file can define an action under. Only consumer is
# _check_action below (AppConfiguration.swift:957: defaults, inserts, urls,
# shortcuts, scriptsShell, scriptsAS).
ACTION_CATEGORIES = ("inserts", "urls", "shortcuts", "scriptsShell", "scriptsAS")
CONFIG_SURGERY_HINT = (
    "macrowhisper --stop-service, edit %s, macrowhisper --start-service, "
    "then confirm with macrowhisper --status"
)


def _version_tuple(text):
    parts = []
    for piece in text.split("."):
        digits = ""
        for char in piece:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _check_env_python(ctx):
    """The interpreter macrovoice.sh will actually get, not the one running us."""
    executable, version = ctx.bridge.env_python()
    if version is None:
        return Finding.unknown("could not run /usr/bin/env python3")
    if _version_tuple(version) < MIN_PYTHON:
        return Finding.problem(
            "/usr/bin/env python3 is %s at %s" % (version, executable),
            "macrovoice needs Python %d.%d or newer" % MIN_PYTHON,
        )
    return Finding.ok("/usr/bin/env python3 is %s" % version)


def _check_macrowhisper_installed(ctx):
    if ctx.mw.available():
        return Finding.ok()
    return Finding.problem("macrowhisper is not on PATH", BREW_INSTALL)


def _check_voiceink_installed(ctx):
    for path in VOICEINK_APP_PATHS:
        if path.exists():
            return Finding.ok("%s" % path)
    return Finding.problem(
        "VoiceInk.app not found in /Applications or ~/Applications",
        "install VoiceInk 2.0 or later, which is where the Custom Command output mode arrived",
    )


def _check_watch_dirs(ctx):
    snapshot = ctx.bridge.snapshot()
    if not snapshot.watch_exists:
        return Finding.problem(
            "watch root %s does not exist" % snapshot.watch_root,
            "mkdir -p %s/recordings" % snapshot.watch_root,
        )
    if not snapshot.recordings_exists:
        return Finding.problem(
            "%s exists but recordings/ does not" % snapshot.watch_root,
            "mkdir -p %s/recordings" % snapshot.watch_root,
        )
    return Finding.ok("%s" % snapshot.watch_root)


def _check_script(ctx):
    script = ctx.bridge.script_path()
    if not script.exists():
        return Finding.problem("%s does not exist" % script)
    if not os.access(str(script), os.X_OK):
        return Finding.problem("%s is not executable" % script, "chmod +x %s" % script)
    return Finding.ok("%s" % script)


def _check_spool(ctx):
    """A non-empty spool means publishing has been failing and transcripts are
    waiting. They are not lost: the design keeps them recoverable."""
    snapshot = ctx.bridge.snapshot()
    waiting = snapshot.spool_count + snapshot.staging_count
    if waiting:
        return Finding.problem(
            "%d transcript(s) waiting to be published (spool plus staging)" % waiting,
            "%s --drain-only --watch %s" % (ctx.bridge.script_path(), snapshot.watch_root),
        )
    return Finding.ok()


def _check_log_errors(ctx):
    errors = ctx.bridge.recent_log_errors()
    if errors:
        return Finding.problem(
            "%d error(s) in macrovoice.log in the last 24h, newest: %s"
            % (len(errors), errors[-1])
        )
    return Finding.ok()


def _is_temp_path(path):
    """The integration-test hijack: macrowhisper persists whatever --config it
    was last given, and a temp path left by a test run points the daemon at a
    directory that no longer exists. Friction trap 2."""
    prefixes = (tempfile.gettempdir(), "/var/folders/", "/private/var/folders/", "/tmp/")
    return any(str(path).startswith(prefix) for prefix in prefixes)


def _check_config_path(ctx):
    """Catch the integration-test hijack: macrowhisper persists whatever --config
    it was last given, and a temp path left by a test run points the daemon at a
    directory that no longer exists. Friction trap 2.

    Only a PERSISTED path can be that hijack. A fresh install has persisted
    nothing and is simply using its default, which is a healthy state, not a
    fault. Reporting it as unknown made six checks dead on a new machine.
    """
    found = ctx.mw.config_path()
    if found is None:
        return Finding.unknown("could not read macrowhisper --get-config")
    if found.persisted and _is_temp_path(found.path):
        return Finding.problem(
            "macrowhisper's saved config path is a temp directory: %s" % found.path,
            "macrowhisper --set-config ~/.config/macrowhisper/macrowhisper.json",
        )
    if found.persisted:
        return Finding.ok("%s (persisted)" % found.path)
    return Finding.ok("%s (macrowhisper's default, nothing persisted)" % found.path)


def _check_config_exists(ctx):
    saved = ctx.mw.saved_config_path()
    if saved is None:
        return Finding.unknown("no saved config path")
    if not Path(saved).expanduser().is_file():
        sample = ctx.bridge.script_path().parent / "macrovoice.sample.json"
        return Finding.problem(
            "%s does not exist" % saved,
            "cp %s %s" % (sample, saved),
        )
    return Finding.ok()


def _check_config_valid(ctx):
    valid, detail = ctx.mw.validate_config()
    if valid is None:
        return Finding.unknown("could not run macrowhisper --validate-config")
    if not valid:
        return Finding.problem(
            detail.splitlines()[0] if detail else "config is invalid",
            "macrowhisper --validate-config",
        )
    return Finding.ok()


def _check_watch_match(ctx):
    """`--status` reports the daemon's live in-memory `defaults.watch`
    (SocketCommunication.swift:3241 "Superwhisper folder: <path>", read from
    3254's `defaults.watch`), which is what the running daemon actually
    believes, not what is on disk. Preferring it here closes friction trap 5:
    editing the config file while the daemon is running can be silently
    discarded, so a check that only reads the file can report OK about a path
    the daemon has already stopped watching. Fall back to the file when the
    daemon is not running, since this check deliberately does not depend on
    mw.running.
    """
    status = ctx.mw.status()
    saved = ctx.mw.saved_config_path()
    if status.watch_folder is not None:
        configured = status.watch_folder
        source = "the running daemon"
    else:
        config = ctx.mw.read_config(saved) if saved else None
        if config is None:
            return Finding.unknown("could not read the config file")
        configured = config.get("defaults", {}).get("watch")
        if configured is None:
            return Finding.problem(
                "defaults.watch is not set in %s" % saved,
                CONFIG_SURGERY_HINT % saved,
            )
        source = str(saved)

    hint = CONFIG_SURGERY_HINT % (saved or "the config")
    if Path(configured).expanduser() != ctx.watch_root:
        return Finding.problem(
            "macrowhisper watches %s (from %s) but doctor is checking %s"
            % (configured, source, ctx.watch_root),
            hint,
        )
    return Finding.ok("%s (from %s)" % (configured, source))


def _check_service(ctx):
    installed = ctx.mw.service_installed()
    if installed is None:
        return Finding.unknown("could not run macrowhisper --service-status")
    if not installed:
        return Finding.problem(
            "macrowhisper is not installed as a service, so it will not start at login",
            "macrowhisper --install-service",
        )
    return Finding.ok()


def _check_running(ctx):
    status = ctx.mw.status()
    if not status.recognized:
        return Finding.unknown("could not recognise the output of macrowhisper --status")
    if not status.running:
        return Finding.problem(
            "macrowhisper is not running, so nothing is watching for dictations",
            "macrowhisper --start-service",
        )
    return Finding.ok("running, version %s" % (status.version or "unknown"))


def _check_armed(ctx):
    status = ctx.mw.status()
    if status.watcher_armed is None:
        return Finding.unknown("macrowhisper --status did not report the recordings watcher")
    if not status.watcher_armed:
        return Finding.problem(
            "the recordings watcher is not armed, so published folders are dropped",
            "macrowhisper --restart-service, then wait about 8 seconds",
        )
    return Finding.ok("armed, %s pending" % (status.watcher_pending
                                             if status.watcher_pending is not None else "?"))


def _check_folders(ctx):
    status = ctx.mw.status()
    if status.recordings_folder_exists is None:
        return Finding.unknown("macrowhisper --status did not report the recordings folder")
    if not status.recordings_folder_exists:
        if status.recordings_folder:
            return Finding.problem(
                "macrowhisper cannot see %s" % status.recordings_folder,
                "mkdir -p %s" % status.recordings_folder,
            )
        return Finding.problem(
            "macrowhisper did not report its recordings folder path"
        )
    return Finding.ok()


def _check_simesc(ctx):
    """The one setting that destroys user work. Friction trap 3.

    macrowhisper defaults simEsc to TRUE and posts a literal Escape keypress to
    the system-wide HID event tap before pasting (Accessibility.swift:477-494,
    simulateKeyDown key 53). Under Superwhisper that dismisses Superwhisper's
    own window. Under this bridge there is no such window, so the Escape lands
    in whatever app the user is typing into. Measured 2026-08-08: it closed a
    ProtonMail draft.
    """
    status = ctx.mw.status()
    if status.sim_esc is None:
        return Finding.unknown("macrowhisper --status did not report simEsc")
    if status.sim_esc:
        return Finding.problem(
            "simEsc is ON. macrowhisper posts an Escape keypress into your focused "
            "app before pasting, which discards drafts and closes dialogs",
            CONFIG_SURGERY_HINT % (ctx.mw.saved_config_path() or "the config"),
        )
    return Finding.ok()


def _check_moveto(ctx):
    """moveTo empty and moveTo "(none)" are the same state. `--status` never
    emits an empty string: SocketCommunication.swift:3259 prints
    `moveTo.isEmpty ? "(none)" : moveTo`, and AppConfiguration.swift:383 ships
    `moveTo: ""` as the default, so a stock, never-configured macrowhisper
    reports "(none)" here. RecordingsFolderWatcher.swift:1254 guards cleanup
    with `if let path = moveTo, !path.isEmpty`, so both spellings mean no
    cleanup. _check_action below handles the identical upstream idiom for
    "Active action"; this mirrors it.
    """
    status = ctx.mw.status()
    if status.move_to is None:
        return Finding.unknown("macrowhisper --status did not report moveTo")
    if not status.move_to or status.move_to == "(none)":
        return Finding.problem(
            "moveTo is not set, so macrowhisper never cleans up and every "
            "dictation stays on disk as plaintext indefinitely",
            CONFIG_SURGERY_HINT % (ctx.mw.saved_config_path() or "the config"),
        )
    return Finding.ok(status.move_to)


def _check_action(ctx):
    status = ctx.mw.status()
    action = status.active_action
    if not action or action == "(none)":
        return Finding.problem(
            "no active action is set, so macrowhisper will accept dictations and do nothing",
            "macrowhisper --action autoPaste",
        )
    saved = ctx.mw.saved_config_path()
    config = ctx.mw.read_config(saved) if saved else None
    if config is None:
        return Finding.unknown("could not read the config to confirm the action exists")
    for category in ACTION_CATEGORIES:
        bucket = config.get(category)
        if isinstance(bucket, dict) and action in bucket:
            return Finding.ok("%s (%s)" % (action, category))
    return Finding.problem(
        "the active action %r is not defined in %s" % (action, saved),
        "macrowhisper --action <a name that exists>",
    )


def _check_clipboard_buffer(ctx):
    """Under Superwhisper the recording folder appears when recording STARTS.
    Under this bridge it appears when dictation ENDS, so with the 5s default any
    dictation longer than five seconds loses its pre-recording clipboard
    entirely (RecordingsFolderWatcher.swift:441-443, ClipboardMonitor.swift:1139-1152).

    Reads the config file, not `--status`: unlike `defaults.watch`, `--status`
    has no line for `clipboardBuffer`, so there is no live value to prefer here.
    """
    saved = ctx.mw.saved_config_path()
    config = ctx.mw.read_config(saved) if saved else None
    if config is None:
        return Finding.unknown("could not read the config file")
    value = config.get("defaults", {}).get("clipboardBuffer")
    if value is None:
        return Finding.problem(
            "clipboardBuffer is unset, so it defaults to %.0fs and any dictation "
            "longer than that loses its pre-recording clipboard"
            % DEFAULT_CLIPBOARD_BUFFER_S,
            CONFIG_SURGERY_HINT % saved,
        )
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return Finding.problem(
            "clipboardBuffer is %r, which is not a number" % (value,),
            CONFIG_SURGERY_HINT % saved,
        )
    if float(value) <= DEFAULT_CLIPBOARD_BUFFER_S:
        return Finding.problem(
            "clipboardBuffer is %.0fs; dictations longer than that lose their "
            "pre-recording clipboard" % float(value),
            "raise it to %.0f" % RECOMMENDED_CLIPBOARD_BUFFER_S,
        )
    return Finding.ok("%.0fs" % float(value))


def _check_accessibility(ctx):
    """Friction trap 4: macrowhisper checks and logs Accessibility exactly once
    per process, unconditionally, at true process startup
    (main.swift:1905 requestAccessibilityPermissionOnStartup(), which logs one
    of Accessibility.swift:51 or :62), in the same instant it raises the
    prompt. Because every startup emits exactly one such line, the newest one
    in the log always describes the currently running process: there is no
    freshness comparison to make, and there used to be a false one here.

    A previous version of this check compared the grant line's timestamp
    against a "daemon start time" derived from `--status`'s watcher age
    (`watcher_started_ago_s`, itself parsed from RecordingsFolderWatcher.swift's
    watcherStartedAt). That derived time is NOT the process start time and must
    never be treated as one: main.swift:672-683 registers an
    NSWorkspace.didWakeNotification observer whose handler calls
    rearmFilesystemWatchersAfterWake() (main.swift:557-583), which restarts the
    watcher, and it is also re-armed on a watch-path change or when the
    recordings folder appears late. Comparing the grant line's timestamp
    against that derived "start" produced a FAIL-severity UNKNOWN on a
    healthy machine after every lid-open.

    UNKNOWN is reserved for the one genuinely unresolvable case: no
    Accessibility line found in the tail at all.
    """
    granted, _when = ctx.mw.accessibility_state()
    if granted is None:
        return Finding.unknown("no Accessibility line found in macrowhisper's log")
    if not granted:
        return Finding.problem(
            "macrowhisper started without Accessibility permission, so it cannot paste",
            "grant it in System Settings > Privacy & Security > Accessibility, "
            "then macrowhisper --restart-service",
        )
    return Finding.ok("granted")


CHECKS = (
    Check(
        id="pre.python",
        title="python3 is 3.9 or newer",
        severity=Severity.FAIL,
        inspect=_check_env_python,
    ),
    Check(
        id="pre.macrowhisper",
        title="macrowhisper is installed",
        severity=Severity.FAIL,
        inspect=_check_macrowhisper_installed,
    ),
    Check(
        id="pre.voiceink",
        title="VoiceInk is installed",
        severity=Severity.FAIL,
        inspect=_check_voiceink_installed,
    ),
    Check(
        id="bridge.watch",
        title="the watch directory exists",
        severity=Severity.FAIL,
        inspect=_check_watch_dirs,
    ),
    Check(
        id="bridge.script",
        title="macrovoice.sh is executable",
        severity=Severity.FAIL,
        inspect=_check_script,
    ),
    Check(
        id="mw.configpath",
        title="macrowhisper's saved config path is sane",
        severity=Severity.FAIL,
        inspect=_check_config_path,
        depends_on=("pre.macrowhisper",),
    ),
    Check(
        id="mw.configexists",
        title="the config file exists",
        severity=Severity.FAIL,
        inspect=_check_config_exists,
        depends_on=("mw.configpath",),
    ),
    Check(
        id="mw.configvalid",
        title="the config is valid",
        severity=Severity.FAIL,
        inspect=_check_config_valid,
        depends_on=("mw.configexists",),
    ),
    Check(
        id="mw.watchmatch",
        title="macrowhisper watches the bridge directory",
        severity=Severity.FAIL,
        inspect=_check_watch_match,
        depends_on=("mw.configexists",),
    ),
    Check(
        id="mw.service",
        title="macrowhisper is installed as a service",
        severity=Severity.WARN,
        inspect=_check_service,
        depends_on=("pre.macrowhisper",),
    ),
    Check(
        id="mw.running",
        title="macrowhisper is running",
        severity=Severity.FAIL,
        inspect=_check_running,
        depends_on=("pre.macrowhisper",),
    ),
    Check(
        id="mw.armed",
        title="the recordings watcher is armed",
        severity=Severity.FAIL,
        inspect=_check_armed,
        depends_on=("mw.running",),
    ),
    Check(
        id="mw.folders",
        title="macrowhisper can see the recordings folder",
        severity=Severity.FAIL,
        inspect=_check_folders,
        depends_on=("mw.running",),
    ),
    Check(
        id="mw.simesc",
        title="simEsc is off",
        severity=Severity.FAIL,
        inspect=_check_simesc,
        depends_on=("mw.running",),
    ),
    Check(
        id="mw.moveto",
        title="moveTo cleans up recordings",
        severity=Severity.WARN,
        inspect=_check_moveto,
        depends_on=("mw.running",),
    ),
    Check(
        id="mw.action",
        title="the active action is defined",
        severity=Severity.WARN,
        inspect=_check_action,
        depends_on=("mw.running", "mw.configexists"),
    ),
    Check(
        id="mw.clipboardbuffer",
        title="clipboardBuffer is raised above the default",
        severity=Severity.WARN,
        inspect=_check_clipboard_buffer,
        depends_on=("mw.configexists",),
    ),
    Check(
        id="mw.accessibility",
        title="Accessibility is granted to macrowhisper",
        severity=Severity.FAIL,
        inspect=_check_accessibility,
        depends_on=("mw.running",),
    ),
    Check(
        id="bridge.spool",
        title="the spool is empty",
        severity=Severity.WARN,
        inspect=_check_spool,
        depends_on=("bridge.watch",),
    ),
    Check(
        id="bridge.log",
        title="no recent errors in macrovoice.log",
        severity=Severity.WARN,
        inspect=_check_log_errors,
        depends_on=("bridge.watch",),
    ),
)
