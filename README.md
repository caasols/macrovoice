# vi2meta — use VoiceInk with macrowhisper

[macrowhisper](https://github.com/ognistik/macrowhisper) is an automation layer for
[Superwhisper](https://superwhisper.com): it watches Superwhisper's recordings folder and, on
each completed dictation, runs configured actions with contextual triggers, text templating and
chaining. [VoiceInk](https://github.com/beingpax/VoiceInk) is a different macOS dictation app,
and it writes no such folder, so the two cannot talk.

`vi2meta` bridges them. A VoiceInk *Custom Command* runs this adapter, which publishes a
synthetic Superwhisper-shaped `recordings/<id>/meta.json`. **Stock, unmodified macrowhisper**
picks it up and does the rest. Neither app is patched.

```
VoiceInk Mode (Output = Custom Command)
   -> vi2meta.sh --mode <name>
   -> ~/mw-bridge/recordings/<id>/meta.json     (atomic directory rename)
   -> stock macrowhisper: validate -> match triggers -> run action
```

**Status:** the macrowhisper side is proven end to end against a real macrowhisper 2.1.1
install. 112 tests pass. The VoiceInk side is built to the behavior of VoiceInk's source at
`v2.1` and has not yet been exercised against live VoiceInk — run the probe in step 5 before
relying on it.

Requires macOS, Python 3 (system Python is fine, no dependencies), VoiceInk 2.0 or later for
the Custom Command output mode, and macrowhisper.

---

## Quickstart

**1. Install macrowhisper**

```sh
brew install ognistik/formulae/macrowhisper
macrowhisper --version          # expect 2.1.1 or later
```

**2. Create a dedicated watch directory**

```sh
mkdir -p ~/mw-bridge/recordings
```

Do **not** point this at `~/superwhisper`. If you also run Superwhisper, the bridge would
interleave synthetic recordings with genuine ones.

**3. Configure macrowhisper**

```sh
cp prototype/macrowhisper.sample.json ~/.config/macrowhisper/macrowhisper.json
macrowhisper --start-service
macrowhisper --status           # confirm the recordings watcher is armed
```

Read the `_comment` blocks in the sample before editing. `clipboardBuffer: 60.0` is deliberate,
not a stray value; see Limitations.

**4. Prove the macrowhisper half, without VoiceInk**

Set `defaults.activeAction` to `markerLog` in the config, then:

```sh
VOICEINK_TRANSCRIPT='hello world' ./prototype/vi2meta.sh --watch ~/mw-bridge
sleep 2 && cat ~/mw-bridge/fired.log
```

A line appears. That is the entire downstream path working. If it does not, fix it here before
involving VoiceInk.

**5. Find out what VoiceInk actually sends**

- VoiceInk → new Mode `bridge-probe` → Output = **Custom Command**
- Command: the absolute path to `prototype/probe.sh`
- Dictate 3 or 4 times: short, long, punctuated, and once from a different app
- Read `~/mw-bridge/probe.log`

Confirm one `INVOCATION` block per dictation, `VOICEINK_TRANSCRIPT` matching stdin, and nothing
pasted into the focused app.

**6. Go live**

Point the Mode's command at `prototype/vi2meta.sh`, set `defaults.activeAction` back to
`autoPaste`, grant macrowhisper Accessibility permission (System Settings → Privacy & Security →
Accessibility), focus a text field, and dictate. Then try saying "google best pizza" to exercise
the voice trigger in the sample config.

**Per-Mode setup.** VoiceInk does not tell the command which Mode fired: the entire environment
it builds is `{"VOICEINK_TRANSCRIPT": transcript}`. So bake the name into each Mode's command
line if you want macrowhisper's `triggerModes` to work:

```
/abs/path/to/prototype/vi2meta.sh --mode email
/abs/path/to/prototype/vi2meta.sh --mode notes
```

---

## Why it is built this way

macrowhisper's watcher has four behaviors that a naive bridge trips over. Each one is a **silent**
failure: nothing errors, the dictation just disappears. Line references are to
`src/macrowhisper/Watcher/RecordingsFolderWatcher.swift` in macrowhisper 2.1.1.

**Publish a complete folder, atomically.** There is a fast path when `meta.json` already exists
as the folder appears (`:457-462`). Miss it and macrowhisper starts an audio watcher and a
17-second timer, then logs `TIMEOUT CANCELLATION` for any folder still lacking a `.wav`
(`:38`, `:1928-1935`). Bridge folders never have a `.wav`. So `vi2meta` builds the folder in a
staging directory and renames the whole directory into place, where it can never be seen
half-built.

**Never let two folders appear at once.** If more than one new directory shows up in a single
filesystem event, macrowhisper marks them all processed and runs **none** (`:327-345`).
Measured: three folders created in a tight loop produced one action and two losses. `vi2meta`
serializes publication behind an `flock` with a minimum gap.

**Never publish a name that sorts backwards.** A folder whose name sorts below the newest
existing one is discarded as cloud-sync replay (`:350`). This was found by running the
experiment, not by reading the code: five concurrent dictations put five folders on disk but
fired only four actions. `vi2meta` mints the published name at publish time, always above the
current maximum.

**Never lose the transcript.** VoiceInk has already suppressed its own paste by the time the
command runs (`TranscriptionDelivery.swift:43-46`), so the text exists nowhere else, and it
kills the command at 10 seconds (`:115`). `vi2meta` spools first, which is fast and
unconditional, and only then tries to publish. It always exits 0: a non-zero exit shows the user
an error without recovering their words.

---

## Limitations

**Clipboard context is degraded, and the amount is calculable.** macrowhisper captures
pre-recording clipboard when the recording folder appears, looking back `clipboardBuffer`
seconds. Under Superwhisper the folder appears when recording *starts*; here it appears when
dictation *ends*. With the 5-second default, **any dictation longer than 5 seconds loses its
pre-recording clipboard entirely.** The sample config sets 60.0.

**`{{selectedText}}` reflects post-dictation state**, not what was selected before speaking.
Raising `clipboardBuffer` does not fix this.

**Only the final text exists.** VoiceInk exposes no raw-versus-enhanced split, so the transcript
goes in `result` and `languageModelName`/`llmResult` are left absent. `{{llmResult}}` will be
empty; use `{{swResult}}`.

**No `segments`.** No speaker diarization is available, so `{{segments}}` is empty.

**Per-Mode, not global.** Only Modes set to Custom Command feed the bridge.

Front-app placeholders and voice, app, URL and mode triggers are unaffected, since those resolve
at action time.

All of these are artifacts of bridging. A native Superwhisper-compatible `meta.json` export
inside VoiceInk would fix every one at the source, and would make VoiceInk a drop-in for
macrowhisper and every other Superwhisper-folder tool.

---

## Development

```sh
cd prototype && python3 -m unittest discover -s tests -t tests -v     # 112 tests
```

`test_harness.py` is a branch-for-branch port of macrowhisper's `isValidRecordingMetaJson`
(`Utils/RecordingReferenceResolver.swift:34-53`). It is the test oracle: every generated
document is asserted against it, which is what lets the adapter be verified without macrowhisper
running. **If macrowhisper changes that gate, update the port** — `tests/test_harness_port.py`
will show what drifted.

| File | Role |
|---|---|
| `vi2meta/transcript.py` | resolve transcript from env, stdin fallback |
| `vi2meta/meta.py` | pure meta.json construction |
| `vi2meta/publisher.py` | staging, spool, drain lock, atomic renames |
| `vi2meta/cli.py` | wiring, logging, exit-code policy |
| `test_harness.py` | the ported validation oracle |

Tests cover a 31-entry matrix of transcripts that break naive JSON emitters (quotes,
backslashes, CRLF, NUL, control characters, astral-plane codepoints, combining accents, RTL,
100k chars, JSON lookalikes, shell metacharacters), name monotonicity across 10,000 sequential
calls, a watcher thread asserting no directory is ever observed without its `meta.json`, and
12-thread concurrency asserting zero transcript loss.

**Logging.** `vi2meta` records transcript *length*, not content, so the log does not become a
plaintext record of everything you dictate. `--log-transcript` opts in.

**Tuning `--gap`.** The minimum seconds between publishes, defaulting to 1.0. It defends the
burst-protection behavior above. It costs nothing for an isolated dictation, since the wait is
zero when the previous publish was longer ago than the gap. Erring large is nearly free; erring
small loses dictations silently. To lower it, bisect downward and treat macrowhisper's own log
as the oracle:

```sh
grep -iE "burst protection|older than existing" ~/Library/Logs/Macrowhisper/macrowhisper.log
```

## Troubleshooting

| Symptom | Cause |
|---|---|
| Nothing happens at all | Is the service running? `macrowhisper --status`. Is `watch` the same directory you passed to `--watch`? |
| Some dictations do nothing | Check `~/Library/Logs/Macrowhisper/macrowhisper.log` for `burst protection` or `older than existing`. Raise `--gap`. |
| Text appears but no paste | macrowhisper needs Accessibility permission. Without it the log says `No accessibility permissions`. |
| Quotes look backslashed in `fired.log` | Expected. macrowhisper shell-escapes `{{swResult}}` for shell actions. Insert actions are not escaped. |
| Transcript missing entirely | Check `~/mw-bridge/vi2meta.log` and `~/mw-bridge/.spool/`. A deferred entry publishes on the next run, or force it with `--drain-only`. |

## Scope

Independent personal project. Not affiliated with VoiceInk, macrowhisper, or Superwhisper.
Line references describe third-party source as audited at macrowhisper 2.1.1 and VoiceInk 2.1;
verify against current releases before relying on them.

MIT licensed.
