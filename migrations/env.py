from alembic import context
from sqlalchemy import create_engine, pool

from app import config
from app.db import normalize_url
from app.models import Base

target_metadata = Base.metadata


def run_migrations_online() -> None:
    url = context.config.attributes.get('database_url') or config.get().database_url
    engine = create_engine(normalize_url(url), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
