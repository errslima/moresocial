"""Historical migrations stay executable on a populated database."""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from app import db
from app.models import ProviderUsage
from conftest import drain
from worker.main import schedule


def test_migration_0003_round_trip_keeps_usage_history(browser, database):
    browser.login('alex', calendar=False)
    schedule()
    drain()
    with db.session() as s:
        before = s.scalar(select(func.count()).select_from(ProviderUsage))
    assert before
    cfg = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    cfg.attributes['database_url'] = database['runtime_url']
    try:
        command.downgrade(cfg, '0002')
    finally:
        command.upgrade(cfg, 'head')
    command.check(cfg)
    with db.session() as s:
        rows = s.scalars(select(ProviderUsage)).all()
        # Revision 0003 backfills the provider label that existed when it was written.
        assert len(rows) == before and all(u.billing == 'operator' and u.provider == 'anthropic' for u in rows)


def test_assistant_migration_preserves_suggestions_and_enforces_invariants(browser, database):
    """0014: legacy suggestions keep their data and identity; new invariants hold in SQL;
    the downgrade refuses while proactive data exists."""
    import uuid
    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from app import suggestions
    from app.models import Suggestion, SuggestionReview
    from conftest import workspace_of
    from test_suggestions import add, review
    browser.login('alex')
    wid = workspace_of('alex@example.test')
    add(wid, 'Can you join dinner on Friday?')
    add(wid, "I'll send you the photos tomorrow.", mine=True, age=20)
    first, second = review(wid)
    with db.session() as s:
        a, b = s.get(Suggestion, first.id), s.get(Suggestion, second.id)
        a.draft, a.draft_revision = 'My edited draft', 3
        b.state, b.disposition = 'dismissed', 'no_reply_needed'
    legacy = 'id, review_id, anchor_id, person_id, kind, title, reason, state, disposition, draft, draft_revision, stale'
    with db.engine().connect() as c:
        before = c.execute(text(f'SELECT {legacy} FROM suggestions ORDER BY id')).all()
    cfg = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    cfg.attributes['database_url'] = database['runtime_url']
    try:
        command.downgrade(cfg, '0013')
        command.upgrade(cfg, 'head')
        command.check(cfg)
        with db.engine().connect() as c:
            assert c.execute(text(f'SELECT {legacy} FROM suggestions ORDER BY id')).all() == before
            extra = {r.id: r for r in c.execute(text('SELECT id, review_id, anchor_id, kind, producer, action_key, '
                                                     'draft_origin FROM suggestions')).all()}
        for row in extra.values():
            assert row.producer == 'conversation'
            assert row.action_key == suggestions.legacy_action_key(row.review_id, row.anchor_id, row.kind)
        assert extra[first.id].draft_origin == 'user' and extra[second.id].draft_origin is None
        with db.session() as s:
            rv = s.get(SuggestionReview, first.review_id)
            chat, pid = rv.conversation_id, first.person_id

        def insert(**values):
            row = dict(id=uuid.uuid4(), workspace_id=wid, review_id=first.review_id, person_id=pid, title='t', reason='r',
                       kind='check_in', producer='proactive', action_key=uuid.uuid4().hex, anchor_id=None,
                       target_account='self', target_chat=chat)
            row.update(values)
            cols = ', '.join(row)
            with db.engine().begin() as c:
                c.execute(text(f'INSERT INTO suggestions ({cols}) VALUES ({", ".join(":" + k for k in row)})'), row)
        for bad in (dict(target_chat=None), dict(kind='reply'), dict(producer='conversation'),
                    dict(target_chat='31699999999@c.us'), dict(action_key=extra[first.id].action_key),
                    dict(event_link='auto'), dict(producer='robot')):
            with pytest.raises(IntegrityError):
                insert(**bad)
        insert()  # a valid proactive action
        with pytest.raises(RuntimeError, match='proactive_suggestions_present'):
            command.downgrade(cfg, '0013')
        from alembic.script import ScriptDirectory
        head = ScriptDirectory.from_config(cfg).get_current_head()
        with db.engine().connect() as c:  # the refused downgrade changed nothing
            assert c.execute(text('SELECT version_num FROM alembic_version')).scalar() == head
        with db.engine().begin() as c:
            c.execute(text("DELETE FROM suggestions WHERE producer = 'proactive'"))
            c.execute(text("INSERT INTO assistant_settings (workspace_id, warmth) VALUES (:w, 'warm')"), {'w': wid})
        with pytest.raises(RuntimeError, match='assistant_data_present:assistant_settings'):
            command.downgrade(cfg, '0013')
    finally:
        command.upgrade(cfg, 'head')


def test_draft_evidence_migration_backfills_style_mode_and_guards_downgrade(make_browser, database):
    """0016: default-only tone settings become conversation voice, deliberate ones stay custom
    (tone values retained); a correction link blocks the downgrade; the lineage FK only nulls."""
    import uuid
    import pytest
    from sqlalchemy import text
    from app.models import Claim
    from app.repo import Scoped
    from conftest import workspace_of
    for user in ('alex', 'blake'):
        make_browser().login(user)
    alex, blake = workspace_of('alex@example.test'), workspace_of('blake@example.test')
    cfg = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    cfg.attributes['database_url'] = database['runtime_url']
    try:
        command.downgrade(cfg, '0015')
        with db.engine().begin() as c:
            c.execute(text('INSERT INTO assistant_settings (workspace_id) VALUES (:w)'), {'w': alex})
            c.execute(text("INSERT INTO assistant_settings (workspace_id, humour) VALUES (:w, 'playful')"), {'w': blake})
        command.upgrade(cfg, 'head')
        command.check(cfg)
        with db.engine().connect() as c:
            rows = {r.workspace_id: (r.style_mode, r.humour) for r in
                    c.execute(text('SELECT workspace_id, style_mode, humour FROM assistant_settings')).all()}
        assert rows == {alex: ('conversation', 'light'), blake: ('custom', 'playful')}
        with db.session() as s:
            ws = Scoped(s, alex)
            original = ws.add(Claim(id=uuid.uuid4(), subject='self', category='life_update', attribute='location',
                                    value='Rotterdam', basis='explicit', status='rejected', origin='extraction',
                                    claim_key=uuid.uuid4().hex))
            s.flush()
            correction = ws.add(Claim(id=uuid.uuid4(), subject='self', category='life_update', attribute='location',
                                      value='Utrecht', basis='user', status='confirmed', origin='user',
                                      claim_key=uuid.uuid4().hex, corrects_id=original.id))
            ids = original.id, correction.id
        with pytest.raises(RuntimeError, match='claim_corrections_present'):
            command.downgrade(cfg, '0015')
        from alembic.script import ScriptDirectory
        head = ScriptDirectory.from_config(cfg).get_current_head()
        with db.engine().begin() as c:
            assert c.execute(text('SELECT version_num FROM alembic_version')).scalar() == head
            c.execute(text('DELETE FROM claims WHERE id = :id'), {'id': ids[0]})
            kept = c.execute(text('SELECT workspace_id, corrects_id FROM claims WHERE id = :id'), {'id': ids[1]}).one()
        assert kept.workspace_id == alex and kept.corrects_id is None
        command.downgrade(cfg, '0015')  # nothing left to lose
    finally:
        command.upgrade(cfg, 'head')
