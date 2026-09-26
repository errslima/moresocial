"""Suggestion contracts with real PostgreSQL and deterministic synthetic AI."""
from datetime import timedelta
import copy
import uuid

import pytest
from sqlalchemy import select

from app import ai, db, ingest, suggestions
from app.models import (Job, Person, Source, Suggestion, SuggestionReview, SuggestionSource)
from app.repo import Scoped
from conftest import workspace_of

CHAT = '31600000999@c.us'


def add(wid, text, *, mine=False, age=24, oid=None, conv=CHAT):
    with db.session() as s:
        ws = Scoped(s, wid)
        result = ingest.IngestResult()
        row = ingest.upsert(ws, ingest.SourceRecord(provider='whatsapp', provider_account='self',
            provider_object_id=oid or uuid.uuid4().hex, kind='message', conversation_id=conv,
            conversation_title='Maya', body=text, occurred_at=suggestions.utcnow() - timedelta(hours=age),
            from_me=mine, meta={'is_group': conv.endswith('@g.us')},
            author=None if mine else ingest.Participant('whatsapp', CHAT, 'author', 'Maya'),
            participants=[ingest.Participant('whatsapp', CHAT, 'member', 'Maya')]), set(), result)
        ingest.schedule_rebuilds(ws, result)
        return row.id


def review(wid):
    with db.session() as s:
        rid = s.scalar(select(SuggestionReview.id).where(SuggestionReview.workspace_id == wid))
    assert suggestions.review_conversation(wid, rid) in ('reviewed', 'empty')
    with db.session() as s:
        return list(s.scalars(select(Suggestion).where(Suggestion.workspace_id == wid).order_by(Suggestion.created_at)))


@pytest.fixture
def setup(browser):
    browser.login('alex')
    wid = workspace_of('alex@example.test')
    src = add(wid, 'Can you join dinner on Friday?')
    row = review(wid)[0]
    return wid, src, row


def test_evidence_page_and_draft_lifecycle(browser, setup):
    wid, src, row = setup
    p = browser.get('/suggestions')
    assert p.status_code == 200
    assert 'Reply about dinner' in p.text and str(src) in p.text and 'Why this appeared' in p.text
    calls = len(ai.provider().calls)
    assert browser.get(f'/suggestions/{row.id}').status_code == 200
    assert len(ai.provider().calls) == calls  # reading never generates
    csrf = browser.csrf('/suggestions')
    assert browser.post(f'/suggestions/{row.id}/draft', {'csrf': csrf}).status_code == 303
    with db.session() as s:
        d = s.get(Suggestion, row.id)
        assert d.draft and d.state == 'active'
        rev, rrev = d.draft_revision, d.draft_review_revision
    assert browser.post(f'/suggestions/{row.id}/draft/save', {'csrf': csrf, 'revision': rev,
        'review_revision': rrev, 'text': 'Yes, Friday works!'}).status_code == 303
    assert browser.post(f'/suggestions/{row.id}/draft/save', {'csrf': csrf, 'revision': rev,
        'review_revision': rrev, 'text': 'Stale tab overwrites edit'}).status_code == 409
    assert browser.post(f'/suggestions/{row.id}/draft', {'csrf': csrf}).status_code == 303
    assert 'Yes, Friday works!' in browser.get(f'/suggestions/{row.id}').text
    with db.session() as s:
        assert s.get(Suggestion, row.id).state == 'active'


def test_dismissal_is_stable_and_new_question_is_new(browser, setup):
    wid, src, row = setup
    csrf = browser.csrf('/suggestions')
    assert browser.post(f'/suggestions/{row.id}/state', {'csrf': csrf, 'action': 'no_reply_needed'}).status_code == 303
    rows = review(wid)
    assert len(rows) == 1 and rows[0].state == 'dismissed'
    assert 'Reply about dinner' not in browser.get('/suggestions').text
    assert 'Reply about dinner' in browser.get('/suggestions?tab=history').text
    add(wid, 'Can you join dinner next Friday?', age=1)
    rows = review(wid)
    assert len(rows) == 2 and sorted(r.state for r in rows) == ['active', 'dismissed']


