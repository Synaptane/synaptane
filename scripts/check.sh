#!/usr/bin/env bash
# Local and CI checks: dependencies, lint, format, typing, tests.
#
#   ./scripts/check.sh                 full suite, in parallel, with coverage
#   ./scripts/check.sh tests/unit      only these tests, in parallel, without coverage
#
# Coverage (lines and branches, subprocesses included) and its threshold are set in
# [tool.coverage] of pyproject.toml; the detailed report is written to htmlcov/ and
# coverage.xml. The threshold only makes sense for the full suite: a selection of tests would
# necessarily miss it.
set -euo pipefail
cd "$(dirname "$0")/.."
uv lock --check
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
if [ "$#" -eq 0 ]; then
  set -- --cov --cov-report=term --cov-report=xml --cov-report=html
fi
# One process per core: each process has its own throwaway database (tests/integration/conftest.py).
uv run --locked pytest -n auto "$@"
