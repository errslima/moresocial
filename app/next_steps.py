"""Home's small optional next-steps list: catch-up reminders for pinned contacts the user
chose, which can be snoozed or dismissed. Nothing here contacts anyone."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import or_, select

from .models import NextStepState, Person, PersonIdentifier, Source
from .repo import Scoped


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def person_identifiers(ws: Scoped, person_id) -> list[str]:
    return list(ws.s.scalars(ws.q(PersonIdentifier, PersonIdentifier.value).where(PersonIdentifier.person_id == person_id)))


def next_steps(ws: Scoped, limit: int = 3) -> list[dict]:
    """Up to three optional catch-up steps for pinned contacts not heard from in 30 days."""
    now = utcnow()
    hidden = {n.step_key for n in ws.all(ws.q(NextStepState).where(
        or_(NextStepState.snoozed_until.is_(None), NextStepState.snoozed_until > now)))}
    steps = []
    for person in ws.all(ws.q(Person).where(Person.pinned.is_(True)).limit(20)):
        ids = person_identifiers(ws, person.id)
        last = ws.s.scalar(select(Source.occurred_at).where(Source.workspace_id == ws.wid, Source.author.in_(ids))
                           .order_by(Source.occurred_at.desc()).limit(1)) if ids else None
        if last is None or last < now - timedelta(days=30):
            steps.append({'key': f'catchup:{person.id}', 'text': f'Maybe catch up with {person.display_name}?',
                          'href': f'/people/{person.id}'})
    return [s for s in steps if s['key'] not in hidden][:limit]


def hide_step(ws: Scoped, key: str, days: int | None) -> None:
    key = key[:200]
    row = ws.first(ws.q(NextStepState).where(NextStepState.step_key == key))
    until = utcnow() + timedelta(days=days) if days else None
    if row:
        row.snoozed_until = until
    else:
        ws.add(NextStepState(id=uuid.uuid4(), step_key=key, snoozed_until=until))
