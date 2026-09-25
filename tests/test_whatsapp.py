"""M3: per-workspace WhatsApp connectors, scoped keys, idempotent ingestion, provisioner."""
import dataclasses
import json
from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config, db, security, synthetic_data, whatsapp
from app.internal import create_internal_app
from app.models import Person, PersonIdentifier, Source, WhatsAppConnector
from conftest import workspace_of

OPERATOR = {'Authorization': 'Bearer synthetic-operator-key-for-tests-only'}


@pytest.fixture
def internal(database):
    return TestClient(create_internal_app())


def link(browser):
    assert browser.post('/connections/whatsapp/connect', {'csrf': browser.csrf('/connections')}).status_code == 303


def key_of(email):
    with db.session() as s:
        row = s.scalars(select(WhatsAppConnector).where(WhatsAppConnector.workspace_id == workspace_of(email))).one()
        return security.decrypt(row.key_enc)


def wa_sources(email):
    with db.session() as s:
        return list(s.scalars(select(Source).where(Source.workspace_id == workspace_of(email), Source.provider == 'whatsapp')))


def packet(n=0, body='Lasergame sounds fun!', chat='31600000001@c.us', **extra):
    ts = int(time.time()) - 3600
    return {'chat': {'id': chat, 'name': 'Noor', 'is_group': chat.endswith('@g.us')},
            'messages': [{'id': f'false_{chat}_X{n}', 'ts': ts, 'sender': '31600000001@c.us', 'from_me': False,
                          'kind': 'chat', 'body': body}], 'contacts': [], **extra}


def test_management_api_requires_operator_key(make_browser, internal):
    a = make_browser()
    a.login('alex')
    link(a)
    assert internal.get('/internal/manage/connectors').status_code == 401
    assert internal.get('/internal/manage/connectors', headers={'Authorization': 'Bearer ' + key_of('alex@example.test')}).status_code == 401
    data = internal.get('/internal/manage/connectors', headers=OPERATOR).json()
    assert data['complete'] and data['workspace_count'] == 1
    row = data['connectors'][0]
    assert row['workspace'] == workspace_of('alex@example.test').hex and row['desired'] == 'running' and row['key']


def test_connector_keys_are_scoped_to_their_workspace(make_browser, internal):
    a, b = make_browser(), make_browser()
    a.login('alex')
    b.login('blake')
    link(a)
    link(b)
    ka, kb = key_of('alex@example.test'), key_of('blake@example.test')
    assert ka != kb
    # payload claims to be Blake's workspace: ignored, the key decides
    body = packet(workspace=workspace_of('blake@example.test').hex, workspace_id=str(workspace_of('blake@example.test')))
    r = internal.post('/internal/connector/ingest', json=body, headers={'Authorization': 'Bearer ' + ka})
    assert r.status_code == 200 and r.json()['created'] == 1
    assert len(wa_sources('alex@example.test')) == 1 and wa_sources('blake@example.test') == []
    assert internal.post('/internal/connector/ingest', json=packet(), headers={'Authorization': 'Bearer nope'}).status_code == 401
    assert internal.post('/internal/connector/ingest', json=packet(), headers=OPERATOR).status_code == 401


def test_qr_only_for_owner_and_not_cached(make_browser, monkeypatch):
    a, b = make_browser(), make_browser()
    a.login('alex')
    b.login('blake')
    link(a)
    r = a.get('/api/whatsapp/status.json')
    assert r.status_code == 200 and r.headers['cache-control'] == 'no-store'
    assert r.json()['state'] == 'pairing' and r.json()['qr'].startswith('data:image/')
    other = b.get('/api/whatsapp/status.json').json()
    assert other['state'] == 'not_connected' and 'qr' not in other
    anon = make_browser()
    assert anon.get('/api/whatsapp/status.json', headers={'Accept': 'application/json'}).status_code == 401
    # real (non-synthetic) routing only ever targets the signed-in workspace's own connector
    seen = []
    monkeypatch.setattr(config, 'get', lambda s=config.get(): dataclasses.replace(s, synthetic_providers=False))

    class Resp:
        is_success = True
        def json(self):
            return {'state': 'pairing', 'qr': 'data:image/png;base64,AAAA'}

    class Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def request(self, method, url, headers):
            seen.append((url, headers['Authorization']))
            return Resp()
    monkeypatch.setattr(whatsapp.httpx, 'Client', Client)
    a.get('/api/whatsapp/status.json')
    assert seen == [(f"http://ms-wa-{workspace_of('alex@example.test').hex}:8790/status", 'Bearer ' + key_of('alex@example.test'))]


