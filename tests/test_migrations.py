"""Historical migrations stay executable on a populated database."""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from app import db
from app.models import ProviderUsage
from conftest import drain
from worker.main import schedule


def test_migration_0003_round_trip_keeps_usage_history(browser, database):
    browser.login('alex', calendar=False)
    schedule()
    drain()
    with db.session() as s:
        before = s.scalar(select(func.count()).select_from(ProviderUsage))
    assert before
    cfg = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    cfg.attributes['database_url'] = database['runtime_url']
    try:
        command.downgrade(cfg, '0002')
    finally:
        command.upgrade(cfg, 'head')
    command.check(cfg)
    with db.session() as s:
        rows = s.scalars(select(ProviderUsage)).all()
        # Revision 0003 backfills the provider label that existed when it was written.
        assert len(rows) == before and all(u.billing == 'operator' and u.provider == 'anthropic' for u in rows)
