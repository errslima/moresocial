"""Regressions for the six defects found in the implementation review.

Real disposable Postgres; synthetic HTTP providers and fake Docker. No real accounts.
"""
import asyncio
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
import uuid

import httpx
import pytest
from sqlalchemy import create_engine, func, select, update

from app import ai, db, google_sync, ingest, memory, retrieval
from app.internal import create_internal_app
from app.models import Chunk, ChunkSource, ConnectorCleanup, Job, Source, SyncStream, PIPELINE_VERSION
from app.repo import Scoped
from conftest import ORIGIN, workspace_of
from test_backup_restore import pg_bin, libpq
from test_whatsapp import A, KEY_A, OPERATOR, internal, key_of, link, packet, provisioner


def test_slow_ask_keeps_public_and_internal_listeners_responsive(browser, app, monkeypatch):
    browser.login('alex')
    link(browser)
    connector_key = key_of('alex@example.test')
    csrf = browser.csrf('/ask')
    entered, release = threading.Event(), threading.Event()

    def slow(*args):
        entered.set()
        release.wait(2)  # bounded even if an event-loop regression prevents the test's finally block
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(retrieval, 'ask', slow)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN,
                                     cookies=dict(browser.c.cookies), headers={'Origin': ORIGIN}) as public, \
                httpx.AsyncClient(transport=httpx.ASGITransport(app=create_internal_app()), base_url=ORIGIN) as private:
            ask = asyncio.create_task(public.post('/moresocial/ask', data={'question': 'test', 'csrf': csrf}))
            try:
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(.01)
                assert entered.is_set() and not ask.done()
                assert (await asyncio.wait_for(public.get('/moresocial/health'), .5)).status_code == 200
                result = await asyncio.wait_for(private.post('/internal/connector/ingest', json=packet(),
                    headers={'Authorization': 'Bearer ' + connector_key}), .5)
                assert result.status_code == 200
                assert not ask.done(), 'A slow provider blocked both listeners'
            finally:
                release.set()
                response = await ask
            assert response.status_code == 303

    asyncio.run(run())


def test_final_account_cleanup_survives_deletion_and_lost_ack(browser, internal, tmp_path):
    browser.login('alex')
    link(browser)
    wid = workspace_of('alex@example.test')
    old_key = key_of('alex@example.test')
    fetch = lambda: internal.get('/internal/manage/connectors', headers=OPERATOR).json()
    p, _, _ = provisioner(tmp_path, fetch)
    p.reconcile()
    marker = tmp_path / wid.hex / 'session' / 'auth.marker'
    marker.write_text('synthetic linked session')
    assert browser.post('/account/delete', {'confirm': 'DELETE', 'csrf': browser.csrf()}).status_code == 303
    state = fetch()
    assert state['workspace_count'] == 0
    assert state['connectors'] == [{'workspace': wid.hex, 'desired': 'deleted', 'observed': 'pending', 'key': None}]
    assert internal.post('/internal/connector/ingest', json=packet(),
                         headers={'Authorization': 'Bearer ' + old_key}).status_code == 401

    def lost_ack(*args):
        raise OSError('synthetic lost acknowledgment')

    p.report = lost_ack
    p.reconcile()
    assert not marker.exists() and not p.docker.containers
    assert fetch()['connectors']  # cleanup is durable until acknowledged
    p.report = lambda ws, state, detail=None: internal.post(f'/internal/manage/connectors/{ws}/observed',
                                                           json={'state': state}, headers=OPERATOR).raise_for_status()
    p.reconcile()
    assert fetch()['connectors'] == []
    with db.session() as s:
        assert s.get(ConnectorCleanup, wid) is None