def test_pairing_then_ready_ingests_fixture_history(browser):
    browser.login('alex')
    link(browser)
    assert browser.get('/api/whatsapp/status.json').json()['state'] == 'pairing'
    ready = browser.get('/api/whatsapp/status.json').json()
    assert ready['state'] == 'ready' and 'qr' not in ready or ready.get('qr') is None
    assert len(wa_sources('alex@example.test')) == 4
    browser.get('/api/whatsapp/status.json')
    assert len(wa_sources('alex@example.test')) == 4


def test_repeated_history_and_live_packets_do_not_duplicate(browser, internal):
    browser.login('alex')
    link(browser)
    h = {'Authorization': 'Bearer ' + key_of('alex@example.test')}
    for _ in range(3):
        internal.post('/internal/connector/ingest', json=packet(1), headers=h)
    assert len(wa_sources('alex@example.test')) == 1
    edited = internal.post('/internal/connector/ingest', json=packet(1, body='Edited text'), headers=h).json()
    assert edited['updated'] == 1 and wa_sources('alex@example.test')[0].body == 'Edited text'
    revoked = packet(1)
    revoked['messages'][0]['kind'] = 'revoked'
    internal.post('/internal/connector/ingest', json=revoked, headers=h)
    src = wa_sources('alex@example.test')[0]
    assert src.deleted_at is not None and src.body == ''


def test_bounds_and_malformed_packets(browser, internal):
    browser.login('alex')
    link(browser)
    h = {'Authorization': 'Bearer ' + key_of('alex@example.test')}
    old = packet(2)
    old['messages'][0]['ts'] = int(time.time()) - 100 * 86400
    assert internal.post('/internal/connector/ingest', json=old, headers=h).json()['created'] == 0
    assert internal.post('/internal/connector/ingest', json={'chat': {'id': '../etc'}, 'messages': []}, headers=h).status_code == 400
    too_many = packet(3)
    too_many['messages'] = too_many['messages'] * 301
    assert internal.post('/internal/connector/ingest', json=too_many, headers=h).status_code == 400


def test_group_authors_are_separate_and_names_never_merge(browser):
    browser.login('alex')
    link(browser)
    browser.get('/api/whatsapp/status.json')
    browser.get('/api/whatsapp/status.json')
    with db.session() as s:
        wid = workspace_of('alex@example.test')
        group = s.scalars(select(Source).where(Source.workspace_id == wid, Source.conversation_id == '120363000000000001@g.us')).all()
        assert {g.author for g in group} == {'31600000002@c.us', '31600000003@c.us'}
        idents = s.scalars(select(PersonIdentifier).where(PersonIdentifier.workspace_id == wid)).all()
        assert not any(i.value.endswith('@g.us') for i in idents)
        # "Sam" on WhatsApp stays separate from both Sams on email; nothing merges by name
        from conftest import drain
        from worker.main import schedule
        schedule(); drain()
        sams = s.scalars(select(Person).where(Person.workspace_id == wid, Person.display_name.ilike('sam%'))).all()
        assert len(sams) == 3
        # own number never becomes a contact
        assert not any(i.value == synthetic_data.USERS['alex']['whatsapp'] for i in idents)


def test_excluded_chat_is_not_reimported(browser, internal):
    browser.login('alex')
    link(browser)
    h = {'Authorization': 'Bearer ' + key_of('alex@example.test')}
    internal.post('/internal/connector/ingest', json=packet(4), headers=h)
    src = wa_sources('alex@example.test')[0]
    assert browser.post(f'/sources/{src.id}/exclude', {'scope': 'conversation', 'csrf': browser.csrf()}).status_code == 303
    assert wa_sources('alex@example.test') == []
    r = internal.post('/internal/connector/ingest', json=packet(5), headers=h).json()
    assert r['created'] == 0 and r['skipped'] == 1


