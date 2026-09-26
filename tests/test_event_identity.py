"""Event identity: grouping, corrections, resolver and field reconciliation.

Synthetic, fictional data only: people, venue, links and booking references are invented.
The fixture reproduces the shape of a confirmed production regression: a sparse invitation,
a reservation conversation whose time changed, and a confirmed two-round booking, with an
unrelated birthday nearby in the same chat and a separate dinner later that day.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
import uuid

import pytest
from sqlalchemy import text

from app import ai, db, event_identity as ei, event_resolver as er, events, ingest
from app.models import Event, EventIdentityDecision, EventMention, EventPairScan, Job, Person, Source
from app.repo import Scoped
from conftest import workspace_of

TZ = events.TZ
NOW = datetime(2031, 5, 16, 18, 0, tzinfo=timezone.utc)  # Friday evening, before the outing
ME = 'alex@example.test'


def local(day, hour, minute=0):
    return datetime(2031, 5, day, hour, minute, tzinfo=TZ)


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(events, 'now', lambda: NOW)


@pytest.fixture
def ws_id(browser, clock):
    browser.login('alex')
    return workspace_of(ME)


def msg(ws, conv, body, at, who=None, provider='whatsapp', title=None, to=()):
    who = who or ME
    rec = ingest.SourceRecord(provider=provider, provider_account=ME, provider_object_id=str(uuid.uuid4()),
        conversation_id=conv, kind='email' if provider == 'gmail' else 'message', title=title, body=body,
        occurred_at=at.astimezone(timezone.utc), from_me=who == ME,
        author=ingest.Participant('email', who, 'author', who.split('@')[0].title()),
        participants=[ingest.Participant('email', p, 'to', p.split('@')[0].title()) for p in to])
    return ingest.upsert(ws, rec, {ME}, ingest.IngestResult())


def person(ws, email):
    from app.models import PersonIdentifier
    return str(ws.first(ws.q(PersonIdentifier).where(PersonIdentifier.value == email)).person_id)


def item(title, activity, start, end=None, kind='plan', evidence=('M1',), role='invited', place='', participants=(),
         summary='', match_id=''):
    return {'title': title, 'activity': activity, 'starts_at': start.isoformat(), 'ends_at': end.isoformat() if end else '',
            'all_day': False, 'place': place, 'summary': summary or title, 'role': role, 'kind': kind, 'match_id': match_id,
            'evidence': list(evidence), 'participants': list(participants)}


def rep(ws, eid):
    e = ws.get(Event, eid)
    return e.canonical_event_id or e.id


def outing(ws):
    """Returns original event IDs: invite, reservation, booking, booking_chat, birthday, dinner."""
    group = 'group-chat'
    g = [msg(ws, group, "Who's in for Sunday afternoon? I'll book something for about 15:00", local(16, 10), to=['sam@example.test', 'kim@example.test', 'robin@example.test']),
         msg(ws, group, 'Reminder: my birthday drinks are Saturday at 20:00, everyone welcome!', local(16, 10, 5), 'robin@example.test'),
         msg(ws, group, "I'm in for Sunday!", local(16, 10, 10), 'sam@example.test'),
         msg(ws, group, 'Happy to come to the birthday on Saturday', local(16, 10, 12), 'kim@example.test')]
    sam, kim = person(ws, 'sam@example.test'), person(ws, 'kim@example.test')
    events.persist(ws, 'group', g, [
        item('Sunday booking', 'Booking, activity unspecified', local(18, 15), kind='invitation', role='organizer',
             evidence=['M1', 'M3'], participants=[{'person': sam, 'status': 'accepted', 'evidence': 'M3'}]),
        item("Robin's birthday drinks", 'Birthday drinks', local(17, 20), kind='invitation', evidence=['M2', 'M4'],
             participants=[{'person': kim, 'status': 'accepted', 'evidence': 'M4'}])])
    v = [msg(ws, 'venue-chat', 'Hi! Could we reserve for Sunday 16:00 for six people?', local(16, 11)),
         msg(ws, 'venue-chat', '16:00 is taken; 16:15 works for two rounds. Payment request: '
             'https://pay.example.test/payment/Q7X9K2M4 Route: https://maps.example.test/place/lumen-arena',
             local(16, 11, 30), 'desk@lumen.example.test'),
         msg(ws, 'venue-chat', "Perfect, we'll take 16:15. Paid!", local(16, 11, 40))]
    events.persist(ws, 'venue', v, [item('Sunday reservation with Lumen', 'Reservation', local(18, 16), local(18, 17),
                                         role='organizer', evidence=['M1', 'M2', 'M3'])])
    e = [msg(ws, 'mail-1', 'Lasergame – 2 rounds\n' + '\n ' * 400 + 'Sunday 18 May 2031\nRound 1 16:15-16:30\nRound 2 16:30-17:00\n'
             'Booking number: LA-58213\nPlease pay: https://pay.example.test/payment/Q7X9K2M4?utm_source=email',
             local(16, 12), 'bookings@lumen.example.test', provider='gmail', title='Booking confirmation LA-58213')]
    events.persist(ws, 'mail', e, [item('Lasergame – 2 rounds', 'Lasergame', local(18, 16, 15), local(18, 17), kind='booking',
                                        role='ticket_holder', place='Lumen Arena')])
    f = [msg(ws, 'friends', 'Lasergame this Sunday 16:15 at Lumen Arena, two rounds! Pay your share here: '
             'https://pay.example.test/payment/Q7X9K2M4', local(16, 13), to=['sam@example.test', 'kim@example.test']),
         msg(ws, 'friends', 'Count me in', local(16, 13, 5), 'kim@example.test'),
         msg(ws, 'friends', "Sorry, something came up, I can't make Sunday after all", local(16, 13, 30), 'sam@example.test')]
    events.persist(ws, 'friends', f, [item('Lasergame – two rounds', 'Lasergame', local(18, 16, 15), local(18, 17),
        role='organizer', place='Lumen Arena', evidence=['M1', 'M2', 'M3'],
        participants=[{'person': kim, 'status': 'accepted', 'evidence': 'M2'},
                      {'person': sam, 'status': 'declined', 'evidence': 'M3'}])])
    d = [msg(ws, 'dinner-chat', 'Dinner at Olive on Sunday at 19:30 after the lasergame?', local(16, 14), to=['sam@example.test']),
         msg(ws, 'dinner-chat', 'yes!', local(16, 14, 10), 'sam@example.test')]
    events.persist(ws, 'dinner', d, [item('Dinner at Olive', 'Dinner', local(18, 19, 30), role='organizer', place='Olive',
        evidence=['M1', 'M2'], participants=[{'person': sam, 'status': 'accepted', 'evidence': 'M2'}])])
    by_title = {m.data['title']: m.event_id for m in ws.all(ws.q(EventMention))}
    ids = dict(invite=by_title['Sunday booking'], reservation=by_title['Sunday reservation with Lumen'],
               booking=by_title['Lasergame – 2 rounds'], booking_chat=by_title['Lasergame – two rounds'],
               dinner=by_title['Dinner at Olive'], birthday=by_title["Robin's birthday drinks"])
    ids['sources'] = {k: [str(x.id) for x in rows] for k, rows in dict(g=g, v=v, e=e, f=f, d=d).items()}
    return ids


def sunday_cards(ws):
    return [c for c in events.cards(ws) if events.day(c.starts_at) == local(18, 12).date()]


def fake_calls(monkeypatch, respond=None):
    calls = []
    original = ai.generate

    def generate(wid, **kw):
        calls.append(kw['context']['purpose'])
        if respond:
            return respond(wid, kw)
        return original(wid, **kw)
    monkeypatch.setattr(ai, 'generate', generate)
    return calls


def set_mode(value):
    with db.session() as s:
        ei.set_mode(s, value, 'test')


# ---------------- M1: fixtures, schema

def test_regression_fixture_shows_current_duplication(ws_id):
    with db.session() as s:
        ws = Scoped(s, ws_id)
        ids = outing(ws)
        cards = sunday_cards(ws)
        # Invitation, reservation and booking group (booking email + its chat) are three cards.
        assert {c.id for c in cards} == {ids['invite'], ids['reservation'], rep(ws, ids['booking']), ids['dinner']}
        assert rep(ws, ids['booking_chat']) == rep(ws, ids['booking'])  # lexical match, now reversible
        d = ws.first(ws.q(EventIdentityDecision))
        assert d.origin == 'automatic' and d.via == 'lexical' and d.reasons == ['matching_title_time_place']


def test_migration_upgrade_keeps_populated_events(database, ws_id):
    from alembic import command
    from alembic.config import Config
    from conftest import ROOT
    with db.session() as s:
        ws = Scoped(s, ws_id)
        src = msg(ws, 'c', 'Dinner Sunday', local(16, 9))
        events.persist(ws, 'c', [src], [item('Dinner', 'Dinner', local(18, 19))])
        eid = ws.first(ws.q(Event)).id
    db.reset()
    cfg = Config(str(ROOT / 'alembic.ini'))
    cfg.attributes['database_url'] = database['runtime_url']
    command.downgrade(cfg, '0010')
    command.upgrade(cfg, 'head')
    with db.session() as s:
        ws = Scoped(s, ws_id)
        e = ws.get(Event, eid)
        assert e.canonical_event_id is None and e.title == 'Dinner' and e.details['source_count'] == 1
        assert len(ws.all(ws.q(EventMention))) == 1


def test_schema_constraints_isolation_and_account_deletion_cascade(ws_id, make_browser):
    from sqlalchemy.exc import IntegrityError
    from app import accounts
    other = make_browser()
    other.login('blake')
    blake = workspace_of('blake@example.test')
    with db.session() as s:
        ws = Scoped(s, ws_id)
        ids = outing(ws)
        ei.same(ws, [ids['invite'], ids['reservation']])
        bws = Scoped(s, blake)
        with pytest.raises(LookupError):
            ei.same(bws, [ids['invite'], ids['booking']])  # foreign IDs are invisible
    with db.session() as s:
        a, b = ei.pair(ids['invite'], ids['dinner'])
        s.add(EventIdentityDecision(id=uuid.uuid4(), workspace_id=blake, event_a=a, event_b=b, verdict='same', origin='user'))
        with pytest.raises(IntegrityError):  # composite FK: no cross-workspace pairs
            s.flush()
        s.rollback()
        s.add(EventIdentityDecision(id=uuid.uuid4(), workspace_id=ws_id, event_a=b, event_b=a, verdict='same', origin='user'))
        with pytest.raises(IntegrityError):  # normalized pair order
            s.flush()
        s.rollback()
    with db.session() as s:
        accounts.delete_account(s, ws_id)
    with db.session() as s:
        for table in ('event_identity_decisions', 'event_pair_scans', 'events', 'event_mentions'):
            assert s.execute(text(f'SELECT count(*) FROM {table} WHERE workspace_id = :w'), {'w': ws_id}).scalar() == 0


# ---------------- M2: grouping and corrections

def test_owner_correction_groups_three_records_with_exact_source_union(ws_id):
    with db.session() as s:
        ws = Scoped(s, ws_id)
        ids = outing(ws)
        before = set()
        for key in ('invite', 'reservation', 'booking'):
            before |= set(ei.source_union(ws, rep(ws, ids[key])))
        result = ei.same(ws, [ids['invite'], ids['reservation'], ids['booking']], via='operator')
        g = ws.get(Event, result.representative)
        assert g.id == ids['invite'] or g.canonical_event_id is None
        assert set(result.members) == {ids['invite'], ids['reservation'], ids['booking'], ids['booking_chat']}
        union = set(ei.source_union(ws, g.id))
        src = ids['sources']
        assert union == before == {src['g'][0], src['g'][2], *src['v'], *src['e'], *src['f']}
        assert src['g'][1] not in union and src['g'][3] not in union  # the birthday stays out
        assert g.details['source_count'] == len(union) == 9
        # Confirmed booking wins: activity, venue and both rounds.
        assert g.starts_at == local(18, 16, 15) and g.ends_at == local(18, 17)
        assert g.activity == 'Lasergame' and g.details['place'] == 'Lumen Arena' and g.title == 'Lasergame – 2 rounds'
        # Organizer established by the invitations although the email carries the booking.
        assert g.details['role'] == 'organizer'
        assert set(g.details['provenance']['role']) <= {src['g'][0], src['g'][2], *src['v'], *src['f']}
        assert g.details['provenance']['schedule'] == [src['e'][0]]
        # Earlier proposals stay as history, not competing cards.
        assert {h['starts_at'] for h in g.details['history']} == {local(18, 15).isoformat(), local(18, 16).isoformat()}
        # Attendance: one entry per person, latest explicit statement; birthday reply does not leak.
        sam, kim = person(ws, 'sam@example.test'), person(ws, 'kim@example.test')
        attendance = {p['person']: p['status'] for p in g.details['participants']}
        assert attendance == {sam: 'declined', kim: 'accepted'}
        cards = sunday_cards(ws)
        assert {c.id for c in cards} == {g.id, ids['dinner']}
        birthday = ws.get(Event, ids['birthday'])
        assert birthday.canonical_event_id is None and birthday in events.cards(ws)
        # Idempotent: repeating the correction writes nothing new.
        n = len(ws.all(ws.q(EventIdentityDecision)))
        ei.same(ws, [ids['invite'], ids['reservation'], ids['booking']])
        assert len(ws.all(ws.q(EventIdentityDecision))) == n


def test_transitive_same_never_overrides_keep_separate(ws_id):
    with db.session() as s:
        ws = Scoped(s, ws_id)
        ids = outing(ws)
        a, b, c = ids['invite'], ids['reservation'], ids['dinner']
        ei.separate(ws, a, c)
        ei.same(ws, [a, b])
        count = len(ws.all(ws.q(EventIdentityDecision)))
        with pytest.raises(ei.Conflict, match='Undo that correction first'):
            ei.same(ws, [b, c])
        assert len(ws.all(ws.q(EventIdentityDecision))) == count  # no partial update
        assert rep(ws, c) != rep(ws, a)


def test_automatic_edges_respect_different_across_groups(ws_id):
    with db.session() as s:
        ws = Scoped(s, ws_id)
        ids = outing(ws)
        a, b, c = ids['invite'], ids['reservation'], ids['dinner']
        ei.record_automatic(ws, a, c, 'different', via='resolver', applied=True, citations=[[ids['sources']['g'][0], 'h']])
        ei.record_automatic(ws, a, b, 'same', via='resolver', applied=True)
        ei.record_automatic(ws, b, c, 'same', via='resolver', applied=True)
        ei.refresh(ws)
        assert rep(ws, a) == rep(ws, b) != rep(ws, c)
        plan = ei.preview(ws)
        assert len(plan.rejected) == 1


def test_split_undo_redirects_and_new_evidence(ws_id, browser):
    with db.session() as s:
        ws = Scoped(s, ws_id)
        ids = outing(ws)
        r = ei.same(ws, [ids['invite'], ids['reservation'], ids['booking']])
        g = r.representative
        member = next(x for x in r.members if x != g)
    # Old member URL: temporary redirect to the current representative.
    resp = browser.get(f'/events/{member}')
    assert resp.status_code == 307 and resp.headers['location'].endswith(f'/events/{g}')
    with db.session() as s:
        ws = Scoped(s, ws_id)
        # New evidence arrives for the invitation after grouping; it stays with its identity.
        extra = msg(ws, 'group-chat', 'Sunday plan still on, see you all', local(16, 15))
        rows = [ws.get(Source, x) for x in (ids['sources']['g'] + [str(extra.id)])]
        birthday = ws.first(ws.q(EventMention).where(EventMention.event_id == ids['birthday']))
        sam = person(ws, 'sam@example.test')
        events.persist(ws, 'group', rows, [
            item('Sunday booking', 'Booking, activity unspecified', local(18, 15), kind='invitation', role='organizer',
                 evidence=['M1', 'M3', 'M5'], participants=[{'person': sam, 'status': 'accepted', 'evidence': 'M3'}]),
            dict(birthday.data, evidence=['M2', 'M4'], participants=[])])
        assert ws.get(Event, ids['invite']).id in {m.id for m in ei.members(ws, g)}
        assert str(extra.id) in ei.source_union(ws, g)
        parts = [[ids['invite']], [x for x in r.members if x != ids['invite']]]
        with pytest.raises(ei.Stale):
            ei.split(ws, g, parts, state='outdated')
        ei.split(ws, g, parts, state=ei.state_token(ws, g))
        invite = ws.get(Event, ids['invite'])
        assert invite.canonical_event_id is None and str(extra.id) in ei.source_union(ws, invite.id)
        assert rep(ws, ids['reservation']) == rep(ws, ids['booking']) != invite.id
        superseded = ws.all(ws.q(EventIdentityDecision).where(EventIdentityDecision.superseded_reason == 'split'))
        assert superseded
        # No immediate remerge: the resolver treats the pair as settled by the user.
        todo, stats = er.candidates(ws)
        assert all({c.a, c.b} != {invite.id, rep(ws, ids['booking'])} for c in todo)
        s.commit()
    for x in r.members:
        resp = browser.get(f'/events/{x}')
        assert resp.status_code in (200, 307)
    # Undo a single same edge: another path may still connect; the result reports it.
    with db.session() as s:
        ws = Scoped(s, ws_id)
        res = ei.same(ws, [ids['reservation'], ids['dinner']])
        d = ws.get(EventIdentityDecision, res.decisions[0])
        out = ei.undo(ws, d.id)
        assert not out.connected and rep(ws, ids['dinner']) == ids['dinner']
        with pytest.raises(LookupError):
            ei.undo(ws, d.id)


def test_dismissal_rules_through_merge_and_split(ws_id):
    with db.session() as s:
        ws = Scoped(s, ws_id)
        ids = outing(ws)
        ws.get(Event, ids['invite']).dismissed = True
        r = ei.same(ws, [ids['invite'], ids['reservation']])
        g = ws.get(Event, r.representative)
        assert events.group_dismissed(ws, g) and g not in events.cards(ws)
        assert g in events.cards(ws, 'dismissed')
        events.set_dismissed(ws, g, False)
        assert all(not m.dismissed for m in ei.members(ws, g.id))
        events.set_dismissed(ws, g, True)
        ei.split(ws, g.id, [[ids['invite']], [ids['reservation']]])
        # After a split each original keeps its last explicitly applied state.
        assert ws.get(Event, ids['invite']).dismissed and ws.get(Event, ids['reservation']).dismissed
        events.set_dismissed(ws, ws.get(Event, ids['reservation']), False)
        assert ws.get(Event, ids['reservation']) in events.cards(ws)
        # Rescan keeps dismissal on the original identity.
        rows = [ws.get(Source, x) for x in ids['sources']['g']]
        events.persist(ws, 'group', rows, [item('Sunday booking', 'Booking', local(18, 15), kind='invitation', evidence=['M1'])])
        assert ws.get(Event, ids['invite']).dismissed


def test_concurrent_corrections_are_serialized(ws_id):
    with db.session() as s:
        ids = outing(Scoped(s, ws_id))
    errors = []

    def run(fn):
        try:
            with db.session() as s:
                fn(Scoped(s, ws_id))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    t1 = threading.Thread(target=run, args=(lambda ws: ei.same(ws, [ids['invite'], ids['reservation']]),))
    t2 = threading.Thread(target=run, args=(lambda ws: ei.separate(ws, ids['invite'], ids['reservation']),))
    t1.start(); t2.start(); t1.join(); t2.join()
    with db.session() as s:
        ws = Scoped(s, ws_id)
        current = [d for d in ei.decisions(ws) if d.origin == 'user']
        # Either order is consistent: one user decision for the pair, grouping follows it.
        assert len({(d.event_a, d.event_b) for d in current}) == len(current)
        together = rep(ws, ids['invite']) == rep(ws, ids['reservation'])
        latest = [d for d in current if {d.event_a, d.event_b} == {ids['invite'], ids['reservation']}]
        assert len(latest) == 1 and together == (latest[0].verdict == 'same')
        assert all(isinstance(e, (ei.Conflict, ValueError)) for e in errors)


# ---------------- M3: resolver

def test_resolver_links_strong_reference_and_leaves_sparse_invitation_uncertain(ws_id, monkeypatch):
    calls = fake_calls(monkeypatch)
    set_mode('apply')
    with db.session() as s:
        ids = outing(Scoped(s, ws_id))
    assert er.run(ws_id) == 'resolved'
    with db.session() as s:
        ws = Scoped(s, ws_id)
        # Different titles, one shared payment reference: grouped without lexical overlap.
        assert rep(ws, ids['reservation']) == rep(ws, ids['booking'])
        # The sparse invitation stays separate and reviewable; dinner and birthday stay separate.
        assert rep(ws, ids['invite']) not in {rep(ws, ids['booking']), rep(ws, ids['dinner'])}
        assert rep(ws, ids['dinner']) != rep(ws, ids['booking'])
        assert ws.get(Event, ids['birthday']).canonical_event_id is None
        review = er.review_pairs(ws)
        pairs = [{p['a'].id, p['b'].id} for p in review]
        assert {rep(ws, ids['invite']), rep(ws, ids['booking'])} in pairs
        assert {ids['invite'], ids['dinner']} not in pairs  # same day and people, hours apart: not a duplicate
        verdicts = {d.verdict for d in ws.all(ws.q(EventIdentityDecision).where(EventIdentityDecision.via == 'resolver'))}
        assert verdicts == {'same', 'uncertain', 'different'}
        first = len(calls)
        union = ei.source_union(ws, rep(ws, ids['booking']))
    # Unchanged rerun and backfill: no repeated model call, same grouping and sources.
    er.run(ws_id)
    with db.session() as s:
        assert er.bootstrap(s) == 1
    er.run(ws_id, payload={'events': [str(ids['invite'])]})
    assert len(calls) == first and 'events' not in calls
    with db.session() as s:
        ws = Scoped(s, ws_id)
        assert ei.source_union(ws, rep(ws, ids['booking'])) == union


def test_observe_mode_stores_without_grouping_and_apply_promotes(ws_id, monkeypatch):
    fake_calls(monkeypatch)
    with db.session() as s:
        assert ei.mode(s) == 'observe'
        ids = outing(Scoped(s, ws_id))
    er.run(ws_id)
    with db.session() as s:
        ws = Scoped(s, ws_id)
        assert rep(ws, ids['reservation']) != rep(ws, ids['booking'])
        d = ws.first(ws.q(EventIdentityDecision).where(EventIdentityDecision.via == 'resolver', EventIdentityDecision.verdict == 'same'))
        assert d and not d.applied
        assert any({p['a'].id, p['b'].id} == {ids['reservation'], rep(ws, ids['booking'])} for p in er.review_pairs(ws))
    with db.session() as s:
        assert ei.set_mode(s, 'apply', 'test') == [ws_id]
    er.run(ws_id)
    with db.session() as s:
        ws = Scoped(s, ws_id)
        # Promoted, not paid for again (other pairs are re-evaluated: their group changed).
        a, b = ei.pair(ids['reservation'], rep(ws, ids['booking']))
        assert len(ws.all(ws.q(EventIdentityDecision).where(EventIdentityDecision.event_a == a,
                                                            EventIdentityDecision.event_b == b))) == 1
    with db.session() as s:
        ws = Scoped(s, ws_id)
        assert rep(ws, ids['reservation']) == rep(ws, ids['booking'])
    set_mode('off')
    assert er.run(ws_id) == 'off'
    with db.session() as s:
        ws = Scoped(s, ws_id)
        assert rep(ws, ids['reservation']) == rep(ws, ids['booking'])  # off keeps existing groups


def test_server_validation_rejects_unsupported_same_verdicts(ws_id, monkeypatch):
    """Only same date/time/organizer, or a reused map link: the model's same is not enough."""
    set_mode('apply')

    def say_same(wid, kw):
        ctx = kw['context']
        a = [m['label'] for m in ctx['messages'] if m['label'][0] in 'AS'][:1]
        b = [m['label'] for m in ctx['messages'] if m['label'][0] in 'BS'][:1]
        from types import SimpleNamespace
        return SimpleNamespace(data={'verdict': 'same', 'evidence_a': a, 'evidence_b': b, 'supporting': ['same_time'],
                                     'conflicting': [], 'explanation': 'x'})
    fake_calls(monkeypatch, say_same)
    with db.session() as s:
        ws = Scoped(s, ws_id)
        one = [msg(ws, 'x1', 'Lasergame Sunday 16:00! Route https://maps.app.goo.gl/Venue123abc', local(16, 9), to=['sam@example.test'])]
        two = [msg(ws, 'x2', 'Booked our own lasergame Sunday 16:30, route https://maps.app.goo.gl/Venue123abc', local(16, 9, 30),
                   to=['sam@example.test'])]
        events.persist(ws, 'x1', one, [item('Lasergame with team', 'Lasergame', local(18, 16), role='organizer', place='Arena')])
        events.persist(ws, 'x2', two, [item('Lasergame with cousins', 'Lasergame', local(18, 16, 30), role='organizer', place='Arena')])
        a = ws.first(ws.q(EventMention).where(EventMention.scan_key == 'x1')).event_id
        b = ws.first(ws.q(EventMention).where(EventMention.scan_key == 'x2')).event_id
    er.run(ws_id)
    with db.session() as s:
        ws = Scoped(s, ws_id)
        assert rep(ws, a) != rep(ws, b)
        d = ws.first(ws.q(EventIdentityDecision).where(EventIdentityDecision.via == 'resolver'))
        assert d.verdict == 'uncertain' and 'same_without_verified_link' in d.reasons


