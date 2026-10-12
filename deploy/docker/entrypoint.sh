#!/bin/sh
# First start: master key and folders (nothing is overwritten afterwards).
set -eu
mkdir -p "$HOME"
synaptane init >/dev/null
exec "$@"
