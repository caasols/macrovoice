"""The check table: the specification of what doctor knows.

Ordered the way a user meets the traps, which is also roughly dependency order.
Each entry is data. Adding a trap is one function and one row.
"""

import os
import shlex
import tempfile
from pathlib import Path

from .adapters.voiceink import BUNDLE_ID as VOICEINK_BUNDLE_ID
from .model import Check, Finding, Severity

VOICEINK_APP_PATHS = (
    Path("/Applications/VoiceInk.app"),
    Path("~/Applications/VoiceInk.app").expanduser(),
)
# The wrapper VoiceInk is told to run. Bridge Modes are recognised by this name
# appearing in their command, never by matching the correct path: vi.command
# exists precisely to catch a stored path that is wrong, and it could not see a
# wrong path if finding the Mode required a right one.
SCRIPT_NAME = "macrovoice.sh"
# Appended to every vi.* repair hint. VoiceInk loads its Mode store once at
# launch and never re-reads it, and cfprefsd caches on the running app's behalf,
# so a reading taken now can lag what the UI shows. Without this line a user who
# has just fixed the Mode is told, wrongly, that it is still broken.
STALE_HINT = "then quit VoiceInk and re-run doctor (it caches its Modes in memory)"
MIN_PYTHON = (3, 9)
BREW_INSTALL = "brew install ognistik/formulae/macrowhisper"
# The config sample shipped at the repo root, named here so the hint and the file
# cannot drift apart unnoticed. They did: this said "macrovoice.sample.json" while
# the repo ships "macrowhisper.sample.json", so doctor's first repair instruction
# to a fresh user pointed at a file that does not exist. Pinned to reality by
# tests/test_doctor_checks.py, which stats it rather than restating it.
SAMPLE_CONFIG_NAME = "macrowhisper.sample.json"
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
        sample = ctx.bridge.script_path().parent / SAMPLE_CONFIG_NAME
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


def _bridge_modes(ctx):
    """(modes, error) where `error` is a Finding to return, or None to proceed.

    Every vi.* check begins here, so the "cannot read VoiceInk" and "no bridge
    Mode" cases are answered once and identically rather than five times.

    A bridge Mode is any Mode whose command mentions `macrovoice.sh`, matched on
    the SCRIPT NAME and not on a path, because the whole point of vi.command is
    that the stored path may be wrong. Matching on the correct path first would
    make the dead-path check unable to see the thing it exists to find.
    """
    if ctx.vi is None:
        return None, Finding.unknown("no VoiceInk adapter available")
    modes = ctx.vi.modes()
    if modes is None:
        return None, Finding.unknown(
            "could not read VoiceInk's Mode store (defaults export %s)" % VOICEINK_BUNDLE_ID
        )
    found = tuple(m for m in modes if m.command and SCRIPT_NAME in m.command)
    if not found:
        return None, Finding.problem(
            "no VoiceInk Mode runs %s, so nothing feeds the bridge" % SCRIPT_NAME,
            "create a Mode with Output = Custom Command pointing at %s, %s"
            % (ctx.bridge.script_path(), STALE_HINT),
        )
    return found, None


def _stale_note(ctx):
    """VoiceInk reads its Mode store once at launch (ModeConfig.swift:288) and
    observes nothing after, while cfprefsd caches for the running app. So a read
    taken now can lag the UI. Measured 2026-08-15: a setting changed in the UI
    still read as absent here minutes later."""
    running = ctx.vi.is_running() if ctx.vi else None
    return " (VoiceInk is running, so this reading may lag its UI)" if running else ""


def _check_vi_mode(ctx):
    modes, error = _bridge_modes(ctx)
    if error:
        return error
    return Finding.ok(
        "%s%s" % (", ".join(m.name for m in modes), _stale_note(ctx))
    )


def _check_vi_outputmode(ctx):
    """A Mode can hold a perfectly good command and still never run it.

    VoiceInk only invokes the Custom Command when outputMode is `customCommand`
    (TranscriptionDelivery.swift:43-46). On any other setting the text pastes
    normally and the command is never called, which is indistinguishable from
    the bridge being broken.
    """
    modes, error = _bridge_modes(ctx)
    if error:
        return error
    wrong = [m for m in modes if m.output_mode != "customCommand"]
    if wrong:
        return Finding.problem(
            "Mode %s has Output = %s, not customCommand, so its command never runs"
            % (", ".join(m.name for m in wrong), wrong[0].output_mode or "unset"),
            "set Output to Custom Command in VoiceInk, %s" % STALE_HINT,
        )
    return Finding.ok()


