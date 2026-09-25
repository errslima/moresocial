"""Test harness: a disposable real PostgreSQL + pgvector database, migrated with Alembic,
used through a non-superuser runtime role. Synthetic Google/WhatsApp/AI providers only.

Set TEST_DATABASE_ADMIN_URL (superuser, e.g. a CI service container) to use an existing
server; otherwise a private pgserver instance is started in a temporary directory.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit
import uuid

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ORIGIN = 'https://moresocial.test'
RUNTIME_ROLE = 'moresocial_runtime'
RUNTIME_PASSWORD = 'synthetic-test-password'


def _admin_url() -> tuple[str, object]:
    url = os.environ.get('TEST_DATABASE_ADMIN_URL')
    if url:
        return url, None
    import pgserver
    data = Path(tempfile.mkdtemp(prefix='moresocial-pg-'))
    server = pgserver.get_server(data, cleanup_mode='delete')
    return server.get_uri(), server


@pytest.fixture(scope='session')
def database():
    admin_url, server = _admin_url()
    name = 'moresocial_test_' + uuid.uuid4().hex[:8]
    admin = create_engine(admin_url.replace('postgresql://', 'postgresql+psycopg://', 1), isolation_level='AUTOCOMMIT')
    with admin.connect() as c:
        c.execute(text(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RUNTIME_ROLE}') THEN "
                       f"CREATE ROLE {RUNTIME_ROLE} LOGIN PASSWORD '{RUNTIME_PASSWORD}' NOSUPERUSER NOCREATEDB NOCREATEROLE; END IF; END $$"))
        c.execute(text(f'CREATE DATABASE {name} OWNER {RUNTIME_ROLE}'))
    db_admin_url = _with_db(admin_url, name)
    ext = create_engine(db_admin_url.replace('postgresql://', 'postgresql+psycopg://', 1), isolation_level='AUTOCOMMIT')
    with ext.connect() as c:
        c.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
    ext.dispose()
    runtime_url = _with_user(db_admin_url, RUNTIME_ROLE, RUNTIME_PASSWORD)
    tmp = Path(tempfile.mkdtemp(prefix='moresocial-secrets-'))
    (tmp / 'operator_key').write_text('synthetic-operator-key-for-tests-only')
    env = {'MORESOCIAL_MODE': 'test', 'PUBLIC_ORIGIN': ORIGIN, 'BASE_PATH': '/moresocial', 'DATABASE_URL': runtime_url,
           'SYNTHETIC_PROVIDERS': '1', 'AI_PROVIDER': 'fake', 'BETA_ALLOWLIST': 'alex@example.test,blake@example.test',
           'OPERATOR_KEY_FILE': str(tmp / 'operator_key'), 'WHATSAPP_CONNECTOR_CAP': '2'}
    os.environ.update(env)
    from app import config, db
    config.override(config.load(env))
    from alembic import command
    from alembic.config import Config
    cfg = Config(str(ROOT / 'alembic.ini'))
    cfg.attributes['database_url'] = runtime_url
    command.upgrade(cfg, 'head')
    with create_engine(runtime_url.replace('postgresql://', 'postgresql+psycopg://', 1)).connect() as c:
        assert c.execute(text('SELECT rolsuper FROM pg_roles WHERE rolname = current_user')).scalar() is False
    yield {'runtime_url': runtime_url, 'admin_url': db_admin_url, 'env': env}
    db.reset()
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)'))
    admin.dispose()
    if server is not None:
        server.cleanup()


def _with_db(url: str, name: str) -> str:
    parts = urlsplit(url)
    return parts._replace(path='/' + name).geturl()


def _with_user(url: str, user: str, password: str) -> str:
    parts = urlsplit(url)
    host = parts.netloc.split('@')[-1]
    return parts._replace(netloc=f'{user}:{password}@{host}').geturl()


@pytest.fixture(autouse=True)
def clean(database):
    from app import ai, db, google, synthetic, whatsapp, security
    from app.models import Base
    with db.engine().begin() as c:
        tables = ', '.join(t.name for t in Base.metadata.sorted_tables)
        c.execute(text(f'TRUNCATE {tables} CASCADE'))
    synthetic.STATE.__init__()
    google.set_provider(None)
    ai.set_provider(None)
    whatsapp.FAKE.clear()
    from app import auth_routes
    auth_routes._hits.clear()
    yield


@pytest.fixture
def app(database):
    from app.prefix import PrefixStrip
    from app.web import create_app
    return PrefixStrip(create_app())


class Browser:
    """A TestClient that behaves like a same-origin browser behind the Caddy prefix."""

    def __init__(self, app):
        from fastapi.testclient import TestClient
        self.c = TestClient(app, base_url=ORIGIN, follow_redirects=False)
        self.c.headers['Origin'] = ORIGIN

    def get(self, path, **kw):
        return self.c.get('/moresocial' + path, **kw)

    def post(self, path, data=None, **kw):
        return self.c.post('/moresocial' + path, data=data, **kw)

    def csrf(self, path='/home') -> str:
        import re
        html = self.get(path).text
        m = re.search(r'name="csrf" value="([^"]+)"', html)
        assert m, 'no csrf token on page'
        return m.group(1)

    def start_google(self, purpose='login', csrf=''):
        r = self.post('/api/auth/google/start', {'purpose': purpose, 'csrf': csrf})
        assert r.status_code == 303, r.text
        return r

    def login(self, user='alex', gmail=True, calendar=True, offline=True, action='allow'):
        r = self.start_google()
        return self.consent(r, user, gmail, calendar, offline, action)

    def consent(self, start_response, user='alex', gmail=True, calendar=True, offline=True, action='allow'):
        loc = urlsplit(start_response.headers['location'])
        assert loc.path == '/moresocial/dev/google/authorize'
        q = {k: v[0] for k, v in parse_qs(loc.query).items()}
        form = {'state': q['state'], 'nonce': q['nonce'], 'user': user, 'action': action}
        if gmail:
            form['gmail'] = '1'
        if calendar:
            form['calendar'] = '1'
        if offline:
            form['offline'] = 'yes'
        r = self.c.post('/moresocial/dev/google/authorize', data=form)
        assert r.status_code == 303
        cb = urlsplit(r.headers['location'])
        return self.c.get(cb.path + '?' + cb.query)


@pytest.fixture
def browser(app):
    return Browser(app)


@pytest.fixture
def make_browser(app):
    return lambda: Browser(app)


def workspace_of(email: str):
    from app import db
    from app.models import Account
    with db.session() as s:
        return s.query(Account).filter(Account.email == email).one().workspace_id


def drain():
    from worker.main import drain as _drain
    return _drain()
