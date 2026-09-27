"""The non-additive gathering cleanup refuses data and preserves active records."""
import json
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, text

from app import db, legacy_inventory
from conftest import drain, workspace_of
from worker.main import schedule

LEGACY = ('drafts', 'event_candidates', 'event_people')


@pytest.fixture
def predecessor(browser, database):
    browser.login('alex')
    schedule()
    drain()
    assert browser.post('/ask', {'question': 'Does Sam like lasergame?', 'csrf': browser.csrf('/ask')}).status_code == 303
    cfg = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    cfg.attributes['database_url'] = database['runtime_url']
    command.downgrade(cfg, '0011')
    wid = workspace_of('alex@example.test')
    with db.engine().begin() as c:
        event, person = uuid.uuid4(), uuid.uuid4()
        c.execute(text("INSERT INTO events (id, workspace_id, title, notes, timezone, status) "
                       "VALUES (:id, :w, 'Historical plan', 'KEEP_CANARY', 'Europe/Amsterdam', 'planning')"),
                  {'id': event, 'w': wid})
        c.execute(text("INSERT INTO people (id, workspace_id, display_name, name_source, pinned) "
                       "VALUES (:id, :w, 'Synthetic friend', 'user', true)"), {'id': person, 'w': wid})
        # Group K is deliberately nonempty: G must leave its encrypted bytes/metadata alone.
        c.execute(text("INSERT INTO connections (id, workspace_id, provider, provider_account, scopes, state, "
                       "access_token_enc, key_hint, validated_at) "
                       "VALUES (:id, :w, 'anthropic', 'synthetic', '{}', 'active', 'KEEP_KEY_CANARY', 'hint', now())"),
                  {'id': uuid.uuid4(), 'w': wid})
    try:
        yield cfg, {'w': wid, 'e': event, 'p': person, 'id': uuid.uuid4()}
    finally:
        with db.engine().begin() as c:
            c.execute(text('TRUNCATE workspaces CASCADE'))
        command.upgrade(cfg, 'head')


def contents():
    """Compare all active records, including cipher bytes, without changing their values."""
    with db.engine().connect() as c:
        out = {}
        for table in inspect(c).get_table_names():
            if table in (*LEGACY, 'alembic_version'):
                continue
            expr = "to_jsonb(t) - 'draft_id'" if table == 'answer_sources' else 'to_jsonb(t)'
            rows = c.execute(text(f'SELECT {expr} FROM "{table}" t')).scalars()
            out[table] = sorted(json.dumps(row, sort_keys=True) for row in rows)
        return out


def structure():
    with db.engine().connect() as c:
        inspector = inspect(c)
        out = {}
        for table in (*LEGACY, 'answer_sources'):
            out[table] = {
                'columns': sorted((col['name'], str(col['type']), col['nullable'], col['default'])
                                  for col in inspector.get_columns(table)),
                'foreign_keys': sorted(json.dumps(f, sort_keys=True) for f in inspector.get_foreign_keys(table)),
                'indexes': sorted(json.dumps(i, sort_keys=True) for i in inspector.get_indexes(table)),
                'unique': sorted(json.dumps(u, sort_keys=True) for u in inspector.get_unique_constraints(table)),
                'primary': inspector.get_pk_constraint(table),
            }
        return out


def test_empty_gathering_upgrade_and_structural_downgrade_preserve_active_data(predecessor, database):
    cfg, ids = predecessor
    before, old_structure = contents(), structure()
    command.upgrade(cfg, 'head')
    command.check(cfg)
    assert contents() == before
    report = legacy_inventory.run(database['runtime_url'])
    assert report['groups']['G']['status'] == 'removed'
    assert report['groups']['K']['status'] == 'ineligible'
    with db.engine().connect() as c:
        assert not set(LEGACY) & set(inspect(c).get_table_names())
        assert 'draft_id' not in {x['name'] for x in inspect(c).get_columns('answer_sources')}
    command.downgrade(cfg, '0011')
    assert structure() == old_structure
    assert contents() == before
    command.upgrade(cfg, 'head')
    command.check(cfg)
    assert contents() == before


@pytest.mark.parametrize('kind', ['drafts', 'event_candidates', 'event_people', 'orphan_reference', 'draft_reference', 'both_references'])
def test_nonempty_gathering_upgrade_refuses_atomically(predecessor, kind, capsys):
    cfg, ids = predecessor
    with db.engine().begin() as c:
        if kind in ('drafts', 'draft_reference', 'both_references'):
            c.execute(text("INSERT INTO drafts (id, workspace_id, event_id, person_id, purpose, text, edited, stale) "
                           "VALUES (:id, :w, :e, :p, 'invitation', 'PRIVATE_DRAFT_CANARY', false, false)"), ids)
        elif kind == 'event_candidates':
            c.execute(text("INSERT INTO event_candidates (id, workspace_id, event_id, starts_at, ends_at, chosen) "
                           "VALUES (:id, :w, :e, now(), now() + interval '1 hour', false)"), ids)
        elif kind == 'event_people':
            c.execute(text("INSERT INTO event_people (id, workspace_id, event_id, person_id, status) "
                           "VALUES (:id, :w, :e, :p, 'accepted')"), ids)
        if kind == 'orphan_reference':
            c.execute(text('UPDATE answer_sources SET answer_id = NULL'))
        elif kind in ('draft_reference', 'both_references'):
            assignment = 'draft_id = :id' + (', answer_id = NULL' if kind == 'draft_reference' else '')
            c.execute(text('UPDATE answer_sources SET ' + assignment), ids)
    before, old_structure = contents(), structure()
    with db.engine().connect() as c:
        old_counts = {table: c.scalar(text(f'SELECT count(*) FROM {table}')) for table in LEGACY}
    capsys.readouterr()
    with pytest.raises(RuntimeError, match='legacy_gathering_'):
        command.upgrade(cfg, 'head')
    printed = ''.join(capsys.readouterr())
    assert 'CANARY' not in printed
    assert contents() == before and structure() == old_structure
    with db.engine().connect() as c:
        assert c.scalar(text('SELECT version_num FROM alembic_version')) == '0011'
        assert {table: c.scalar(text(f'SELECT count(*) FROM {table}')) for table in LEGACY} == old_counts
