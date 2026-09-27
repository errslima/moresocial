"""M2: event-aware suggestions through the shared assistant context builder.

Synthetic Aloha fixture only (tests/assistant_fixtures.py): fictional people and messages.
"""
from datetime import timedelta
import copy
import json
import re
import uuid

import pytest
from sqlalchemy import select

from app import ai, assistant_context as ac, db, event_identity, events, ingest, suggestions
from app.models import Event, Source, Suggestion, SuggestionReview
from app.repo import Scoped
from assistant_fixtures import MICKAEL, aloha, at, email, item, wa
from conftest import workspace_of


@pytest.fixture
def world(browser):
    browser.login('alex')
    wid = workspace_of('alex@example.test')
    with db.session() as s:
        ids = aloha(Scoped(s, wid))
    return wid, ids


def review_id(wid, chat=MICKAEL):
    with db.session() as s:
        return s.scalar(select(SuggestionReview.id).where(SuggestionReview.workspace_id == wid,
                                                          SuggestionReview.conversation_id == chat))


def run_review(wid, chat=MICKAEL):
    assert suggestions.review_conversation(wid, review_id(wid, chat)) in ('reviewed', 'empty')
    with db.session() as s:
        return list(s.scalars(select(Suggestion).where(Suggestion.workspace_id == wid).order_by(Suggestion.created_at)))


def context(wid, chat=MICKAEL):
    with db.session() as s:
        ws = Scoped(s, wid)
        return suggestions.snapshot(ws, ws.get(SuggestionReview, review_id(wid, chat)))[0]


def rep_of(wid, eid):
    with db.session() as s:
        e = s.get(Event, eid)
        return e.canonical_event_id or e.id


def test_mickael_card_names_aloha_and_links_the_grouped_event(world, browser):
    wid, ids = world
    rows = run_review(wid)
    assert len(rows) == 1
    row = rows[0]
    assert row.title == 'Meet Mickaël at Aloha on Sunday'
    # The original member whose own evidence the suggestion cites, not just the representative.
    assert row.event_id == ids['lasergame_chat'] and row.event_link == 'auto'
    rep = rep_of(wid, ids['lasergame_chat'])
    assert rep == rep_of(wid, ids['booking'])
    page = browser.get('/suggestions').text
    assert f'/events/{rep}' in page and 'Lasergame – 2 rondes' in page and 'Aloha Amsterdam' in page
    detail = browser.get(f'/suggestions/{row.id}').text
    assert 'Linked because this suggestion shares messages with the event' in detail
    assert f'/sources/{ids["mail"][0]}' in detail  # schedule and place provenance


def test_context_keeps_booked_start_and_meeting_time_distinct(world):
    wid, ids = world
    ctx = context(wid)
    laser = next(e for e in ctx['events'] if e['title'] == 'Lasergame – 2 rondes')
    assert laser['booked_start'] == '16:15' and laser['end'] == '17:00' and laser['place'] == 'Aloha Amsterdam'
    assert laser['relation'] == 'shares messages with this conversation' and laser['recipient_attendance'] == 'confirmed'
    # Facts cite supplied labels only: the booking email as E1, chat messages by their M labels.
    labels = {m['label'] for m in ctx['messages']} | {m['label'] for m in ctx['event_messages']}
    assert laser['facts']['schedule'] == ['E1'] and set(sum(laser['facts'].values(), [])) <= labels
    assert any('16:00' in m['text'] for m in ctx['messages'])
    run_review(wid)
    with db.session() as s:
        booking = s.get(Event, rep_of(wid, ids['booking']))
        assert booking.starts_at.astimezone(events.TZ).strftime('%H:%M') == '16:15'  # nothing overwritten


def test_same_day_participant_event_is_offered_but_never_guessed(world, monkeypatch):
    wid, ids = world
    ctx = context(wid)
    dinner = next(e for e in ctx['events'] if e['title'] == 'Dinner at Olive')
    assert dinner['relation'] == 'the recipient takes part; not confirmed as related'
    original = ai.generate

    def wrong_event(wid_, **kw):
        out = original(wid_, **kw)
        for it in out.data['items']:
            it['event'] = dinner['label']  # the model claims the dinner
        return out
    monkeypatch.setattr(ai, 'generate', wrong_event)
    row = run_review(wid)[0]
    assert row.event_id == ids['lasergame_chat']  # evidence overlap decides, not the claim


def test_other_guests_messages_are_not_supplied(world):
    wid, ids = world
    ctx = context(wid)
    text = json.dumps(ctx['event_messages'] + ctx['messages'])
    assert 'my sister is visiting' not in text
    assert 'Count me in for dinner' in text  # Mickaël's own statement is his attendance evidence
    assert ctx['event_coverage']['omitted_private'] >= 1  # Sam's message is counted, not shown


