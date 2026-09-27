"""Synthetic assistant fixtures. Fictional people, venue, messages and booking only.

The Aloha fixture reproduces the shape of the production case: a one-to-one chat with
Mickaël about lasergaming on Sunday (a grouped member with chat evidence and his confirmed
attendance, proposing to meet at 16:00), a booking email for the same outing (another member
carrying the venue and a 16:15–17:00 booked time), and an unrelated dinner with Mickaël later
that day that shares no messages with his chat.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid
from zoneinfo import ZoneInfo

from app import event_identity, events, ingest
from app.models import EventMention, PersonIdentifier, WhatsAppConnector

TZ = ZoneInfo('Europe/Amsterdam')
ME_WA = '31600000100@c.us'
MICKAEL = '31611110001@c.us'
SAM = '31611110002@c.us'
NOOR = '31611110003@c.us'
NAMES = {MICKAEL: 'Mickaël', SAM: 'Sam', NOOR: 'Noor', 'friends@g.us': 'Friends'}


def next_sunday(now: datetime | None = None) -> datetime:
    """Local midnight of the Sunday at least two days ahead."""
    local = (now or datetime.now(timezone.utc)).astimezone(TZ)
    days = (6 - local.weekday()) % 7
    days = days if days >= 2 else days + 7
    day = (local + timedelta(days=days)).date()
    return datetime(day.year, day.month, day.day, tzinfo=TZ)


def at(day: datetime, hour: int, minute: int = 0) -> datetime:
    return day.replace(hour=hour, minute=minute)


def link_connector(ws, account=ME_WA, state='ready'):
    row = ws.first(ws.q(WhatsAppConnector))
    if row is None:
        row = ws.add(WhatsAppConnector(id=uuid.uuid4(), desired='running', observed=state, key_hash=uuid.uuid4().hex,
                                       key_enc='unused', status={}))
    row.desired, row.observed, row.account_wid = 'running', state, account
    ws.s.flush()
    return row


def wa(ws, chat, body, when, who=None, *, group=False, account=ME_WA):
    """One WhatsApp message; who=None means the user sent it."""
    title = NAMES.get(chat, chat)
    rec = ingest.SourceRecord(provider='whatsapp', provider_account=account, provider_object_id=uuid.uuid4().hex,
        kind='message', conversation_id=chat, conversation_title=title, body=body,
        occurred_at=when.astimezone(timezone.utc), from_me=who is None, meta={'is_group': group, 'kind': 'chat'},
        author=None if who is None else ingest.Participant('whatsapp', who, 'author', NAMES.get(who, who)),
        participants=[] if group else [ingest.Participant('whatsapp', chat, 'member', title)])
    result = ingest.IngestResult()
    src = ingest.upsert(ws, rec, {account}, result)
    ingest.schedule_rebuilds(ws, result)
    return src


def email(ws, subject, body, when, sender='bookings@aloha.example.test'):
    rec = ingest.SourceRecord(provider='gmail', provider_account='alex@example.test', provider_object_id=uuid.uuid4().hex,
        kind='email', conversation_id='thread-' + uuid.uuid4().hex[:8], title=subject, body=body,
        occurred_at=when.astimezone(timezone.utc), author=ingest.Participant('email', sender, 'from', sender.split('@')[0].title()))
    return ingest.upsert(ws, rec, {'alex@example.test'}, ingest.IngestResult())


def person(ws, ident):
    return ws.first(ws.q(PersonIdentifier).where(PersonIdentifier.value == ident)).person_id


def item(title, start, *, end=None, kind='plan', evidence=('M1',), place='', role='organizer', participants=(), activity=''):
    return {'title': title, 'activity': activity or title, 'starts_at': start.isoformat(),
            'ends_at': end.isoformat() if end else '', 'all_day': False, 'place': place, 'summary': title,
            'role': role, 'kind': kind, 'match_id': '', 'evidence': list(evidence), 'participants': list(participants)}


def aloha(ws, now: datetime | None = None, *, group=True) -> dict:
    now = now or datetime.now(timezone.utc)
    sunday = next_sunday(now)
    sent = now - timedelta(hours=30)
    chat = [wa(ws, MICKAEL, 'Lasergamen this Sunday? I am in!', sent, MICKAEL),
            wa(ws, MICKAEL, 'Great, I booked two rounds for Sunday afternoon.', sent + timedelta(minutes=5)),
            wa(ws, MICKAEL, 'Are we meeting at 16:00 at the entrance on Sunday?', now - timedelta(hours=20), MICKAEL)]
    mick = str(person(ws, MICKAEL))
    events.persist(ws, 'fixture-mickael', chat, [item('Lasergamen', at(sunday, 16), evidence=['M1', 'M2', 'M3'],
        participants=[{'person': mick, 'status': 'accepted', 'evidence': 'M1'}], activity='Lasergame')])
    mail = [email(ws, 'Booking confirmation LA-4471', 'Lasergame – 2 rondes\nSunday\nRound 1 16:15-16:30\n'
                  'Round 2 16:30-17:00\nAloha Amsterdam, Example Street 1\nBooking LA-4471', sent - timedelta(hours=1))]
    events.persist(ws, 'fixture-booking', mail, [item('Lasergame – 2 rondes', at(sunday, 16, 15), end=at(sunday, 17),
        kind='booking', place='Aloha Amsterdam', role='ticket_holder', activity='Lasergame')])
    dinner_chat = [wa(ws, 'friends@g.us', 'Dinner at Olive on Sunday at 19:30?', sent, None, group=True),
                   wa(ws, 'friends@g.us', 'Count me in for dinner', sent + timedelta(minutes=3), MICKAEL, group=True),
                   wa(ws, 'friends@g.us', 'Sorry, I cannot make the dinner, my sister is visiting', sent + timedelta(minutes=4),
                      SAM, group=True)]
    events.persist(ws, 'fixture-dinner', dinner_chat, [item('Dinner at Olive', at(sunday, 19, 30), place='Olive',
        evidence=['M1', 'M2', 'M3'], activity='Dinner',
        participants=[{'person': mick, 'status': 'accepted', 'evidence': 'M2'},
                      {'person': str(person(ws, SAM)), 'status': 'declined', 'evidence': 'M3'}])])
    by_title = {m.data['title']: m.event_id for m in ws.all(ws.q(EventMention))}
    ids = {'lasergame_chat': by_title['Lasergamen'], 'booking': by_title['Lasergame – 2 rondes'],
           'dinner': by_title['Dinner at Olive'], 'chat': [s.id for s in chat], 'mail': [s.id for s in mail],
           'group_chat': [s.id for s in dinner_chat], 'mickael': mick, 'sunday': sunday}
    if group:
        event_identity.same(ws, [ids['lasergame_chat'], ids['booking']])
    ws.s.flush()
    return ids