def test_connector_cap(make_browser, monkeypatch):
    original = config.get()
    config.override(dataclasses.replace(original, connector_cap=1))
    try:
        a, b = make_browser(), make_browser()
        a.login('alex')
        b.login('blake')
        link(a)
        link(b)
        with db.session() as s:
            rows = {r.workspace_id: r for r in s.scalars(select(WhatsAppConnector))}
        assert rows[workspace_of('alex@example.test')].desired == 'running'
        assert rows[workspace_of('blake@example.test')].observed == 'capacity'
        assert 'slots are in use' in b.get('/connections').text
    finally:
        config.override(original)


def test_disconnect_and_delete_session_flow(browser, internal):
    browser.login('alex')
    link(browser)
    h = {'Authorization': 'Bearer ' + key_of('alex@example.test')}
    ws = workspace_of('alex@example.test').hex
    browser.post('/connections/whatsapp/disconnect', {'remove': 'yes', 'csrf': browser.csrf('/connections')})
    listing = internal.get('/internal/manage/connectors', headers=OPERATOR).json()['connectors']
    assert listing == [{'workspace': ws, 'desired': 'deleted', 'observed': 'stopped', 'key': None}]
    assert internal.post('/internal/connector/ingest', json=packet(6), headers=h).status_code == 401  # old key is dead
    r = internal.post(f'/internal/manage/connectors/{ws}/observed', json={'state': 'removed'}, headers=OPERATOR)
    assert r.json().get('deleted') is True
    assert internal.post('/internal/manage/connectors/..%2F/observed', json={'state': 'removed'}, headers=OPERATOR).status_code in (400, 404)


# ---------------- provisioner (fake Docker) ----------------

class FakeDocker:
    def __init__(self):
        self.calls = []
        self.containers = {}
        self.images = {}
        self.networks = set()

    def run(self, *args, check=True):
        import subprocess
        self.calls.append(args)
        out, rc = '', 0
        if args[0] == 'ps':
            out = '\n'.join(self.containers)
        elif args[0] == 'inspect':
            name = args[-1]
            if name == 'moresocial-web-1':
                out = json.dumps({n: {} for n in self.networks if n in getattr(self, 'web_nets', set())})
            elif name in self.containers:
                out = self.images.get(name, '') if args[2] == '{{.Image}}' else self.containers[name]
            else:
                rc = 1
        elif args[:2] == ('network', 'inspect'):
            rc = 0 if args[2] in self.networks else 1
        elif args[:2] == ('network', 'create'):
            self.networks.add(args[-1])
        elif args[:2] == ('network', 'rm'):
            self.networks.discard(args[-1])
        elif args[0] == 'run':
            name = args[args.index('--name') + 1]
            self.containers[name] = 'running'
            self.images[name] = 'sha256:' + args[-1]
        elif args[:2] == ('image', 'inspect'):
            out = 'sha256:' + args[-1]
        elif args[0] == 'stop':
            self.containers[args[-1]] = 'exited'
        elif args[0] == 'rm':
            self.containers.pop(args[-1], None)
        elif args[0] == 'start':
            self.containers[args[-1]] = 'running'
        return subprocess.CompletedProcess(args, rc, out, '')


def provisioner(tmp_path, rows, complete=True, count=None):
    import importlib
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy'))
    prov = importlib.import_module('provisioner')
    reports = []
    fetch = rows if callable(rows) else (lambda: {'connectors': rows, 'image': 'moresocial-connector:abc123', 'complete': complete,
                                                 'workspace_count': len(rows) if count is None else count})
    p = prov.Provisioner(docker=FakeDocker(), fetch=fetch, report=lambda ws, st, d=None: reports.append((ws, st)), base=tmp_path)
    return p, reports, prov


A, B = 'a' * 32, 'b' * 32
KEY_A, KEY_B = 'k' * 43, 'j' * 43


