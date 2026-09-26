"""Structured diagnostics: the shared event contract, secret canaries, context isolation,
request/job correlation and failure-tolerant writing. Synthetic providers only."""
import asyncio
import json
import os
import threading
import time

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app import ai, db, diagnostics, jobs, security
from app.models import Job
from conftest import drain, workspace_of
from test_whatsapp import internal, key_of, link, packet  # noqa: F401  (fixture)

HEX32 = r'^[0-9a-f]{32}$'


def events(err: str, name: str | None = None) -> list[dict]:
    rows = [json.loads(line) for line in err.splitlines() if line.startswith('{')]
    return [r for r in rows if name is None or r['event'] == name]


@pytest.fixture(autouse=True)
def fresh_buckets():
    diagnostics._buckets.clear()
    yield
    diagnostics._buckets.clear()


def test_event_contract_and_secret_canaries(capsys, monkeypatch):
    monkeypatch.setenv('MORESOCIAL_RELEASE', 'e373a49')
    monkeypatch.setenv('MORESOCIAL_SERVICE', 'worker')
    canaries = {
        'prompt': 'CANARY prompt text', 'message': 'CANARY body', 'authorization': 'Bearer CANARY',
        'cookie': 'moresocial_session=CANARY', 'url': 'https://x.test/cb?code=CANARY', 'embedding': [0.123456, 0.654321],
        'body': '{"CANARY": 1}', 'key': 'sk-or-v1-CANARY0000',
        # allowed field names carrying disallowed values
        'code': 'sk-or-v1-CANARYCANARY', 'reason': 'Bearer', 'outcome': 'has spaces CANARY', 'route': 'https://CANARY',
        'requested_model': 'model with CANARY spaces', 'provider_request_id': 'sk-CANARY', 'workspace_id': 'CANARY',
        'job_id': 'CANARY', 'request_id': 'CANARY', 'count': 'CANARY', 'status': True, 'kind': 'a' * 81,
        'stage': 'ya29.CANARYTOKEN', 'category': 'x' * 45,
    }
    try:
        raise RuntimeError('CANARY exception message with a secret sk-or-v1-CANARY')
    except RuntimeError as exc:
        ref = security.emit('canary_probe', exc, **canaries)
    err = capsys.readouterr().err
    assert 'CANARY' not in err and '0.123456' not in err
    row = events(err, 'canary_probe')[0]
    assert set(row) == {'schema', 'ts', 'level', 'service', 'release', 'event', 'reference', 'error_type', 'frames'}
    assert (row['schema'], row['level'], row['service'], row['release'], row['reference']) == (1, 'error', 'worker', 'e373a49', ref)
    assert row['error_type'] == 'RuntimeError' and row['frames'][-1]['file'] == 'test_diagnostics.py'
    assert set(row['frames'][-1]) == {'file', 'function', 'line'}


def test_valid_fields_survive_with_types(capsys):
    security.emit('typed_probe', requested_model='voyageai/voyage-4-20260727', provider_request_id='gen-17-AbC',
                  space='v2:openrouter:voyageai/voyage-4:1024:v1:plain-text:document-query', route='/people/{pid}',
                  cost_usd=0.00012, count=3, ok=False, workspace_id='6f3a1c6e-7d7c-4c19-9a8e-2b0d7d9c0f11')
    row = events(capsys.readouterr().err, 'typed_probe')[0]
    assert row['requested_model'] == 'voyageai/voyage-4-20260727' and row['route'] == '/people/{pid}'
    assert row['cost_usd'] == 0.00012 and row['count'] == 3 and row['ok'] is False
    assert row['provider_request_id'] == 'gen-17-AbC' and row['workspace_id'].startswith('6f3a1c6e')


def test_context_is_isolated_across_threads_and_tasks(capsys):
    def worker(n):
        with diagnostics.bind(request_id=f'{n:032x}'):
            time.sleep(0.01 * (5 - n))
            security.emit('thread_probe', count=n)
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    async def task(n):
        with diagnostics.operation():
            op = diagnostics.current()['operation_id']
            await asyncio.sleep(0.01 * (5 - n))
            security.emit('task_probe', count=n)
            return op

    async def run():
        with diagnostics.bind(request_id='f' * 32):
            return await asyncio.gather(*(task(n) for n in range(5)))
    ops = asyncio.run(run())
    err = capsys.readouterr().err
    for row in events(err, 'thread_probe'):
        assert row['request_id'] == f"{row['count']:032x}"
    tasks = events(err, 'task_probe')
    assert len({r['operation_id'] for r in tasks}) == 5
    for row in tasks:
        assert row['operation_id'] == ops[row['count']] and row['parent_operation_id'] == 'f' * 32
    assert diagnostics.current() == {}