def _check_vi_command(ctx):
    """Friction trap 10, and the hazard this project created for itself.

    Renaming the repo on 2026-08-09 left VoiceInk's stored command pointing at a
    path that no longer existed, and nothing in the repo could see it. Worse
    than missing: if a stale copy still sits at the old path it runs happily,
    and the user edits a checkout that is not the one in use.

    VoiceInk runs the command with cwd=/ (Gate 2), so a relative path cannot
    work either.
    """
    modes, error = _bridge_modes(ctx)
    if error:
        return error

    ours = Path(ctx.bridge.script_path()).resolve()
    for mode in modes:
        try:
            argv = shlex.split(mode.command)
        except ValueError:
            return Finding.problem(
                "Mode %s has an unparseable command: %s" % (mode.name, mode.command),
                "fix the command in VoiceInk, %s" % STALE_HINT,
            )
        # argv cannot be empty: _bridge_modes only yields Modes whose command
        # CONTAINS the script name, and shlex.split returns [] only for a string
        # that is entirely whitespace. argv[0] can still be "" (a command opening
        # with an empty quoted token), which the absolute-path branch below
        # rejects with the right message.
        script = argv[0]
        if not script.startswith("/"):
            return Finding.problem(
                "Mode %s runs %r, which is not an absolute path. VoiceInk runs the "
                "command with cwd=/, so a relative path cannot resolve"
                % (mode.name, script),
                "use the full path %s, %s" % (ours, STALE_HINT),
            )
        path = Path(script)
        if not path.exists():
            return Finding.problem(
                "Mode %s points at %s, which does not exist" % (mode.name, script),
                "update the command to %s, %s" % (ours, STALE_HINT),
            )
        if not os.access(script, os.X_OK):
            return Finding.problem(
                "Mode %s points at %s, which is not executable" % (mode.name, script),
                "chmod +x %s" % script,
            )
    return Finding.ok("%s%s" % (argv[0], _stale_note(ctx)))


def _check_vi_checkout(ctx):
    """Whether VoiceInk runs the copy of macrovoice you are inspecting.

    Split out of vi.command, at WARN rather than FAIL, after running the thing:
    a Mode pointing at a DIFFERENT but perfectly working copy is not a broken
    bridge. Anyone with two copies, a developer in a worktree or a user who
    moved the folder and kept the old one, has a setup that dictates fine, and
    failing them would pin exit 1 on a healthy machine. That is the false-alarm
    shape this project has already been bitten by twice in this same command.

    It still deserves saying, because it is the 2026-08-09 hazard: the folder was
    renamed, a stale copy survived at the old path, VoiceInk kept running it, and
    every edit went to a checkout nothing was using. Silent, and invisible to
    every other check. The genuinely fatal cases, a path that is missing, not
    absolute, or not executable, stay in vi.command at FAIL.
    """
    modes, error = _bridge_modes(ctx)
    if error:
        return error
    ours = Path(ctx.bridge.script_path()).resolve()
    for mode in modes:
        try:
            argv = shlex.split(mode.command)
        except ValueError:
            return Finding.unknown("Mode %s has an unparseable command" % mode.name)
        theirs = Path(argv[0])  # non-empty by the same invariant as vi.command
        if not theirs.exists():
            # vi.command owns this failure and reports it properly.
            return Finding.unknown("blocked: %s does not exist" % argv[0])
        if theirs.resolve() != ours:
            return Finding.problem(
                "VoiceInk runs %s, but doctor is inspecting %s. The bridge may work "
                "fine; the point is that edits here do not reach what VoiceInk runs"
                % (theirs.resolve(), ours),
                "point the Mode at %s, or re-run doctor from %s, %s"
                % (ours, theirs.resolve().parent, STALE_HINT),
            )
    return Finding.ok()


def _check_vi_reachable(ctx):
    """Friction trap 1: a saved Mode that never runs.

    VoiceInk resolves the Mode per dictation in
    ActiveWindowService.beginApplyingConfiguration (:19-46): a Mode-specific
    shortcut wins outright, otherwise the generic hotkey resolves an app rule or
    the Mode marked default. A Mode that is neither default nor shortcut-bound
    is never selected, so every dictation goes through the normal paste Mode and
    the command is never called.

    That presents as "VoiceInk does not suppress the paste, so this cannot
    work", which is the honest conclusion from the symptom and is wrong. It cost
    this project its first Gate 2 attempt.

    The shortcut is a separate defaults key, not a field of the Mode, so this
    cannot be answered by reading the Mode alone.
    """
    modes, error = _bridge_modes(ctx)
    if error:
        return error
    for mode in modes:
        if mode.is_default:
            continue
        bound = ctx.vi.has_shortcut(mode.id)
        if bound is None:
            return Finding.unknown(
                "could not read whether Mode %s has a keyboard shortcut" % mode.name
            )
        if not bound:
            return Finding.problem(
                "Mode %s is neither the default nor shortcut-bound, so it never runs "
                "and every dictation goes through your normal Mode instead" % mode.name,
                "give it a keyboard shortcut in VoiceInk, or set it as default, %s"
                % STALE_HINT,
            )
    return Finding.ok()