def test_provisioner_isolates_mounts_networks_and_keys(tmp_path):
    p, reports, _ = provisioner(tmp_path, [{'workspace': A, 'desired': 'running', 'observed': 'pending', 'key': KEY_A},
                                           {'workspace': B, 'desired': 'running', 'observed': 'pending', 'key': KEY_B}])
    p.reconcile()
    runs = [c for c in p.docker.calls if c[0] == 'run']
    assert len(runs) == 2
    for ws, key, run in ((A, KEY_A, runs[0]), (B, KEY_B, runs[1])):
        mounts = [run[i + 1] for i, x in enumerate(run) if x == '-v']
        assert mounts == [f'{tmp_path / ws}/session:/session', f'{tmp_path / ws}/secret/connector_key:/run/secrets/connector_key:ro']
        assert run[run.index('--network') + 1] == f'ms-wa-{ws}'
        assert '-p' not in run and '--publish' not in run and '--privileged' not in run
        assert not any('docker.sock' in x for x in run)
        assert '--read-only' in run and '--memory' in run and '--pids-limit' in run
        assert (tmp_path / ws / 'secret' / 'connector_key').read_text() == key
        assert (tmp_path / ws / 'secret' / 'connector_key').stat().st_mode & 0o777 == 0o400
        assert (tmp_path / ws).stat().st_mode & 0o777 == 0o700
    assert ('network', 'connect', '--alias', 'web', f'ms-wa-{A}', 'moresocial-web-1') in p.docker.calls
    assert sorted(reports) == [(A, 'starting'), (B, 'starting')]


def test_provisioner_rejects_bad_ids_and_images(tmp_path):
    p, reports, prov = provisioner(tmp_path, [{'workspace': '../../etc', 'desired': 'deleted', 'key': None},
                                              {'workspace': A, 'desired': 'running', 'key': KEY_A}])
    p.fetch = lambda: {'connectors': [{'workspace': '../../etc', 'desired': 'deleted'}, {'workspace': A, 'desired': 'running',
                       'key': KEY_A}], 'image': 'evil; rm -rf /', 'complete': True, 'workspace_count': 1}
    p.reconcile()
    assert not any(c[0] == 'run' for c in p.docker.calls)
    assert (A, 'error') in reports
    with pytest.raises(ValueError):
        prov.workspace_dir('../x')


def test_provisioner_restart_keeps_session_and_stop_keeps_data(tmp_path):
    rows = [{'workspace': A, 'desired': 'running', 'observed': 'ready', 'key': KEY_A}]
    p, reports, _ = provisioner(tmp_path, rows)
    p.reconcile()
    (tmp_path / A / 'session' / 'auth.marker').write_text('session')
    p.docker.containers[f'ms-wa-{A}'] = 'exited'  # container crashed / host rebooted
    p.reconcile()
    assert ('start', f'ms-wa-{A}') in p.docker.calls
    assert (tmp_path / A / 'session' / 'auth.marker').exists()
    rows[0]['desired'] = 'stopped'
    p.reconcile()
    assert f'ms-wa-{A}' not in p.docker.containers and (tmp_path / A / 'session' / 'auth.marker').exists()
    rows[0]['desired'] = 'deleted'
    p.reconcile()
    assert not (tmp_path / A).exists() and (A, 'removed') in reports


def test_provisioner_never_deletes_when_api_fails_or_listing_is_suspicious(tmp_path):
    (tmp_path / A / 'session').mkdir(parents=True)

    def down():
        raise OSError('api down')
    p, _, _ = provisioner(tmp_path, down)
    p.docker.containers[f'ms-wa-{A}'] = 'running'
    for _ in range(5):
        p.reconcile()
    assert (tmp_path / A).exists() and f'ms-wa-{A}' in p.docker.containers
    p, _, _ = provisioner(tmp_path, [], count=0)  # empty database (e.g. a failed restore)
    for _ in range(5):
        p.reconcile()
    assert (tmp_path / A).exists()
    p, _, _ = provisioner(tmp_path, [], count=3)  # genuine orphan of a deleted account
    p.reconcile(); p.reconcile()
    assert (tmp_path / A).exists()
    p.reconcile()
    assert not (tmp_path / A).exists()