def test_snooze_and_resolution(browser, setup):
    wid, src, row = setup
    csrf = browser.csrf('/suggestions')
    assert browser.post(f'/suggestions/{row.id}/state', {'csrf': csrf, 'action': 'snooze', 'days': '3'}).status_code == 303
    assert 'Reply about dinner' not in browser.get('/suggestions').text
    assert 'Reply about dinner' in browser.get('/suggestions?tab=snoozed').text
    review(wid)
    with db.session() as s:
        assert s.get(Suggestion, row.id).state == 'snoozed'
        s.get(Suggestion, row.id).snoozed_until = suggestions.utcnow() - timedelta(seconds=1)
    assert 'Reply about dinner' in browser.get('/suggestions').text
    add(wid, 'Yes, Friday works!', mine=True, age=0)
    rows = review(wid)
    assert rows[0].state == 'resolved'


def test_later_answer_and_unrelated_outgoing(browser):
    browser.login('alex')
    wid = workspace_of('alex@example.test')
    add(wid, 'Can you join dinner on Friday?', age=30)
    add(wid, 'Yes, Friday works!', mine=True, age=29)
    assert not review(wid)
    add(wid, "I'll send you the photos tomorrow.", mine=True, age=20)
    add(wid, 'Have a good day!', mine=True, age=19)
    rows = review(wid)
    assert len(rows) == 1 and rows[0].kind == 'promise'
    add(wid, 'Here are the photos.', mine=True, age=0)
    assert review(wid)[0].state == 'resolved'


def test_revision_race_discards_result(browser, setup, monkeypatch):
    wid, src, row = setup
    original = ai.generate
    def concurrent(*args, **kw):
        out = original(*args, **kw)
        add(wid, 'Yes, Friday works!', mine=True, age=0)
        return out
    monkeypatch.setattr(ai, 'generate', concurrent)
    assert suggestions.review_conversation(wid, row.review_id) == 'superseded'
    with db.session() as s:
        assert s.get(Suggestion, row.id).stale
    monkeypatch.setattr(ai, 'generate', original)
    assert review(wid)[0].state == 'resolved'


def test_exclusion_erases_derived_text_and_cancels_late_result(browser, setup, monkeypatch):
    wid, src, row = setup
    suggestions.draft(wid, row.id)
    original = ai.generate
    def exclude(*args, **kw):
        out = original(*args, **kw)
        with db.session() as s:
            ws = Scoped(s, wid)
            ingest.exclude(ws, ws.get(Source, src), 'conversation')
        return out
    monkeypatch.setattr(ai, 'generate', exclude)
    assert suggestions.review_conversation(wid, row.review_id) == 'superseded'
    with db.session() as s:
        assert s.get(Suggestion, row.id) is None
        assert not list(s.scalars(select(SuggestionSource)))
    assert 'Reply about dinner' not in browser.get('/suggestions?tab=history').text


def test_draft_race_and_old_draft_requires_review(browser, setup, monkeypatch):
    wid, src, row = setup
    original = ai.generate
    def concurrent(*args, **kw):
        out = original(*args, **kw)
        add(wid, 'The location has changed.', age=0)
        return out
    monkeypatch.setattr(ai, 'generate', concurrent)
    with pytest.raises(ValueError, match='changed'):
        suggestions.draft(wid, row.id)
    monkeypatch.setattr(ai, 'generate', original)
    review(wid)
    suggestions.draft(wid, row.id)
    add(wid, 'Please check the address.', age=0)
    review(wid)
    page = browser.get(f'/suggestions/{row.id}').text
    assert 'predates the latest conversation update' in page
    assert 'data-copy="suggestion-draft"' not in page


def test_pause_workspace_boundaries_and_csrf(browser, make_browser, setup):
    wid, src, row = setup
    other = make_browser()
    other.login('blake')
    csrf = other.csrf()
    assert other.get(f'/suggestions/{row.id}').status_code == 404
    assert other.post(f'/suggestions/{row.id}/draft', {'csrf': csrf}).status_code == 404
    assert other.post(f'/suggestions/{row.id}/state', {'csrf': csrf, 'action': 'done'}).status_code == 404
    assert other.post(f'/suggestions/conversations/{row.review_id}', {'csrf': csrf, 'action': 'pause'}).status_code == 404
    assert browser.post(f'/suggestions/{row.id}/state', {'action': 'done'}).status_code == 403
    csrf = browser.csrf('/suggestions')
    assert browser.post(f'/suggestions/conversations/{row.review_id}', {'csrf': csrf, 'action': 'pause'}).status_code == 303
    assert suggestions.review_conversation(wid, row.review_id) == 'skipped'
    assert 'Reply about dinner' not in browser.get('/suggestions').text
    assert browser.post(f'/suggestions/{row.id}/draft', {'csrf': csrf}).status_code == 409
    assert browser.post(f'/suggestions/conversations/{row.review_id}', {'csrf': csrf, 'action': 'resume'}).status_code == 303
    assert review(wid)[0].state == 'active'


