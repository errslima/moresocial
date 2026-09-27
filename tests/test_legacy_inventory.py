"""The read-only legacy schema inventory (docs/cleanup/03-historical-schema.md, phase A)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text

from app import ai, config, db, legacy_inventory, operator_settings, security
from app.models import Base
from conftest import _with_db, _with_user, RUNTIME_PASSWORD, RUNTIME_ROLE, drain, workspace_of
from worker.main import schedule

KEY_CANARY = 'sk-ant-api03-InventoryCanaryKey-0001-wxyz'
TEXT_CANARY = 'Inventory canary: Sam likes secret lasergame'


def alembic_config():
    from app import config
    cfg = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    cfg.attributes['database_url'] = config.get().database_url
    return cfg


@pytest.fixture(autouse=True)
def legacy_schema(clean, database):
    """Inspect historical data at 0011, even after head has retired its empty tables.
    Application data is created at head (the ORM maps later columns), then downgraded."""
    cfg = alembic_config()
    try:
        yield
    finally:
        # Disposable fixture data intentionally includes nonempty legacy tables.
        with db.engine().begin() as c:
            c.execute(text('TRUNCATE workspaces CASCADE'))
        command.upgrade(cfg, 'head')


def setup_workspace(browser):
    browser.login('alex')
    schedule()
    drain()
    r = browser.post('/ask', {'question': 'Does Sam like lasergame?', 'csrf': browser.csrf('/ask')})
    assert r.status_code == 303
    wid = workspace_of('alex@example.test')
    command.downgrade(alembic_config(), '0011')
    return wid


def seed_legacy(wid):
    """Pre-cleanup rows written with explicit SQL, like data left by the retired features."""
    event, person, draft = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with db.engine().begin() as c:
        source = c.execute(text('SELECT id FROM sources WHERE workspace_id = :w LIMIT 1'), {'w': wid}).scalar()
        answer = c.execute(text('SELECT id FROM answers WHERE workspace_id = :w LIMIT 1'), {'w': wid}).scalar()
        c.execute(text("INSERT INTO people (id, workspace_id, display_name, name_source, pinned) "
                       "VALUES (:id, :w, 'Sam Canary', 'user', false)"),
                  {'id': person, 'w': wid})
        c.execute(text("INSERT INTO events (id, workspace_id, title, notes, timezone, status) "
                       "VALUES (:id, :w, 'Lasergame', :n, 'Europe/Amsterdam', 'planning')"),
                  {'id': event, 'w': wid, 'n': TEXT_CANARY})
        c.execute(text("INSERT INTO event_candidates (id, workspace_id, event_id, starts_at, ends_at, chosen) "
                       "VALUES (:id, :w, :e, now(), now() + interval '2 hours', false)"),
                  {'id': uuid.uuid4(), 'w': wid, 'e': event})
        c.execute(text("INSERT INTO event_people (id, workspace_id, event_id, person_id, status) "
                       "VALUES (:id, :w, :e, :p, 'not_contacted')"), {'id': uuid.uuid4(), 'w': wid, 'e': event, 'p': person})
        c.execute(text("INSERT INTO drafts (id, workspace_id, event_id, person_id, purpose, text, edited, stale) "
                       "VALUES (:id, :w, :e, :p, 'invitation', :t, false, false)"),
                  {'id': draft, 'w': wid, 'e': event, 'p': person, 't': TEXT_CANARY})
        for answer_id, draft_id in [(None, draft), (answer, draft), (None, None)]:
            c.execute(text("INSERT INTO answer_sources (id, workspace_id, answer_id, draft_id, source_id, source_version, label, excerpt) "
                           "SELECT :id, :w, :a, :d, id, version, 'F1', :t FROM sources WHERE id = :s"),
                      {'id': uuid.uuid4(), 'w': wid, 'a': answer_id, 'd': draft_id, 's': source, 't': TEXT_CANARY})
        c.execute(text("INSERT INTO connections (id, workspace_id, provider, provider_account, scopes, access_token_enc, state, "
                       "key_hint, validated_at) VALUES (:id, :w, 'anthropic', 'fingerprint-canary', '{}', :k, 'active', 'wxyz', now())"),
                  {'id': uuid.uuid4(), 'w': wid, 'k': security.encrypt(KEY_CANARY)})
        c.execute(text("UPDATE connections SET key_hint = 'gggg' WHERE provider = 'google'"))
        c.execute(text("UPDATE workspaces SET ai_preference = 'anthropic' WHERE id = :w"), {'w': wid})
        c.execute(text("INSERT INTO operator_settings (name, value, hint) VALUES ('anthropic_api_key', :k, 'wxyz'), "
                       "('voyage_api_key', NULL, NULL), ('ai_provider', 'anthropic', NULL)"),
                  {'k': security.encrypt(KEY_CANARY)})
        c.execute(text("INSERT INTO provider_usage (id, workspace_id, day, operation, model, provider, billing, reserved_tokens, status) "
                       "VALUES (:id, :w, current_date, 'generate', 'claude-test', 'anthropic', 'user', 10, 'reserved')"),
                  {'id': uuid.uuid4(), 'w': wid})
        c.execute(text("UPDATE self_profiles SET preferences = :t WHERE workspace_id = :w"), {'t': TEXT_CANARY, 'w': wid})


def snapshot() -> dict[str, list[str]]:
    from sqlalchemy import inspect
    with db.engine().connect() as c:
        present = set(inspect(c).get_table_names())
        return {t.name: c.execute(text(f'SELECT t::text FROM {t.name} t ORDER BY 1')).scalars().all()
                for t in Base.metadata.sorted_tables if t.name in present}


def run_cli(capsys) -> tuple[int, dict, str]:
    rc = legacy_inventory.main([])
    out, err = capsys.readouterr()
    return rc, (json.loads(out) if out.strip() else {}), out + err


def test_empty_candidates_are_eligible(browser, capsys):
    setup_workspace(browser)
    rc, report, _ = run_cli(capsys)
    assert rc == 0 and report['status'] == 'ok'
    assert report['transaction'] == {'read_only': True, 'isolation': 'repeatable read'}
    schema = report['schema']
    assert schema['revision'] == '0011' and schema['code_head'] == '0014'
    assert {t: schema[t] for t in ('event_candidates', 'event_people', 'drafts')} == {
        t: {'state': 'present', 'rows': 0} for t in ('event_candidates', 'event_people', 'drafts')}
    answers = schema['answer_sources']
    assert answers['rows'] > 0 and answers['draft_id_column'] == 'present'
    assert (answers['with_draft'], answers['with_answer_and_draft'], answers['with_neither']) == (0, 0, 0)
    assert schema['connections']['personal_key_rows'] == {'anthropic': 0, 'openai': 0}
    assert schema['connections']['key_hint'] == {'state': 'present', 'non_null': 0, 'non_null_outside_personal_keys': 0}
    assert schema['workspaces']['ai_preference'] == {'state': 'present', 'non_null': 0}
    assert schema['provider_usage']['by_billing_status']['operator']['reconciled'] > 0
    assert schema['self_profiles']['rows'] == 1
    assert report['groups'] == {'G': {'status': 'eligible', 'reasons': []}, 'K': {'status': 'eligible', 'reasons': []}}


def test_populated_candidates_are_ineligible_and_nothing_secret_is_printed(browser, capsys):
    wid = setup_workspace(browser)
    seed_legacy(wid)
    rc, report, printed = run_cli(capsys)
    assert rc == 0
    schema, groups = report['schema'], report['groups']
    assert groups['G'] == {'status': 'ineligible', 'reasons': [
        'event_candidates_nonempty', 'event_people_nonempty', 'drafts_nonempty', 'draft_linked_answer_sources',
        'ambiguous_answer_source_both', 'ambiguous_answer_source_neither']}
    assert groups['K'] == {'status': 'ineligible', 'reasons': [
        'anthropic_connection_rows', 'connections_key_hint_not_null', 'connections_validated_at_not_null',
        'workspaces_ai_preference_not_null']}
    assert (schema['answer_sources']['with_draft'], schema['answer_sources']['with_answer_and_draft'],
            schema['answer_sources']['with_neither']) == (2, 1, 1)
    assert schema['events']['by_origin']['legacy'] == 1
    assert schema['connections']['personal_key_rows_with_credential'] == 1
    assert schema['connections']['key_hint'] == {'state': 'present', 'non_null': 2, 'non_null_outside_personal_keys': 1}
    assert schema['operator_settings'] == {'legacy_keys': {'anthropic_api_key': {'rows': 1, 'with_value': 1},
                                                           'openai_api_key': {'rows': 0, 'with_value': 0},
                                                           'voyage_api_key': {'rows': 1, 'with_value': 0}},
                                           'legacy_provider_choice': 1}
    assert schema['provider_usage']['by_billing_status']['user'] == {'reserved': 1, 'reconciled': 0, 'failed': 0}
    assert schema['self_profiles']['with_preferences'] == 1
    with db.engine().connect() as c:
        stored = [c.execute(text("SELECT access_token_enc FROM connections WHERE provider = 'anthropic'")).scalar(),
                  c.execute(text("SELECT value FROM operator_settings WHERE name = 'anthropic_api_key'")).scalar()]
    for canary in [KEY_CANARY, 'InventoryCanary', 'wxyz', 'gggg', 'fingerprint-canary', TEXT_CANARY, 'Sam Canary',
                   str(wid), 'synthetic-test-password', RUNTIME_ROLE, 'postgresql', *stored]:
        assert canary not in printed


def test_report_is_read_only_and_calls_no_provider_or_decryption(browser, capsys, monkeypatch):
    wid = setup_workspace(browser)
    seed_legacy(wid)
    before = snapshot()

    def forbidden(*a, **kw):
        raise AssertionError('the inventory must not load providers or decrypt')
    for owner, name in [(security, 'decrypt'), (operator_settings, 'values'), (ai, 'provider'), (ai, 'reload_operator'),
                        (ai, 'build_operator'), (config, 'read_secret'), (config, 'load_google_client')]:
        monkeypatch.setattr(owner, name, forbidden)
    rc, report, _ = run_cli(capsys)
    assert rc == 0 and report['groups']['G']['status'] == 'ineligible'
    monkeypatch.undo()
    assert snapshot() == before

    def write(c):
        c.execute(text("UPDATE workspaces SET timezone = 'UTC'"))
    monkeypatch.setattr(legacy_inventory, 'collect', write)
    rc, report, printed = run_cli(capsys)
    assert rc == 1 and report == {}
    assert json.loads(printed) == {'status': 'error', 'error': 'inventory_failed', 'type': 'InternalError',
                                   'sqlstate': '25006'}  # read_only_sql_transaction
    assert snapshot() == before


def test_cli_runs_as_module_and_from_stdin_without_echoing_connection_details(database):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, **database['env']}

    def cli(args, stdin=None, **extra):
        return subprocess.run([sys.executable, *args], input=stdin, cwd=root, env={**env, **extra}, capture_output=True,
                              timeout=120)
    # `python -` is how an operator can run this file against an image that does not contain it yet.
    for args, stdin in [(['-m', 'app.legacy_inventory'], None), (['-'], (root / 'app' / 'legacy_inventory.py').read_bytes())]:
        r = cli(args, stdin)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)['status'] == 'ok' and r.stderr == b''
    missing = database['runtime_url'].rsplit('/', 1)[0] + '/missing_db_canary'
    r = cli(['-m', 'app.legacy_inventory'], DATABASE_URL=missing)
    assert r.returncode == 1 and r.stdout == b''
    error = json.loads(r.stderr)
    assert (error['status'], error['error'], error['type']) == ('error', 'inventory_failed', 'OperationalError')
    for canary in ('missing_db_canary', RUNTIME_PASSWORD, RUNTIME_ROLE, missing):
        assert canary.encode() not in r.stderr
    assert cli(['-m', 'app.legacy_inventory', '--apply']).returncode == 2


@pytest.fixture
def isolated_database(database):
    """A separate disposable database migrated to head; dropped afterwards."""
    name = 'moresocial_inventory_' + uuid.uuid4().hex[:8]
    admin = create_engine(database['admin_url'].replace('postgresql://', 'postgresql+psycopg://', 1),
                          isolation_level='AUTOCOMMIT')
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE {name} OWNER {RUNTIME_ROLE}'))
    ext = create_engine(_with_db(database['admin_url'], name).replace('postgresql://', 'postgresql+psycopg://', 1),
                        isolation_level='AUTOCOMMIT')
    with ext.connect() as c:
        c.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
    ext.dispose()
    url = _with_user(_with_db(database['admin_url'], name), RUNTIME_ROLE, RUNTIME_PASSWORD)
    cfg = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    cfg.attributes['database_url'] = url
    command.upgrade(cfg, '0011')
    try:
        yield url
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)'))
        admin.dispose()


def test_removed_objects_are_absent_and_unsupported_schemas_fail(isolated_database):
    url = isolated_database
    engine = create_engine(url.replace('postgresql://', 'postgresql+psycopg://', 1))

    def ddl(*statements):
        with engine.begin() as c:
            for statement in statements:
                c.execute(text(statement))

    def fails(code):
        with pytest.raises(legacy_inventory.InventoryError) as exc:
            legacy_inventory.run(url)
        assert str(exc.value) == code

    try:
        assert legacy_inventory.run(url)['groups'] == {'G': {'status': 'eligible', 'reasons': []},
                                                       'K': {'status': 'eligible', 'reasons': []}}
        ddl('ALTER TABLE answer_sources DROP COLUMN draft_id', 'DROP TABLE drafts')
        fails('schema_partial_gathering_removal')
        ddl('DROP TABLE event_people', 'DROP TABLE event_candidates', 'ALTER TABLE answer_sources ADD COLUMN draft_id uuid')
        fails('schema_partial_gathering_removal')
        ddl('ALTER TABLE answer_sources DROP COLUMN draft_id', 'ALTER TABLE connections DROP COLUMN key_hint')
        fails('schema_partial_key_metadata_removal')
        ddl('ALTER TABLE connections DROP COLUMN validated_at', 'ALTER TABLE workspaces DROP COLUMN ai_preference')
        report = legacy_inventory.run(url)
        schema = report['schema']
        assert report['groups'] == {'G': {'status': 'removed', 'reasons': []}, 'K': {'status': 'removed', 'reasons': []}}
        assert [schema[t] for t in ('event_candidates', 'event_people', 'drafts')] == [{'state': 'absent'}] * 3
        assert schema['answer_sources'] == {'rows': 0, 'draft_id_column': 'absent', 'with_neither': 0}
        assert schema['connections']['key_hint'] == {'state': 'absent'}
        assert schema['workspaces']['ai_preference'] == {'state': 'absent'}
        ddl("UPDATE alembic_version SET version_num = 'unknown'")
        fails('schema_revision_unsupported')
        ddl("UPDATE alembic_version SET version_num = '0011'", 'DROP TABLE provider_usage')
        fails('schema_required_object_missing')
        ddl('DROP TABLE alembic_version')
        fails('schema_unversioned')
    finally:
        engine.dispose()
