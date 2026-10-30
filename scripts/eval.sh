#!/usr/bin/env bash
# Offline evaluation bench (tidying and search), run by hand: docs/dev/eval.md.
#   ./scripts/eval.sh                  simulated model, then search (free)
#   ./scripts/eval.sh tidy --model claude-sonnet-5-5 --budget 3 --trials 3   (paid)
#   ./scripts/eval.sh mcp [--model M --budget-usd B]   MCP tools seen by an assistant
# Needs the development PostgreSQL: docker compose -f compose.dev.yaml up -d
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$#" -gt 0 ]; then
  exec uv run --locked python -m tests.eval "$@"
fi
uv run --locked python -m tests.eval tidy
uv run --locked python -m tests.eval search
uv run --locked python -m tests.eval mcp