def test_worker_scheduling_deduplication_and_bootstrap(browser, setup):
    wid, src, row = setup
    with db.session() as s:
        r = s.get(SuggestionReview, row.review_id)
        r.next_review_at = suggestions.utcnow() - timedelta(seconds=1)
    with db.session() as s:
        assert suggestions.schedule(s) == 1
    with db.session() as s:
        assert suggestions.schedule(s) == 0
    from worker.main import drain
    assert drain(kinds=['review_suggestions']) == 1
    add(wid, 'Thanks!', conv='123@g.us')
    with db.session() as s:
        suggestions.bootstrap(s)
        assert len(list(s.scalars(select(SuggestionReview)))) == 1
        s.delete(s.get(SuggestionReview, row.review_id))
    with db.session() as s:
        suggestions.bootstrap(s)
        assert len(list(s.scalars(select(SuggestionReview)))) == 1


@pytest.mark.parametrize('damage', ['foreign_source', 'foreign_id', 'wrong_direction', 'bad_date', 'bad_shape'])
def test_invalid_model_output_rejected(browser, setup, damage):
    wid, src, row = setup
    with db.session() as s:
        ws = Scoped(s, wid)
        ctx = suggestions.snapshot(ws, ws.get(SuggestionReview, row.review_id))[0]
    from app.fake_generation import suggestion_review
    data = copy.deepcopy(suggestion_review(ctx))
    item = data['items'][0]
    if damage == 'foreign_source': item['evidence'] = ['other-workspace-source']
    if damage == 'foreign_id': item['id'] = str(uuid.uuid4())
    if damage == 'wrong_direction': item['kind'] = 'promise'
    if damage == 'bad_date': item['due_at'] = 'Friday'
    if damage == 'bad_shape': item['kind'] = []
    with pytest.raises(ai.InvalidOutput):
        suggestions.validate(data, ctx)


def test_omission_is_not_resolution(browser, setup, monkeypatch):
    wid, src, row = setup
    original = ai.generate
    def omit(*args, **kw):
        out = original(*args, **kw)
        out.data = {'items': [], 'resolved': []}
        return out
    monkeypatch.setattr(ai, 'generate', omit)
    assert review(wid)[0].state == 'active'


def test_no_suggestions_for_thanks(browser):
    browser.login('alex')
    wid = workspace_of('alex@example.test')
    add(wid, 'Thanks!')
    assert not review(wid)


def test_truncation_retries_once_with_headroom(browser, setup, monkeypatch):
    wid, src, row = setup
    original, limits = ai.generate, []
    def truncated(*args, **kw):
        limits.append(kw['max_tokens'])
        if len(limits) == 1:
            raise ai.InvalidOutput('truncated')
        return original(*args, **kw)
    monkeypatch.setattr(ai, 'generate', truncated)
    assert review(wid)[0].state == 'active'
    assert limits == [5000, 10000]


def test_follow_up_and_deadline_are_stored_with_evidence(browser, setup, monkeypatch):
    wid, src, row = setup
    anchor = add(wid, 'Please confirm by tomorrow so I can book the tickets.', mine=True, age=20)
    original = ai.generate
    due = suggestions.utcnow() + timedelta(hours=4)
    def followup(*args, **kw):
        out = original(*args, **kw)
        label = next(m['label'] for m in kw['context']['messages'] if m['id'] == str(anchor))
        out.data['items'].append({'id': None, 'kind': 'follow_up', 'anchor': label,
            'title': 'Check before booking', 'reason': 'You requested an answer before booking the tickets.',
            'evidence': [label], 'due_at': due.isoformat()})
        return out
    monkeypatch.setattr(ai, 'generate', followup)
    rows = review(wid)
    follow = next(r for r in rows if r.kind == 'follow_up')
    assert follow.anchor_id == anchor and follow.due_at == due
    with db.session() as s:
        assert s.get(SuggestionReview, row.review_id).next_review_at == due