def test_write_failures_never_raise_and_are_counted(tmp_path, monkeypatch, capsys):
    class Broken:
        def write(self, _):
            raise OSError('stderr closed')
    missing = tmp_path / 'missing' / 'store'
    monkeypatch.setenv('DIAGNOSTICS_DIR', str(missing))
    monkeypatch.setattr(diagnostics.SINK, 'dropped', 0)
    monkeypatch.setattr(diagnostics.SINK, 'reported_dropped', 0)
    real = diagnostics.sys.stderr
    monkeypatch.setattr(diagnostics.sys, 'stderr', Broken())
    assert len(security.emit('lost_probe')) == 16
    assert diagnostics.SINK.dropped == 1
    monkeypatch.setattr(diagnostics.sys, 'stderr', real)
    missing.mkdir(parents=True)
    security.emit('recovered_probe')
    row = events(capsys.readouterr().err, 'recovered_probe')[0]
    assert row['dropped'] == 1
    stored = [json.loads(line) for f in missing.iterdir() for line in f.read_text().splitlines()]
    assert [r['event'] for r in stored] == ['recovered_probe']
    assert oct(next(missing.iterdir()).stat().st_mode & 0o777) == '0o600'


def test_repeated_events_are_suppressed_then_counted(capsys, monkeypatch):
    for _ in range(diagnostics.RATE_LIMIT + 10):
        security.emit('flood_probe', code='same')
    assert len(events(capsys.readouterr().err, 'flood_probe')) == diagnostics.RATE_LIMIT
    for bucket in diagnostics._buckets.values():
        bucket[0] -= diagnostics.RATE_WINDOW + 1
    security.emit('flood_probe', code='same')
    assert events(capsys.readouterr().err, 'flood_probe')[0]['suppressed'] == 10
    for _ in range(diagnostics.RATE_LIMIT + 5):
        security.emit('audited_probe', audit=True)
    assert len(events(capsys.readouterr().err, 'audited_probe')) == diagnostics.RATE_LIMIT + 5  # audit is never dropped


