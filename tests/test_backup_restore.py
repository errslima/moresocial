"""M6: the database part of deploy/backup.sh + deploy/restore.sh, run against the disposable
test Postgres (no Docker): pg_dump -Fc, the same openssl encryption, restore into a new
database, migration head and data checks. Container/session steps are verified on the host."""
import os
from pathlib import Path
import shutil
import subprocess
import uuid

import pytest
from sqlalchemy import create_engine, text

from app import db, security
from conftest import drain, workspace_of
from worker.main import schedule


def pg_bin(name):
    if os.environ.get('TEST_DATABASE_ADMIN_URL'):  # external server: use matching client tools on PATH
        found = shutil.which(name)
        if not found:
            pytest.skip(f'{name} not available')
        return found
    try:
        import pgserver
        candidate = Path(pgserver.__file__).parent / 'pginstall' / 'bin' / name
        if candidate.exists():
            return str(candidate)
    except ImportError:
        pass
    found = shutil.which(name)
    if not found:
        pytest.skip(f'{name} not available')
    return found


def libpq(url):
    return url.replace('postgresql+psycopg://', 'postgresql://', 1)


def test_backup_encrypt_restore_roundtrip(browser, database, tmp_path):
    if not shutil.which('openssl'):
        pytest.skip('openssl not available')
    browser.login('alex')
    schedule()
    drain()
    passfile = tmp_path / 'backup_passphrase'
    passfile.write_text('synthetic-backup-passphrase')
    dump = tmp_path / 'db.dump'
    subprocess.run([pg_bin('pg_dump'), '-Fc', '-d', libpq(database['admin_url']), '-f', str(dump)], check=True)
    enc = tmp_path / 'moresocial.tar.enc'
    subprocess.run(['openssl', 'enc', '-aes-256-cbc', '-pbkdf2', '-iter', '200000', '-salt', '-pass', f'file:{passfile}',
                    '-in', str(dump), '-out', str(enc)], check=True)
    assert b'alex@example.test' not in enc.read_bytes()
    restored = tmp_path / 'restored.dump'
    subprocess.run(['openssl', 'enc', '-d', '-aes-256-cbc', '-pbkdf2', '-iter', '200000', '-pass', f'file:{passfile}',
                    '-in', str(enc), '-out', str(restored)], check=True)
    name = 'moresocial_restore_' + uuid.uuid4().hex[:8]
    admin = create_engine(database['admin_url'].replace('postgresql://', 'postgresql+psycopg://', 1), isolation_level='AUTOCOMMIT')
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE {name}'))
    target = database['admin_url'].rsplit('/', 1)[0] + '/' + name
    if '?' in database['admin_url']:
        target = database['admin_url'].split('?')[0].rsplit('/', 1)[0] + '/' + name + '?' + database['admin_url'].split('?', 1)[1]
    try:
        subprocess.run([pg_bin('pg_restore'), '--no-owner', '--exit-on-error', '-d', libpq(target), str(restored)], check=True)
        e = create_engine(target.replace('postgresql://', 'postgresql+psycopg://', 1))
        with e.connect() as c, db.engine().connect() as live:
            assert c.execute(text('SELECT version_num FROM alembic_version')).scalar() == \
                live.execute(text('SELECT version_num FROM alembic_version')).scalar()
            for table in ('workspaces', 'sources', 'chunks', 'embeddings', 'claims', 'connections'):
                assert c.execute(text(f'SELECT count(*) FROM {table}')).scalar() == \
                    live.execute(text(f'SELECT count(*) FROM {table}')).scalar(), table
            token = c.execute(text('SELECT refresh_token_enc FROM connections')).scalar()
            assert security.decrypt(token)  # grants readable with the preserved encryption key
            # vector search works on the restored copy
            assert c.execute(text('SELECT count(*) FROM embeddings WHERE vector <=> vector IS NOT NULL')).scalar() > 0
        e.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)'))
        admin.dispose()