def test_source_edit_clears_old_text_and_identity_split_keeps_item(browser, setup):
    wid, src, row = setup
    suggestions.draft(wid, row.id)
    with db.session() as s:
        ws = Scoped(s, wid)
        source = ws.get(Source, src)
        ingest.invalidate(ws, [src])
        assert ws.get(Suggestion, row.id).draft == ''
        assert ws.get(Suggestion, row.id).reason == ''
        assert ws.get(Suggestion, row.id).state == 'invalidated'
    review(wid)
    with db.session() as s:
        ws = Scoped(s, wid)
        from app.models import PersonIdentifier
        p = ws.get(Person, row.person_id)
        ws.add(PersonIdentifier(id=uuid.uuid4(), person_id=p.id, kind='email', value='maya@example.test'))
        s.flush()
        ident = ws.first(ws.q(PersonIdentifier).where(PersonIdentifier.value == CHAT))
        new = ingest.unlink_identifier(ws, ident)
        assert ws.get(Suggestion, row.id).person_id == new.id


def test_open_promise_keeps_context_after_thirty_days(browser, setup):
    wid, src, row = setup
    with db.session() as s:
        s.get(Source, src).occurred_at = suggestions.utcnow() - timedelta(days=40)
    assert review(wid)[0].state == 'active'


def test_model_contract_cites_only_snapshot_labels(browser, setup, monkeypatch):
    import json
    wid, src, row = setup
    original = ai.generate
    base = copy.deepcopy(suggestions.SCHEMA)
    def check(*args, **kw):
        prompt = json.loads(kw['prompt'])
        labels = [m['label'] for m in kw['context']['messages']]
        assert all('id' not in m and 'version' not in m for m in prompt['messages'])
        assert 'person_id' not in prompt
        properties = kw['schema']['properties']
        item = properties['items']['items']['properties']
        assert item['anchor']['enum'] == labels
        assert item['evidence']['items']['enum'] == labels
        assert properties['resolved']['items']['properties']['evidence']['items']['enum'] == labels
        assert 'enum' not in item['title'] and 'enum' not in item['reason']
        return original(*args, **kw)
    monkeypatch.setattr(ai, 'generate', check)
    assert review(wid)[0].state == 'active'
    assert suggestions.SCHEMA == base


def test_quoted_evidence_still_rejected(browser, setup):
    wid, src, row = setup
    with db.session() as s:
        ctx = suggestions.snapshot(Scoped(s, wid), s.get(SuggestionReview, row.review_id))[0]
    from app.fake_generation import suggestion_review
    data = copy.deepcopy(suggestion_review(ctx))
    data['items'][0]['evidence'] = [ctx['messages'][0]['text']]
    with pytest.raises(ai.InvalidOutput, match='suggestions_evidence'):
        suggestions.validate(data, ctx)


def test_failure_notice_clears_when_review_is_retried(browser, setup):
    wid, src, row = setup
    now = suggestions.utcnow()
    with db.session() as s:
        # Two failed attempts for one conversation count once.
        s.get(SuggestionReview, row.review_id).checked_at = now - timedelta(hours=1)
        for i in range(2):
            s.add(Job(workspace_id=wid, kind='review_suggestions', idempotency_key=f'failed-{i}',
                      payload={'review_id': str(row.review_id)}, status='failed',
                      created_at=now - timedelta(minutes=10-i), updated_at=now - timedelta(minutes=9-i)))
    page = browser.get('/suggestions').text
    assert '1 conversation review(s) still need a retry' in page
    assert browser.post('/suggestions/review', {'csrf': browser.csrf()}).status_code == 303
    page = browser.get('/suggestions').text
    assert 'still need a retry' not in page and 'review(s) queued or running' in page
    from worker.main import drain
    drain(kinds=['review_suggestions'])
    assert 'still need a retry' not in browser.get('/suggestions').text


def test_failed_review_empty_state_is_not_all_clear(browser):
    browser.login('alex')
    wid = workspace_of('alex@example.test')
    add(wid, 'Can you join dinner?')
    with db.session() as s:
        rid = s.scalar(select(SuggestionReview.id))
        s.add(Job(workspace_id=wid, kind='review_suggestions', idempotency_key='failed',
                  payload={'review_id': str(rid)}, status='failed'))
    page = browser.get('/suggestions').text
    assert 'this inbox may be incomplete' in page
    assert 'There are no active recommendations in the reviewed history' not in page
