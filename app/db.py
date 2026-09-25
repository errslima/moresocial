from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from . import config

_engine: Engine | None = None
_factory: sessionmaker | None = None


def normalize_url(url: str) -> str:
    if url.startswith('postgresql://'):
        return 'postgresql+psycopg://' + url[len('postgresql://'):]
    return url


def engine() -> Engine:
    global _engine, _factory
    if _engine is None:
        _engine = create_engine(normalize_url(config.get().database_url), pool_pre_ping=True, pool_size=5,
                                max_overflow=5, future=True)
        _factory = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def reset() -> None:
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _factory = None


@contextmanager
def session() -> Iterator[Session]:
    engine()
    s = _factory()
    try:
        yield s
        s.commit()
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()


def migration_state() -> tuple[bool, str | None]:
    """(database reachable and at head, current revision)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from pathlib import Path
    cfg = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    with engine().connect() as c:
        current = c.execute(text('SELECT version_num FROM alembic_version')).scalar()
    return current == head, current
