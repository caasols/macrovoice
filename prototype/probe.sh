#!/bin/zsh
# probe.sh - capture exactly what VoiceInk hands a Custom Command.
#
# Run this BEFORE trusting vi2meta. It answers, empirically, the questions a source
# audit can only answer theoretically:
#
#   - Is $VOICEINK_TRANSCRIPT the final (enhanced) text?
#   - Is stdin the same text?
#   - Does it fire exactly once per dictation?
#   - Is VoiceInk's own paste actually suppressed?
#   - Is anything else exposed (mode, front app)?
#
# The code says the answer to the last one is "no, only VOICEINK_TRANSCRIPT"
# (CustomCommandDeliveryRunner.swift:12-16). This proves it against the build you
# actually have installed, which may differ from the audited snapshot.
#
# SETUP
#   1. chmod +x prototype/probe.sh
#   2. In VoiceInk: create a Mode named "bridge-probe", set Output = Custom Command,
#      and set the command to the absolute path of this script.
#   3. Dictate 3 or 4 times: something short, something long, something with
#      punctuation, and one from a different front app.
#   4. Read ~/mw-bridge/probe.log
#
# WHAT TO LOOK FOR
#   - One INVOCATION block per dictation. More than one means the firing semantics
#     assumption is wrong and vi2meta needs idempotence.
#   - ENV and STDIN sections carrying the same text.
#   - Nothing pasted into the focused app while probing (paste suppression working).
#   - Any VOICEINK_* variable beyond VOICEINK_TRANSCRIPT would be a genuine finding:
#     it would mean modeName could be discovered rather than passed via --mode.

set -u

LOG="${MW_BRIDGE_WATCH:-$HOME/mw-bridge}/probe.log"
mkdir -p "$(dirname "$LOG")"

{
  echo "================================================================"
  echo "INVOCATION $(date -u +%Y-%m-%dT%H:%M:%S.%NZ)"
  echo "  pid=$$ ppid=$PPID"
  echo "  cwd=$(pwd)"
  echo "  argv=($*)"
  echo "  tty=$(tty 2>/dev/null || echo 'not a tty')"
  echo
  echo "--- VOICEINK_TRANSCRIPT ---"
  if [[ -n "${VOICEINK_TRANSCRIPT-}" ]]; then
    echo "  set, ${#VOICEINK_TRANSCRIPT} chars"
    printf '  >>>%s<<<\n' "$VOICEINK_TRANSCRIPT"
  else
    echo "  NOT SET"
  fi
  echo
  echo "--- all VOICEINK_* variables ---"
  env | grep '^VOICEINK_' || echo "  (none beyond what is shown above)"
  echo
  echo "--- stdin ---"
  stdin_text="$(cat)"
  echo "  ${#stdin_text} chars"
  printf '  >>>%s<<<\n' "$stdin_text"
  echo
  echo "--- do env and stdin agree? ---"
  if [[ "${VOICEINK_TRANSCRIPT-}" == "$stdin_text" ]]; then
    echo "  identical"
  else
    echo "  DIFFERENT - worth investigating, vi2meta prefers the env var"
  fi
  echo
  echo "--- full environment ---"
  env | sort | sed 's/^/  /'
  echo
} >> "$LOG" 2>&1

exit 0
