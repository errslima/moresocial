"""User-authored assistant settings: persona, per-person preferences and shareable updates.

Only these explicit, CSRF-protected forms write them; nothing imported (messages, email,
diary) can. Every change bumps a revision that is part of draft and proactive fingerprints,
so existing drafts become visibly stale instead of being replaced. Revoking or narrowing an
update removes derived text from the suggestions that used it, including edited drafts.
Nothing here can send a message.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
import json
import re
import uuid

from . import ai, assistant_context as ac
from .models import (AssistantSettings, Person, PersonPreference, ShareableUpdate, ShareableUpdatePerson, Suggestion)
from .repo import Scoped

INSTRUCTIONS_MAX = 4000
EXAMPLES_MAX = 2000
STYLE_MAX = 1000
UPDATE_MAX = 300
LANGUAGE_RE = re.compile(r"^[^\W\d_]+(?:[ '()-][^\W\d_]+)*$")
PREVIEW = {'purpose': 'assistant_preview', 'person': 'Robin (example)', 'timezone': 'Europe/Amsterdam',
           'note': 'A fictional example conversation, used only to preview your style.',
           'action': {'kind': 'reply', 'title': 'Reply to a dinner invitation', 'reason': 'Robin asked a question.',
                      'event': None},
           'messages': [{'label': 'M1', 'from_me': False, 'at': '2026-09-25T18:00:00+02:00',
                         'text': 'Are you free for dinner on Friday? I found a new place near the station.'}],
           'events': [], 'event_messages': [], 'notes': [], 'relationship': {'style_and_boundaries': ''}}


class Invalid(ValueError):
    pass


def _choice(values, name, options):
    value = values.get(name)
    if value not in options:
        raise Invalid(f'Choose a valid {name}.')
    return value


def _flag(values, name) -> bool:
    return values.get(name) in ('yes', 'on', '1', True)


def save_settings(ws: Scoped, values: dict) -> bool:
    """Validate and store the persona. Returns whether anything changed."""
    instructions = (values.get('instructions') or '').strip()
    preferred = (values.get('examples_preferred') or '').strip()
    avoid = (values.get('examples_avoid') or '').strip()
    language = ' '.join((values.get('language') or '').split())
    if len(instructions) > INSTRUCTIONS_MAX:
        raise Invalid(f'Keep instructions under {INSTRUCTIONS_MAX:,} characters.')
    if len(preferred) + len(avoid) > EXAMPLES_MAX:
        raise Invalid(f'Keep the wording examples under {EXAMPLES_MAX:,} characters in total.')
    if language and (len(language) > 40 or not LANGUAGE_RE.match(language)):
        raise Invalid('Enter a language name, or leave it empty to match each conversation.')
    row = ws.s.get(AssistantSettings, ws.wid, with_for_update=True)
    # A form without the mode (an older page) keeps the current one. Tone values are saved in
    # both modes, so switching back to custom restores them.
    mode = values.get('style_mode') or (row.style_mode if row is not None else 'conversation')
    if mode not in ac.STYLE_MODES:
        raise Invalid('Choose how drafts should sound.')
    new = dict(instructions=instructions, examples_preferred=preferred, examples_avoid=avoid, language=language,
               style_mode=mode, initiative=_choice(values, 'initiative', ac.INITIATIVES),
               event_preparation=_flag(values, 'event_preparation'), check_ins=_flag(values, 'check_ins'),
               **{k: _choice(values, k, options) for k, options in ac.STYLE.items()})
    if row is None:
        row = AssistantSettings(workspace_id=ws.wid, revision=0, paused=False)
        ws.s.add(row)
    if all(getattr(row, k, None) == v for k, v in new.items()):
        return False
    for k, v in new.items():
        setattr(row, k, v)
    row.revision = (row.revision or 0) + 1
    ws.s.flush()
    _changed(ws)
    return True


def set_paused(ws: Scoped, paused: bool) -> None:
    row = ws.s.get(AssistantSettings, ws.wid, with_for_update=True)
    if row is None:
        row = AssistantSettings(workspace_id=ws.wid, revision=0)
        ws.s.add(row)
    if row.paused != paused:
        row.paused = paused
        row.revision = (row.revision or 0) + 1
        ws.s.flush()
        _changed(ws)


def save_preferences(ws: Scoped, person: Person, values: dict) -> bool:
    check_ins = {'default': None, 'on': True, 'off': False}.get(values.get('check_ins'), 'invalid')
    if check_ins == 'invalid':
        raise Invalid('Choose whether check-ins are on, off or follow the default.')
    try:
        interval = int(values.get('interval_days') or ac.DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        raise Invalid('Enter a number of days between 1 and 365.') from None
    if not 1 <= interval <= 365:
        raise Invalid('Enter a number of days between 1 and 365.')
    style = (values.get('style') or '').strip()
    if len(style) > STYLE_MAX:
        raise Invalid(f'Keep style and boundaries under {STYLE_MAX:,} characters.')
    row = ws.first(ws.q(PersonPreference).where(PersonPreference.person_id == person.id).with_for_update())
    if row is None:
        row = ws.add(PersonPreference(id=uuid.uuid4(), person_id=person.id, revision=0))
    new = dict(check_ins=check_ins, interval_days=interval, style=style)
    if all(getattr(row, k, None) == v for k, v in new.items()):
        return False
    for k, v in new.items():
        setattr(row, k, v)
    row.revision = (row.revision or 0) + 1
    ws.s.flush()
    _changed(ws, person_id=person.id)
    return True


# ---------------- shareable updates

def _expiry(ws: Scoped, value: str | None):
    if not value:
        return None
    try:
        day = datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        raise Invalid('Enter the expiry as a date.') from None
    end = datetime.combine(day, time(23, 59, 59), tzinfo=ac.tz(ws))
    if end <= ac.utcnow():
        raise Invalid('Choose an expiry date in the future.')
    return end


def _people(ws: Scoped, audience: str, person_ids) -> list[uuid.UUID]:
    if audience == 'eligible':
        return []
    ids = []
    for raw in person_ids or []:
        p = ws.get(Person, raw)
        if p is None:
            raise Invalid('Choose people from your contacts.')
        ids.append(p.id)
    if not ids:
        raise Invalid('Choose at least one person, or share with your check-in contacts.')
    return sorted(set(ids), key=str)


def _audience(values):
    audience = values.get('audience')
    if audience not in ('eligible', 'selected'):
        raise Invalid('Choose who this update is for.')
    return audience


def _text(values):
    text = ' '.join((values.get('text') or '').split())
    if not 1 <= len(text) <= UPDATE_MAX:
        raise Invalid(f'Write an update of 1–{UPDATE_MAX} characters.')
    return text


def create_update(ws: Scoped, values: dict, person_ids=()) -> ShareableUpdate:
    audience = _audience(values)
    row = ws.add(ShareableUpdate(id=uuid.uuid4(), text=_text(values), audience=audience,
                                 expires_at=_expiry(ws, values.get('expires')), state='active', revision=1))
    ws.s.flush()
    for pid in _people(ws, audience, person_ids):
        ws.add(ShareableUpdatePerson(id=uuid.uuid4(), update_id=row.id, person_id=pid))
    ws.s.flush()
    _changed(ws, update_id=row.id)
    return row


def audience_ids(ws: Scoped, update: ShareableUpdate) -> set:
    return set(ws.s.scalars(ws.q(ShareableUpdatePerson, ShareableUpdatePerson.person_id)
                            .where(ShareableUpdatePerson.update_id == update.id)))


def edit_update(ws: Scoped, update: ShareableUpdate, values: dict, person_ids=()) -> None:
    if update.state != 'active':
        raise Invalid('This update was revoked. Write a new one instead.')
    audience = _audience(values)
    people = set(_people(ws, audience, person_ids))
    update.text, update.audience, update.expires_at = _text(values), audience, _expiry(ws, values.get('expires'))
    ws.delete(ShareableUpdatePerson, ShareableUpdatePerson.update_id == update.id)
    for pid in sorted(people, key=str):
        ws.add(ShareableUpdatePerson(id=uuid.uuid4(), update_id=update.id, person_id=pid))
    update.revision += 1
    ws.s.flush()
    # Anyone who is no longer in the audience loses text derived from this update.
    for row in ws.all(ws.q(Suggestion).where(Suggestion.update_id == update.id).with_for_update()):
        person = ws.get(Person, row.person_id)
        if audience == 'selected' and row.person_id not in people or audience == 'eligible' and not ac.check_ins_enabled(
                person, ac.preference(ws, person.id)):
            erase(row, 'This update is no longer shared with this person.')
    _changed(ws, update_id=update.id)


def revoke_update(ws: Scoped, update: ShareableUpdate) -> int:
    """Stop sharing: erase the update's text and every derived draft, edited or not."""
    update.state, update.text = 'revoked', ''
    update.revision += 1
    rows = ws.all(ws.q(Suggestion).where(Suggestion.update_id == update.id).with_for_update())
    for row in rows:
        erase(row, 'You stopped sharing this update.')
    ws.s.flush()
    _changed(ws, update_id=update.id)
    return len(rows)


