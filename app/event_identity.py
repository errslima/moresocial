"""Event identity: reversible grouping of original detected events.

Original `Event` rows are stable identities; their mentions stay attached to them. A group
is a connected component of applied `same` decisions, materialized as
`Event.canonical_event_id` pointers (NULL = representative). Pointers are derived data:
`rebuild()` recomputes them from active decisions. User decisions outrank automatic ones,
and no join is made while any cross-group pair is marked different.

Every write takes the workspace event lock, uses scoped queries and runs in the caller's
transaction. User corrections, the resolver and operator repair all use this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import or_, select

from . import events as ev, jobs
from .models import Event, EventIdentityDecision, EventMention, EventPairScan, OperatorSetting, Source
from .repo import Scoped

MODE_SETTING = 'event_resolver_mode'
MODES = ('off', 'observe', 'apply')
DEFAULT_MODE = 'observe'


class Conflict(Exception):
    """A correction contradicts an earlier user correction; nothing was changed."""


class Stale(Exception):
    """The event changed after the page was shown; nothing was changed."""


def now():
    return datetime.now(timezone.utc)


def pair(a, b) -> tuple[uuid.UUID, uuid.UUID]:
    a, b = uuid.UUID(str(a)), uuid.UUID(str(b))
    if a == b:
        raise ValueError('an event cannot be paired with itself')
    return (a, b) if a < b else (b, a)


# ---------------- resolver mode (operator setting, no restart needed)

def mode(s) -> str:
    row = s.get(OperatorSetting, MODE_SETTING)
    return row.value if row and row.value in MODES else DEFAULT_MODE


def set_mode(s, value: str, by: str) -> list[uuid.UUID]:
    """Switch mode. Entering `apply` promotes still-active observed same verdicts, which
    were validated when stored, so reviewed work is not paid for twice. Returns the
    workspaces whose groups need a rebuild."""
    from . import operator_settings
    if value not in MODES:
        raise ValueError('mode must be one of ' + ', '.join(MODES))
    operator_settings._put(s, MODE_SETTING, value, None, by)
    touched = []
    if value == 'apply':
        rows = s.scalars(select(EventIdentityDecision).where(
            EventIdentityDecision.origin == 'automatic', EventIdentityDecision.verdict == 'same',
            EventIdentityDecision.active.is_(True), EventIdentityDecision.applied.is_(False))).all()
        for d in rows:
            d.applied = True
            touched.append(d.workspace_id)
    return sorted(set(touched), key=str)


# ---------------- decisions and group computation

def decisions(ws: Scoped, event_ids=None) -> list[EventIdentityDecision]:
    q = ws.q(EventIdentityDecision).where(EventIdentityDecision.active.is_(True))
    if event_ids is not None:
        ids = list(event_ids)
        q = q.where(or_(EventIdentityDecision.event_a.in_(ids), EventIdentityDecision.event_b.in_(ids)))
    return ws.all(q.order_by(EventIdentityDecision.created_at, EventIdentityDecision.id))


def effective(rows) -> dict:
    """One current decision per pair: a user decision outranks an automatic one."""
    out = {}
    for d in rows:
        if not d.active:
            continue
        key = (d.event_a, d.event_b)
        old = out.get(key)
        if old is None or (d.origin == 'user' and old.origin != 'user'):
            out[key] = d
    return out


def blocked(eff: dict, x, y, origin: str) -> bool:
    d = eff.get(pair(x, y))
    if d is None or d.verdict != 'different':
        return False
    return d.origin == 'user' if origin == 'user' else True


@dataclass
class Plan:
    rep: dict = field(default_factory=dict)          # event id -> representative id
    groups: dict = field(default_factory=dict)       # representative id -> sorted member ids
    rejected: list = field(default_factory=list)     # same decisions not applied because of a conflict


def plan_groups(events: list[Event], rows) -> Plan:
    """Deterministic components. User same edges first (oldest first), then automatic.
    A join is refused if any cross pair is different: A=B, B=C never overrides A≠C."""
    by_id = {e.id: e for e in events}
    eff = effective(rows)
    edges = sorted((d for d in eff.values() if d.verdict == 'same' and d.applied
                    and d.event_a in by_id and d.event_b in by_id),
                   key=lambda d: (d.origin != 'user', d.created_at or now(), str(d.id)))
    group = {e.id: {e.id} for e in events}
    plan = Plan()
    for d in edges:
        ga, gb = group[d.event_a], group[d.event_b]
        if ga is gb:
            continue
        if any(blocked(eff, x, y, d.origin) for x in ga for y in gb):
            plan.rejected.append(d)
            continue
        merged = ga | gb
        for x in merged:
            group[x] = merged
    seen = set()
    for members in group.values():
        key = min(members, key=str)
        if key in seen:
            continue
        seen.add(key)
        ms = [by_id[x] for x in members]
        # Keep an existing representative when it remains in the component.
        current = [e for e in ms if e.canonical_event_id is None and
                   (len(ms) == 1 or any(o.canonical_event_id == e.id for o in ms))]
        pool = current or ms
        chosen = min(pool, key=lambda e: (e.created_at or now(), str(e.id)))
        plan.groups[chosen.id] = sorted(members, key=str)
        for x in members:
            plan.rep[x] = chosen.id
    return plan


def rebuild(ws: Scoped, extra=()) -> set:
    """Apply the plan to canonical pointers. Returns representatives of changed groups."""
    events = ws.all(ws.q(Event))
    plan = plan_groups(events, list(decisions(ws)) + list(extra))
    changed = set()
    for e in events:
        target = plan.rep[e.id]
        pointer = None if target == e.id else target
        if e.canonical_event_id != pointer:
            changed.add(target)
            if e.canonical_event_id:
                changed.add(e.canonical_event_id)
            if e.canonical_event_id is None:
                changed.add(e.id)
            e.canonical_event_id = pointer
    ws.s.flush()
    return {plan.rep.get(x, x) for x in changed}


def members(ws: Scoped, rep_id) -> list[Event]:
    return ws.all(ws.q(Event).where(or_(Event.id == rep_id, Event.canonical_event_id == rep_id))
                  .order_by(Event.created_at, Event.id))


def representative(ws: Scoped, event: Event) -> Event:
    return ws.get(Event, event.canonical_event_id) if event.canonical_event_id else event


def refresh(ws: Scoped, event_ids=()) -> set:
    """Rebuild groups and recompute every affected group projection."""
    changed = rebuild(ws)
    reps = set(changed)
    for eid in event_ids:
        e = ws.get(Event, eid)
        if e:
            reps.add(e.canonical_event_id or e.id)
    for rep in sorted(reps, key=str):
        e = ws.get(Event, rep)
        if e and e.canonical_event_id is None:
            ev.recompute_group(ws, e)
    ws.s.flush()
    notify(ws, reps)
    return reps


def notify(ws: Scoped, reps) -> None:
    """Suggestions and proactive preparation follow event changes (see app/suggestions.py)."""
    from . import suggestions
    if reps:
        suggestions.events_changed(ws, reps)


def state_token(ws: Scoped, rep_id) -> str:
    """Current-state check for forms: group members and the decisions touching them."""
    ms = members(ws, rep_id)
    ids = [m.id for m in ms]
    return ev.digest([sorted(str(i) for i in ids),
                      sorted(str(d.id) for d in decisions(ws, ids))])[:24]


def check_state(ws: Scoped, rep_id, token: str | None):
    if token is not None and token != state_token(ws, rep_id):
        raise Stale('This event changed since the page was loaded. Review it again.')


def enqueue(ws: Scoped, token, delay: int = 30) -> bool:
    return jobs.enqueue(ws.s, ws.wid, 'resolve_events', 'resolve:' + ev.digest(['resolve', token])[:40], {},
                        run_after=now() + timedelta(seconds=delay))


def schedule(ws: Scoped) -> bool:
    """Coalesce bursts (a sync touching many sources, many windows) into one resolver run
    per workspace per minute, started after that minute has closed."""
    bucket = now().replace(second=0, microsecond=0)
    return jobs.enqueue(ws.s, ws.wid, 'resolve_events', 'resolve:minute:' + bucket.isoformat(), {},
                        run_after=bucket + timedelta(seconds=90))


# ---------------- writes

def _supersede(d: EventIdentityDecision, reason: str):
    d.active = False
    d.superseded_at = now()
    d.superseded_reason = reason


def _write(ws: Scoped, a, b, verdict, origin, via, op, *, reasons=(), citations=(), fingerprint='',
           resolver_version='', applied=True) -> EventIdentityDecision:
    x, y = pair(a, b)
    for old in ws.all(ws.q(EventIdentityDecision).where(
            EventIdentityDecision.event_a == x, EventIdentityDecision.event_b == y,
            EventIdentityDecision.origin == origin, EventIdentityDecision.active.is_(True))):
        _supersede(old, 'replaced')
    ws.s.flush()
    d = ws.add(EventIdentityDecision(id=uuid.uuid4(), event_a=x, event_b=y, verdict=verdict, origin=origin, via=via,
        active=True, applied=applied, operation_id=op, created_at=now(), reasons=list(reasons),
        citations=[list(c) for c in citations], fingerprint=fingerprint, resolver_version=resolver_version))
    ws.s.flush()
    return d


def _current_user(ws, x, y):
    x, y = pair(x, y)
    return ws.first(ws.q(EventIdentityDecision).where(
        EventIdentityDecision.event_a == x, EventIdentityDecision.event_b == y,
        EventIdentityDecision.origin == 'user', EventIdentityDecision.active.is_(True)))


def _events(ws: Scoped, ids) -> list[Event]:
    out = []
    for i in dict.fromkeys(str(x) for x in ids):
        e = ws.get(Event, i)
        if e is None:
            raise LookupError('That event was not found.')
        if e.origin == 'legacy':
            raise ValueError('Earlier manual plans cannot be grouped.')
        out.append(e)
    return out


def _label(e: Event) -> str:
    when = e.starts_at.astimezone(ev.TZ).strftime('%a %d %b') if e.starts_at else 'undated'
    return f'“{e.title}” ({when})'


@dataclass
class Result:
    representative: uuid.UUID | None
    members: list
    operation: uuid.UUID
    decisions: list
    connected: bool = True


def same(ws: Scoped, ids, *, via='web', state=None, rep_for_state=None) -> Result:
    """The user says these detected events are one outing."""
    ev.lock(ws)
    if rep_for_state is not None:
        check_state(ws, rep_for_state, state)
    selected = _events(ws, ids)
    if not 2 <= len(selected) <= 12:
        raise ValueError('Choose between two and twelve different events.')
    groups = []
    for e in selected:
        ms = {m.id for m in members(ws, e.canonical_event_id or e.id)}
        if not any(ms == g for g in groups):
            groups.append(ms)
    union = set().union(*groups)
    # Validate every cross-group pair before writing anything.
    for i, g in enumerate(groups):
        for h in groups[i + 1:]:
            for x in g:
                for y in h:
                    d = _current_user(ws, x, y)
                    if d and d.verdict == 'different':
                        a, b = ws.get(Event, d.event_a), ws.get(Event, d.event_b)
                        raise Conflict(f'You marked {_label(a)} and {_label(b)} as separate events. '
                                       'Undo that correction first.')
    op = uuid.uuid4()
    written = []
    # Every selected pair, so separating one record later keeps the others together.
    for i, x in enumerate(selected):
        for y in selected[i + 1:]:
            existing = _current_user(ws, x.id, y.id)
            if existing and existing.verdict == 'same':
                continue
            written.append(_write(ws, x.id, y.id, 'same', 'user', via, op))
    # A user correction may overrule automatic different verdicts; record that it did.
    for d in decisions(ws, union):
        if (d.origin == 'automatic' and d.verdict == 'different' and d.event_a in union and d.event_b in union):
            _supersede(d, 'overruled_by_user')
            for w in written:
                if 'overrules_automatic_different' not in w.reasons:
                    w.reasons = [*w.reasons, 'overrules_automatic_different']
    refresh(ws, union)
    rep = representative(ws, selected[0])
    schedule(ws)
    return Result(rep.id, [m.id for m in members(ws, rep.id)], op, [d.id for d in written],
                  connected=all(e.id in {m.id for m in members(ws, rep.id)} for e in selected))


def separate(ws: Scoped, a, b, *, via='web', state=None) -> Result:
    """Keep two currently separate groups apart: every cross pair gets a user different."""
    ev.lock(ws)
    ea, eb = _events(ws, [a, b])
    ra, rb = (ea.canonical_event_id or ea.id), (eb.canonical_event_id or eb.id)
    if ra == rb:
        raise ValueError('These are currently one event. Use Separate on the event page instead.')
    if state is not None and state != pair_token(ws, ra, rb):
        raise Stale('These events changed since the page was loaded. Review them again.')
    op = uuid.uuid4()
    written = []
    for x in members(ws, ra):
        for y in members(ws, rb):
            d = _current_user(ws, x.id, y.id)
            if d and d.verdict == 'different':
                continue
            written.append(_write(ws, x.id, y.id, 'different', 'user', via, op))
    refresh(ws, [ra, rb])
    schedule(ws)
    return Result(None, [ra, rb], op, [d.id for d in written])


def pair_token(ws: Scoped, ra, rb) -> str:
    return ev.digest([state_token(ws, ra), state_token(ws, rb)])[:24]


def split(ws: Scoped, rep_id, parts, *, via='web', state=None) -> Result:
    """Separate a group into an explicit partition of its original identities.
    Crossing same decisions are superseded and crossing pairs become user different."""
    ev.lock(ws)
    check_state(ws, rep_id, state)
    ms = {m.id for m in members(ws, rep_id)}
    parts = [{uuid.UUID(str(x)) for x in p} for p in parts if p]
    flat = [x for p in parts for x in p]
    if len(parts) < 2 or len(flat) != len(set(flat)) or set(flat) != ms:
        raise ValueError('Choose which detected records form each separate event.')
    op = uuid.uuid4()
    where = {x: i for i, p in enumerate(parts) for x in p}
    for d in decisions(ws, ms):
        if (d.verdict == 'same' and d.event_a in ms and d.event_b in ms and where[d.event_a] != where[d.event_b]):
            _supersede(d, 'split')
    written = []
    for i, p in enumerate(parts):
        for q in parts[i + 1:]:
            for x in p:
                for y in q:
                    written.append(_write(ws, x, y, 'different', 'user', via, op))
    refresh(ws, ms)
    schedule(ws)
    reps = sorted({(ws.get(Event, x).canonical_event_id or x) for x in ms}, key=str)
    return Result(None, reps, op, [d.id for d in written])


def undo(ws: Scoped, decision_id, *, state=None, rep_for_state=None) -> Result:
    """Withdraw one user correction. Another path may still connect the group."""
    ev.lock(ws)
    if rep_for_state is not None:
        check_state(ws, rep_for_state, state)
    d = ws.get(EventIdentityDecision, decision_id)
    if d is None or d.origin != 'user' or not d.active:
        raise LookupError('That correction is no longer active.')
    _supersede(d, 'undone')
    ws.s.flush()
    refresh(ws, [d.event_a, d.event_b])
    op = uuid.uuid4()
    schedule(ws)
    a, b = ws.get(Event, d.event_a), ws.get(Event, d.event_b)
    connected = (a.canonical_event_id or a.id) == (b.canonical_event_id or b.id)
    return Result(a.canonical_event_id or a.id, [a.id, b.id], op, [d.id], connected=connected)


def preview(ws: Scoped, *, undo_ids=(), extra=()) -> Plan:
    """What grouping would result, without writing: used to show the actual outcome of an
    undo (another path may keep events together) before the user confirms."""
    rows = [d for d in decisions(ws) if d.id not in set(undo_ids)] + list(extra)
    return plan_groups(ws.all(ws.q(Event)), rows)


def record_automatic(ws: Scoped, a, b, verdict: str, *, via: str, applied: bool, reasons=(), citations=(),
                     fingerprint='', resolver_version='') -> EventIdentityDecision | None:
    """Store an automatic verdict unless a user decision already settles the pair."""
    if _current_user(ws, a, b):
        return None
    return _write(ws, a, b, verdict, 'automatic', via, None, reasons=reasons, citations=citations,
                  fingerprint=fingerprint, resolver_version=resolver_version, applied=applied)


def withdraw_for_sources(ws: Scoped, source_ids) -> set:
    """Deactivate automatic decisions citing removed/changed sources; returns touched events."""
    ids = {str(x) for x in source_ids}
    touched = set()
    for d in ws.all(ws.q(EventIdentityDecision).where(EventIdentityDecision.origin == 'automatic',
                                                      EventIdentityDecision.active.is_(True))):
        if any(c and str(c[0]) in ids for c in d.citations):
            _supersede(d, 'source_changed')
            touched |= {d.event_a, d.event_b}
    if touched:
        ws.delete(EventPairScan, or_(EventPairScan.event_a.in_(touched), EventPairScan.event_b.in_(touched)))
    ws.s.flush()
    return touched


def withdraw_automatic(ws: Scoped, event_ids=None, dry_run=True) -> list:
    """Operator rollback of automatic grouping. User corrections and evidence are kept."""
    ev.lock(ws)
    rows = [d for d in decisions(ws, event_ids) if d.origin == 'automatic' and d.verdict == 'same' and d.applied]
    if not dry_run:
        for d in rows:
            _supersede(d, 'operator_withdrawn')
        ws.s.flush()
        refresh(ws, {x for d in rows for x in (d.event_a, d.event_b)})
    return rows


# ---------------- read models for pages and reports

def history(ws: Scoped, rep_id) -> list[EventIdentityDecision]:
    """Grouping history: your corrections and automatic same/separate decisions. Uncertain
    verdicts are review suggestions, and replaced automatic rows are superseded bookkeeping."""
    ids = [m.id for m in members(ws, rep_id)]
    rows = ws.all(ws.q(EventIdentityDecision).where(
        or_(EventIdentityDecision.event_a.in_(ids), EventIdentityDecision.event_b.in_(ids)))
        .order_by(EventIdentityDecision.created_at.desc(), EventIdentityDecision.id))
    return [d for d in rows if d.origin == 'user' or (d.verdict != 'uncertain' and d.superseded_reason != 'replaced')]


def source_union(ws: Scoped, rep_id, visible_only=True) -> list[str]:
    ids = [m.id for m in members(ws, rep_id)]
    found = sorted({sid for m in ws.all(ws.q(EventMention).where(EventMention.event_id.in_(ids))) for sid in m.evidence})
    if not visible_only or not found:
        return found
    return sorted(str(x) for x in ws.s.scalars(ws.q(Source, Source.id).where(
        Source.id.in_([uuid.UUID(x) for x in found]), Source.included.is_(True), Source.deleted_at.is_(None))))


def member_sources(ws: Scoped, event_id) -> list[str]:
    return sorted({sid for m in ws.all(ws.q(EventMention).where(EventMention.event_id == event_id)) for sid in m.evidence})
