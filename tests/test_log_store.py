"""Persistent diagnostic store (deploy/logs.py): rotation, search across container
recreation, retention and disk cap, alert rules with deduplication, collector outages
and scoped DEBUG. No database or network needed."""
from datetime import datetime, timedelta, timezone
import importlib
import json
import os
from pathlib import Path
import sys
import time

import pytest

from app import diagnostics

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy'))
logs = importlib.import_module('logs')


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / 'logs'
    for d in ('app', 'host', 'connectors'):
        (root / d).mkdir(parents=True)
    monkeypatch.setenv('DIAGNOSTICS_DIR', str(root / 'app'))
    monkeypatch.setattr(diagnostics.SINK, 'day', None)
    monkeypatch.setattr(diagnostics, '_buckets', {})
    return root


def write(path: Path, *rows: dict, age_days: float = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as fh:
        for row in rows:
            fh.write(json.dumps(row) + '\n')
    if age_days:
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
    return path


def ts(minutes_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat().replace('+00:00', 'Z')


def test_segments_rotate_by_size(store, monkeypatch):
    monkeypatch.setattr(diagnostics.Sink, 'SEGMENT_BYTES', 400)
    monkeypatch.setenv('MORESOCIAL_SERVICE', 'worker')
    for n in range(12):
        diagnostics.emit('rotation_probe', count=n)
    segments = sorted(p.name for p in (store / 'app').iterdir())
    assert len(segments) >= 3 and all(name.startswith('worker-') and name.endswith('.jsonl') for name in segments)
    assert all((store / 'app' / name).stat().st_size < 400 + 300 for name in segments)
    counts = [json.loads(l)['count'] for name in segments for l in (store / 'app' / name).read_text().splitlines()]
    assert sorted(counts) == list(range(12))


def test_reference_stays_searchable_after_container_recreation(store, monkeypatch, capsys):
    rid = 'd' * 32
    monkeypatch.setenv('MORESOCIAL_SERVICE', 'web')
    monkeypatch.setattr(diagnostics.SINK, 'host', 'oldcontainer1')
    with diagnostics.bind(request_id=rid):
        diagnostics.emit('admin_openrouter_test_failed', stage='query_probe', code='embedding_model')
        with diagnostics.operation():
            diagnostics.emit('provider_call', outcome='failed')
    # The container is replaced: a new hostname writes new files; the old ones remain.
    monkeypatch.setattr(diagnostics.SINK, 'host', 'newcontainer2')
    monkeypatch.setenv('MORESOCIAL_SERVICE', 'worker')
    with diagnostics.bind(job_id='6f3a1c6e-7d7c-4c19-9a8e-2b0d7d9c0f11', operation_id='e' * 32, parent_operation_id=rid):
        diagnostics.emit('job_completed', outcome='done')
    write(store / 'connectors' / ('a' * 32) / 'events.jsonl',
          {'schema': 1, 'ts': ts(), 'service': 'connector', 'event': 'request_failed', 'request_id': rid})
    write(store / 'host' / 'provisioner-20260926.jsonl', {'schema': 1, 'ts': ts(), 'service': 'provisioner', 'event': 'unrelated'})
    rows = logs.find(rid, store)
    assert [r['event'] for r in rows] == ['admin_openrouter_test_failed', 'provider_call', 'job_completed', 'request_failed']
    assert {r['service'] for r in rows} == {'web', 'worker', 'connector'}
    capsys.readouterr()
    logs.main(['--root', str(store), 'find', rid])
    assert capsys.readouterr().out.count('\n') == 4


def test_retention_by_age_and_disk_cap(store):
    old = write(store / 'app' / 'web-20260901-x-000.jsonl', {'event': 'old'}, age_days=20)
    stale_dir = store / 'connectors' / ('b' * 32)
    write(stale_dir / 'events.jsonl.1', {'event': 'old'}, age_days=20)
    os.utime(stale_dir, (time.time() - 20 * 86400,) * 2)
    mid = write(store / 'app' / 'web-20260920-x-000.jsonl', *({'event': 'x' * 200} for _ in range(10)), age_days=5)
    new = write(store / 'app' / 'web-20260926-x-000.jsonl', *({'event': 'y' * 200} for _ in range(10)))
    result = logs.prune(store, days=14, max_bytes=new.stat().st_size + 500)
    assert not old.exists() and not mid.exists() and new.exists()  # age first, then oldest-first to fit the cap
    assert stale_dir.is_dir() and not any(stale_dir.iterdir())  # may be a live bind mount: kept, emptied
    assert result['deleted_files'] == 3 and result['bytes'] <= new.stat().st_size + 500
    retention = [json.loads(l) for f in (store / 'host').glob('host-*.jsonl') for l in f.read_text().splitlines()]
    assert retention[-1]['event'] == 'log_retention' and retention[-1]['deleted_files'] == 3


def healthy(store, minutes_ago=5):
    write(store / 'app' / 'worker-20260926-x-000.jsonl',
          {'ts': ts(minutes_ago), 'level': 'info', 'service': 'worker', 'event': 'queue_stats', 'oldest_queue_age_ms': 0,
           'stalled_streams': 0})


def test_alerts_fire_deduplicate_and_resolve(store):
    healthy(store)
    ready = lambda: (True, 200)  # noqa: E731
    assert logs.check(store, probe=ready) == []
    write(store / 'app' / 'worker-20260926-x-000.jsonl',
          {'ts': ts(1), 'level': 'error', 'service': 'worker', 'event': 'job_failed_terminal'},
          {'ts': ts(1), 'level': 'info', 'service': 'worker', 'event': 'queue_stats', 'oldest_queue_age_ms': 3_600_000,
           'stalled_streams': 2},
          *({'ts': ts(2), 'level': 'error', 'service': 'web', 'event': 'provider_call', 'outcome': 'failed'} for _ in range(6)))
    fired = logs.check(store, probe=lambda: (False, 503))
    assert {f['rule'] for f in fired} == {'terminal_jobs', 'queue_lag', 'sync_stalls', 'provider_failures', 'readiness'}
    assert not any(f['suppressed'] for f in fired)
    again = logs.check(store, probe=lambda: (False, 503))
    assert all(f['suppressed'] for f in again)
    host = [json.loads(l) for f in (store / 'host').glob('host-*.jsonl') for l in f.read_text().splitlines()]
    assert sum(1 for r in host if r['event'] == 'alert') == 5  # re-alerting waits six hours
    assert any(r['event'] == 'readiness_probe' and r['ok'] is False and r['status'] == 503 for r in host)
    for f in (store / 'app').iterdir():
        f.unlink()
    healthy(store)
    assert logs.check(store, probe=ready) == []
    host = [json.loads(l) for f in (store / 'host').glob('host-*.jsonl') for l in f.read_text().splitlines()]
    assert {r['rule'] for r in host if r['event'] == 'alert_resolved'} == {
        'terminal_jobs', 'queue_lag', 'sync_stalls', 'provider_failures', 'readiness'}
    assert logs.main(['--root', str(store), 'check']) in (0, 2)


def test_backup_rules_wait_for_a_full_cycle(store):
    healthy(store)
    ready = lambda: (True, 200)  # noqa: E731
    assert logs.check(store, probe=ready) == []  # just installed: no backup expected yet
    (store / 'host' / 'installed').write_text(str(time.time() - 30 * 3600))
    assert [f['rule'] for f in logs.check(store, probe=ready)] == ['backup_failures']
    logs.main(['--root', str(store), 'emit', 'backup_completed', '--service', 'backup', '--field', 'bytes=1234'])
    assert logs.check(store, probe=ready) == []
    logs.main(['--root', str(store), 'emit', 'backup_failed', '--level', 'error', '--service', 'backup',
               '--field', 'stage=encrypt', '--field', 'note=has spaces so dropped'])
    assert [f['rule'] for f in logs.check(store, probe=ready)] == ['backup_failures']
    failed = [json.loads(l) for l in next((store / 'host').glob('backup-*.jsonl')).read_text().splitlines()][-1]
    assert failed['stage'] == 'encrypt' and 'note' not in failed and failed['level'] == 'error'


def test_collector_outage_is_detected_and_never_breaks_the_app(store, monkeypatch, capsys):
    (store / 'app').chmod(0o500)
    try:
        monkeypatch.setattr(diagnostics.SINK, 'dropped', 0)
        monkeypatch.setattr(diagnostics.SINK, 'reported_dropped', 0)
        assert len(diagnostics.emit('queue_stats', queued=0)) == 16  # the app keeps going
        if os.geteuid() != 0:
            assert diagnostics.SINK.dropped == 1
    finally:
        (store / 'app').chmod(0o700)
    assert 'queue_stats' in capsys.readouterr().err  # stderr still carries it
    fired = logs.check(store, probe=lambda: (True, 200))
    if os.geteuid() != 0:
        assert 'collector_silent' in {f['rule'] for f in fired}
    diagnostics.emit('queue_stats', queued=0)
    stored = [json.loads(l) for f in (store / 'app').iterdir() for l in f.read_text().splitlines()]
    assert stored[-1].get('dropped', 0) >= (1 if os.geteuid() != 0 else 0)


def test_debug_command_scopes_the_running_app(store, monkeypatch, capsys):
    monkeypatch.setenv('MORESOCIAL_SERVICE', 'web')
    monkeypatch.setattr(diagnostics, '_debug_cache', (0.0, None))
    diagnostics.emit('job_claimed', level='debug')
    logs.main(['--root', str(store), 'debug', '--service', 'web', '--events', 'job_', '--minutes', '999'])
    scope = json.loads((store / 'app' / 'debug.json').read_text())
    assert scope['until'] - time.time() <= 120 * 60 + 1  # capped at two hours
    monkeypatch.setattr(diagnostics, '_debug_cache', (0.0, None))
    diagnostics.emit('job_claimed', level='debug', prompt='CANARY')
    err = capsys.readouterr().err
    assert [json.loads(l)['event'] for l in err.splitlines() if l.startswith('{"schema"') and '"service":"web"' in l] == ['job_claimed']
    assert 'CANARY' not in err


def test_provisioner_mounts_connector_store_and_replaces_on_spec_change(tmp_path, monkeypatch):
    from test_whatsapp import A, KEY_A, provisioner
    rows = [{'workspace': A, 'desired': 'running', 'observed': 'ready', 'key': KEY_A}]
    p, _, prov = provisioner(tmp_path / 'wa', rows)
    p.reconcile()  # started before the log store existed: no diagnostics mount
    run = [c for c in p.docker.calls if c[0] == 'run'][-1]
    assert not any(x.endswith(':/diagnostics') for x in run)
    monkeypatch.setattr(prov, 'LOGS', tmp_path / 'logs')
    (tmp_path / 'logs' / 'host').mkdir(parents=True)
    p.reconcile()  # spec changed (new mount): replaced once, session kept
    runs = [c for c in p.docker.calls if c[0] == 'run']
    assert len(runs) == 2 and f'{tmp_path / "logs" / "connectors" / A}:/diagnostics' in runs[-1]
    assert 'DIAGNOSTICS_DIR=/diagnostics' in runs[-1] and runs[-1][-1] == 'moresocial-connector:abc123'
    assert (tmp_path / 'logs' / 'connectors' / A).stat().st_mode & 0o777 == 0o700
    p.reconcile()
    assert len([c for c in p.docker.calls if c[0] == 'run']) == 2  # stable spec: no churn
    events = [json.loads(l) for f in (tmp_path / 'logs' / 'host').glob('provisioner-*.jsonl') for l in f.read_text().splitlines()]
    replaced = [e for e in events if e['event'] == 'connector_replaced']
    assert [e['code'] for e in replaced] == ['spec'] and replaced[0]['workspace_id'].replace('-', '') == A