def test_explicit_reschedule_to_another_day_keeps_one_identity_with_history(ws_id, monkeypatch):
    set_mode('apply')

    def respond(wid, kw):
        from types import SimpleNamespace
        ctx = kw['context']
        shared = [m['label'] for m in ctx['messages'] if m['label'].startswith('S')]
        return SimpleNamespace(data={'verdict': 'same', 'evidence_a': shared, 'evidence_b': shared,
            'supporting': ['explicit_reschedule'], 'conflicting': ['different_date'], 'explanation': 'moved'})
    fake_calls(monkeypatch, respond)
    with db.session() as s:
        ws = Scoped(s, ws_id)
        m1 = msg(ws, 'c', 'Bowling Saturday 15:00?', local(15, 9), to=['sam@example.test'])
        events.persist(ws, 'w0', [m1], [item('Bowling', 'Bowling', local(17, 15), role='organizer')])
        m2 = msg(ws, 'c', "Let's move bowling to Sunday 16:00 instead", local(16, 9), to=['sam@example.test'])
        events.persist(ws, 'w1', [m1, m2], [item('Bowling', 'Bowling', local(18, 16), kind='reschedule', role='organizer',
                                                 evidence=['M1', 'M2'])])
        ids = sorted({m.event_id for m in ws.all(ws.q(EventMention))}, key=str)
        assert len(ids) == 2  # different day: a new original record, linked by the shared message
    er.run(ws_id)
    with db.session() as s:
        ws = Scoped(s, ws_id)
        assert rep(ws, ids[0]) == rep(ws, ids[1])
        g = ws.get(Event, rep(ws, ids[0]))
        assert g.starts_at == local(18, 16)
        assert [h['starts_at'] for h in g.details['history']] == [local(17, 15).isoformat()]