def erase(row: Suggestion, reason: str) -> None:
    """Remove derived text (including a user's edited draft) and take the action out of view."""
    row.title, row.reason, row.draft, row.draft_origin, row.draft_fingerprint = 'Shared update withdrawn', reason, '', None, None
    row.draft_revision += 1
    row.rationale = {'summary': reason}
    if row.state in ('active', 'snoozed'):
        row.state = 'invalidated'


# ---------------- preview

def preview(wid: uuid.UUID) -> str:
    """One explicit, accounted model call on a fictional scenario with the saved explicit
    preferences (and tone settings in custom mode). It has no contact data, so it cannot show
    the per-person voice real drafts learn from the user's own messages. Sends nothing."""
    from . import db, draft_evidence
    with db.session() as s:
        settings = ac.settings(Scoped(s, wid))
        mode = settings.style_mode
        data = {**PREVIEW, 'persona': ac.persona(settings), 'style': {'coverage': 'none', 'examples': []},
                'answers': {'permitted_facts': [], 'unresolved': [
                    {'question': 'Are you free for dinner on Friday?', 'status': 'missing', 'reason': 'no_evidence'}]}}
    out = ai.generate(wid, system=draft_evidence.draft_system(mode), prompt=json.dumps(ac.public(data), ensure_ascii=False),
                      schema=draft_evidence.writer_schema([]), max_tokens=600, effort='low', context=data)
    try:
        text, _ = draft_evidence.validate_writer(out.data, [])
    except ai.InvalidOutput:
        raise ai.InvalidOutput('assistant_preview') from None
    if len(text) > 2000:
        raise ai.InvalidOutput('assistant_preview')
    return text


def _changed(ws: Scoped, **scope) -> None:
    """Proactive preparation re-evaluates affected candidates (drafts go stale by fingerprint)."""
    from . import proactive
    proactive.invalidate(ws, **scope)