def test_forged_event_label_rejected(world):
    wid, _ = world
    ctx = context(wid)
    from app.fake_generation import suggestion_review
    data = copy.deepcopy(suggestion_review(ctx))
    data['items'][0]['event'] = 'EV9'
    with pytest.raises(ai.InvalidOutput, match='suggestions_event'):
        suggestions.validate(data, ctx)
    prompt = json.loads(suggestions.review_prompt(ctx))
    assert '_events' not in prompt and all('id' not in m for m in prompt['event_messages'])
    schema = suggestions.review_schema(ctx)
    variant = schema['properties']['items']['items']['anyOf'][0]['properties']['event']
    assert variant['anyOf'][0]['enum'] == [e['label'] for e in ctx['events']]


def test_split_and_regroup_reevaluate_association(world, browser):
    wid, ids = world
    row = run_review(wid)[0]
    with db.session() as s:
        ws = Scoped(s, wid)
        rep = rep_of(wid, ids['booking'])
        event_identity.split(ws, rep, [[ids['booking']], [ids['lasergame_chat']]])
    with db.session() as s:
        again = s.get(Suggestion, row.id)
        # Still the original identity with the evidence; its card now shows that record alone.
        assert again.event_id == ids['lasergame_chat'] and again.stale
        assert s.get(SuggestionReview, again.review_id).next_review_at is not None
    assert 'Lasergamen' in browser.get(f'/suggestions/{row.id}').text
    rows = run_review(wid)
    assert rows[0].event_id == ids['lasergame_chat'] and not rows[0].stale
    assert rows[0].title == 'Meet Mickaël there on Sunday'  # no venue in that record alone


def newest(ws, title):
    """The record just created for this title (a new original identity)."""
    return ws.first(ws.q(Event).where(Event.title == title).order_by(Event.created_at.desc()))


def reschedule(ws, ids, hour):
    later = [email(ws, 'Change LA-4471', f'Your booking LA-4471 moved to {hour}:00, Aloha Amsterdam', suggestions.utcnow())]
    events.persist(ws, f'fixture-change-{hour}', later, [item('Lasergame – 2 rondes', at(ids['sunday'], hour), kind='reschedule',
                                                             place='Aloha Amsterdam', activity='Lasergame')])
    fresh = next(e for e in ws.all(ws.q(Event).order_by(Event.created_at.desc()))
                 if e.id not in (ids['booking'], ids['lasergame_chat'], ids['dinner']))
    event_identity.same(ws, [ids['booking'], fresh.id])


def test_reschedule_cancellation_and_conflict_refresh_the_action(world):
    wid, ids = world
    row = run_review(wid)[0]
    with db.session() as s:
        reschedule(Scoped(s, wid), ids, 17)
    with db.session() as s:
        assert s.get(Suggestion, row.id).stale
    laser = next(e for e in context(wid)['events'] if e['title'].startswith('Lasergame'))
    assert laser['booked_start'] == '17:00' and laser['earlier_times']
    assert not run_review(wid)[0].stale
    with db.session() as s:
        ws = Scoped(s, wid)
        cancel = [wa(ws, MICKAEL, 'The lasergame on Sunday is cancelled, the venue is closed.', suggestions.utcnow(), MICKAEL)]
        events.persist(ws, 'fixture-cancel', cancel, [item('Lasergamen', at(ids['sunday'], 17), kind='cancellation',
                                                           activity='Lasergame')])
        fresh = next(e for e in ws.all(ws.q(Event).order_by(Event.created_at.desc())) if e.title == 'Lasergamen'
                     and e.id != ids['lasergame_chat'])
        event_identity.same(ws, [ids['lasergame_chat'], fresh.id])
    laser = next(e for e in context(wid)['events'] if e['title'].startswith('Lasergame'))
    assert laser['status'] == 'cancelled'
    with db.session() as s:
        assert s.get(Suggestion, row.id).stale


def test_conflicting_confirmations_are_marked_needs_checking(world):
    wid, ids = world
    run_review(wid)
    with db.session() as s:
        ws = Scoped(s, wid)
        other = [email(ws, 'Booking LA-4471 (copy)', 'Lasergame 2 rondes, Sunday 15:00, Aloha Amsterdam',
                       suggestions.utcnow() - timedelta(hours=40))]
        events.persist(ws, 'fixture-conflict', other, [item('Lasergame – 2 rondes', at(ids['sunday'], 15), kind='booking',
                                                             place='Aloha Amsterdam', role='ticket_holder', activity='Lasergame')])
        fresh = next(e for e in ws.all(ws.q(Event).order_by(Event.created_at.desc()))
                     if e.id not in (ids['booking'], ids['lasergame_chat'], ids['dinner']))
        event_identity.same(ws, [ids['booking'], fresh.id])
    laser = next(e for e in context(wid)['events'] if e['title'].startswith('Lasergame'))
    assert laser['needs_checking'] is True
    row = run_review(wid)[0]
    suggestions.draft(wid, row.id)
    with db.session() as s:
        assert 'see you at' not in s.get(Suggestion, row.id).draft  # no guessed reconciliation