def test_gmail_burst_resumes_history_pages_without_losing_messages(browser, monkeypatch):
    browser.login('alex', calendar=False)
    wid = workspace_of('alex@example.test')
    with db.session() as s:
        st = s.scalars(select(SyncStream).where(SyncStream.workspace_id == wid, SyncStream.stream == 'gmail')).one()
        st.cursor = 'before-burst'
        sid = st.id
    pages = []
    failing = [True]

    def history(token, cursor, page):
        pages.append(page)
        if page is None:
            ids, nxt = range(501), 'page-two'
        else:
            assert page == 'page-two'
            ids, nxt = range(501, 702), None
        return {'historyId': 'after-burst', 'nextPageToken': nxt,
                'history': [{'messagesAdded': [{'message': {'id': str(i)}} for i in ids]}]}

    def message(token, ident):
        if ident == '651' and failing[0]:
            raise google_sync.GoogleError('unavailable')
        return {'id': ident}

    g = google_sync.provider()
    monkeypatch.setattr(g, 'gmail_history', history)
    monkeypatch.setattr(g, 'gmail_message', message)
    monkeypatch.setattr(google_sync, 'gmail_record', lambda raw, account, own: ingest.SourceRecord(
        provider='gmail', provider_account=account, provider_object_id=raw['id'], kind='email',
        conversation_id='burst', body='Synthetic message ' + raw['id']))
    with pytest.raises(google_sync.GoogleError):
        google_sync.run(wid, sid)
    with db.session() as s:
        st = s.get(SyncStream, sid)
        assert st.cursor == 'before-burst' and st.progress['page'] == 'page-two'
        assert s.scalar(select(func.count()).select_from(Source)) == 601
        jobs_before = s.scalar(select(func.count()).select_from(Job))
    failing[0] = False
    google_sync.run(wid, sid)
    assert pages == [None, 'page-two', 'page-two']
    with db.session() as s:
        assert s.get(SyncStream, sid).cursor == 'after-burst'
        assert s.get(SyncStream, sid).progress == {}
        assert s.scalar(select(func.count()).select_from(Source)) == 702
        # Only the two newly applied batches schedule work; replaying the first is a no-op.
        assert s.scalar(select(func.count()).select_from(Job)) - jobs_before == 2


def long_source(wid):
    marker = 'The booking confirmation code is Zebra712.'
    with db.session() as s:
        ws = Scoped(s, wid)
        src = ingest.upsert(ws, ingest.SourceRecord(provider='gmail', provider_account='synthetic',
            provider_object_id='long', conversation_id='long', kind='email',
            body='Opening background. ' * 400 + marker), set(), ingest.IngestResult())
        sid = src.id
    memory.rebuild_conversation(wid, 'gmail', 'long')
    return sid, marker


def test_long_email_uses_matching_segments_for_answers_and_extraction(browser, monkeypatch):
    browser.login('alex')
    wid = workspace_of('alex@example.test')
    sid, marker = long_source(wid)
    with db.session() as s:
        ws = Scoped(s, wid)
        chunks = ws.all(ws.q(Chunk).order_by(Chunk.start_at))
        matched = next(c for c in chunks if marker in c.text)
        evidence = retrieval.evidence_for(ws, [matched.id])
        assert any(marker in e.text and e.source_id == sid for e in evidence)
        assert len(retrieval.evidence_for(ws, [c.id for c in chunks])) == len(chunks)
        chunk_ids = [c.id for c in chunks]
    prompts = []

    def capture(*args, **kw):
        prompts.append(kw['prompt'])
        return ai.Generated(data={'claims': []}, input_tokens=1, output_tokens=1, model='fake')

    monkeypatch.setattr(ai, 'generate', capture)
    for cid in chunk_ids:
        memory.extract_chunk(wid, cid)
    assert sum(marker in prompt for prompt in prompts) == 1
    assert len(set(prompts)) == len(prompts), 'Extraction repeated the same source prefix'


