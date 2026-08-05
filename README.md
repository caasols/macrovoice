# vi2meta

![macOS](https://img.shields.io/badge/macOS-black?logo=apple&style=flat)
![Python](https://img.shields.io/badge/Python_3-black?logo=python&style=flat)
![Tests](https://img.shields.io/badge/tests-112_passing-black?style=flat)
![License](https://img.shields.io/badge/License-MIT-black?style=flat)

Drive [macrowhisper](https://github.com/ognistik/macrowhisper) automations with
[VoiceInk](https://github.com/beingpax/VoiceInk) dictations, without modifying either app.

macrowhisper turns a dictation into an action: paste it, open a URL, run a Shortcut, a shell
script, or AppleScript, with contextual triggers and text templating. It gets those dictations
by watching Superwhisper's recordings folder. VoiceInk writes no such folder, so the two cannot
talk. `vi2meta` is the missing piece.

```
VoiceInk Mode (Output = Custom Command)
  -> vi2meta.sh --mode <name>
  -> ~/mw-bridge/recordings/<id>/meta.json      (atomic directory rename)
  -> stock macrowhisper: validate -> match triggers -> run action
```

## Features

- Every macrowhisper action type works: paste, URL, Shortcut, shell, AppleScript
- Voice, app, URL and mode triggers all fire normally
- Zero changes to VoiceInk or macrowhisper; both run stock
- Survives the three watcher behaviours that silently drop dictations (see [How it works](#how-it-works))
- Never loses a transcript, even when publishing fails
- Logs transcript length, not content, so the log is not a record of everything you say
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
cp prototype/macrowhisper.sample.json ~/.config/macrowhisper/macrowhisper.json
macrowhisper --start-service
macrowhisper --status
```

**3. Verify the macrowhisper half, before involving VoiceInk**

Set `defaults.activeAction` to `markerLog` in the config, then:

```sh
VOICEINK_TRANSCRIPT='hello world' ./prototype/vi2meta.sh --watch ~/mw-bridge
sleep 2 && cat ~/mw-bridge/fired.log
```

A line appears. If it does not, fix that before going further.

**4. Check what VoiceInk actually sends**

In VoiceInk, create a Mode with Output = **Custom Command** pointed at `prototype/probe.sh`,
dictate a few times, then read `~/mw-bridge/probe.log`. Confirm one `INVOCATION` block per
dictation and that nothing was pasted into the focused app.

**5. Go live**

Point the Mode at `prototype/vi2meta.sh`, set `defaults.activeAction` back to `autoPaste`, grant
macrowhisper Accessibility permission, focus a text field, and dictate. Say "google best pizza"
to try the voice trigger from the sample config.

VoiceInk does not tell the command which Mode fired, so bake the name into each Mode's command
if you want macrowhisper's `triggerModes` to work:

```
/abs/path/to/prototype/vi2meta.sh --mode email
```

## Options

| Flag | Default | Description |
| --- | --- | --- |
| `--mode <name>` | none | Written as `modeName`, feeding macrowhisper's `triggerModes` |
| `--watch <path>` | `$MW_BRIDGE_WATCH` or `~/mw-bridge` | macrowhisper's watch root |
| `--gap <seconds>` | `1.0` | Minimum spacing between publishes |
| `--drain-only` | off | Publish anything left in the spool and exit |
| `--log-transcript` | off | Log transcript text instead of just its length |

## How it works

macrowhisper's watcher has four behaviours a naive bridge trips over. Each is a **silent**
failure: nothing errors, the dictation just disappears. Line references are to
`Watcher/RecordingsFolderWatcher.swift` in macrowhisper 2.1.1.

| Behaviour | Evidence | How `vi2meta` handles it |
| --- | --- | --- |
| A folder without `meta.json` inside it takes a slow path, then gets cancelled after 17s for having no `.wav` | `:38`, `:457-462`, `:1928-1935` | Builds the folder in a staging dir and renames the whole directory in, so it is never seen half-built |
| Two folders appearing in one filesystem event means **none** of them run | `:327-345` | Serializes publishing behind an `flock` with a minimum gap |
| A folder whose name sorts below the newest one is discarded as cloud-sync replay | `:350` | Mints the published name at publish time, always above the current maximum |
| VoiceInk suppresses its own paste, then kills the command at 10s, so the transcript exists nowhere else | `TranscriptionDelivery.swift:43-46`, `:115` | Spools first, unconditionally, then publishes. Always exits 0 |

Measured on a real macrowhisper 2.1.1 install: three folders created in a tight loop produced
one action and two losses. Five concurrent dictations produced five folders but only four
actions, until the naming fix. After it, five of five.

## Limitations

| Limitation | Detail |
| --- | --- |
| Clipboard context is degraded | macrowhisper looks back `clipboardBuffer` seconds from when the folder appears. Here that is *after* dictation, so with the 5s default, any dictation longer than 5s loses its pre-recording clipboard. The sample config sets 60 |
| `{{selectedText}}` is post-dictation | Reflects what is selected after you finish speaking, not before. Raising `clipboardBuffer` does not fix this |
| Only the final text | VoiceInk exposes no raw-versus-enhanced split, so `{{llmResult}}` is empty. Use `{{swResult}}` |
| No `{{segments}}` | No speaker diarization is available |
| Per-Mode, not global | Only Modes set to Custom Command feed the bridge |

Front-app placeholders and all trigger types are unaffected, since they resolve at action time.
Every one of these is an artifact of bridging. A native Superwhisper-compatible `meta.json`
export inside VoiceInk would fix them all at the source.

## Where things live

| Path | Purpose |
| :--- | :--- |
| `prototype/vi2meta/transcript.py` | Resolve the transcript from env, with stdin fallback |
| `prototype/vi2meta/meta.py` | Build and serialize the `meta.json` document (pure) |
| `prototype/vi2meta/publisher.py` | Staging, spool, drain lock, atomic renames |
| `prototype/vi2meta/cli.py` | Wiring, logging, exit-code policy |
| `prototype/test_harness.py` | Port of macrowhisper's own validation gate, used as the test oracle |
| `prototype/vi2meta.sh` | The one-liner you paste into VoiceInk |
| `prototype/probe.sh` | Captures what VoiceInk actually sends |
| `prototype/macrowhisper.sample.json` | Ready-to-use macrowhisper config |

## Development

```sh
cd prototype
python3 -m unittest discover -s tests -t tests -v
```

`test_harness.py` is a branch-for-branch port of macrowhisper's `isValidRecordingMetaJson`
(`Utils/RecordingReferenceResolver.swift:34-53`). Every generated document is asserted against
it, which is what lets the adapter be verified without macrowhisper running. If macrowhisper
changes that gate, update the port; `tests/test_harness_port.py` will show what drifted.

Tests cover a 31-entry matrix of transcripts that break naive JSON emitters (quotes,
backslashes, CRLF, NUL, control characters, astral-plane codepoints, combining accents, RTL,
100k chars, JSON lookalikes, shell metacharacters), name monotonicity across 10,000 sequential
calls, a watcher thread asserting no directory is ever observed without its `meta.json`, and
12-thread concurrency asserting zero transcript loss.

To lower `--gap`, bisect downward and use macrowhisper's own log as the oracle:

```sh
grep -iE "burst protection|older than existing" ~/Library/Logs/Macrowhisper/macrowhisper.log
```

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Nothing happens | Is the service running? `macrowhisper --status`. Does `watch` match `--watch`? |
| Some dictations do nothing | Check macrowhisper's log for `burst protection` or `older than existing`, then raise `--gap` |
| Text appears but no paste | macrowhisper needs Accessibility permission |
| Quotes look backslashed in `fired.log` | Expected. macrowhisper shell-escapes `{{swResult}}` for shell actions. Insert actions are not escaped |
| Transcript missing entirely | Check `~/mw-bridge/vi2meta.log` and `~/mw-bridge/.spool/`, or force it with `--drain-only` |

## Status

The macrowhisper half is proven end to end against a real macrowhisper 2.1.1 install. The
VoiceInk half is built to VoiceInk's source at `v2.1` and has not yet been run against live
VoiceInk, which is what step 4 of Setup is for.

## Contributing

Issues and pull requests are welcome. Please open a discussion first if you plan a larger
change, so we can align on the approach.

## License

Released under the [MIT License](./LICENSE).

Independent personal project, not affiliated with VoiceInk, macrowhisper, or Superwhisper.
Line references describe third-party source as audited at macrowhisper 2.1.1 and VoiceInk 2.1.
