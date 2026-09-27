"""Review-only reproductions using synthetic data; no production mutations."""
from datetime import datetime, timedelta, timezone

from app import ai, assistant, assistant_context as ac, db, ingest, proactive, suggestions
from app.models import Source, Suggestion
from app.repo import Scoped
from test_social_assistant import (world, Clock, saturday_evening, run_scan, rows,
                                   settings_form, drain, pinned, NOOR)


def prepared_event(world, monkeypatch):
    wid, ids = world
    Clock(monkeypatch, saturday_evening(ids))
    run_scan(wid)
    drain(kinds=['prepare_message'])
    return rows(wid, kind='event_reminder')[0]


def test_review_excluding_booking_erases_dependent_prepared_text(world, monkeypatch):
    wid, ids = world
    row = prepared_event(world, monkeypatch)
    assert 'Aloha' in row.draft
    with db.session() as s:
        ws = Scoped(s, wid)
        ingest.exclude(ws, ws.get(Source, ids['mail'][0]), 'object')
    with db.session() as s:
        after = s.get(Suggestion, row.id)
        assert not after.draft, 'Excluded booking content remains in the prepared draft'


def test_review_proactive_user_event_correction_survives_repreparation(world, browser, monkeypatch):
    wid, ids = world
    row = prepared_event(world, monkeypatch)
    assert browser.post(f'/suggestions/{row.id}/event', {
        'csrf': browser.csrf(f'/suggestions/{row.id}'), 'event': ids['dinner']
    }).status_code == 303
    with db.session() as s:
        assert s.get(Suggestion, row.id).event_link == 'user'
        assistant.save_settings(Scoped(s, wid), settings_form(warmth='warm'))
    run_scan(wid)
    drain(kinds=['prepare_message'])
    with db.session() as s:
        after = s.get(Suggestion, row.id)
        assert after.event_id == ids['dinner'] and after.event_link == 'user', 'Background preparation overwrote the correction'


def test_review_expired_snooze_refreshes_changed_checkin(world, browser, monkeypatch):
    wid, ids = world
    clock = Clock(monkeypatch, datetime.now(timezone.utc).replace(hour=12))
    with db.session() as s:
        pinned(Scoped(s, wid), NOOR, 'Noor', days_ago=40)
    run_scan(wid)
    drain(kinds=['prepare_message'])
    row = rows(wid, kind='check_in')[0]
    assert browser.post(f'/suggestions/{row.id}/state', {
        'csrf': browser.csrf('/suggestions'), 'action': 'snooze', 'days': 1
    }).status_code == 303
    with db.session() as s:
        assistant.save_settings(Scoped(s, wid), settings_form(warmth='warm'))
        expires = s.get(Suggestion, row.id).snoozed_until
    clock.set(expires + timedelta(hours=1))
    run_scan(wid, trigger='manual')
    drain(kinds=['prepare_message'])
    with db.session() as s:
        ws = Scoped(s, wid)
        assert suggestions.draft_current(ws, ws.get(Suggestion, row.id))[0], 'Expired snooze prevents refreshing the draft indefinitely'


def test_review_relative_day_draft_expires_at_local_midnight(world, monkeypatch):
    wid, ids = world
    clock = Clock(monkeypatch, saturday_evening(ids))
    monkeypatch.setattr(ac, 'utcnow', lambda: clock.value)
    generate = ai.generate
    def tomorrow(*args, **kwargs):
        result = generate(*args, **kwargs)
        if kwargs['context'].get('purpose') == 'proactive':
            result.data['draft'] = 'See you tomorrow at Aloha at 16:15!'
        return result
    monkeypatch.setattr(ai, 'generate', tomorrow)
    run_scan(wid)
    drain(kinds=['prepare_message'])
    row = rows(wid, kind='event_reminder')[0]
    with db.session() as s:
        ws = Scoped(s, wid)
        assert suggestions.draft_current(ws, ws.get(Suggestion, row.id))[0]
    clock.set(clock.value + timedelta(hours=14))
    with db.session() as s:
        ws = Scoped(s, wid)
        assert not suggestions.draft_current(ws, ws.get(Suggestion, row.id))[0], 'Yesterday\'s tomorrow draft is still current on the event day'