def test_scoped_debug_expires_and_never_relaxes_fields(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('DIAGNOSTICS_DIR', str(tmp_path))
    monkeypatch.setattr(diagnostics, '_debug_cache', (0.0, None))
    security.emit('job_claimed', level='debug', prompt='CANARY')
    assert events(capsys.readouterr().err) == []
    (tmp_path / 'debug.json').write_text(json.dumps({'until': time.time() + 60, 'services': ['web'], 'events': ['job_']}))
    monkeypatch.setattr(diagnostics, '_debug_cache', (0.0, None))
    security.emit('job_claimed', level='debug', prompt='CANARY', kind='embed_chunk')
    security.emit('sync_page', level='debug')
    err = capsys.readouterr().err
    assert [r['event'] for r in events(err)] == ['job_claimed'] and 'CANARY' not in err
    (tmp_path / 'debug.json').write_text(json.dumps({'until': time.time() - 1}))
    monkeypatch.setattr(diagnostics, '_debug_cache', (0.0, None))
    security.emit('job_claimed', level='debug')
    assert events(capsys.readouterr().err) == []


def test_public_requests_get_trusted_ids_and_route_templates(browser, capsys, monkeypatch):
    monkeypatch.setenv('HTTP_LOG_SAMPLE', '1')
    browser.login('alex')
    capsys.readouterr()
    r = browser.get('/people/00000000-0000-0000-0000-000000000000', headers={'X-Request-ID': 'a' * 32})
    rid = r.headers['X-Request-ID']
    assert r.status_code == 404 and rid != 'a' * 32 and len(rid) == 32  # client IDs are not trusted
    assert browser.get('/health').headers['X-Request-ID']
    err = capsys.readouterr().err
    requests = events(err, 'http_request')
    assert [(e['route'], e['status'], e['request_id']) for e in requests] == [('/people/{pid}', 404, rid)]
    assert 'health' not in err and '00000000-0000' not in err


def test_unexpected_errors_show_the_request_reference(app, monkeypatch, capsys):
    from app import gatherings
    client = TestClient(app, base_url='https://moresocial.test', raise_server_exceptions=False)
    client.headers['Origin'] = 'https://moresocial.test'
    from conftest import Browser
    b = Browser(app)
    b.login('alex')
    client.cookies = b.c.cookies

    def boom(ws):
        raise RuntimeError('CANARY private detail')
    monkeypatch.setattr(gatherings, 'next_steps', boom)
    capsys.readouterr()
    r = client.get('/moresocial/home')
    rid = r.headers['X-Request-ID']
    assert r.status_code == 500 and f'<code>{rid}</code>' in r.text and 'CANARY' not in r.text
    err = capsys.readouterr().err
    failed = events(err, 'request_failed')[0]
    assert failed['request_id'] == rid and failed['route'] == '/home' and failed['error_type'] == 'RuntimeError'
    assert any(f['function'] == 'boom' for f in failed['frames']) and 'CANARY' not in err
    assert events(err, 'http_request')[0]['status'] == 500


def test_connector_request_correlates_through_jobs(make_browser, internal, capsys):
    a = make_browser()
    a.login('alex')
    link(a)
    drain()
    capsys.readouterr()
    rid = 'c' * 32
    r = internal.post('/internal/connector/ingest', json=packet(), headers={'Authorization': 'Bearer ' + key_of('alex@example.test'),
                                                                             'X-Request-ID': rid})
    assert r.status_code == 200 and r.headers['X-Request-ID'] == rid  # the internal API adopts the connector's ID
    with db.session() as s:
        traced = s.scalars(select(Job).where(Job.workspace_id == workspace_of('alex@example.test'), Job.status == 'queued')).all()
        from app.jobs import utcnow
        for job in traced:
            job.run_after = utcnow()  # Advance the event recognition settling period.
    assert traced and all(j.payload['_trace'] == {'op': rid} for j in traced)
    drain()
    completed = events(capsys.readouterr().err, 'job_completed')
    direct = [e for e in completed if e.get('parent_operation_id') == rid]
    assert {e['job_id'] for e in direct} == {str(j.id) for j in traced}
    assert all(e['operation_id'] != rid and 'queue_age_ms' in e and 'duration_ms' in e for e in direct)
    # Follow-up jobs enqueued by those jobs chain to the job's own operation.
    ops = {e['operation_id'] for e in direct}
    assert any(e.get('parent_operation_id') in ops for e in completed if e not in direct)


def test_job_retry_terminal_and_deferral_events(browser, capsys, monkeypatch):
    browser.login('alex')
    drain()
    wid = workspace_of('alex@example.test')
    with db.session() as s:
        jobs.enqueue(s, wid, 'embed_chunk', 'probe:retry', {'chunk_id': '00000000-0000-0000-0000-000000000001'}, max_attempts=2)
    from worker import main as worker
    calls = []

    def flaky(job):
        calls.append(job.attempts)
        raise RuntimeError('CANARY')
    monkeypatch.setattr(worker, 'handle', flaky)
    capsys.readouterr()
    assert worker.run_one()
    with db.session() as s:
        s.execute(Job.__table__.update().values(run_after=jobs.utcnow()))
    assert worker.run_one()
    err = capsys.readouterr().err
    assert [e['event'] for e in events(err) if e['event'].startswith('job_') and e['event'] != 'job_failed'] == [
        'job_retry_scheduled', 'job_failed_terminal']
    assert events(err, 'job_failed_terminal')[0]['final'] is True and 'CANARY' not in err
    assert len(events(err, 'job_failed')) == 2 and events(err, 'job_failed')[0]['frames']

    with db.session() as s:
        jobs.enqueue(s, wid, 'embed_chunk', 'probe:budget', {'chunk_id': '00000000-0000-0000-0000-000000000002'})

    def budget(job):
        raise ai.BudgetExhausted('global')
    monkeypatch.setattr(worker, 'handle', budget)
    assert worker.run_one()
    deferred = events(capsys.readouterr().err, 'job_deferred')[0]
    assert (deferred['code'], deferred['scope'], deferred['kind']) == ('budget_exhausted', 'global', 'embed_chunk')


def test_queue_stats_reports_lag_and_failures(browser, capsys):
    browser.login('alex')
    wid = workspace_of('alex@example.test')
    with db.session() as s:
        jobs.enqueue(s, wid, 'embed_chunk', 'probe:lag', {'chunk_id': '00000000-0000-0000-0000-000000000003'},
                     run_after=jobs.utcnow().replace(year=2020))
    from worker import main as worker
    worker.queue_stats()
    row = events(capsys.readouterr().err, 'queue_stats')[0]
    assert row['queued'] >= 1 and row['oldest_queue_age_ms'] > 86_400_000 and 'stalled_streams' in row


def test_google_sync_emits_start_and_completion_counts(browser, capsys):
    from worker import main as worker
    capsys.readouterr()
    browser.login('alex')
    worker.schedule()
    drain()
    err = capsys.readouterr().err
    done = [e for e in events(err, 'sync_completed') if e.get('stream') == 'gmail']
    assert done and done[0]['outcome'].startswith('initial') and done[0]['items'] > 0 and done[0]['pages'] >= 1
    assert done[0]['job_id'] and events(err, 'sync_started')
