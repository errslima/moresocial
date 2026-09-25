#!/usr/bin/env bash
# Local synthetic environment at http://localhost:8772/moresocial/ (never the production URL).
# Synthetic Google consent, synthetic WhatsApp pairing, deterministic fake AI; a private
# Postgres under .dev/ . Ctrl-C stops everything.
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --frozen -p 3.12 >/dev/null
mkdir -p .dev
export MORESOCIAL_MODE=development PUBLIC_ORIGIN=http://localhost:8772 BASE_PATH=/moresocial \
  SYNTHETIC_PROVIDERS=1 AI_PROVIDER=fake BETA_ALLOWLIST=alex@example.test,blake@example.test
export DATABASE_URL=$(uv run --frozen python -c "
import pgserver; s = pgserver.get_server('.dev/postgres', cleanup_mode=None)
s.psql('CREATE EXTENSION IF NOT EXISTS vector;'); print(s.get_uri())")
uv run --frozen alembic upgrade head
uv run --frozen python -m worker.main & worker=$!
trap 'kill $worker 2>/dev/null' EXIT
uv run --frozen python -c "
import uvicorn
from app.prefix import PrefixStrip
from app.web import create_app
uvicorn.run(PrefixStrip(create_app()), host='127.0.0.1', port=8772)"