def test_user_correction_is_remembered(world, browser):
    wid, ids = world
    row = run_review(wid)[0]
    csrf = browser.csrf(f'/suggestions/{row.id}')
    page = browser.get(f'/suggestions/{row.id}').text
    assert str(ids['dinner']) in page  # eligible choice
    assert browser.post(f'/suggestions/{row.id}/event', {'csrf': csrf, 'event': str(ids['dinner'])}).status_code == 303
    run_review(wid)
    with db.session() as s:
        again = s.get(Suggestion, row.id)
        assert again.event_id == ids['dinner'] and again.event_link == 'user'
    assert browser.post(f'/suggestions/{row.id}/event', {'csrf': csrf, 'event': 'none'}).status_code == 303
    run_review(wid)
    with db.session() as s:
        again = s.get(Suggestion, row.id)
        assert again.event_id is None and again.event_link == 'user'
    assert browser.post(f'/suggestions/{row.id}/event', {'csrf': csrf, 'event': str(uuid.uuid4())}).status_code == 400


def test_excluding_booking_email_removes_venue_from_context(world):
    wid, ids = world
    row = run_review(wid)[0]
    with db.session() as s:
        ws = Scoped(s, wid)
        ingest.exclude(ws, ws.get(Source, ids['mail'][0]), 'object')
    with db.session() as s:
        assert s.get(Suggestion, row.id).stale
    ctx = context(wid)
    assert 'Aloha' not in json.dumps(ctx['events'] + ctx['event_messages'])
    rows = run_review(wid)
    assert rows[0].title == 'Meet Mickaël there on Sunday'


def test_ambiguous_overlap_stays_unlinked_and_visible(world, browser):
    wid, ids = world
    with db.session() as s:
        ws = Scoped(s, wid)
        # A second, separate plan citing the same question message.
        events.persist(ws, 'fixture-other', [ws.get(Source, ids['chat'][2])],
                       [item('Board games', at(ids['sunday'], 11), activity='Board games')])
    original = ai.generate

    def no_event(wid_, **kw):
        out = original(wid_, **kw)
        for it in out.data['items']:
            it['event'] = None
        return out
    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(ai, 'generate', no_event)
    try:
        row = run_review(wid)[0]
    finally:
        mp.undo()
    assert row.event_id is None
    assert 'Possibly related' in browser.get(f'/suggestions/{row.id}').text


def test_event_evidence_is_bounded(world):
    wid, ids = world
    with db.session() as s:
        ws = Scoped(s, wid)
        rows = [wa(ws, MICKAEL, f'Detail {n}: ' + 'x' * 1500, suggestions.utcnow() - timedelta(minutes=200 - n))
                for n in range(60)]
        events.persist(ws, 'fixture-big', rows, [item('Lasergamen', at(ids['sunday'], 16), activity='Lasergame',
                                                       evidence=[f'M{n + 1}' for n in range(60)])])
        rep = ws.get(Event, rep_of(wid, ids['booking']))
        # Force every message into provenance to exercise the bounds.
        rep.details = {**rep.details, 'provenance': {**rep.details['provenance'],
                                                     'schedule': [str(r.id) for r in rows]}}
        cands = ac.event_candidates(ws, source_ids=[r.id for r in rows], person_id=ids['mickael'])
        evc = ac.event_context(ws, cands, person_id=ids['mickael'], chat=MICKAEL)
    assert len(evc.messages) <= ac.EVENT_MESSAGES and sum(len(m['text']) for m in evc.messages) <= ac.EVENT_CHARS
    assert len(evc.events) <= ac.EVENT_CANDIDATES


def test_draft_uses_event_facts_and_goes_stale_when_event_changes(world, browser):
    wid, ids = world
    row = run_review(wid)[0]
    suggestions.draft(wid, row.id)
    with db.session() as s:
        d = s.get(Suggestion, row.id)
        assert 'Aloha Amsterdam' in d.draft and '16:15' in d.draft and d.draft_origin == 'generated'
    page = browser.get(f'/suggestions/{row.id}').text
    assert 'data-copy="suggestion-draft"' in page
    with db.session() as s:
        ws = Scoped(s, wid)
        rep = ws.get(Event, rep_of(wid, ids['booking']))
        events.set_dismissed(ws, rep, False)  # a no-op change keeps the draft current
    assert 'data-copy="suggestion-draft"' in browser.get(f'/suggestions/{row.id}').text
    with db.session() as s:
        reschedule(Scoped(s, wid), ids, 17)
    run_review(wid)
    page = browser.get(f'/suggestions/{row.id}').text
    assert 'data-draft-stale' in page and 'data-copy="suggestion-draft"' not in page
    with db.session() as s:
        assert '16:15' in s.get(Suggestion, row.id).draft  # the text itself is never replaced
    context_value = re.search(r'name="context" value="([^"]*)"', page).group(1)
    with db.session() as s:
        d = s.get(Suggestion, row.id)
        rev, rrev = d.draft_revision, s.get(SuggestionReview, d.review_id).revision
    csrf = browser.csrf(f'/suggestions/{row.id}')
    assert browser.post(f'/suggestions/{row.id}/draft/save', {'csrf': csrf, 'revision': rev, 'review_revision': rrev,
        'text': 'See you at Aloha at 17:00!', 'context': context_value}).status_code == 303
    assert 'data-copy="suggestion-draft"' in browser.get(f'/suggestions/{row.id}').text