def test_legacy_chunks_are_rebuilt_before_retrieval(browser):
    from worker.main import reconcile_ai, drain
    browser.login('alex')
    wid = workspace_of('alex@example.test')
    _, marker = long_source(wid)
    with db.session() as s:
        for chunk in s.scalars(select(Chunk)):
            chunk.pipeline_version = 'v1'
            chunk.chunk_key = memory.h('v1', chunk.id)
        s.execute(update(ChunkSource).values(segment_text=''))
        old_ids = list(s.scalars(select(Chunk.id)))
        assert retrieval.evidence_for(Scoped(s, wid), old_ids) == []
    reconcile_ai()
    drain(kinds=['rebuild_conversation'])
    with db.session() as s:
        chunks = list(s.scalars(select(Chunk)))
        assert chunks and all(c.pipeline_version == PIPELINE_VERSION for c in chunks)
        assert not set(old_ids) & {c.id for c in chunks}
        assert any(marker in e.text for e in retrieval.evidence_for(Scoped(s, wid), [c.id for c in chunks]))


def test_restore_bootstrap_sql_runs_outside_a_transaction(database):
    suffix = uuid.uuid4().hex[:10]
    owner, role, name = 'review_owner_' + suffix, 'review_role_' + suffix, 'review_db_' + suffix
    admin = create_engine(database['admin_url'].replace('postgresql://', 'postgresql+psycopg://', 1),
                          isolation_level='AUTOCOMMIT')
    script = Path(__file__).resolve().parents[1] / 'deploy' / 'restore-init.sql'
    try:
        with script.open('rb') as sql:
            r = subprocess.run([pg_bin('psql'), '-d', libpq(database['admin_url']), '-v', 'ON_ERROR_STOP=1',
                '-v', f'restore_owner={owner}', '-v', f'restore_role={role}', '-v', f'restore_db={name}'],
                stdin=sql, capture_output=True)
        assert r.returncode == 0, r.stderr.decode()
    finally:
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')
            c.exec_driver_sql(f'DROP ROLE IF EXISTS {owner}')
            c.exec_driver_sql(f'DROP ROLE IF EXISTS {role}')
        admin.dispose()


def test_review_migration_upgrades_existing_data_and_matches_models(browser, database):
    from alembic import command
    from alembic.config import Config
    browser.login('alex')
    wid = workspace_of('alex@example.test')
    sid, _ = long_source(wid)
    cfg = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    cfg.attributes['database_url'] = database['runtime_url']
    try:
        command.downgrade(cfg, '0001')
    finally:
        command.upgrade(cfg, 'head')
    command.check(cfg)
    with db.session() as s:
        assert s.get(Source, sid).body
        assert list(s.scalars(select(ChunkSource.segment_text)))
        assert all(value == '' for value in s.scalars(select(ChunkSource.segment_text)))


def test_connector_upgrade_and_rollback_preserve_session(tmp_path):
    rows = [{'workspace': A, 'desired': 'running', 'observed': 'pending', 'key': KEY_A}]
    p, reports, _ = provisioner(tmp_path, rows)
    p.reconcile()
    marker = tmp_path / A / 'session' / 'auth.marker'
    marker.write_text('synthetic persistent session')
    for revision in ('newrevision', 'abc123'):
        p.fetch = lambda revision=revision: {'connectors': rows, 'image': 'moresocial-connector:' + revision,
                                           'complete': True, 'workspace_count': 1}
        p.reconcile()
        assert p.docker.images['ms-wa-' + A] == 'sha256:moresocial-connector:' + revision
        assert marker.read_text() == 'synthetic persistent session'
    assert len([c for c in p.docker.calls if c[0] == 'run']) == 3
    p.reconcile()
    assert len([c for c in p.docker.calls if c[0] == 'run']) == 3  # no-op on same image
    assert ('stop', '--time', '30', 'ms-wa-' + A) in p.docker.calls


def test_unavailable_connector_upgrade_keeps_working_container(tmp_path):
    p, _, _ = provisioner(tmp_path, [{'workspace': A, 'desired': 'running', 'observed': 'ready', 'key': KEY_A}])
    p.reconcile()
    original = p.docker.run

    def unavailable(*args, **kw):
        if args[:2] == ('image', 'inspect'):
            raise subprocess.CalledProcessError(1, args)
        return original(*args, **kw)

    p.docker.run = unavailable
    p.reconcile()
    assert p.docker.containers['ms-wa-' + A] == 'running'
    assert not any(c[0] in ('stop', 'rm') for c in p.docker.calls)
