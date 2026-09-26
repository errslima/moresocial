"""Diary search lifecycle and non-destructive, workspace-scoped semantic grouping."""
import re
import uuid

import pytest
from sqlalchemy import select, func

from app import ai, db, diary, ingest, memory, observation_groups as groups, retrieval
from app.models import Claim, ClaimEmbedding, ClaimSource, Chunk, Embedding, Job, SelfProfile, Source
from app.repo import Scoped
from conftest import drain, workspace_of
from worker.main import reconcile_ai


def submit(browser, body, token=None):
    html = browser.get('/me').text
    token = token or re.search(r'name="entry_token" value="([^"]+)"', html).group(1)
    return browser.post('/me', {'body': body, 'entry_token': token, 'csrf': browser.csrf('/me')})


def test_diary_search_edit_delete_and_idempotency(browser):
    browser.login('alex', gmail=False, calendar=False)
    wid = workspace_of('alex@example.test')
    token = str(uuid.uuid4())
    assert submit(browser, 'I enjoy alpine sketching.', token).status_code == 303
    assert submit(browser, 'I enjoy alpine sketching.', token).status_code == 303
    assert 'Searchable' not in browser.get('/me').text
    drain()
    assert 'Searchable · observations processed' in browser.get('/me').text
    with db.session() as s:
        ws = Scoped(s, wid)
        source = ws.first(ws.q(Source).where(Source.provider == 'diary'))
        sid, when = source.id, source.occurred_at
        assert s.scalar(select(func.count()).select_from(Source)) == 1
        assert ws.first(ws.q(Embedding))
        assert any(e.source_id == sid for e in retrieval.evidence_for(ws, retrieval.search_chunks(ws, 'alpine sketching')))
        assert ws.first(ws.q(Claim)).subject == 'self'
        answer_id = retrieval.ask(ws, 'alpine sketching').id
    assert browser.post(f'/me/diary/{sid}/edit', {'body': 'Tonight I just need some quiet.', 'csrf': browser.csrf()}).status_code == 303
    with db.session() as s:
        ws = Scoped(s, wid)
        assert ws.get(Source, sid).occurred_at == when
        assert not ws.first(ws.q(Embedding))
        assert not ws.first(ws.q(Claim))
    drain()
    with db.session() as s:
        ws = Scoped(s, wid)
        assert ws.first(ws.q(Embedding))  # freeform thought remains searchable without claims
        assert not ws.first(ws.q(Claim))
    assert browser.post(f'/me/diary/{sid}/delete', {'csrf': browser.csrf()}).status_code == 303
    with db.session() as s:
        ws = Scoped(s, wid)
        assert not ws.get(Source, sid)
        assert not ws.first(ws.q(Embedding))
    assert 'removed' in browser.get('/answers/' + str(answer_id)).text
    assert submit(browser, 'I enjoy alpine sketching.', token).status_code == 400


def test_diary_validation_and_workspace_isolation(make_browser):
    a, b = make_browser(), make_browser()
    a.login('alex', gmail=False, calendar=False)
    b.login('blake', gmail=False, calendar=False)
    assert submit(a, ' ').status_code == 400
    assert submit(a, 'x' * 30001).status_code == 400
    assert submit(a, '<script>private diary</script>').status_code == 303
    assert '&lt;script&gt;private diary&lt;/script&gt;' in a.get('/me').text
    with db.session() as s:
        src = s.scalar(select(Source))
        sid = src.id
    assert 'private diary' not in b.get('/me').text
    assert b.post(f'/me/diary/{sid}/edit', {'body': 'changed', 'csrf': b.csrf()}).status_code == 404
    assert b.post(f'/me/diary/{sid}/delete', {'csrf': b.csrf()}).status_code == 404
    assert a.post('/me', {'body': 'missing CSRF', 'entry_token': str(uuid.uuid4())}).status_code == 403


def test_legacy_profile_migration_preserves_text_and_date(browser):
    browser.login('alex', gmail=False, calendar=False)
    wid = workspace_of('alex@example.test')
    with db.session() as s:
        p = s.get(SelfProfile, wid)
        p.preferences, p.goals = 'Small groups', 'See friends monthly'
        s.flush()
        old_date = p.updated_at
        diary.migrate_profiles(s)
        diary.migrate_profiles(s)
    drain()
    with db.session() as s:
        sources = list(s.scalars(select(Source).where(Source.provider == 'diary')))
        assert {x.body for x in sources} == {'Small groups', 'See friends monthly'}
        assert all(x.occurred_at == old_date for x in sources)
        assert len(list(s.scalars(select(Embedding)))) == 2
        assert s.get(SelfProfile, wid).preferences == 'Small groups'


