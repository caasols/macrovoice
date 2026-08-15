# macrovoice

![macOS](https://img.shields.io/badge/macOS-black?logo=apple&style=flat)
![Python](https://img.shields.io/badge/Python_3-black?logo=python&style=flat)
[![Tests](https://github.com/caasols/macrovoice/actions/workflows/tests.yml/badge.svg)](https://github.com/caasols/macrovoice/actions/workflows/tests.yml)
![License](https://img.shields.io/badge/License-MIT-black?style=flat)

Drive [macrowhisper](https://github.com/ognistik/macrowhisper) automations with
[VoiceInk](https://github.com/beingpax/VoiceInk) dictations, without modifying either app.

macrowhisper turns a dictation into an action: paste it, open a URL, run a Shortcut, a shell
script, or AppleScript, with contextual triggers and text templating. It gets those dictations
by watching Superwhisper's recordings folder. VoiceInk writes no such folder, so the two cannot
talk. `macrovoice` is the missing piece.

```
VoiceInk Mode (Output = Custom Command)
  -> macrovoice.sh --mode <name>
  -> ~/mw-bridge/recordings/<id>/meta.json      (atomic directory rename)
  -> stock macrowhisper: validate -> match triggers -> run action
```

Proven end to end against live VoiceInk 2.1 and macrowhisper 2.1.1: dictate, and macrowhisper
pastes with smart casing and spacing, voice triggers fire, `triggerModes` matches. Neither app
is patched and neither knows the other exists.

## Features

- Every macrowhisper action type works: paste, URL, Shortcut, shell, AppleScript
- Voice, app, URL and mode triggers all fire normally
- Zero changes to VoiceInk or macrowhisper; both run stock
- Survives the four watcher behaviours that silently drop dictations (see [How it works](#how-it-works))
- Never loses a transcript, even when publishing fails or the process is killed at the deadline
- Logs transcript length, not content, so the log is not a record of everything you say
- Drives the whole pipeline from a pipe too, so actions are testable without a microphone
- No dependencies beyond system Python 3

## Requirements

| | |
| --- | --- |
| macOS | Tested on Darwin 25.6 |
| [VoiceInk](https://github.com/beingpax/VoiceInk) | 2.0 or later, for the Custom Command output mode |
| [macrowhisper](https://github.com/ognistik/macrowhisper) | 2.1.1 or later |
| Python 3 | System Python is fine, no packages needed |

## Setup

**1. Install macrowhisper and create a watch directory**

```sh
brew install ognistik/formulae/macrowhisper
mkdir -p ~/mw-bridge/recordings
```

Do not point this at `~/superwhisper`. If you also run Superwhisper, the bridge would interleave
synthetic recordings with real ones.

**2. Configure macrowhisper**

```sh
cp macrowhisper.sample.json ~/.config/macrowhisper/macrowhisper.json
macrowhisper --start-service
sleep 8 && macrowhisper --status
```

The `sleep` matters. Folders that already exist when the recordings watcher arms are marked
processed and dropped, so publishing during the startup window looks like nothing happening.

Switch actions with the CLI, not by editing the config while the daemon runs:

```sh
macrowhisper --action markerLog     # switch
macrowhisper --get-action           # verify it took
```

A file edit landing just after macrowhisper's own internal write is swallowed
(`Suppressed config reload after internal write`), and `autoUpdateConfig` then rewrites the file
from memory, erasing your change.

**3. Verify the macrowhisper half, before involving VoiceInk**

```sh
macrowhisper --action markerLog
VOICEINK_TRANSCRIPT='hello world' ./macrovoice.sh --watch ~/mw-bridge
sleep 2 && cat ~/mw-bridge/fired.log
```

A line appears. If it does not, fix that before going further.

**4. Check what VoiceInk actually sends**

In VoiceInk, create a Mode with Output = **Custom Command** pointed at `probe.sh`, give it a
keyboard shortcut or set it as default (see [Picking the Mode](#picking-the-mode-the-trap-everyone-hits)),
dictate a few times, then read `~/mw-bridge/probe.log`. Confirm one `INVOCATION` block per
dictation and that nothing was pasted into the focused app.

**5. Go live**

Point the Mode at `macrovoice.sh`, switch back with `macrowhisper --action autoPaste`, grant
macrowhisper Accessibility permission, focus a text field, and dictate. Say "ask google best
pizza" or "google best pizza" to try the voice trigger from the sample config, which ships both
phrasings as `"triggerVoice": "ask google|google"`. The sample also ships `ask youtube`,
`ask perplexity` and `ask openai` (which also answers to "ask chatgpt"), each with the same
both-phrasings treatment.

A dictation that matches a voice trigger runs **that** action and nothing else: the trigger action
replaces your default action rather than running alongside it, so "ask google ..." opens a browser
and pastes nothing. Only a dictation that matches no trigger falls through to `autoPaste`.

Voice triggers are **prefix-anchored**: macrowhisper builds `"^(?i)" + escaped pattern`
(`Utils/TriggerEvaluator.swift:205`), so the trigger must be at the **start** of the dictation.
`|` separates alternatives and each one is anchored independently, which is why the sample lists
"ask google" as well as the bare word. "Google best pizza" and "Ask Google best pizza" both
match; "Can you Google the best pizza" does not, and silently falls through to your default
action.

Matching is a raw prefix, not a word boundary, so "googled the answer" also matches and searches
for the leftover "D the answer". Use a `==regex==` trigger if that matters to you, remembering
that raw regex disables the automatic prefix stripping.

VoiceInk does not tell the command which Mode fired, so bake the name into each Mode's command
if you want `triggerModes` to work:

```
/abs/path/to/macrovoice.sh --mode email
```

## Picking the Mode, the trap everyone hits

A Custom Command Mode is **inert unless it is the default or has its own keyboard shortcut.**
VoiceInk resolves the Mode per dictation in `ActiveWindowService.beginApplyingConfiguration`
(`Modes/ActiveWindowService.swift:19-46`):

1. A **Mode-specific shortcut** passes that Mode's id and wins outright.
2. The **generic hotkey** passes no id and resolves
   `getConfigurationForApp(bundleId) ?? getDefaultConfiguration()`: an app rule if one matches,
   otherwise the Mode marked **Set as default**.
3. With no default at all, it falls back to list order (`Modes/ModeConfig.swift:420-428`).

So a Mode that is saved but neither default nor shortcut-bound never runs. Every dictation goes
through your normal Mode, the text pastes as usual, and your command is never called. That looks
exactly like "VoiceInk does not suppress the paste, so this cannot work," and it is not.

Verify what is actually stored:

```sh
python3 -c "
import subprocess, plistlib, json, io
p = subprocess.run(['defaults','export','com.prakashjoshipax.VoiceInk','-'], capture_output=True)
for c in json.loads(plistlib.load(io.BytesIO(p.stdout))['modeConfigurationsV2']):
    print(f\"{c['name']:16} outputMode={c['outputMode']:14} isDefault={c['isDefault']}\")
"
```

Do not diagnose from `activeConfigurationId`. It looks like a sticky override and is not: it is
rewritten at the start of every dictation, so it only records what the last one used.

**The recommended layout:** leave your everyday Mode as default, and give the bridge Mode its own
shortcut. Normal dictation then pastes as usual and never depends on macrowhisper being alive;
the shortcut routes through the bridge on demand.

## Set `simEsc: false`, or macrowhisper will discard your work

The sample config sets this for you. If you write your own, do not skip it.

macrowhisper defaults `simEsc` to **true** and, before pasting, posts a literal Escape keypress
to the system-wide HID event tap (`Utils/Accessibility.swift:477-494`, `simulateKeyDown` key 53).
Under Superwhisper that ESC dismisses Superwhisper's own recording window. Under this bridge
there is no such window, so the Escape lands in whatever app you are typing into.

Measured: dictating into a ProtonMail compose window **closed the draft**, and the paste then had
nowhere to land. Any app where ESC means cancel, close, or discard is exposed, and you lose work
with no error.

What makes it easy to misdiagnose is that the damage is app-specific. The same dictation pastes
perfectly into a browser address bar or a terminal, where Escape is harmless, so it reads as a
paste bug in one app rather than a global setting doing collateral damage.

## Diagnosing a broken setup

```sh
./macrovoice.sh doctor --check
```

Twenty checks across both apps, reported in the order you hit them. It is read-only: it never
creates a directory, edits a config, or touches VoiceInk.

Three outcomes, and the third one matters. `ok` and `PROBLEM` mean what you expect. `unknown`
means a check could not be run, and it names what blocked it: if macrowhisper is not running,
`simEsc` is not fine and not broken, it is unknowable. Fix the problem at the top and re-run.
That is also why a bare machine reports one real problem and a run of unknowns instead of a wall
of alarms: everything downstream of the one thing that is actually broken has nothing to inspect.

Exit codes: `0` healthy, `1` a fatal problem remains, `2` a fatal check could not be determined.

## Options

| Flag | Default | Description |
| --- | --- | --- |
| `--mode <name>` | none | Written as `modeName`, feeding macrowhisper's `triggerModes` |
| `--watch <path>` | `$MW_BRIDGE_WATCH` or `~/mw-bridge` | macrowhisper's watch root |
| `--gap <seconds>` | `1.0` | Minimum spacing between publishes |
| `--drain-only` | off | Publish anything left in the spool and exit |
| `--log-transcript` | off | Log transcript text instead of just its length |

## Text input, without a microphone

`macrovoice` takes the transcript from `VOICEINK_TRANSCRIPT`, and falls back to **stdin** when that
variable is absent. So the entire pipeline runs from a pipe, with no dictation and no VoiceInk:

```sh
echo "ask google best pizza in madrid" | ./macrovoice.sh
```

That publishes a real `meta.json`, and macrowhisper matches triggers and runs its action exactly as
it would for a spoken dictation. It is the cheapest way to test an action you are writing, and it
also makes the bridge scriptable by anything that can write to a pipe.

Point it somewhere harmless while you experiment, so you are not firing actions into your real
setup:

```sh
mkdir -p /tmp/mv-scratch
echo "hello world" | ./macrovoice.sh --watch /tmp/mv-scratch
cat /tmp/mv-scratch/recordings/*/meta.json
```

Pair it with macrowhisper's own dry run, which resolves the full action-selection pipeline against
a hand-written `meta.json` and skips `moveTo`, so nothing is consumed:

```sh
macrowhisper --run-auto --meta /path/to/meta.json
```

Three things worth knowing:

- **The text is passed through byte for byte**, so `echo` contributes a trailing newline. Use
  `printf '%s' "..."` if that matters, though macrowhisper's `smartSpacing` handles stray edge
  whitespace at insertion time anyway.
- **Empty or whitespace-only input publishes nothing** and exits 0, logging `skipped`.
- **The stdin read blocks by design** when `VOICEINK_TRANSCRIPT` is absent, because in that case
  stdin genuinely is the only source of the words. If you call `macrovoice` from something that
  opens a pipe and never closes it, pass the transcript in the environment variable instead.

## How it works

macrowhisper's watcher has four behaviours a naive bridge trips over. Each is a **silent**
failure: nothing errors, the dictation just disappears. Line references are to
`Watcher/RecordingsFolderWatcher.swift` in macrowhisper 2.1.1.

| Behaviour | Evidence | How `macrovoice` handles it |
| --- | --- | --- |
| A folder without `meta.json` inside it takes a slow path, then is cancelled after 17s for having no `.wav` | `:38`, `:457-462`, `:1928-1935` | Builds the folder in a staging dir and renames the whole directory in, so it is never seen half-built |
| Two folders appearing in one filesystem event means **none** of them run | `:327-345` | Serializes publishing behind an `flock` with a minimum gap |
| A folder whose name sorts below the newest one is discarded as cloud-sync replay | `:350` | Mints the published name at publish time, always above the current maximum |
| Folders that already exist when the watcher arms are marked processed at startup | startup path | Cannot be defended from this side. Wait a few seconds after starting the daemon |

Plus one from VoiceInk: it suppresses its own paste and then kills the command at 10 seconds
(`TranscriptionDelivery.swift:43-46`, `:115`), so the transcript exists nowhere else.
`macrovoice` spools first, unconditionally, and only then publishes. It always exits 0, because
a non-zero exit shows the user an error without recovering their words.

Measured on a real install: three folders created in a tight loop produced one action and two
losses. Five concurrent dictations produced five folders but only four actions, until the naming
fix; after it, five of five. At human speaking cadence, five dictations in ten seconds all
delivered, and a 633-character dictation arrived intact.

## Tests

375 tests, 8 skipped: 187 on the delivery path, 188 for `doctor`. Total branch coverage is 99%
(99.12%), measured across the whole package with subprocess tracing. That last part matters: a
naive run reports `cli.py` at 0%, which is wrong, because its tests drive it as a real subprocess
that `coverage` cannot see without `COVERAGE_PROCESS_START` and a `sitecustomize.py`. Every
delivery-path module (`transcript.py`, `meta.py`, `publisher.py`, `cli.py`) is at 100%, and so is
most of `doctor`: `model.py`, `registry.py`, `runner.py`, `report.py`, and `process.py`. What is
left uncovered: `__main__.py`'s entry-point guard, two loop branches in the bridge adapter, and
five spots in the macrowhisper adapter, none reachable without a real daemon. CI runs the suite on
macOS across Python 3.9, 3.12 and 3.13. The 3.9 entry is deliberate: `macrovoice.sh` execs
`/usr/bin/env python3`, and on a stock Mac that is the system Python.

| File | Tests | Covers |
| --- | --- | --- |
| `tests/test_publisher.py` | 48 | Staging, spool, drain lock, burst spacing, atomic renames, name monotonicity, cross-process collisions, and a future-dated `.last-publish` no longer stalling delivery |
| `tests/test_cli.py` | 25 | The CLI driven through real subprocesses, including the exit-code policy and the open-stdin regression |
| `tests/test_harness_port.py` | 24 | That the oracle still matches macrowhisper's validation gate, branch for branch |
| `tests/test_meta.py` | 23 | A 31-entry escaping matrix and the `meta.json` schema contract |
| `tests/test_transcript.py` | 22 | Env and stdin resolution, the empty-input policy, and whether stdin needs reading at all |
| `tests/test_integration_safety.py` | 20 | The integration suite's own guard against hijacking your macrowhisper |
| `tests/test_sample_config.py` | 9 | The shipped `macrowhisper.sample.json`: structure, and the settings that cause harm when wrong |
| `tests/test_voiceink_invocation.py` | 8 | The `.sh` wrappers through `/bin/zsh -lc`, exactly as VoiceInk calls them |
| `tests/test_integration_macrowhisper.py` | 8 | Opt-in; drives a **real macrowhisper daemon** |
| `tests/test_doctor_*.py` (8 files) | 188 | `doctor`'s checks, adapters, runner, report and status parser, exercised without a real macrowhisper |

```sh
python3 -m unittest discover -s tests -t tests -v      # 375 tests, 8 skipped
MACROVOICE_INTEGRATION=1 python3 -m unittest discover -s tests -t tests
```

`test_harness.py` is a branch-for-branch port of macrowhisper's `isValidRecordingMetaJson`
(`Utils/RecordingReferenceResolver.swift:34-53`). Every generated document is asserted against
it, which is what lets the adapter be verified without macrowhisper running. If macrowhisper
changes that gate, update the port; `tests/test_harness_port.py` will show what drifted.

The escaping matrix covers transcripts that break naive JSON emitters: quotes, backslashes,
CRLF, NUL, control characters, astral-plane codepoints, combining accents, RTL, 100k characters,
JSON lookalikes and shell metacharacters. Other tests assert name monotonicity across 10,000
sequential calls, that a watcher thread never observes a directory without its `meta.json`, and
zero transcript loss under 12-thread concurrency.

Several regression tests exist because the failure they guard is a **hang**, so they carry hard
timeouts. A future-dated folder, or one whose name starts with a letter, used to make the
publisher spin forever waiting for the clock to overtake it. Separately, the CLI used to read
stdin before checking whether the environment already held the transcript, so a caller that
opened a pipe and never closed it (launchd, cron, CI, a backgrounded shell) blocked forever
*before* the transcript reached the spool, which is the one place it cannot be lost from.

The integration tests are opt-in because they launch a real daemon. They confine themselves to a
temporary watch directory and a temporary config, so `~/mw-bridge` and `~/.config/macrowhisper/`
are never touched, and they use a shell action rather than a paste, so no Accessibility
permission is needed and nothing is typed into whatever app you have focused. `macrowhisper
--config` *persists* the path it is given, so the original is captured at import, restored
afterwards, and asserted not to be a temp directory.

## Four things that will surprise you

Each of these is easy to get wrong in a way that costs you something, and none of them is obvious
from either app's own documentation. Provenance is stated per item, because "the source says" and
"we measured it" are not the same claim.

### `moveTo: .delete` does not delete your transcript

It disposes of the synthetic folder `macrovoice` created in the watch directory. That is all it
does. **VoiceInk keeps its own copy of every dictation, and macrowhisper cannot reach it.**

Read from VoiceInk's source rather than inferred: when recording stops, VoiceInk builds a
`Transcription` record and calls `modelContext.insert` plus `save()` **before** the transcription
pipeline runs (`VoiceInkEngine.swift:200-206`), the pipeline then writes the final text into that
same record (`TranscriptionPipeline.swift:146`), and the record also stores `audioFileURL`
(`:739`), so **the audio file is retained too**. The delivery routing only chooses
`.customCommand` afterwards (`TranscriptionDelivery.swift:43-46`), which means your Mode's output
setting has no bearing on any of it, and no bridge configuration can change that.

If you are setting `moveTo: .delete` for privacy reasons, you have deleted the copy that was never
the sensitive one. Manage retention in VoiceInk itself: its History, and its audio cleanup
settings.

### Voice triggers survive AI enhancement, but arrive reshaped

If a Mode has AI enhancement on, the bridge only ever sees the **final, post-enhancement** text.
Anything the model does to your opening words has already happened.

Measured 2026-08-15 with OpenAI `gpt-5.5` and VoiceInk's Default prompt. Dictating
"ask google best pizza in madrid" arrived as:

```
Ask Google: “Best pizza place in Madrid.”
```

The trigger still fired, and the detail is the useful part. The sample ships
`"triggerVoice": "ask google|google"`, and each `|` alternative is anchored independently. Here
`ask google` matched and the bare `google` did **not**, because the text no longer begins with it.
In plain dictations without AI enhancement, which arrive as "Google, what is...", the reverse is
true. **Each alternative was the only one that matched in one of the two cases, so list both.**

The second effect has no workaround: the model added a colon and **curly quotes** (U+201C and
U+201D, not ASCII), macrowhisper strips the matched trigger but not the punctuation around it, and
the residue lands in your action's payload. The search above ran for `Best pizza place in Madrid."`
including the stray quote. Harmless for a web search; it matters for any action that parses,
compares or greps the text, and it compounds the NFD note below, since these quotes are non-ASCII
too.

Treat this as one measurement, not a law: one model, one prompt, two dictations. A different model
or a prompt that rewrites more aggressively could still drop the trigger word entirely.

### `{{xml:tag}}` extracts the tag but leaves the markup in `{{swResult}}`

Use `{{xml:note}}` in an action and the tag's content is extracted correctly. But `{{swResult}}` in
that same action still carries the raw `<note>...</note>` markup.

This is specific to the bridge and cannot happen upstream. `processAllPlaceholders`
(`Placeholders.swift:1759-1780`) branches on which field holds the text: when `llmResult` is
present it writes the cleaned text back (`updatedMetaJson["llmResult"] = cleaned`), but when only
`result` is present it discards the cleaned copy (`let (_, tags) = ...`) and never updates
`result`. The bridge deliberately never sets `llmResult`, because doing so flips macrowhisper's
validation gate to require a non-empty `llmResult` that VoiceInk cannot supply, so the second
branch is the only one we ever take. Verified in the source and confirmed against a live daemon in
`tests/test_integration_macrowhisper.py`.

### macrowhisper rewrites your config file

`defaults.autoUpdateConfig` defaults to **true**, and on every startup macrowhisper reloads the
config, normalises it, and writes it back (`main.swift:1871-1876` calling `updateConfiguration()`,
which ends in `saveConfig()`). So the file will stop matching whatever you copied in: expect added
keys, a `$schema` and `configVersion` entry, and different formatting.

Measured on a real install: a config copied from `macrowhisper.sample.json` with 8 keys under
`defaults` had 28 a few days later, plus `$schema`, `configVersion`, `scriptsAS` and `shortcuts`,
and a `macrowhisper.json.backup.pre-v2` file sitting beside it from the version migration.

Nothing is lost, and a pre-migration backup is written, but do not be surprised when the file no
longer matches the sample, and do not keep your config under version control expecting it to stay
put. Set `defaults.autoUpdateConfig: false` to opt out, at the cost of an "Outdated Config"
notification if your file is ever behind.

## Limitations

**Clipboard context is degraded, and the amount is calculable.** macrowhisper captures
pre-recording clipboard when the folder appears, looking back `clipboardBuffer` seconds. Under
Superwhisper the folder appears when recording *starts*; here it appears when dictation *ends*.
With the 5-second default, any dictation longer than 5 seconds loses its pre-recording clipboard
entirely. The sample config sets 60.

**`{{selectedText}}` reflects post-dictation state**, not what you had selected when you began
speaking.

**Only the final text exists.** VoiceInk exposes no raw-versus-enhanced split, so `result`
carries the final text and `languageModelName`/`llmResult` are left absent. `{{llmResult}}` will
be empty; use `{{swResult}}`.

**No `segments`**, so `{{segments}}` is empty. **No `duration`**: VoiceInk does not expose one,
and macrowhisper reads that field as milliseconds, so a placeholder `0.0` would render as `"0ms"`
and look measured. The key is omitted, and `{{duration}}` renders empty instead.

**Accented text is NFD.** VoiceInk emits combining marks, so `café` arrives as `e` + U+0301, not
U+00E9. `macrovoice` is byte-faithful and never normalises, and `.autoPaste` passes it through
unchanged. This only matters if you write actions that grep or compare dictated text: a `café`
typed into a config is NFC and will never match. Normalise both sides.

**Per-Mode, not global.** Only Modes set to Custom Command feed the bridge.

**Front-app placeholders and voice, app, URL and mode triggers are unaffected**, since those
resolve at action time.

## Where things live

| Path | Purpose |
| :--- | :--- |
| `macrovoice/transcript.py` | Resolve the transcript from env, with stdin fallback |
| `macrovoice/meta.py` | Build and serialize the `meta.json` document (pure) |
| `macrovoice/publisher.py` | Staging, spool, drain lock, atomic renames |
| `macrovoice/cli.py` | Wiring, logging, exit-code policy |
| `macrovoice/doctor/` | Read-only inspection of the whole setup, see [Diagnosing a broken setup](#diagnosing-a-broken-setup) |
| `test_harness.py` | Port of macrowhisper's own validation gate, used as the test oracle |
| `macrovoice.sh` | The one-liner you paste into VoiceInk |
| `probe.sh` | Captures what VoiceInk actually sends |
| `macrowhisper.sample.json` | Ready-to-use macrowhisper config |

## Troubleshooting

**Start with `./macrovoice.sh doctor --check`.** Seven of the thirteen setup traps this project
has hit in practice are detected there, including the three that silently look like the bridge
being broken.

| Symptom | Cause |
| --- | --- |
| Nothing happens at all | Is your Mode default or shortcut-bound? See [Picking the Mode](#picking-the-mode-the-trap-everyone-hits) |
| Text pastes normally, your command never runs | The same thing. The Mode is inert |
| The bridge publishes but nothing fires | Does macrowhisper's **saved** config match? `macrowhisper --get-config`, then check its `watch` |
| Some dictations do nothing | Check macrowhisper's log for `burst protection` or `older than existing`, then raise `--gap` |
| Text appears but no paste | macrowhisper needs Accessibility permission, and the daemon must be restarted after you grant it |
| A dictation closed my draft | `simEsc`. See the section above |
| Config edits appear to do nothing | Use `macrowhisper --action`, not a file edit while the daemon runs |
| Quotes look backslashed in `fired.log` | Expected. macrowhisper shell-escapes `{{swResult}}` for shell actions. Insert actions are not escaped |
| Transcript missing entirely | Check `~/mw-bridge/macrovoice.log` and `~/mw-bridge/.spool/`, or force it with `--drain-only` |
| A voice trigger fired but nothing pasted | Expected. A matched trigger runs instead of your default action, not alongside it |
| My config file changed on its own | `autoUpdateConfig`, which defaults to true. See [Four things that will surprise you](#four-things-that-will-surprise-you) |
| Stray quotes or punctuation in a triggered search | AI enhancement reshaped the dictation before the bridge saw it. Same section |

`macrowhisper --status`'s **exit code** is useless as a liveness check: it exits 0 either way.
Its **output** is not: it prints the literal line `macrowhisper is not running.` when nothing is
listening, which is exactly what `doctor`'s own liveness check reads. To lower `--gap`, bisect
downward and use macrowhisper's own log as the oracle:

```sh
grep -iE "burst protection|older than existing" ~/Library/Logs/Macrowhisper/macrowhisper.log
```

## Status

Proven end to end against live VoiceInk 2.1 and macrowhisper 2.1.1. A dictation goes from
VoiceInk through `macrovoice` to a synthetic `meta.json` and out through stock macrowhisper,
which pastes it with smart casing and spacing; `--mode` reaches `triggerModes`, and voice
triggers fire. Verified at real human speaking cadence: five dictations in ten seconds all
delivered, a 633-character dictation intact, quotes and shell metacharacters clean, and accented
text measured end to end.

## Contributing

Issues and pull requests are welcome. Please open a discussion first if you plan a larger
change, so we can align on the approach.

## License

Released under the [MIT License](./LICENSE).

Independent personal project, not affiliated with VoiceInk, macrowhisper, or Superwhisper.
Line references describe third-party source as audited at macrowhisper 2.1.1 and VoiceInk 2.1.
