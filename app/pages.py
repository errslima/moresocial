"""Server-rendered screens, scoped through ctx.ws. Synchronous handlers run in
FastAPI's thread pool: blocking DB/provider operations must not run on the shared
public/internal event loop. A request's session is used sequentially, never concurrently.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select

from . import accounts, ai, config, diagnostics, events as automatic_events, gatherings, ingest, memory, retrieval, security, whatsapp
from .models import (Answer, AnswerSource, Claim, ClaimSource, Connection, Draft, Event, EventCandidate, EventPerson,
                     Exclusion, Job, Person, PersonIdentifier, RelationshipNote, SelfProfile, Source,
                     SourceParticipant, SyncStream, WhatsAppConnector)
from .auth_routes import throttle
from .web import SESSION_COOKIE, Ctx, Problem, optional_user, redirect, render, require_mutation, require_user

router = APIRouter()
CLAIM_LABELS = {'interest': 'Interest', 'life_update': 'Life update', 'preference': 'Preference',
                'commitment': 'Commitment', 'note': 'Note'}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def found(obj, what='That item'):
    if obj is None:
        raise Problem(404, f'{what} was not found.')
    return obj


def ai_error(exc: Exception) -> str:
    if isinstance(exc, ai.BudgetExhausted) and exc.scope == 'userkey':
        return ("Today's Moresocial limit for your own API key is used up. AI features resume tomorrow; "
                'everything else keeps working.')
    if isinstance(exc, ai.BudgetExhausted):
        return "Today's AI budget is used up. AI features resume tomorrow; everything else keeps working."
    if isinstance(exc, ai.InvalidOutput):
        return 'The assistant returned something unusable. Please try again.'
    return 'The assistant is unavailable right now. Please try again later.'


def ai_failure(purpose: str, exc: Exception) -> str | None:
    """Record an expected AI failure the user sees; return the reference to show them."""
    code = ('budget_exhausted' if isinstance(exc, ai.BudgetExhausted) else
            'invalid_output' if isinstance(exc, ai.InvalidOutput) else
            'key_rejected' if isinstance(exc, ai.KeyRejected) else 'unavailable')
    detail = exc.scope if isinstance(exc, ai.BudgetExhausted) else str(exc).split(':')[-1]
    security.emit('ai_action_failed', level='warn', purpose=purpose, code=code, reason=detail,
                  http_status=getattr(exc, 'http_status', None))
    return diagnostics.current().get('request_id')


def claim_view(ws, claims: list[Claim]) -> list[dict]:
    from .observation_groups import bundles
    return [{**item, 'label': CLAIM_LABELS.get(item['claim'].category, item['claim'].category)}
            for item in bundles(ws, claims)]


# ---------------- welcome / home ----------------

@router.get('/')
def welcome(request: Request, ctx: Ctx | None = Depends(optional_user)):
    if ctx is not None:
        return redirect('/home')
    return render(request, 'welcome.html', None)


@router.get('/home')
def home(request: Request, ctx: Ctx = Depends(require_user)):
    ws = ctx.ws
    conn = ws.first(ws.q(Connection).where(Connection.provider == 'google'))
    events = automatic_events.cards(ws)[:5]
    sources = ws.s.scalar(select(func.count()).select_from(Source).where(Source.workspace_id == ws.wid))
    return render(request, 'home.html', ctx, steps=gatherings.next_steps(ws), conn=conn, events=events, sources=sources,
                  budget=ai.budget_state(ws.wid), event_tz=automatic_events.TZ)


@router.post('/home/steps')
def step_state(request: Request, key: str = Form(...), action: str = Form('dismiss'),
                     ctx: Ctx = Depends(require_mutation)):
    gatherings.hide_step(ctx.ws, key, 7 if action == 'snooze' else None)
    return redirect('/home')


# ---------------- people ----------------

@router.get('/people')
def people(request: Request, q: str = '', ctx: Ctx = Depends(require_user)):
    ws = ctx.ws
    stmt = ws.q(Person)
    if q.strip():
        like = '%' + q.strip()[:100].replace('%', '').replace('_', '') + '%'
        stmt = stmt.where(or_(Person.display_name.ilike(like), Person.id.in_(
            select(PersonIdentifier.person_id).where(PersonIdentifier.workspace_id == ws.wid, PersonIdentifier.value.ilike(like)))))
    rows = ws.all(stmt.order_by(Person.pinned.desc(), Person.display_name).limit(200))
    idents = {}
    for i in ws.all(ws.q(PersonIdentifier).where(PersonIdentifier.person_id.in_([p.id for p in rows]))):
        label = i.value
        if i.kind == 'whatsapp':
            number, _, domain = i.value.partition('@')
            if domain not in ('c.us', 's.whatsapp.net') or not number.isascii() or not number.isdigit():
                continue  # LIDs are opaque identities, never phone numbers.
            label = '+' + number
        labels = idents.setdefault(i.person_id, [])
        if label not in labels:
            labels.append(label)
    return render(request, 'people.html', ctx, people=rows, idents=idents, q=q)


@router.get('/people/{pid}')
def person(request: Request, pid: str, ctx: Ctx = Depends(require_user)):
    ws = ctx.ws
    p = found(ws.get(Person, pid), 'That person')
    identifiers = ws.all(ws.q(PersonIdentifier).where(PersonIdentifier.person_id == p.id))
    claims = ws.all(ws.q(Claim).where(Claim.person_id == p.id).order_by(Claim.status, Claim.created_at.desc()))
    notes = ws.all(ws.q(RelationshipNote).where(RelationshipNote.person_id == p.id).order_by(RelationshipNote.created_at.desc()))
    timeline = ws.all(ws.q(Source).where(Source.id.in_(select(SourceParticipant.source_id).where(
        SourceParticipant.workspace_id == ws.wid, SourceParticipant.person_id == p.id)), Source.deleted_at.is_(None))
        .order_by(Source.occurred_at.desc()).limit(50))
    others = ws.all(ws.q(Person).where(Person.id != p.id).order_by(Person.display_name).limit(500))
    return render(request, 'person.html', ctx, person=p, identifiers=identifiers, claims=claim_view(ws, claims), notes=notes,
                  timeline=timeline, others=others)


@router.post('/people/{pid}/claims/{cid}')
def person_claim(pid: str, cid: str, action: str = Form(...), value: str = Form(''), ctx: Ctx = Depends(require_mutation)):
    ws = ctx.ws
    claim = found(ws.get(Claim, cid), 'That memory')
    back = f'/people/{claim.person_id}' if claim.person_id else '/me'
    from . import observation_groups
    try:
        observation_groups.act(ws, claim, action, value)
    except ValueError as exc:
        raise Problem(400, str(exc))
    return redirect(back)


@router.post('/people/{pid}/claims')
def add_claim(pid: str, category: str = Form('note'), value: str = Form(...), ctx: Ctx = Depends(require_mutation)):
    ws = ctx.ws
    p = found(ws.get(Person, pid), 'That person')
    if category not in CLAIM_LABELS or not value.strip():
        raise Problem(400, 'Enter a memory to add.')
    key = memory.claim_key(ws.wid, f'person:{p.id}', category, None, value)
    if not ws.first(ws.q(Claim).where(Claim.claim_key == key)):
        ws.add(Claim(id=uuid.uuid4(), subject='person', person_id=p.id, category=category, value=value.strip()[:300],
                     basis='user', status='confirmed', origin='user', valid_from=utcnow(), claim_key=key))
    return redirect(f'/people/{p.id}')


@router.post('/people/{pid}/notes')
def add_note(pid: str, body: str = Form(...), kind: str = Form('note'), ctx: Ctx = Depends(require_mutation)):
    ws = ctx.ws
    p = found(ws.get(Person, pid), 'That person')
    if kind not in ('note', 'preference', 'shared_history') or not body.strip():
        raise Problem(400, 'Enter a note.')
    ws.add(RelationshipNote(id=uuid.uuid4(), person_id=p.id, kind=kind, body=body.strip()[:4000]))
    from . import suggestions
    suggestions.person_changed(ws, p.id)
    return redirect(f'/people/{p.id}')


@router.post('/people/{pid}/notes/{nid}/delete')
def delete_note(pid: str, nid: str, ctx: Ctx = Depends(require_mutation)):
    note = found(ctx.ws.get(RelationshipNote, nid), 'That note')
    from . import suggestions
    suggestions.person_changed(ctx.ws, note.person_id)
    ctx.s.delete(note)
    return redirect(f'/people/{note.person_id}')


@router.post('/people/{pid}/edit')
def edit_person(pid: str, name: str = Form(''), pinned: str = Form(''), ctx: Ctx = Depends(require_mutation)):
    p = found(ctx.ws.get(Person, pid), 'That person')
    if name.strip():
        p.display_name, p.name_source = name.strip()[:200], 'user'
    p.pinned = pinned == 'yes'
    return redirect(f'/people/{p.id}')


@router.post('/people/{pid}/link')
def link(pid: str, other: str = Form(...), ctx: Ctx = Depends(require_mutation)):
    ws = ctx.ws
    keep, merge = found(ws.get(Person, pid), 'That person'), found(ws.get(Person, other), 'That person')
    ingest.link_people(ws, keep, merge)
    return redirect(f'/people/{keep.id}')


@router.post('/people/{pid}/unlink')
def unlink(pid: str, identifier: str = Form(...), ctx: Ctx = Depends(require_mutation)):
    ws = ctx.ws
    ident = found(ws.get(PersonIdentifier, identifier), 'That identifier')
    new = ingest.unlink_identifier(ws, ident)
    return redirect(f'/people/{new.id if new else ident.person_id}')


# ---------------- sources ----------------

@router.get('/sources')
def sources(request: Request, provider: str = '', q: str = '', ctx: Ctx = Depends(require_user)):
    ws = ctx.ws
    stmt = ws.q(Source).where(Source.deleted_at.is_(None))
    if provider in ('gmail', 'calendar', 'whatsapp', 'diary'):
        stmt = stmt.where(Source.provider == provider)
    if q.strip():
        like = '%' + q.strip()[:100].replace('%', '') + '%'
        stmt = stmt.where(or_(Source.title.ilike(like), Source.body.ilike(like), Source.conversation_title.ilike(like)))
    rows = ws.all(stmt.order_by(Source.occurred_at.desc().nulls_last()).limit(200))
    return render(request, 'sources.html', ctx, sources=rows, provider=provider, q=q)


@router.get('/sources/{sid}')
def source(request: Request, sid: str, ctx: Ctx = Depends(require_user)):
    ws = ctx.ws
    src = found(ws.get(Source, sid), 'That source')
    participants = ws.all(ws.q(SourceParticipant).where(SourceParticipant.source_id == src.id))
    people = {p.id: p for p in ws.all(ws.q(Person).where(Person.id.in_([x.person_id for x in participants if x.person_id])))}
    return render(request, 'source.html', ctx, source=src, participants=participants, people=people)


@router.post('/sources/{sid}/exclude')
def exclude(sid: str, scope: str = Form('object'), ctx: Ctx = Depends(require_mutation)):
    src = found(ctx.ws.get(Source, sid), 'That source')
    if scope not in ('object', 'conversation'):
        raise Problem(400, 'Unknown scope.')
    ingest.exclude(ctx.ws, src, scope)
    return redirect('/sources')


@router.post('/exclusions/{eid}/remove')
def remove_exclusion(eid: str, ctx: Ctx = Depends(require_mutation)):
    row = found(ctx.ws.get(Exclusion, eid), 'That exclusion')
    ctx.s.delete(row)
    return redirect('/connections')


# ---------------- ask ----------------

@router.get('/ask')
def ask_page(request: Request, ctx: Ctx = Depends(require_user)):
    recent = ctx.ws.all(ctx.ws.q(Answer).order_by(Answer.created_at.desc()).limit(10))
    people_ = ctx.ws.all(ctx.ws.q(Person).order_by(Person.display_name).limit(500))
    return render(request, 'ask.html', ctx, recent=recent, people=people_, error=None, reference=None)


@router.post('/ask')
def ask(request: Request, question: str = Form(...), person: str = Form(''), ctx: Ctx = Depends(require_mutation)):
    if not question.strip():
        raise Problem(400, 'Type a question.')
    ws = ctx.ws
    p = ws.get(Person, person) if person else None
    try:
        answer = retrieval.ask(ws, question, p.id if p else None)
    except (ai.AIUnavailable, ai.BudgetExhausted, ai.InvalidOutput) as exc:
        reference = ai_failure('answer', exc)
        recent = ws.all(ws.q(Answer).order_by(Answer.created_at.desc()).limit(10))
        return render(request, 'ask.html', ctx, recent=recent, people=ws.all(ws.q(Person).limit(500)), error=ai_error(exc),
                      reference=reference, status=503)
    return redirect(f'/answers/{answer.id}')


@router.get('/answers/{aid}')
def answer(request: Request, aid: str, ctx: Ctx = Depends(require_user)):
    ws = ctx.ws
    a = found(ws.get(Answer, aid), 'That answer')
    cites = ws.all(ws.q(AnswerSource).where(AnswerSource.answer_id == a.id).order_by(AnswerSource.label))
    srcs = {s.id: s for s in ws.all(ws.q(Source).where(Source.id.in_([c.source_id for c in cites])))}
    return render(request, 'answer.html', ctx, answer=a, cites=cites, sources=srcs)


# ---------------- events ----------------

@router.get('/gatherings')
def gatherings_page(ctx: Ctx = Depends(require_user)):
    from fastapi.responses import RedirectResponse
    return RedirectResponse(config.get().url('/events'), status_code=301)


@router.post('/gatherings')
@router.post('/events')
def create_gathering(ctx: Ctx = Depends(require_mutation)):
    raise Problem(405, 'Events are recognized automatically from WhatsApp, email and Calendar.')


@router.get('/gatherings/{eid}')
def old_gathering(eid: str, ctx: Ctx = Depends(require_user)):
    from fastapi.responses import RedirectResponse
    found(ctx.ws.get(Event, eid), 'That event')
    return RedirectResponse(config.get().url('/events/' + eid), status_code=301)


@router.get('/events')
def events_page(request: Request, view: str = 'upcoming', ctx: Ctx = Depends(require_user)):
    if view not in ('upcoming', 'past', 'dismissed'):
        view = 'upcoming'
    from zoneinfo import ZoneInfo
    from .models import Job
    from . import event_resolver
    pending = ctx.ws.s.scalar(ctx.ws.q(Job, func.count()).where(Job.kind == 'detect_events', Job.status.in_(['queued', 'running'])))
    review = event_resolver.review_pairs(ctx.ws) if view == 'upcoming' else []
    return render(request, 'events.html', ctx, events=automatic_events.cards(ctx.ws, view), view=view, review=review,
                  counts={v: len(automatic_events.cards(ctx.ws, v)) for v in ('upcoming', 'past', 'dismissed')},
                  pending=pending, tz=ZoneInfo('Europe/Amsterdam'), roles=automatic_events.ROLES)


@router.get('/events/{eid}')
def gathering(request: Request, eid: str, ctx: Ctx = Depends(require_user)):
    from .models import EventMention
    from zoneinfo import ZoneInfo
    from . import event_identity, event_resolver
    e = found(ctx.ws.get(Event, eid), 'That event')
    if e.canonical_event_id:
        # Temporary: a later split must be able to give this identity its own page again.
        from fastapi.responses import RedirectResponse
        return RedirectResponse(config.get().url('/events/' + str(e.canonical_event_id)), status_code=307)
    members = event_identity.members(ctx.ws, e.id)
    mentions = ctx.ws.all(ctx.ws.q(EventMention).where(EventMention.event_id.in_([m.id for m in members])))
    ids = {uuid.UUID(sid) for m in mentions for sid in m.evidence}
    evidence = ctx.ws.all(ctx.ws.q(Source).where(Source.id.in_(ids), Source.included.is_(True), Source.deleted_at.is_(None))
                          .order_by(Source.occurred_at, Source.id)) if ids else []
    people_ = {str(p.id): p for p in ctx.ws.all(ctx.ws.q(Person))}
    source_names = {i.value: people_[str(i.person_id)].display_name for i in ctx.ws.all(ctx.ws.q(PersonIdentifier))
                    if str(i.person_id) in people_}
    visible = {str(x.id): x for x in evidence}
    records = []
    for m in members:
        own = automatic_events.own_projection(ctx.ws, m)
        sids = [x for x in event_identity.member_sources(ctx.ws, m.id) if x in visible]
        records.append({'event': m, 'fields': own[0] if own else None, 'sources': [visible[x] for x in sids]})
    names = {}
    for d in event_identity.history(ctx.ws, e.id):
        for x in (d.event_a, d.event_b):
            if x not in names:
                other = ctx.ws.get(Event, x)
                names[x] = other
    choices = [c for c in automatic_events.cards(ctx.ws) + automatic_events.cards(ctx.ws, 'past')
               + automatic_events.cards(ctx.ws, 'dismissed') if c.id != e.id and c.origin != 'legacy'][:100]
    return render(request, 'event.html', ctx, event=e, evidence=evidence, people=people_,
                  tz=ZoneInfo(e.timezone), roles=automatic_events.ROLES, statuses=automatic_events.LABELS, source_names=source_names,
                  records=records, history=event_identity.history(ctx.ws, e.id), history_events=names,
                  state=event_identity.state_token(ctx.ws, e.id), dismissed=automatic_events.group_dismissed(ctx.ws, e),
                  choices=choices, visible=visible, reason_text=event_resolver.reason_text,
                  earlier=[dict(h, when=automatic_events.dt(h['starts_at'])) for h in e.details.get('history', [])
                           if automatic_events.dt(h.get('starts_at'))])


@router.post('/events/{eid}/dismiss')
def dismiss_event(eid: str, action: str = Form('dismiss'), ctx: Ctx = Depends(require_mutation)):
    from . import event_identity
    e = event_identity.representative(ctx.ws, found(ctx.ws.get(Event, eid), 'That event'))
    automatic_events.set_dismissed(ctx.ws, e, action != 'restore')
    return redirect('/events' if action != 'restore' else '/events/' + str(e.id))


def _identity_errors(fn):
    from . import event_identity
    try:
        return fn()
    except event_identity.Stale as exc:
        raise Problem(409, str(exc))
    except event_identity.Conflict as exc:
        raise Problem(409, str(exc))
    except LookupError:
        raise Problem(404, 'That event was not found.')
    except ValueError as exc:
        raise Problem(400, str(exc))


@router.post('/events/identity/same')
def events_same(a: str = Form(...), b: str = Form(...), state: str = Form(''), back: str = Form(''),
                ctx: Ctx = Depends(require_mutation)):
    """Same event: a correction of detected identities, never manual event creation."""
    from . import event_identity
    ea, eb = found(ctx.ws.get(Event, a), 'That event'), found(ctx.ws.get(Event, b), 'That event')
    ra = event_identity.representative(ctx.ws, ea)
    rb = event_identity.representative(ctx.ws, eb)
    def run():
        if back == 'detail':
            return event_identity.same(ctx.ws, [ra.id, rb.id], state=state, rep_for_state=ra.id)
        if state != event_identity.pair_token(ctx.ws, ra.id, rb.id):
            raise event_identity.Stale('These events changed since the page was loaded. Review them again.')
        return event_identity.same(ctx.ws, [ra.id, rb.id])
    result = _identity_errors(run)
    return redirect('/events/' + str(result.representative), notice='events-grouped')


@router.post('/events/identity/separate')
def events_separate(a: str = Form(...), b: str = Form(...), state: str = Form(''), ctx: Ctx = Depends(require_mutation)):
    from . import event_identity
    found(ctx.ws.get(Event, a), 'That event'), found(ctx.ws.get(Event, b), 'That event')
    _identity_errors(lambda: event_identity.separate(ctx.ws, a, b, state=state))
    return redirect('/events', notice='events-separate')


@router.get('/events/{eid}/split')
def split_preview(request: Request, eid: str, member: str = '', ctx: Ctx = Depends(require_user)):
    from zoneinfo import ZoneInfo
    from . import event_identity
    e = found(ctx.ws.get(Event, eid), 'That event')
    if e.canonical_event_id:
        raise Problem(404, 'That event was not found.')
    members = event_identity.members(ctx.ws, e.id)
    moving = found(next((m for m in members if str(m.id) == member), None), 'That record')
    if len(members) < 2:
        raise Problem(400, 'This event has a single detected record.')
    visible = {str(x.id): x for x in ctx.ws.all(ctx.ws.q(Source).where(Source.included.is_(True), Source.deleted_at.is_(None),
        Source.id.in_([uuid.UUID(x) for x in event_identity.source_union(ctx.ws, e.id, visible_only=False)])))}
    def part(ms):
        ids = sorted({x for m in ms for x in event_identity.member_sources(ctx.ws, m.id) if x in visible},
                     key=lambda x: (visible[x].occurred_at, x))
        return {'records': ms, 'sources': [visible[x] for x in ids]}
    parts = [part([moving]), part([m for m in members if m.id != moving.id])]
    return render(request, 'event_split.html', ctx, event=e, moving=moving, parts=parts, tz=ZoneInfo(e.timezone),
                  state=event_identity.state_token(ctx.ws, e.id))


@router.post('/events/{eid}/split')
def split_event(eid: str, member: str = Form(...), state: str = Form(''), ctx: Ctx = Depends(require_mutation)):
    from . import event_identity
    e = found(ctx.ws.get(Event, eid), 'That event')
    members = [m.id for m in event_identity.members(ctx.ws, e.id)]
    moving = [m for m in members if str(m) == member]
    if not moving or e.canonical_event_id:
        raise Problem(404, 'That record was not found.')
    _identity_errors(lambda: event_identity.split(ctx.ws, e.id, [moving, [m for m in members if m not in moving]], state=state))
    return redirect('/events/' + str(event_identity.representative(ctx.ws, e).id), notice='events-separated')


@router.get('/events/{eid}/undo')
def undo_preview(request: Request, eid: str, decision: str = '', ctx: Ctx = Depends(require_user)):
    """Undoing one correction may leave another path connecting the group: show the result."""
    from zoneinfo import ZoneInfo
    from .models import EventIdentityDecision
    from . import event_identity
    e = found(ctx.ws.get(Event, eid), 'That event')
    d = ctx.ws.get(EventIdentityDecision, decision)
    if e.canonical_event_id or d is None or d.origin != 'user' or not d.active:
        raise Problem(404, 'That correction is no longer active.')
    plan = event_identity.preview(ctx.ws, undo_ids=[d.id])
    groups = {}
    for m in event_identity.members(ctx.ws, e.id):
        groups.setdefault(plan.rep[m.id], []).append(m)
    return render(request, 'event_undo.html', ctx, event=e, decision=d, a=ctx.ws.get(Event, d.event_a),
                  b=ctx.ws.get(Event, d.event_b), groups=list(groups.values()), tz=ZoneInfo(e.timezone),
                  state=event_identity.state_token(ctx.ws, e.id))


@router.post('/events/{eid}/undo')
def undo_correction(eid: str, decision: str = Form(...), state: str = Form(''), confirm: str = Form(''),
                    ctx: Ctx = Depends(require_mutation)):
    from . import event_identity
    e = found(ctx.ws.get(Event, eid), 'That event')
    result = _identity_errors(lambda: event_identity.undo(ctx.ws, decision, state=state, rep_for_state=e.id))
    target = event_identity.representative(ctx.ws, e)
    return redirect('/events/' + str(target.id), notice='events-undone-connected' if result.connected else 'events-undone')


# ---------------- about me ----------------

@router.get('/me')
def me(request: Request, page: int = 1, saved: str = '', ctx: Ctx = Depends(require_user)):
    from . import diary
    ws = ctx.ws
    page = max(1, page)
    observations = ws.all(ws.q(Claim).where(Claim.subject == 'self').order_by(Claim.status, Claim.created_at.desc()))
    entries = diary.entries(ws, (page - 1) * 20, 21)
    return render(request, 'me.html', ctx, observations=claim_view(ws, observations), entries=entries[:20],
                  more=len(entries) > 20, page=page, entry_token=str(uuid.uuid4()), saved=saved == 'yes')


@router.post('/me')
def add_diary(body: str = Form(''), entry_token: str = Form(...), ctx: Ctx = Depends(require_mutation)):
    from . import diary
    from .repo import as_uuid
    token = as_uuid(entry_token)
    if token is None:
        raise Problem(400, 'Reload the page and try again.')
    try:
        diary.save(ctx.ws, body, token=str(token))
    except ValueError as exc:
        raise Problem(400, str(exc))
    return redirect('/me', saved='yes')


@router.post('/me/diary/{sid}/edit')
def edit_diary(sid: str, body: str = Form(''), ctx: Ctx = Depends(require_mutation)):
    from . import diary
    entry = found(ctx.ws.get(Source, sid), 'That diary entry')
    if entry.provider != 'diary':
        raise Problem(404, 'Diary entry not found.')
    try:
        diary.save(ctx.ws, body, entry=entry)
    except ValueError as exc:
        raise Problem(400, str(exc))
    return redirect('/me', saved='yes')


@router.post('/me/diary/{sid}/delete')
def delete_diary(sid: str, ctx: Ctx = Depends(require_mutation)):
    entry = found(ctx.ws.get(Source, sid), 'That diary entry')
    if entry.provider != 'diary':
        raise Problem(404, 'Diary entry not found.')
    ingest.exclude(ctx.ws, entry, 'object')
    return redirect('/me')


# ---------------- connections / settings ----------------

@router.get('/connections')
def connections(request: Request, ctx: Ctx = Depends(require_user)):
    return connections_page(request, ctx)


def connections_page(request: Request, ctx: Ctx, status: int = 200):
    ws = ctx.ws
    conn = ws.first(ws.q(Connection).where(Connection.provider == 'google'))
    streams = {st.stream: st for st in ws.all(ws.q(SyncStream))} if conn else {}
    counts = dict(ws.s.execute(select(Source.provider, func.count()).where(Source.workspace_id == ws.wid,
                                                                          Source.deleted_at.is_(None))
                               .group_by(Source.provider)).all())
    wa = ws.first(ws.q(WhatsAppConnector))
    exclusions = ws.all(ws.q(Exclusion).order_by(Exclusion.created_at.desc()).limit(100))
    ai_jobs = ws.s.execute(select(Job.kind, Job.status, func.count()).where(
        Job.workspace_id == ws.wid, Job.kind.in_(['embed_chunk', 'extract_chunk', 'rebuild_conversation']),
        Job.status.in_(['queued', 'running'])).group_by(Job.kind, Job.status)).all()
    settings = config.get()
    operator = ai.provider()
    assistant = {'tier': 'operator' if operator.can_generate() else 'none',
                 'generation_model': operator.generation_model,
                 'embedding_model': operator.embedding_model,
                 'shared': True}
    return render(request, 'connections.html', ctx, status=status, conn=conn, streams=streams, counts=counts, wa=wa,
                  exclusions=exclusions, scopes={'gmail': config.GMAIL_SCOPE, 'calendar': config.CALENDAR_SCOPE},
                  pending=sum(r[2] for r in ai_jobs), budget=ai.budget_state(ws.wid), limits=settings,
                  ai_configured=assistant['tier'] != 'none', assistant=assistant)


@router.post('/connections/google/sync')
def google_sync_now(ctx: Ctx = Depends(require_mutation)):
    ctx.ws.update(SyncStream, SyncStream.status != 'disabled', SyncStream.next_run_at.is_(None), next_run_at=utcnow())
    ctx.ws.update(SyncStream, SyncStream.status != 'disabled', SyncStream.next_run_at > utcnow(), next_run_at=utcnow())
    return redirect('/connections')


@router.post('/connections/google/disconnect')
def google_disconnect(ctx: Ctx = Depends(require_mutation)):
    accounts.disconnect_google(ctx.s, ctx.ws.wid)
    return redirect('/connections')


@router.post('/connections/google/delete-data')
def google_delete(ctx: Ctx = Depends(require_mutation)):
    ingest.delete_provider_data(ctx.ws, ['gmail', 'calendar'])
    ctx.ws.update(SyncStream, cursor=None, progress={}, items_seen=0, truncated=False, last_success_at=None,
                  status='paused', next_run_at=None)
    return redirect('/connections')


@router.post('/connections/whatsapp/connect')
def wa_connect(ctx: Ctx = Depends(require_mutation)):
    whatsapp.request_connect(ctx.ws)
    return redirect('/connections')


@router.post('/connections/whatsapp/disconnect')
def wa_disconnect(remove: str = Form(''), ctx: Ctx = Depends(require_mutation)):
    whatsapp.stop(ctx.ws, remove_session=(remove == 'yes'))
    return redirect('/connections')


@router.post('/connections/whatsapp/delete-data')
def wa_delete(ctx: Ctx = Depends(require_mutation)):
    ingest.delete_provider_data(ctx.ws, ['whatsapp'])
    return redirect('/connections')


@router.get('/api/whatsapp/status.json')
def wa_status(ctx: Ctx = Depends(require_user)):
    return JSONResponse(whatsapp.status(ctx.ws), headers={'Cache-Control': 'no-store'})


@router.post('/account/delete')
def delete_account(confirm: str = Form(''), ctx: Ctx = Depends(require_mutation)):
    if confirm.strip().lower() != 'delete':
        raise Problem(400, 'Type DELETE to confirm account deletion.')
    whatsapp.stop(ctx.ws, remove_session=True)
    accounts.delete_account(ctx.s, ctx.ws.wid)
    settings = config.get()
    response = redirect('/', notice='account-deleted')
    response.delete_cookie(SESSION_COOKIE, path=settings.cookie_path, secure=settings.cookie_secure, httponly=True,
                           samesite='lax')
    return response