def add_claim(wid, value, *, source=None, status='active', category='preference'):
    with db.session() as s:
        ws = Scoped(s, wid)
        src = ws.get(Source, source) if source else diary.save(ws, value)
        c = ws.add(Claim(id=uuid.uuid4(), subject='self', category=category, value=value, basis='explicit',
            origin='extraction', status=status, claim_key=memory.claim_key(wid, 'self', category, None, value)))
        s.flush()
        ws.add(ClaimSource(id=uuid.uuid4(), claim_id=c.id, source_id=src.id, source_version=src.version))
        return c.id, src.id


def judge(monkeypatch, relation='equivalent', hook=None):
    def generate(*args, **kwargs):
        assert kwargs['context']['purpose'] == 'group_observations'
        if hook:
            hook()
        return ai.Generated({'decisions': [{'candidate': key, 'relation': relation}
            for key in kwargs['context']['candidates']]}, 10, 10, 'synthetic-judge')
    monkeypatch.setattr(ai, 'generate', generate)


def test_grouping_unions_evidence_preserves_confirmed_text_and_ungroups(browser, monkeypatch):
    browser.login('alex', gmail=False, calendar=False)
    wid = workspace_of('alex@example.test')
    a, sa = add_claim(wid, 'Prefers small gatherings', status='confirmed')
    b, sb = add_claim(wid, 'Likes socializing in small groups')
    c, _ = add_claim(wid, 'Enjoys small-group socializing', source=sb, category='interest')
    judge(monkeypatch)
    assert groups.consolidate(wid, a) == 'distinct'
    assert groups.consolidate(wid, b) == 'grouped'
    assert groups.consolidate(wid, c) == 'grouped'
    page = browser.get('/me').text
    assert page.count('class="claim status-') == 1
    assert 'Evidence (2)' in page and '<strong>Prefers small gatherings</strong>' in page
    assert 'Grouped observations (3)' in page
    assert browser.post(f'/people/self/claims/{a}', {'action': 'ungroup', 'csrf': browser.csrf()}).status_code == 303
    assert browser.get('/me').text.count('class="claim status-') == 3
    with db.session() as s:
        assert all(not c.auto_group and c.group_id is None for c in s.scalars(select(Claim)))
    assert groups.consolidate(wid, c) == 'gone'


@pytest.mark.parametrize('relation', ['related', 'contradictory', 'uncertain'])
def test_similarity_alone_never_merges(browser, monkeypatch, relation):
    browser.login('alex', gmail=False, calendar=False)
    wid = workspace_of('alex@example.test')
    a, _ = add_claim(wid, 'Likes small groups')
    b, _ = add_claim(wid, 'Dislikes noisy venues')
    judge(monkeypatch, relation)
    groups.consolidate(wid, a)
    assert groups.consolidate(wid, b) == 'distinct'
    with db.session() as s:
        assert all(c.group_id is None for c in s.scalars(select(Claim)))


def test_rejected_paraphrase_is_not_revived_and_edit_is_authoritative(browser, monkeypatch):
    browser.login('alex', gmail=False, calendar=False)
    wid = workspace_of('alex@example.test')
    a, _ = add_claim(wid, 'Likes small groups')
    b, _ = add_claim(wid, 'Prefers small gatherings')
    judge(monkeypatch)
    groups.consolidate(wid, a)
    groups.consolidate(wid, b)
    browser.post(f'/people/self/claims/{a}', {'action': 'edit', 'value': 'Likes large parties', 'csrf': browser.csrf()})
    with db.session() as s:
        assert s.get(Claim, a).status == s.get(Claim, b).status == 'rejected'
        corrected = s.scalar(select(Claim).where(Claim.value == 'Likes large parties'))
        assert corrected.status == 'confirmed' and corrected.group_id is None
    c, _ = add_claim(wid, 'Enjoys little gatherings')
    assert groups.consolidate(wid, c) == 'grouped'
    with db.session() as s:
        assert s.get(Claim, c).status == 'rejected'
        assert s.get(Claim, corrected.id).status == 'confirmed'


def test_source_deletion_updates_group_without_losing_other_evidence(browser, monkeypatch):
    browser.login('alex', gmail=False, calendar=False)
    wid = workspace_of('alex@example.test')
    a, sa = add_claim(wid, 'Likes small groups')
    b, sb = add_claim(wid, 'Prefers small gatherings')
    judge(monkeypatch)
    groups.consolidate(wid, a)
    groups.consolidate(wid, b)
    browser.post(f'/me/diary/{sa}/delete', {'csrf': browser.csrf()})
    page = browser.get('/me').text
    assert 'Evidence (1)' in page and '<strong>Prefers small gatherings</strong>' in page
    with db.session() as s:
        assert s.get(Claim, a) is None and s.get(Claim, b) is not None
        assert not s.scalar(select(ClaimEmbedding).where(ClaimEmbedding.claim_id == a))


