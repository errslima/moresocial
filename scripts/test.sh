#!/usr/bin/env bash
# One local verification entry point. Needs no Google, WhatsApp or AI credentials.
#   bash scripts/test.sh            # Python unit/integration (real Postgres+pgvector), browser, Node
# Postgres: uses TEST_DATABASE_ADMIN_URL if set (e.g. CI service), else a private pgserver.
# Set SKIP_BROWSER=1 or SKIP_NODE=1 only where those runtimes are unavailable; the summary says so.
set -euo pipefail
cd "$(dirname "$0")/.."
command -v uv >/dev/null || { echo "uv is required (https://docs.astral.sh/uv/)" >&2; exit 1; }
uv sync --frozen -p 3.12 >/dev/null
skipped=()
if [ "${SKIP_BROWSER:-0}" = 1 ]; then
  skipped+=(browser)
  uv run --frozen pytest --ignore tests/test_browser.py
else
  uv run --frozen playwright install chromium >/dev/null
  uv run --frozen pytest
fi
if [ "${SKIP_NODE:-0}" = 1 ]; then
  skipped+=(node)
else
  command -v node >/dev/null || { echo "node 22 is required for connector tests (or SKIP_NODE=1)" >&2; exit 1; }
  (cd connector && node --test)
fi
uv run --frozen python -m compileall -q deploy/provisioner.py app worker
echo "all checks passed${skipped:+ (skipped: ${skipped[*]})}"