def _check_vi_escapehatch(ctx):
    """G4, enforced continuously instead of remembered once.

    If the bridge Mode holds default, every ordinary dictation depends on
    macrowhisper being alive: when it is not, nothing pastes and there is no way
    to dictate normally. The recommended layout keeps an everyday paste Mode as
    default and gives the bridge its own shortcut.
    """
    if ctx.vi is None:
        return Finding.unknown("no VoiceInk adapter available")
    modes = ctx.vi.modes()
    if modes is None:
        return Finding.unknown("could not read VoiceInk's Mode store")
    defaults = [m for m in modes if m.is_default]
    if not defaults:
        return Finding.problem(
            "no VoiceInk Mode is set as default, so VoiceInk falls back to list "
            "order, which is not something you can reason about",
            "set your everyday dictation Mode as default in VoiceInk",
        )
    non_bridge = [m for m in defaults if not (m.command and SCRIPT_NAME in m.command)]
    if not non_bridge:
        return Finding.problem(
            "the bridge Mode %s is your default, so every dictation depends on "
            "macrowhisper running. If it stops, nothing pastes at all"
            % defaults[0].name,
            "set a plain paste Mode as default and give the bridge its own shortcut",
        )
    return Finding.ok("%s holds default" % ", ".join(m.name for m in non_bridge))


def _check_accessibility(ctx):
    """Friction trap 4: macrowhisper checks and logs Accessibility exactly once
    per process, unconditionally, at true process startup
    (main.swift:1905 requestAccessibilityPermissionOnStartup(), which logs
    exactly one of Accessibility.swift:51, :59 or :62), in the same instant it
    raises the prompt. Because every startup emits exactly one such line, the
    newest one in the log always describes the currently running process:
    there is no freshness comparison to make, and there used to be a false one
    here.

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
    Accessibility line found in either the live log or the newest rotated
    backup.

    That genuinely unresolvable case has a second cause, found live on
    2026-08-11 and not by review: macrowhisper rotates its own log at 5MB and
    keeps one backup (Logger.swift:18, :22), so the startup line ages out of
    retention on a long-running daemon. The adapter now reads the retained
    backup as well, which makes this rare rather than routine, but a daemon up
    across two full rotations still lands here. It stays FAIL-severity UNKNOWN
    and still exits 2, deliberately: a genuinely denied permission must not be
    downgraded to a note.

    And a THIRD cause, found live on 2026-08-15 the same way, by running the
    thing rather than re-reading the notes: the log file can be DELETED or
    truncated underneath a running daemon. macrowhisper recreates it on its
    next write, so the file is present, readable, small, and missing the
    startup line, with no rotated backup to fall back to because no rotation
    happened. Measured: a daemon up 1d14h whose live log was 3.9KB and began
    that same afternoon, five orders of magnitude below the 5MB threshold.

    Rotation and deletion are indistinguishable from inside this check, and
    deliberately so: the fallback recovers a line that was rotated away, and
    nothing can recover one that was deleted. The detail string therefore names
    the whole cause set rather than asserting the one that happens to be
    likeliest, because naming only rotation made a healthy machine's exit 2
    look like a defect in doctor. The remedy is the same for all three.
    """
    granted, _when = ctx.mw.accessibility_state()
    if granted is None:
        return Finding.unknown(
            "no Accessibility line in the live or rotated log. macrowhisper writes "
            "one only at startup, so its log no longer holds that line: it has "
            "rotated past it, the log was deleted or truncated under the running "
            "daemon, or it is unreadable. This is not a permission failure and not "
            "a doctor defect; run macrowhisper --restart-service to re-log it"
        )
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
    # VoiceInk, stage 2. Read-only. Ordered so the broadest failure comes first
    # and the rest depend on it, which keeps a machine with no bridge Mode from
    # emitting four separate alarms about the same missing thing.
    Check(
        id="vi.mode",
        title="a VoiceInk Mode runs macrovoice",
        severity=Severity.FAIL,
        inspect=_check_vi_mode,
        depends_on=("pre.voiceink",),
    ),
    Check(
        id="vi.outputmode",
        title="that Mode's output is Custom Command",
        severity=Severity.FAIL,
        inspect=_check_vi_outputmode,
        depends_on=("vi.mode",),
    ),
    Check(
        id="vi.command",
        title="its command points at this macrovoice.sh",
        severity=Severity.FAIL,
        inspect=_check_vi_command,
        depends_on=("vi.mode",),
    ),
    Check(
        id="vi.checkout",
        title="VoiceInk runs the copy you are inspecting",
        severity=Severity.WARN,
        inspect=_check_vi_checkout,
        depends_on=("vi.command",),
    ),
    Check(
        id="vi.reachable",
        title="that Mode is default or shortcut-bound",
        severity=Severity.FAIL,
        inspect=_check_vi_reachable,
        depends_on=("vi.mode",),
    ),
    Check(
        id="vi.escapehatch",
        title="a non-bridge Mode holds default",
        severity=Severity.WARN,
        inspect=_check_vi_escapehatch,
        depends_on=("pre.voiceink",),
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
