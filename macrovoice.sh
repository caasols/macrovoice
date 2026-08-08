#!/bin/zsh
# macrovoice.sh - the one line you paste into VoiceInk's Custom Command field.
#
# VoiceInk runs the command as `/bin/zsh -lc <command>` with VOICEINK_TRANSCRIPT in
# the environment and the transcript on stdin, then kills it after 10 seconds
# (TranscriptionDelivery.swift:115). This wrapper just locates the Python package
# and forwards everything.
#
# PER-MODE USAGE
#   VoiceInk does not tell the command which Mode produced the dictation: the entire
#   environment it builds is {"VOICEINK_TRANSCRIPT": transcript}
#   (CustomCommandDeliveryRunner.swift:12-16). So if you want macrowhisper's
#   triggerModes to work, give each Mode its own command line with the name baked in:
#
#     /path/to/prototype/macrovoice.sh --mode email
#     /path/to/prototype/macrovoice.sh --mode notes
#
#   Without --mode, meta.json simply omits modeName and triggerModes will not match.

set -u

# Resolve this script's own directory (following symlinks) so the macrovoice package is
# importable no matter what working directory VoiceInk happens to invoke us from.
HERE="${0:A:h}"
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"

exec /usr/bin/env python3 -m macrovoice "$@"