def test_new_evidence_elsewhere_reconsiders_without_reextraction(ws_id, monkeypatch):
    calls = fake_calls(monkeypatch)
    set_mode('apply')
    with db.session() as s:
        ws = Scoped(s, ws_id)
        chat = [msg(ws, 'chat', 'Karting Sunday 14:00 with the team?', local(16, 9), to=['sam@example.test'])]
        events.persist(ws, 'chat', chat, [item('Team outing', 'Outing', local(18, 14), role='organizer')])
        mail = [msg(ws, 'mail', 'Your go-kart booking KRT-99812 is confirmed', local(16, 10), 'go@kart.example.test', provider='gmail')]
        events.persist(ws, 'mail', mail, [item('Go-kart session', 'Karting', local(18, 14, 30), kind='booking', role='ticket_holder')])
    er.run(ws_id)
    with db.session() as s:
        ws = Scoped(s, ws_id)
        a = ws.first(ws.q(EventMention).where(EventMention.scan_key == 'chat')).event_id
        b = ws.first(ws.q(EventMention).where(EventMention.scan_key == 'mail')).event_id
        assert rep(ws, a) != rep(ws, b)
        # The chat later quotes the booking reference: the pair's fingerprint changes.
        rows = [ws.get(Source, ws.first(ws.q(EventMention).where(EventMention.scan_key == 'chat')).evidence[0]),
                msg(ws, 'chat', 'Booked! Reference KRT-99812', local(16, 11))]
        events.persist(ws, 'chat', rows, [item('Team outing', 'Outing', local(18, 14), role='organizer', evidence=['M1', 'M2'])])
    er.run(ws_id)
    assert 'events' not in calls
    with db.session() as s:
        ws = Scoped(s, ws_id)
        assert rep(ws, a) == rep(ws, b)


