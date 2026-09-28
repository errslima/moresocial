"""Fictional fixtures for evidence-grounded drafts. No production messages, names or places.

The housing case reproduces the shape of the motivating failure: Jonas asks where the user
will stay after moving; the answer lives in a different WhatsApp chat with Iris, who first
offered a student room or her place and then settled that the user can stay at hers until a
deadline. The same chat holds unrelated private matters (her brother, a confidential
pregnancy) and an old message about being ill. The user's chat with Jonas has been quiet for
over 30 days, so their older messages to him are the only voice examples.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from app import ingest, suggestions
from app.models import PersonIdentifier, Source, Suggestion, SuggestionReview, SuggestionSource
from assistant_fixtures import ME_WA

JONAS = '31622220001@c.us'
IRIS = '31622220002@c.us'
SAM_A = '31622220003@c.us'
SAM_B = '31622220004@c.us'
NOOR = '31622220005@c.us'
NAMES = {JONAS: 'Jonas', IRIS: 'Iris', SAM_A: 'Sam', SAM_B: 'Sam', NOOR: 'Noor'}
QUESTION = 'Hey! Where are you staying when you move to Utrecht next month? And how are you feeling these days?'


def msg(ws, chat, body, when, who=None, *, account=ME_WA, name=None):
    """One one-to-one WhatsApp message; who=None means the user sent it."""
    title = name or NAMES.get(chat, chat)
    rec = ingest.SourceRecord(provider='whatsapp', provider_account=account, provider_object_id=uuid.uuid4().hex,
        kind='message', conversation_id=chat, conversation_title=title, body=body, occurred_at=when.astimezone(timezone.utc),
        from_me=who is None, meta={'is_group': False, 'kind': 'chat'},
        author=None if who is None else ingest.Participant('whatsapp', who, 'author', title),
        participants=[ingest.Participant('whatsapp', chat, 'member', title)])
    result = ingest.IngestResult()
    src = ingest.upsert(ws, rec, {account}, result)
    ingest.schedule_rebuilds(ws, result)
    return src


def mail(ws, subject, body, when, sender='noreply@airline.example.test'):
    rec = ingest.SourceRecord(provider='gmail', provider_account='alex@example.test', provider_object_id=uuid.uuid4().hex,
        kind='email', conversation_id='thread-' + uuid.uuid4().hex[:8], title=subject, body=body,
        occurred_at=when.astimezone(timezone.utc),
        author=ingest.Participant('email', sender, 'from', sender.split('@')[0].title()))
    result = ingest.IngestResult()
    src = ingest.upsert(ws, rec, {'alex@example.test'}, result)
    ingest.schedule_rebuilds(ws, result)
    return src


def person(ws, ident):
    return ws.first(ws.q(PersonIdentifier).where(PersonIdentifier.value == ident)).person_id


def housing(ws, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    ids = {}
    ids['ill'] = msg(ws, IRIS, 'Sorry for the slow replies, I was ill all last week.', now - timedelta(days=30)).id
    ids['alternative'] = msg(ws, IRIS, 'For Utrecht you could take the student room on Kade Street, or stay at my place '
                                       'for a while.', now - timedelta(days=20), IRIS).id
    ids['assent'] = msg(ws, IRIS, 'Staying at yours would be amazing, thank you!', now - timedelta(days=20, minutes=-5)).id
    ids['settled'] = msg(ws, IRIS, "Then it's settled: you can stay at mine until 15 October. My brother moves in after that.",
                         now - timedelta(days=19), IRIS).id
    ids['promise'] = msg(ws, IRIS, "Perfect, I'll find something permanent before the 15th.", now - timedelta(days=19, minutes=-10)).id
    ids['secret'] = msg(ws, IRIS, "Also, please don't tell anyone yet, but I'm pregnant!", now - timedelta(days=18), IRIS).id
    # The chat with Jonas: the user's own older messages (over 30 days ago) and his recent question.
    ids['examples'] = [msg(ws, JONAS, body, now - timedelta(days=days)).id for body, days in [
        ("Hoi Jonas! Busy with the move, I'll let you know soon how it goes :)", 75),
        ("Hoi! I'm flying to Lisbon on the 3rd, back on Sunday.", 70),
        ('Hoi Jonas, thanks for the book tip, reading it now and loving it so far', 60),
        ('Haha yes, see you at the market then', 55),
        ('ok', 54),
        ('Hoi, happy birthday!! Hope you have a great day with everyone', 45)]]
    msg(ws, JONAS, 'Moin! How was Lisbon? Tell me everything over coffee sometime', now - timedelta(days=50), JONAS)
    ids['question'] = msg(ws, JONAS, QUESTION, now - timedelta(hours=20), JONAS).id
    return ids


def reply_action(ws, chat, anchor_id, *, title='Reply about the move', reason='They asked a question.'):
    """A conversation reply suggestion anchored to an incoming message, as a review publishes it."""
    review = ws.first(ws.q(SuggestionReview).where(SuggestionReview.conversation_id == chat))
    anchor = ws.get(Source, anchor_id)
    row = ws.add(Suggestion(id=uuid.uuid4(), review_id=review.id, anchor_id=anchor.id, person_id=person(ws, chat),
                            kind='reply', producer='conversation', title=title, reason=reason, state='active',
                            action_key=suggestions.legacy_action_key(review.id, anchor.id, 'reply'),
                            context_sources=[str(anchor.id)]))
    ws.s.flush()
    ws.add(SuggestionSource(id=uuid.uuid4(), suggestion_id=row.id, source_id=anchor.id, source_version=anchor.version))
    row.input_fingerprint = suggestions.input_fingerprint([(anchor.id, anchor.version)], None)
    ws.s.flush()
    return row.id
