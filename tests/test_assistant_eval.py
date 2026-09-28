"""Small synthetic evaluation set for assistant input quality (plan §8).

Five fictional scenarios: warm/concise vs restrained/direct style, the Aloha event context,
an unresolved detail, a quiet contact, and shareable vs private news. The synthetic model
proves structure, context and constraints only; real-language quality stays pending until
evaluated with a live model. Set MORESOCIAL_EVAL_DUMP=<dir> to write each case's system
prompt and model input for such an evaluation, which must prepare drafts only and never send.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from app import assistant, db, event_identity, events, proactive, suggestions
from app.models import Event, Suggestion, SuggestionReview
from app.repo import Scoped
from assistant_fixtures import MICKAEL, NOOR, at, email, item
from test_social_assistant import (Clock, daytime, laser, pinned, prompts, rows, rows_legacy, run_scan, saturday_evening,  # noqa: F401
                                   sends, settings_form, world)
from worker.main import drain


def dump(case, calls):
    target = os.environ.get('MORESOCIAL_EVAL_DUMP')
    if target:
        Path(target).mkdir(parents=True, exist_ok=True)
        Path(target, f'{case}.json').write_text(json.dumps(calls, indent=2, ensure_ascii=False))


def review_mickael(wid):
    with db.session() as s:
        rid = s.scalar(select(SuggestionReview.id).where(SuggestionReview.conversation_id == MICKAEL))
    suggestions.review_conversation(wid, rid)
    return rows_legacy(wid)[0]


@pytest.mark.parametrize('case', ['style_contrast', 'aloha_event', 'unresolved_detail', 'quiet_contact', 'private_news'])
def test_synthetic_evaluation_case(case, world, browser, prompts, sends, monkeypatch):
    wid, ids = world
    if case == 'style_contrast':
        row = review_mickael(wid)
        drafts = []
        for style in (dict(warmth='warm', brevity='brief', emoji='often'),
                      dict(warmth='reserved', directness='direct', emoji='none', brevity='short')):
            with db.session() as s:
                assistant.save_settings(Scoped(s, wid), settings_form(style_mode='custom', **style))
                s.get(Suggestion, row.id).draft = ''
            suggestions.draft(wid, row.id)
            with db.session() as s:
                drafts.append(s.get(Suggestion, row.id).draft)
        inputs = [json.loads(p['prompt'])['persona']['style'] for p in prompts if p['purpose'] == 'suggestion_draft']
        assert inputs[0] != inputs[1] and drafts[0] != drafts[1]
    elif case == 'aloha_event':
        row = review_mickael(wid)
        suggestions.draft(wid, row.id)
        review = json.loads(next(p['prompt'] for p in prompts if p['purpose'] == 'suggestions'))
        laser_event = next(e for e in review['events'] if e['title'] == 'Lasergame – 2 rondes')
        assert laser_event['place'] == 'Aloha Amsterdam' and laser_event['booked_start'] == '16:15'
        assert any('16:00' in m['text'] for m in review['messages'])  # the meeting time stays separate
        assert row.title == 'Meet Mickaël at Aloha on Sunday'
    elif case == 'unresolved_detail':
        with db.session() as s:
            ws = Scoped(s, wid)
            other = [email(ws, 'Booking LA-4471 (copy)', 'Lasergame, Sunday 15:00, Aloha Amsterdam',
                           datetime.now(timezone.utc) - timedelta(hours=40))]
            events.persist(ws, 'eval-conflict', other, [item('Lasergame – 2 rondes', at(ids['sunday'], 15), kind='booking',
                                                              place='Aloha Amsterdam', role='ticket_holder', activity='Lasergame')])
            fresh = ws.first(ws.q(Event).order_by(Event.created_at.desc()))
            if (fresh.canonical_event_id or fresh.id) != (ws.get(Event, ids['booking']).canonical_event_id or ids['booking']):
                event_identity.same(ws, [ids['booking'], fresh.id])
        Clock(monkeypatch, saturday_evening(ids) - timedelta(days=2))  # questions may come before the checkpoint
        run_scan(wid)
        drain(kinds=['prepare_message'])
        [row] = laser(wid, ids)
        data = json.loads(next(p['prompt'] for p in prompts if p['purpose'] == 'proactive'))
        assert row.kind == 'event_question' and data['events'][0]['needs_checking'] is True
        assert str(ids['lasergame_chat']) not in json.dumps(data)  # no internal IDs in model input
        assert '[time]' in row.draft  # asks rather than choosing a time
    elif case == 'quiet_contact':
        with db.session() as s:
            pinned(Scoped(s, wid), NOOR, 'Noor', days_ago=50, text='The bouldering hall on Friday was great fun.')
        Clock(monkeypatch, daytime())
        run_scan(wid)
        drain(kinds=['prepare_message'])
        [row] = rows(wid, kind='check_in')
        data = json.loads(next(p['prompt'] for p in prompts if p['purpose'] == 'proactive' and 'Noor' in p['prompt']))
        assert data['updates'] == [] and 'bouldering' in json.dumps(data['messages'])
        assert data['candidate']['policy'] == 'contact_rhythm' and 'News from me' not in row.draft
    elif case == 'private_news':
        with db.session() as s:
            ws = Scoped(s, wid)
            pinned(ws, NOOR, 'Noor', days_ago=45)
            from app import diary
            diary.save(ws, 'DIARY_ONLY: I am thinking about quitting my job.', token='eval-diary')
            assistant.create_update(ws, {'text': 'SHAREABLE: I ran my first half marathon', 'audience': 'eligible'})
        Clock(monkeypatch, daytime())
        run_scan(wid)
        drain(kinds=['prepare_message'])
        payload = next(p['prompt'] for p in prompts if p['purpose'] == 'proactive' and 'Noor' in p['prompt'])
        assert 'SHAREABLE' in payload and 'DIARY_ONLY' not in payload
    dump(case, [{'purpose': p['purpose'], 'system': p['system'], 'prompt': json.loads(p['prompt']) if p['prompt'].startswith('{')
                 else p['prompt']} for p in prompts if p['purpose'] in ('suggestions', 'suggestion_draft', 'proactive')])
    assert sends == []