@pytest.mark.parametrize('mutation', ['delete', 'edit', 'ungroup'])
def test_source_or_group_changes_win_inflight_race(browser, monkeypatch, mutation):
    browser.login('alex', gmail=False, calendar=False)
    wid = workspace_of('alex@example.test')
    a, sa = add_claim(wid, 'Likes small groups')
    b, _ = add_claim(wid, 'Prefers small gatherings')
    groups.consolidate(wid, a)
    def mutate():
        with db.session() as s:
            ws = Scoped(s, wid)
            if mutation == 'delete':
                ingest.exclude(ws, ws.get(Source, sa), 'object')
            elif mutation == 'edit':
                diary.save(ws, 'Actually this was a passing thought', entry=ws.get(Source, sa))
            else:
                groups.ungroup(ws, ws.get(Claim, a))
    judge(monkeypatch, hook=mutate)
    with pytest.raises(ai.AIUnavailable, match='group_changed'):
        groups.consolidate(wid, b)
    with db.session() as s:
        assert s.get(Claim, b).group_id is None


def test_grouping_never_sends_other_workspace_evidence(make_browser, monkeypatch):
    a, b = make_browser(), make_browser()
    a.login('alex', gmail=False, calendar=False)
    b.login('blake', gmail=False, calendar=False)
    wa, wb = workspace_of('alex@example.test'), workspace_of('blake@example.test')
    first, _ = add_claim(wa, 'Prefers small gatherings')
    secret, _ = add_claim(wb, 'Private small gatherings')
    groups.consolidate(wb, secret)
    monkeypatch.setattr(ai, 'generate', lambda *a, **kw: pytest.fail('Other workspace became a candidate'))
    assert groups.consolidate(wa, first) == 'distinct'


def test_backfill_is_idempotent_and_groups_existing_records(browser, monkeypatch):
    browser.login('alex', gmail=False, calendar=False)
    wid = workspace_of('alex@example.test')
    a, _ = add_claim(wid, 'Prefers small gatherings')
    b, _ = add_claim(wid, 'Likes small groups')
    judge(monkeypatch)
    assert reconcile_ai() == 2
    assert reconcile_ai() == 0
    from worker.main import drain as run
    run(kinds=['group_claim'])
    with db.session() as s:
        assert s.get(Claim, a).group_id == s.get(Claim, b).group_id is not None
        assert not list(s.scalars(select(Job).where(Job.status == 'failed')))
    assert reconcile_ai() == 0


def test_reverting_diary_text_rebuilds_completed_jobs(browser):
    browser.login('alex', gmail=False, calendar=False)
    wid = workspace_of('alex@example.test')
    submit(browser, 'I enjoy drawing.')
    drain()
    with db.session() as s:
        sid = s.scalar(select(Source.id))
    for body in ('I enjoy dancing.', 'I enjoy drawing.'):
        browser.post(f'/me/diary/{sid}/edit', {'body': body, 'csrf': browser.csrf()})
        drain()
        with db.session() as s:
            ws = Scoped(s, wid)
            assert ws.first(ws.q(Embedding))
            assert ws.first(ws.q(Claim)).value in body
    assert 'Searchable · observations processed' in browser.get('/me').text


def test_invalid_group_decisions_never_publish(browser, monkeypatch):
    browser.login('alex', gmail=False, calendar=False)
    wid = workspace_of('alex@example.test')
    a, _ = add_claim(wid, 'Likes small groups')
    b, _ = add_claim(wid, 'Prefers small gatherings')
    groups.consolidate(wid, a)
    monkeypatch.setattr(ai, 'generate', lambda *a, **kw: ai.Generated(
        {'decisions': [{'candidate': 'OTHER_WORKSPACE', 'relation': 'equivalent'}]}, 10, 10, 'test'))
    with pytest.raises(ai.InvalidOutput):
        groups.consolidate(wid, b)
    with db.session() as s:
        assert s.get(Claim, a).group_id is s.get(Claim, b).group_id is None


def test_real_migration_copies_nonempty_profiles(browser, database):
    from alembic import command
    from alembic.config import Config
    from conftest import ROOT
    browser.login('alex', gmail=False, calendar=False)
    wid = workspace_of('alex@example.test')
    with db.session() as s:
        profile = s.get(SelfProfile, wid)
        profile.preferences, profile.goals = 'Small groups', 'More outdoor time'
    cfg = Config(str(ROOT / 'alembic.ini'))
    cfg.attributes['database_url'] = database['runtime_url']
    command.downgrade(cfg, '0006')
    command.upgrade(cfg, 'head')
    drain()
    with db.session() as s:
        assert {src.body for src in s.scalars(select(Source).where(Source.provider == 'diary'))} == {'Small groups', 'More outdoor time'}
        assert s.scalar(select(func.count()).select_from(Embedding)) == 2
