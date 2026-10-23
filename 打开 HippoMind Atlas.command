#!/bin/zsh
# Double-click this file in Finder to open HippoMind Atlas.
# It starts hippomindd if needed and opens the console in your browser.
cd "$(dirname "$0")" || exit 1
exec ./scripts/launch-atlas.sh
