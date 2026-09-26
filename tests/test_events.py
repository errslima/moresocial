"""Automatic events: synthetic evidence, never private production messages."""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from sqlalchemy import select
from app import db, events, ingest
from app.models import Event, EventMention, EventScan, Source, SourceParticipant
from app.repo import Scoped
from conftest import workspace_of


def sample(ws, provider='whatsapp', text='Dinner tomorrow at 19:00', conv='chat', from_me=False):
    at = datetime.now(timezone.utc)
    rec = ingest.SourceRecord(provider=provider, provider_account='alex@example.test', provider_object_id=str(uuid.uuid4()),
        conversation_id=conv, kind='event' if provider == 'calendar' else 'message', title='Dinner', body=text,
        occurred_at=at if provider != 'calendar' else at + timedelta(days=1),
        author=ingest.Participant('email', 'sam@example.test', 'author', 'Sam'), from_me=from_me)
    return ingest.upsert(ws, rec, {'alex@example.test'}, ingest.IngestResult())


def item(src, **overrides):
    return dict({'title': 'Dinner at Olive', 'activity': 'dinner',
        'starts_at': (src.occurred_at + timedelta(days=1)).isoformat(), 'ends_at': '', 'all_day': False,
        'place': 'Olive', 'summary': 'Dinner together.', 'role': 'organizer', 'kind': 'plan',
        'match_id': '', 'evidence': ['M1'], 'participants': []}, **overrides)


def test_no_manual_creation_and_redirects(browser):
    browser.login('alex')
    assert browser.get('/gatherings').status_code == 301
    page = browser.get('/events')
    assert page.status_code == 200 and 'The plans in your conversations' in page.text
    assert 'Plan a new' not in page.text and '>Create<' not in page.text
    for path in ('/events', '/gatherings'):
        assert browser.post(path, {'csrf': browser.csrf(), 'title': 'Manual'}).status_code == 405
        assert browser.post(path, {'title': 'Manual'}).status_code == 403


def test_cross_channel_merge_booking_precedence_and_dismissal(browser):
    browser.login('alex')
    wid = workspace_of('alex@example.test')
    with db.session() as s:
        ws=Scoped(s,wid)
        a=sample(ws); data=item(a)
        events.persist(ws,'chat',[a],[data])
        e=ws.first(ws.q(Event)); eid=e.id
        e.dismissed=True
        b=sample(ws,'gmail',conv='email')
        booking=item(b,kind='booking',starts_at=(events.dt(data['starts_at'])+timedelta(minutes=45)).isoformat(),
                     summary='Confirmed booking at 19:45.')
        events.persist(ws,'email',[b],[booking])
        assert len(ws.all(ws.q(Event))) == 1
        assert e.dismissed and e.starts_at == events.dt(booking['starts_at'])
        assert e.details['source_count'] == 2
        assert e.details['providers'] == ['gmail','whatsapp']
        events.persist(ws,'chat',[a],[data])
        assert e.starts_at == events.dt(booking['starts_at'])
    assert browser.get(f'/events/{eid}').status_code == 200
    assert 'Confirmed booking' in browser.get(f'/events/{eid}').text
    assert 'Dinner at Olive' not in browser.get('/events').text
    assert 'Dinner at Olive' in browser.get('/events?view=dismissed').text
    assert browser.post(f'/events/{eid}/dismiss',{'csrf':browser.csrf(),'action':'restore'}).status_code == 303
    assert 'Dinner at Olive' in browser.get('/events').text


def test_dates_evidence_and_participants_are_validated(browser):
    browser.login('alex'); wid=workspace_of('alex@example.test')
    with db.session() as s:
        ws=Scoped(s,wid); a=sample(ws); b=sample(ws,conv='another')
        person=str(ws.first(ws.q(SourceParticipant).where(SourceParticipant.source_id==a.id)).person_id)
        data=item(a,participants=[{'person':person,'status':'accepted','evidence':'M1'},
                                  {'person':str(uuid.uuid4()),'status':'accepted','evidence':'M1'}])
        events.persist(ws,'a',[a],[data]); e=ws.first(ws.q(Event))
        assert len(e.details['participants']) == 1
        events.persist(ws,'b',[b],[item(b,starts_at=(a.occurred_at-timedelta(days=20)).isoformat()),
                                      item(b,evidence=['invented'])])
        assert len(ws.all(ws.q(Event))) == 1
        otherday=item(b,starts_at=(events.dt(data['starts_at'])+timedelta(days=1)).isoformat(),match_id=str(e.id))
        events.persist(ws,'c',[b],[otherday])
        assert len(ws.all(ws.q(Event))) == 2


def test_exclusion_erases_derived_data_and_evidence(browser):
    browser.login('alex'); wid=workspace_of('alex@example.test')
    with db.session() as s:
        ws=Scoped(s,wid); a=sample(ws); events.persist(ws,'a',[a],[item(a)])
        eid=ws.first(ws.q(Event)).id
        ingest.exclude(ws,a,'object')
        assert not any(m.evidence for m in ws.all(ws.q(EventMention)))
        assert not events.cards(ws)
        assert ws.get(Event,eid).details == {}
        assert ws.get(Event,eid).title == 'Event evidence removed'
    assert 'Dinner at Olive' not in browser.get(f'/events/{eid}').text


def test_detection_idempotency_and_calendar_without_ai(browser,monkeypatch):
    browser.login('alex'); wid=workspace_of('alex@example.test')
    with db.session() as s:
        a=sample(Scoped(s,wid)); data=item(a)
    calls=[]
    def generate(*args,**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(data={'events':[data]})
    monkeypatch.setattr(events.ai,'generate',generate)
    events.detect(wid,'whatsapp','chat')
    events.detect(wid,'whatsapp','chat')
    assert len(calls)==1
    with db.session() as s:
        ws=Scoped(s,wid); assert len(ws.all(ws.q(EventScan)))==1
        sample(ws,'calendar',conv='calendar')
    events.detect(wid,'calendar','calendar')
    assert len(calls)==1
    assert browser.get('/home').status_code==200


def test_workspace_isolation(make_browser):
    a,b=make_browser(),make_browser(); a.login('alex'); b.login('blake')
    wid=workspace_of('alex@example.test')
    with db.session() as s:
        ws=Scoped(s,wid); src=sample(ws); events.persist(ws,'a',[src],[item(src)]); eid=ws.first(ws.q(Event)).id
    assert b.get(f'/events/{eid}').status_code==404
    assert b.post(f'/events/{eid}/dismiss',{'csrf':b.csrf()}).status_code==404
    assert 'Dinner at Olive' not in b.get('/events').text


def test_dismissal_survives_source_edit_and_redetection(browser):
    browser.login('alex'); wid=workspace_of('alex@example.test')
    with db.session() as s:
        ws=Scoped(s,wid); src=sample(ws); data=item(src)
        events.persist(ws,'a',[src],[data]); e=ws.first(ws.q(Event)); e.dismissed=True
        events.invalidate(ws,[src.id])
        events.persist(ws,'a',[src],[dict(data,title='Dinner with a corrected title')])
        assert len(ws.all(ws.q(Event)))==1
        assert e.dismissed and not events.cards(ws)
