"""Seed the synthetic two-user demo into an isolated development database.

    DATABASE_URL=... SYNTHETIC_PROVIDERS=1 AI_PROVIDER=fake uv run python scripts/seed_demo.py

Creates Alex and Blake through the real onboarding code with synthetic Google, pairs their
synthetic WhatsApp, syncs and processes everything. Refuses to run in production.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import accounts, config, db, google, whatsapp  # noqa: E402
from app.repo import Scoped  # noqa: E402


def main() -> None:
    settings = config.get()
    if settings.is_production or not settings.synthetic_providers or settings.public_origin == config.PRODUCTION_ORIGIN:
        raise SystemExit('seed_demo only runs with SYNTHETIC_PROVIDERS=1 outside production')
    from app import synthetic
    g = google.provider()
    for user in ('alex', 'blake'):
        code = synthetic.issue_code(user, list(config.GOOGLE_SCOPES), nonce='seed')
        tokens = g.exchange_code(code, 'seed-verifier')
        identity = g.verify_identity(tokens.id_token, 'seed')
        with db.session() as s:
            acct = accounts.account_for(s, identity)
            accounts.store_grant(s, acct, identity, tokens)
            wid = acct.workspace_id
        with db.session() as s:
            whatsapp.request_connect(Scoped(s, wid))
        for _ in range(2):
            with db.session() as s:
                whatsapp.status(Scoped(s, wid))
    from worker.main import drain, schedule
    schedule()
    n = drain(5000)
    print(f'seeded synthetic users alex@example.test and blake@example.test; processed {n} jobs')


if __name__ == '__main__':
    main()