def test_user_correction_during_slow_model_call_wins(ws_id, monkeypatch):
    set_mode('apply')
    with db.session() as s:
        ids = outing(Scoped(s, ws_id))
    original = ai.generate

    def slow(wid, **kw):
        out = original(wid, **kw)
        if out.data['verdict'] == 'same':
            with db.session() as s2:
                ws2 = Scoped(s2, wid)
                ei.separate(ws2, ids['reservation'], ids['booking'])
        return out
    monkeypatch.setattr(ai, 'generate', slow)
    er.run(ws_id)
    with db.session() as s:
        ws = Scoped(s, ws_id)
        assert rep(ws, ids['reservation']) != rep(ws, ids['booking'])
        assert not ws.first(ws.q(EventIdentityDecision).where(EventIdentityDecision.via == 'resolver',
                                                                 EventIdentityDecision.verdict == 'same'))
        assert ws.first(ws.q(Job).where(Job.kind == 'resolve_events', Job.status == 'queued'))


def test_simultaneous_resolver_jobs_are_consistent(ws_id, monkeypatch):
    set_mode('apply')
    with db.session() as s:
        ids = outing(Scoped(s, ws_id))
    threads = [threading.Thread(target=er.run, args=(ws_id,)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with db.session() as s:
        ws = Scoped(s, ws_id)
        active = [d for d in ei.decisions(ws) if d.origin == 'automatic']
        assert len({(d.event_a, d.event_b) for d in active}) == len(active)
        assert rep(ws, ids['reservation']) == rep(ws, ids['booking'])
        assert rep(ws, ids['invite']) != rep(ws, ids['booking'])


def test_malformed_timeout_and_overflow_are_safe(ws_id, monkeypatch):
    set_mode('apply')
    with db.session() as s:
        ids = outing(Scoped(s, ws_id))
    calls = []

    def malformed(wid, **kw):
        calls.append(1)
        raise ai.InvalidOutput('bad json')
    monkeypatch.setattr(ai, 'generate', malformed)
    for _ in range(3):
        er.run(ws_id)
    with db.session() as s:
        ws = Scoped(s, ws_id)
        assert not [d for d in ei.decisions(ws) if d.via == 'resolver']
        pairs = len(ws.all(ws.q(EventPairScan)))
        assert pairs and len(calls) == pairs * er.INVALID_LIMIT  # bounded retries per unchanged pair
        assert rep(ws, ids['reservation']) != rep(ws, ids['booking'])

    def timeout(wid, **kw):
        raise ai.AIUnavailable('timeout')
    monkeypatch.setattr(ai, 'generate', timeout)
    with db.session() as s:
        ws = Scoped(s, ws_id)
        ws.delete(EventPairScan)
    with pytest.raises(ai.AIUnavailable):
        er.run(ws_id)
    with db.session() as s:
        ws = Scoped(s, ws_id)
        assert not [d for d in ei.decisions(ws) if d.via == 'resolver']
    # Oversized candidate set: 20 evaluated, the rest continue in a new job.
    monkeypatch.setattr(ai, 'generate', lambda wid, **kw: __import__('types').SimpleNamespace(
        data={'verdict': 'uncertain', 'evidence_a': [], 'evidence_b': [], 'supporting': [], 'conflicting': [], 'explanation': ''}))
    with db.session() as s:
        ws = Scoped(s, ws_id)
        for i in range(8):
            src = msg(ws, f'extra{i}', f'Party number {i} on Sunday', local(16, 8, i), to=['sam@example.test'])
            events.persist(ws, f'extra{i}', [src], [item(f'Party {i}', 'Party', local(18, 16, i), role='organizer')])
        ws.delete(Job)
    monkeypatch.setattr(er, 'MAX_PAIRS', 5)
    er.run(ws_id)
    with db.session() as s:
        ws = Scoped(s, ws_id)
        assert len(ws.all(ws.q(EventPairScan))) == 5
        assert ws.first(ws.q(Job).where(Job.kind == 'resolve_events', Job.idempotency_key.like('resolve:%')))


def test_packet_is_bounded_and_citations_are_validated(ws_id):
    with db.session() as s:
        ws = Scoped(s, ws_id)
        ids = outing(ws)
        many = [msg(ws, 'big', ('Lasergame details ' * 80) + str(i), local(16, 7, i % 60) + timedelta(seconds=i)) for i in range(60)]
        events.persist(ws, 'big', many, [item('Lasergame logistics', 'Lasergame', local(18, 16, 15), role='organizer',
                                              evidence=[f'M{i+1}' for i in range(40)])])
        big = ws.first(ws.q(EventMention).where(EventMention.scan_key == 'big')).event_id
        groups = er.load_groups(ws, [ws.get(Event, rep(ws, big)), ws.get(Event, rep(ws, ids['booking']))])
        g1, g2 = groups[rep(ws, big)], groups[rep(ws, ids['booking'])]
        packet, labels = er.build_packet(ws, g1, g2, er.local_signals(g1, g2))
        assert len(packet['messages']) + len(packet['context']) <= er.MAX_MESSAGES
        assert sum(len(m['text']) for m in packet['messages']) <= er.MAX_CHARS
        assert all(labels[m['label']] in g1.sources or labels[m['label']] in g2.sources for m in packet['messages'])
        a = next(l for l in labels if l[0] == 'A')
        b = next(l for l in labels if l[0] == 'B')
        ok = {'verdict': 'different', 'evidence_a': [a], 'evidence_b': [b], 'supporting': [], 'conflicting': ['separate_bookings'], 'explanation': ''}
        assert er.validate(ok, packet, labels, g1, g2).verdict == 'different'
        assert er.validate(dict(ok, evidence_a=['A999']), packet, labels, g1, g2).verdict is None
        assert er.validate(dict(ok, evidence_a=[b]), packet, labels, g1, g2).verdict is None  # wrong side
        assert er.validate(dict(ok, evidence_a=[]), packet, labels, g1, g2).verdict == 'uncertain'
        assert er.validate({'verdict': 'maybe'}, packet, labels, g1, g2).verdict is None
        assert er.validate(dict(ok, verdict='same'), packet, labels, g1, g2).verdict == 'uncertain'


def test_url_normalization_is_conservative():
    n = er.normalize_url
    assert n('https://WWW.Pay.example.test/payment/Q7X9K2M4?utm_source=x#top')[0] == n('https://pay.example.test/payment/Q7X9K2M4')[0]
    assert n('https://pay.example.test/payment/Q7X9K2M4')[0] != n('https://pay.example.test/payment/Q7X9K2M5')[0]
    assert n('https://shop.example.test/order?id=A123456&utm_medium=m')[0] != n('https://shop.example.test/order?id=A123457')[0]
    assert n('https://pay.example.test/payment/Q7X9K2M4')[1] == 'payment'
    assert n('https://maps.app.goo.gl/Venue123abc')[1] == 'map'
    assert n('https://lumen.example.test/')[1] == 'homepage'


# ---------------- M4: invalidation, people, reinstatement

def test_exclusion_withdraws_automatic_groups_immediately(ws_id, monkeypatch):
    fake_calls(monkeypatch)
    set_mode('apply')
    with db.session() as s:
        ids = outing(Scoped(s, ws_id))
    er.run(ws_id)
    with db.session() as s:
        ws = Scoped(s, ws_id)
        assert rep(ws, ids['reservation']) == rep(ws, ids['booking'])
        email = ws.get(Source, ids['sources']['e'][0])
        ingest.exclude(ws, email, 'object')
        assert rep(ws, ids['reservation']) != rep(ws, ids['booking'])
        assert ws.get(Event, ids['booking']).status == 'unsupported'
        for c in events.cards(ws):
            assert 'LA-58213' not in str(c.details) and c.title != 'Lasergame – 2 rounds' or c.id != ids['booking']
        withdrawn = ws.all(ws.q(EventIdentityDecision).where(EventIdentityDecision.superseded_reason == 'source_changed'))
        assert withdrawn and all(not d.active for d in withdrawn)
        assert ids['sources']['e'][0] not in ei.source_union(ws, rep(ws, ids['booking_chat']))


def test_user_group_survives_exclusion_without_showing_removed_content(ws_id):
    with db.session() as s:
        ws = Scoped(s, ws_id)
        ids = outing(ws)
        r = ei.same(ws, [ids['invite'], ids['reservation'], ids['booking']])
        ingest.exclude(ws, ws.get(Source, ids['sources']['e'][0]), 'object')
        g = ws.get(Event, r.representative)
        assert rep(ws, ids['booking']) == g.id  # the user's identity link is retained
        assert ids['sources']['e'][0] not in ei.source_union(ws, g.id)
        # The removed email no longer supports anything: the lexical link citing it is withdrawn,
        # so the schedule falls back to the reservation chat until the resolver reconsiders.
        assert rep(ws, ids['booking_chat']) != g.id
        assert g.starts_at == local(18, 16) and g.details['place'] == ''
        assert all(ids['sources']['e'][0] not in v for v in g.details['provenance'].values())


def test_linked_people_deduplicate_attendance_and_change_fingerprint(ws_id):
    with db.session() as s:
        ws = Scoped(s, ws_id)
        a = [msg(ws, 'one', 'Picnic Sunday 12:00?', local(16, 9), to=['sam@example.test']),
             msg(ws, 'one', 'yes', local(16, 9, 5), 'sam@example.test')]
        b = [msg(ws, 'two', 'Picnic Sunday at noon, bring food', local(16, 10), to=['sam.work@example.test']),
             msg(ws, 'two', "sorry can't", local(16, 11), 'sam.work@example.test')]
        p1, p2 = person(ws, 'sam@example.test'), person(ws, 'sam.work@example.test')
        events.persist(ws, 'one', a, [item('Picnic', 'Picnic', local(18, 12), evidence=['M1', 'M2'],
                                           participants=[{'person': p1, 'status': 'accepted', 'evidence': 'M2'}])])
        events.persist(ws, 'two', b, [item('Picnic in the park', 'Picnic', local(18, 12), evidence=['M1', 'M2'],
                                           participants=[{'person': p2, 'status': 'declined', 'evidence': 'M2'}])])
        g = ws.first(ws.q(Event).where(Event.canonical_event_id.is_(None)))
        assert len(g.details['participants']) == 2
        reps = ws.all(ws.q(Event))
        groups = er.load_groups(ws, [e for e in reps if e.canonical_event_id is None])
        before = {k: v.digest for k, v in groups.items()}
        ingest.link_people(ws, ws.get(Person, p1), ws.get(Person, p2))
        events.recompute(ws, g)
        assert [(p['person'], p['status']) for p in g.details['participants']] == [(p1, 'declined')]
        groups = er.load_groups(ws, [e for e in ws.all(ws.q(Event)) if e.canonical_event_id is None])
        assert {k: v.digest for k, v in groups.items()} != before


def test_cancellation_and_reinstatement():
    from types import SimpleNamespace as M
    base = {'title': 'Dinner', 'activity': 'Dinner', 'starts_at': local(18, 19).isoformat(), 'provider': 'whatsapp', 'rank': 1}
    plan = M(event_id=1, evidence=['s1'], data=dict(base, kind='plan', at='2031-05-15T10:00'))
    cancel = M(event_id=1, evidence=['s2'], data=dict(base, kind='cancellation', rank=5, at='2031-05-15T11:00'))
    back = M(event_id=1, evidence=['s3'], data=dict(base, kind='plan', at='2031-05-15T12:00'))
    assert events.project([plan, cancel])[2] == 'cancelled'
    assert events.project([plan, cancel, back])[2] == 'planning'
    booked = M(event_id=1, evidence=['s4'], data=dict(base, kind='booking', rank=4, at='2031-05-15T09:00'))
    other = M(event_id=1, evidence=['s5'], data=dict(base, kind='booking', rank=4, at='2031-05-15T09:30',
                                                      starts_at=local(18, 20).isoformat()))
    assert events.project([booked, other])[1].get('needs_clarification')


# ---------------- M5: review UI routes

def test_review_same_keep_separate_and_detail_controls(ws_id, browser, make_browser, monkeypatch):
    fake_calls(monkeypatch)
    with db.session() as s:
        ids = outing(Scoped(s, ws_id))
    er.run(ws_id)  # observe mode: suggestions only
    page = browser.get('/events')
    assert page.status_code == 200 and 'Possible duplicates' in page.text and 'Same event' in page.text
    assert 'Both mention the same payment request' in page.text
    import re
    forms = re.findall(r'action="[^"]*/events/identity/(same|separate)".*?name="a" value="([^"]+)".*?name="b" value="([^"]+)".*?name="state" value="([^"]+)"', page.text)
    assert forms
    with db.session() as s:
        ws = Scoped(s, ws_id)
        target = {ids['reservation'], rep(ws, ids['booking'])}
    action, a, b, state = next(f for f in forms if f[0] == 'same' and {uuid.UUID(f[1]), uuid.UUID(f[2])} == target)
    # CSRF and workspace scoping.
    assert browser.post('/events/identity/same', {'a': a, 'b': b, 'state': state}).status_code == 403
    other = make_browser(); other.login('blake')
    assert other.post('/events/identity/same', {'csrf': other.csrf(), 'a': a, 'b': b, 'state': state}).status_code == 404
    # A stale state token is refused without mutation.
    assert browser.post('/events/identity/same', {'csrf': browser.csrf(), 'a': a, 'b': b, 'state': 'old'}).status_code == 409
    r = browser.post('/events/identity/same', {'csrf': browser.csrf(), 'a': a, 'b': b, 'state': state})
    assert r.status_code == 303
    with db.session() as s:
        ws = Scoped(s, ws_id)
        assert rep(ws, ids['reservation']) == rep(ws, ids['booking'])
        g = rep(ws, ids['booking'])
    detail = browser.get(f'/events/{g}')
    assert 'Is this one event?' in detail.text and 'Separate…' in detail.text and 'Grouping history' in detail.text
    assert 'Earlier times mentioned' in detail.text
    # Missed match: choose any detected event (identity correction, not creation).
    token = re.search(r'name="state" value="([^"]+)"', detail.text).group(1)
    r = browser.post('/events/identity/same', {'csrf': browser.csrf(), 'a': str(g), 'b': str(ids['invite']),
                                               'state': token, 'back': 'detail'})
    assert r.status_code == 303
    with db.session() as s:
        ws = Scoped(s, ws_id)
        assert rep(ws, ids['invite']) == g
    # Split preview shows affected sources, then separates.
    preview = browser.get(f'/events/{g}/split?member={ids["invite"]}')
    assert preview.status_code == 200 and 'Becomes its own event' in preview.text and 'Stays in this event' in preview.text
    token = re.search(r'name="state" value="([^"]+)"', preview.text).group(1)
    assert browser.post(f'/events/{g}/split', {'csrf': browser.csrf(), 'member': str(ids['invite']), 'state': token}).status_code == 303
    with db.session() as s:
        ws = Scoped(s, ws_id)
        assert rep(ws, ids['invite']) == ids['invite'] != rep(ws, ids['booking'])
        d = ws.first(ws.q(EventIdentityDecision).where(EventIdentityDecision.origin == 'user', EventIdentityDecision.verdict == 'same',
                                                       EventIdentityDecision.active.is_(True)))
    # Undo preview shows the actual result before confirming.
    undo = browser.get(f'/events/{g}/undo?decision={d.id}')
    assert undo.status_code == 200 and 'Undo this correction?' in undo.text
    token = re.search(r'name="state" value="([^"]+)"', undo.text).group(1)
    assert browser.post(f'/events/{g}/undo', {'csrf': browser.csrf(), 'decision': str(d.id), 'state': token}).status_code == 303
    # Keep separate from the review list.
    with db.session() as s:
        ws = Scoped(s, ws_id)
        ra, rb = rep(ws, ids['reservation']), rep(ws, ids['booking'])
        state = ei.pair_token(ws, ra, rb)
    if ra != rb:
        assert browser.post('/events/identity/separate', {'csrf': browser.csrf(), 'a': str(ra), 'b': str(rb), 'state': state}).status_code == 303
        with db.session() as s:
            ws = Scoped(s, ws_id)
            assert not any({p['a'].id, p['b'].id} == {ra, rb} for p in er.review_pairs(ws))
    # Contradicting an earlier user correction is refused with an explanation.
    with db.session() as s:
        ws = Scoped(s, ws_id)
        ei.separate(ws, ids['invite'], ids['dinner'])
        state = ei.pair_token(ws, ids['invite'], ids['dinner'])
    r = browser.post('/events/identity/same', {'csrf': browser.csrf(), 'a': str(ids['invite']), 'b': str(ids['dinner']), 'state': state})
    assert r.status_code == 409 and 'Undo that correction first' in r.text


def test_operator_tool_dry_run_then_apply(ws_id, capsys):
    import json
    from app import event_reconcile
    with db.session() as s:
        ids = outing(Scoped(s, ws_id))
    listed = event_reconcile.main(['list', '--email', ME, '--date', '2031-05-18'])
    assert {e['id'] for e in listed['events']} >= {str(ids['invite']), str(ids['reservation']), str(ids['booking'])}
    out = capsys.readouterr().out
    assert 'Q7X9K2M4' not in out and 'Payment request' not in out  # no source bodies or links
    events_arg = ','.join(str(ids[k]) for k in ('invite', 'reservation', 'booking'))
    dry = event_reconcile.main(['same', '--email', ME, '--events', events_arg])
    assert len({g['representative'] for g in dry['before']}) == 3
    assert len({g['representative'] for g in dry['after']}) == 1 and dry['after'][0]['sources'] == 9
    assert dry['after'][0]['starts_at'] == local(18, 16, 15).isoformat()
    with db.session() as s:
        ws = Scoped(s, ws_id)
        assert rep(ws, ids['invite']) != rep(ws, ids['booking'])  # dry run changed nothing
        assert not ws.first(ws.q(EventIdentityDecision).where(EventIdentityDecision.origin == 'user'))
    applied = event_reconcile.main(['same', '--email', ME, '--events', events_arg, '--apply'])
    assert applied['after'] == dry['after']
    with db.session() as s:
        ws = Scoped(s, ws_id)
        assert rep(ws, ids['invite']) == rep(ws, ids['booking'])
        assert {d.via for d in ws.all(ws.q(EventIdentityDecision).where(EventIdentityDecision.origin == 'user'))} == {'operator'}
    assert event_reconcile.main(['recompute', '--email', ME])['changed'] == []  # already current
    report = event_reconcile.main(['report', '--email', ME])['report']
    assert report['mode'] == 'observe' and report['multi_member_groups'] == 1
    cands = event_reconcile.main(['candidates', '--email', ME])
    assert all('signals' in c for c in cands['candidates'])
    assert event_reconcile.main(['mode', '--value', 'apply'])['after'] == 'apply'
    with db.session() as s:
        assert ei.mode(s) == 'observe'  # mode change without --apply is rolled back
    json.loads(json.dumps(report))


def test_template_text_and_date_only_pairs_are_not_suggested(ws_id, monkeypatch):
    """Calendar/email boilerplate is not copied invitation text; a reference shared by many
    separate records is a template; same-day pairs need model-supported evidence to be listed."""
    fake_calls(monkeypatch)
    boiler = ('This event was created automatically from an email you received in your mailbox. '
              'To manage these events, open your settings.')
    with db.session() as s:
        ws = Scoped(s, ws_id)
        trips = []
        for i, (day, title) in enumerate([(20, 'Flight out'), (22, 'Hotel stay'), (25, 'Flight back')]):
            src = msg(ws, f'cal{i}', boiler + f' Reservation {title}', local(15, 9, i), provider='calendar', title=title)
            events.persist(ws, f'cal{i}', [src], [item(title, 'Travel', local(day, 10), kind='calendar')])
            trips.append(ws.first(ws.q(EventMention).where(EventMention.scan_key == f'cal{i}')).event_id)
        assert all(k[0] != 'copied_text' for k in er.signals_of(src).values())
        chats = []
        for i in range(4):  # the same long line forwarded to four unrelated plans
            src = msg(ws, f'chat{i}', 'Please remember to bring your own water bottle and a small towel to every session this season',
                      local(15, 10, i))
            events.persist(ws, f'chat{i}', [src], [item(f'Session {i}', f'Sport{i}', local(20 + i, 18), role='organizer')])
            chats.append(ws.first(ws.q(EventMention).where(EventMention.scan_key == f'chat{i}')).event_id)
        todo, _ = er.candidates(ws)
        assert not todo  # neither boilerplate nor a template line retrieves candidates
        allday = [msg(ws, 'x', 'Chill day on Sunday', local(16, 9))]
        events.persist(ws, 'x', allday, [dict(item('Chill day', '', local(18, 0)), all_day=True)])
        gen = [msg(ws, 'y', 'Plans on Sunday?', local(16, 9, 5))]
        events.persist(ws, 'y', gen, [item('Sunday plans', 'Plan', local(18, 15), kind='invitation', role='organizer')])
    er.run(ws_id)
    with db.session() as s:
        ws = Scoped(s, ws_id)
        assert ws.first(ws.q(EventIdentityDecision).where(EventIdentityDecision.via == 'resolver'))  # evaluated...
        assert not er.review_pairs(ws)  # ...but not offered as a possible duplicate


def test_pairs_with_separate_plan_evidence_are_not_suggested():
    assert er.reviewable(['signal:shared_link', 'model:shared_link'])
    assert not er.reviewable(['signal:shared_link', 'model:shared_link', 'model:different_date'])
    assert er.reviewable(['signal:shared_link', 'model:explicit_reschedule', 'model:different_date'])
    assert not er.reviewable(['signal:shared_payment_link', 'model:separate_bookings'])
    assert not er.reviewable(['signal:compatible_activity', 'signal:near_time'])
    assert er.reviewable(['signal:compatible_activity', 'signal:near_time', 'model:same_activity'])


def test_history_compares_instants_and_lists_grouping_decisions_only(ws_id):
    from types import SimpleNamespace as M
    base = {'title': 'Lasergame', 'activity': 'Lasergame', 'provider': 'gmail', 'rank': 4, 'kind': 'booking'}
    a = M(event_id=1, evidence=['s1'], data=dict(base, starts_at=local(18, 16, 15).isoformat(), at='2031-05-15T10:00'))
    b = M(event_id=1, evidence=['s2'], data=dict(base, rank=2, starts_at=local(18, 16, 15).astimezone(timezone.utc).isoformat(),
                                                  at='2031-05-15T11:00'))
    assert events.project([a, b])[1]['history'] == []
    with db.session() as s:
        ws = Scoped(s, ws_id)
        ids = outing(ws)
        ei.record_automatic(ws, ids['invite'], ids['booking'], 'uncertain', via='resolver', applied=False)
        r = ei.same(ws, [ids['invite'], ids['reservation']])
        verdicts = {(d.origin, d.verdict) for d in ei.history(ws, r.representative)}
        assert ('user', 'same') in verdicts and ('automatic', 'uncertain') not in verdicts
