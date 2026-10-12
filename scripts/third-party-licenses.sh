#!/usr/bin/env bash
# Licenses of the runtime Python dependencies (those of the Docker image and of an installation
# from source), as a Markdown table for THIRD_PARTY_NOTICES.md. Development dependencies are
# left out: the list comes from `uv export --no-dev` (uv.lock).
#   scripts/third-party-licenses.sh                  # Markdown table on standard output
#   scripts/third-party-licenses.sh --format=csv     # any other pip-licenses option
# Licenses are read from the project environment (.venv, synced with uv.lock); packages
# reserved for another platform (environment markers) are reported separately.
set -euo pipefail
cd "$(dirname "$0")/.."

PIP_LICENSES="pip-licenses==5.5.5"

mapfile -t packages < <(
  uv export --frozen --no-dev --no-hashes --no-emit-project --no-header --no-annotate \
    | sed -n 's/^\([A-Za-z0-9._-]*\)==.*/\1/p'
)
if [ "${#packages[@]}" -eq 0 ]; then
  echo "third-party-licenses: no dependency found in uv.lock" >&2
  exit 1
fi

# Locked packages missing here (pywin32, tzdata on Linux…): pip-licenses would keep quiet about them.
uv run --frozen python -I - "${packages[@]}" >&2 <<'EOF'
import sys
from importlib.metadata import PackageNotFoundError, distribution

missing = []
for name in sys.argv[1:]:
    try:
        distribution(name)
    except PackageNotFoundError:
        missing.append(name)
if missing:
    print("Missing on this platform (environment markers):", ", ".join(missing))
EOF

# `--packages` takes the rest of the line: it comes last.
uv run --frozen --with "$PIP_LICENSES" \
  pip-licenses --from=mixed --format=markdown "$@" --packages "${packages[@]}"
