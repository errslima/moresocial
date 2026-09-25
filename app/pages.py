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

from . import accounts, ai, config, gatherings, ingest, memory, retrieval, whatsapp
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


def claim_view(ws, claims: list[Claim]) -> list[dict]:
    out = []
    for c in claims:
        rows = ws.s.execute(select(Source).join(ClaimSource, (ClaimSource.source_id == Source.id) &
                                                (ClaimSource.workspace_id == Source.workspace_id))
                            .where(ClaimSource.workspace_id == ws.wid, ClaimSource.claim_id == c.id)
                            .order_by(Source.occurred_at)).scalars().all()
        out.append({'claim': c, 'label': CLAIM_LABELS.get(c.category, c.category), 'sources': rows})
    return out


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
    events = ws.all(ws.q(Event).where(Event.status == 'planning').order_by(Event.created_at.desc()).limit(5))
    sources = ws.s.scalar(select(func.count()).select_from(Source).where(Source.workspace_id == ws.wid))
    return render(request, 'home.html', ctx, steps=gatherings.next_steps(ws), conn=conn, events=events, sources=sources,
                  budget=ai.budget_state(ws.wid))


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
        idents.setdefault(i.person_id, []).append(i)
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
    if action == 'confirm':
        claim.status = 'confirmed'
    elif action == 'reject':
        memory.reject_claim(ws, claim)
    elif action == 'edit':
        memory.edit_claim(ws, claim, value)
    else:
        raise Problem(400, 'Unknown action.')
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
    return redirect(f'/people/{p.id}')


@router.post('/people/{pid}/notes/{nid}/delete')
def delete_note(pid: str, nid: str, ctx: Ctx = Depends(require_mutation)):
    note = found(ctx.ws.get(RelationshipNote, nid), 'That note')
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
    if provider in ('gmail', 'calendar', 'whatsapp'):
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
    return render(request, 'ask.html', ctx, recent=recent, people=people_, error=None)


@router.post('/ask')
def ask(request: Request, question: str = Form(...), person: str = Form(''), ctx: Ctx = Depends(require_mutation)):
    if not question.strip():
        raise Problem(400, 'Type a question.')
    ws = ctx.ws
    p = ws.get(Person, person) if person else None
    try:
        answer = retrieval.ask(ws, question, p.id if p else None)
    except (ai.AIUnavailable, ai.BudgetExhausted, ai.InvalidOutput) as exc:
        recent = ws.all(ws.q(Answer).order_by(Answer.created_at.desc()).limit(10))
        return render(request, 'ask.html', ctx, recent=recent, people=ws.all(ws.q(Person).limit(500)), error=ai_error(exc),
                      status=503)
    return redirect(f'/answers/{answer.id}')


@router.get('/answers/{aid}')
def answer(request: Request, aid: str, ctx: Ctx = Depends(require_user)):
    ws = ctx.ws
    a = found(ws.get(Answer, aid), 'That answer')
    cites = ws.all(ws.q(AnswerSource).where(AnswerSource.answer_id == a.id).order_by(AnswerSource.label))
    srcs = {s.id: s for s in ws.all(ws.q(Source).where(Source.id.in_([c.source_id for c in cites])))}
    return render(request, 'answer.html', ctx, answer=a, cites=cites, sources=srcs)


# ---------------- gatherings ----------------

@router.get('/gatherings')
def gatherings_page(request: Request, ctx: Ctx = Depends(require_user)):
    rows = ctx.ws.all(ctx.ws.q(Event).order_by(Event.created_at.desc()))
    return render(request, 'gatherings.html', ctx, events=rows)


@router.post('/gatherings')
def create_gathering(title: str = Form(...), activity: str = Form(''), notes: str = Form(''),
                           timezone_: str = Form('Europe/Amsterdam', alias='timezone'), ctx: Ctx = Depends(require_mutation)):
    if not title.strip():
        raise Problem(400, 'Give the gathering a name.')
    if not gatherings.valid_timezone(timezone_):
        raise Problem(400, 'Unknown time zone.')
    e = ctx.ws.add(Event(id=uuid.uuid4(), title=title.strip()[:200], activity=activity.strip()[:100] or None,
                         notes=notes.strip()[:4000], timezone=timezone_))
    return redirect(f'/gatherings/{e.id}')


@router.get('/gatherings/{eid}')
def gathering(request: Request, eid: str, error: str = '', ctx: Ctx = Depends(require_user)):
    ws = ctx.ws
    e = found(ws.get(Event, eid), 'That gathering')
    cands = ws.all(ws.q(EventCandidate).where(EventCandidate.event_id == e.id).order_by(EventCandidate.starts_at))
    invitees = ws.all(ws.q(EventPerson).where(EventPerson.event_id == e.id))
    people_ = {p.id: p for p in ws.all(ws.q(Person).where(Person.id.in_([i.person_id for i in invitees])))}
    drafts = {}
    for d in ws.all(ws.q(Draft).where(Draft.event_id == e.id).order_by(Draft.created_at)):
        drafts[d.person_id] = d
    draft_cites = {d.id: ws.all(ws.q(AnswerSource).where(AnswerSource.draft_id == d.id)) for d in drafts.values()}
    replies = {i.id: gatherings.reply_evidence(ws, e, i) for i in invitees}
    suggestions = {i.id: ws.get(Source, i.suggested_source_id) for i in invitees if i.suggested_source_id}
    all_people = ws.all(ws.q(Person).order_by(Person.pinned.desc(), Person.display_name).limit(500))
    from zoneinfo import ZoneInfo
    return render(request, 'gathering.html', ctx, event=e, candidates=[(c, gatherings.calendar_conflicts(ws, c)) for c in cands],
                  invitees=invitees, people=people_, drafts=drafts, draft_cites=draft_cites, replies=replies,
                  suggestions=suggestions, all_people=all_people, statuses=gatherings.STATUS_LABELS, tz=ZoneInfo(e.timezone),
                  error={'ai': 'The assistant could not write a draft right now. You can still write one yourself.',
                         'budget': "Today's AI budget is used up; drafting resumes tomorrow."}.get(error))


@router.post('/gatherings/{eid}/edit')
def edit_gathering(eid: str, title: str = Form(...), activity: str = Form(''), notes: str = Form(''),
                         status: str = Form('planning'), ctx: Ctx = Depends(require_mutation)):
    e = found(ctx.ws.get(Event, eid), 'That gathering')
    e.title, e.activity, e.notes = title.strip()[:200] or e.title, activity.strip()[:100] or None, notes.strip()[:4000]
    e.status = status if status in ('planning', 'done', 'cancelled') else e.status
    return redirect(f'/gatherings/{e.id}')


@router.post('/gatherings/{eid}/candidates')
def add_candidate(eid: str, start: str = Form(...), hours: float = Form(2.0), ctx: Ctx = Depends(require_mutation)):
    from zoneinfo import ZoneInfo
    e = found(ctx.ws.get(Event, eid), 'That gathering')
    try:
        local = datetime.fromisoformat(start)
    except ValueError:
        raise Problem(400, 'Enter a date and time.')
    starts = (local if local.tzinfo else local.replace(tzinfo=ZoneInfo(e.timezone))).astimezone(timezone.utc)
    hours = min(max(hours, 0.25), 24)
    ctx.ws.add(EventCandidate(id=uuid.uuid4(), event_id=e.id, starts_at=starts, ends_at=starts + timedelta(hours=hours)))
    return redirect(f'/gatherings/{e.id}')


@router.post('/gatherings/{eid}/candidates/{cid}/delete')
def delete_candidate(eid: str, cid: str, ctx: Ctx = Depends(require_mutation)):
    c = found(ctx.ws.get(EventCandidate, cid), 'That time')
    ctx.s.delete(c)
    return redirect(f'/gatherings/{c.event_id}')


@router.post('/gatherings/{eid}/people')
def add_invitee(eid: str, person: str = Form(...), ctx: Ctx = Depends(require_mutation)):
    ws = ctx.ws
    e = found(ws.get(Event, eid), 'That gathering')
    p = found(ws.get(Person, person), 'That person')
    if not ws.first(ws.q(EventPerson).where(EventPerson.event_id == e.id, EventPerson.person_id == p.id)):
        ws.add(EventPerson(id=uuid.uuid4(), event_id=e.id, person_id=p.id))
    return redirect(f'/gatherings/{e.id}')


@router.post('/gatherings/{eid}/people/{epid}/status')
def invitee_status(eid: str, epid: str, status: str = Form(...), ctx: Ctx = Depends(require_mutation)):
    ep = found(ctx.ws.get(EventPerson, epid), 'That invitee')
    if status == 'remove':
        ctx.s.delete(ep)
    else:
        try:
            gatherings.set_status(ep, status)
        except ValueError:
            raise Problem(400, 'Unknown status.')
    return redirect(f'/gatherings/{ep.event_id}')


@router.post('/gatherings/{eid}/people/{epid}/suggestion')
def invitee_suggestion(eid: str, epid: str, action: str = Form(...), ctx: Ctx = Depends(require_mutation)):
    ep = found(ctx.ws.get(EventPerson, epid), 'That invitee')
    if action == 'accept' and ep.suggested_status:
        gatherings.set_status(ep, ep.suggested_status)
    else:
        ep.suggested_status, ep.suggested_source_id = None, None
    return redirect(f'/gatherings/{ep.event_id}')


@router.post('/gatherings/{eid}/check-replies')
def check_replies(eid: str, ctx: Ctx = Depends(require_mutation)):
    ws = ctx.ws
    e = found(ws.get(Event, eid), 'That gathering')
    try:
        for ep in ws.all(ws.q(EventPerson).where(EventPerson.event_id == e.id,
                                                 EventPerson.status.in_(['awaiting_reply', 'maybe']))):
            gatherings.suggest_rsvp(ws, e, ep)
    except (ai.AIUnavailable, ai.BudgetExhausted, ai.InvalidOutput) as exc:
        return redirect(f'/gatherings/{e.id}', error='budget' if isinstance(exc, ai.BudgetExhausted) else 'ai')
    return redirect(f'/gatherings/{e.id}')


@router.post('/gatherings/{eid}/people/{epid}/draft')
def draft_for(eid: str, epid: str, ctx: Ctx = Depends(require_mutation)):
    ws = ctx.ws
    e = found(ws.get(Event, eid), 'That gathering')
    ep = found(ws.get(EventPerson, epid), 'That invitee')
    existing = ws.first(ws.q(Draft).where(Draft.event_id == e.id, Draft.person_id == ep.person_id))
    try:
        if existing:
            ctx.s.delete(existing)
            ctx.s.flush()
        gatherings.create_draft(ws, e, ep)
    except (ai.AIUnavailable, ai.BudgetExhausted, ai.InvalidOutput) as exc:
        ctx.s.rollback()
        return redirect(f'/gatherings/{e.id}', error='budget' if isinstance(exc, ai.BudgetExhausted) else 'ai')
    return redirect(f'/gatherings/{e.id}')


@router.post('/drafts/{did}')
def edit_draft(did: str, text: str = Form(''), action: str = Form('save'), ctx: Ctx = Depends(require_mutation)):
    ws = ctx.ws
    d = found(ws.get(Draft, did), 'That draft')
    if action == 'save':
        if text.strip():
            d.text, d.edited = text.strip()[:2000], True
    elif action in ('shorter', 'casual'):
        try:
            gatherings.rewrite_draft(ws, d, action)
        except (ai.AIUnavailable, ai.BudgetExhausted, ai.InvalidOutput) as exc:
            return redirect(f'/gatherings/{d.event_id}', error='budget' if isinstance(exc, ai.BudgetExhausted) else 'ai')
    elif action == 'blank':
        d.text, d.edited = text.strip()[:2000] or d.text, True
    else:
        raise Problem(400, 'Unknown action.')
    return redirect(f'/gatherings/{d.event_id}')


@router.post('/gatherings/{eid}/people/{epid}/manual-draft')
def manual_draft(eid: str, epid: str, text: str = Form(...), ctx: Ctx = Depends(require_mutation)):
    ws = ctx.ws
    ep = found(ws.get(EventPerson, epid), 'That invitee')
    if text.strip():
        d = ws.first(ws.q(Draft).where(Draft.event_id == ep.event_id, Draft.person_id == ep.person_id))
        if d:
            d.text, d.edited = text.strip()[:2000], True
        else:
            ws.add(Draft(id=uuid.uuid4(), event_id=ep.event_id, person_id=ep.person_id, purpose='invitation',
                         text=text.strip()[:2000], edited=True))
    return redirect(f'/gatherings/{ep.event_id}')


# ---------------- about me ----------------

@router.get('/me')
def me(request: Request, ctx: Ctx = Depends(require_user)):
    ws = ctx.ws
    profile = ws.s.get(SelfProfile, ws.wid)
    observations = ws.all(ws.q(Claim).where(Claim.subject == 'self').order_by(Claim.status, Claim.created_at.desc()))
    return render(request, 'me.html', ctx, profile=profile, observations=claim_view(ws, observations))


@router.post('/me')
def save_me(preferences: str = Form(''), goals: str = Form(''), ctx: Ctx = Depends(require_mutation)):
    profile = ctx.s.get(SelfProfile, ctx.ws.wid)
    if profile is None:
        profile = SelfProfile(workspace_id=ctx.ws.wid)
        ctx.s.add(profile)
    profile.preferences, profile.goals = preferences.strip()[:8000], goals.strip()[:8000]
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
    paused = ws.s.scalar(select(func.count()).select_from(Job).where(Job.workspace_id == ws.wid, Job.status == 'queued',
                                                                     Job.last_error.like('AI paused%')))
    settings = config.get()
    operator = ai.provider()
    assistant = {'tier': 'operator' if operator.can_generate() else 'none',
                 'generation_model': operator.generation_model,
                 'embedding_model': operator.embedding_model,
                 'shared': True}
    return render(request, 'connections.html', ctx, status=status, conn=conn, streams=streams, counts=counts, wa=wa,
                  exclusions=exclusions, scopes={'gmail': config.GMAIL_SCOPE, 'calendar': config.CALENDAR_SCOPE},
                  pending=sum(r[2] for r in ai_jobs), ai_paused=paused, budget=ai.budget_state(ws.wid), limits=settings,
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
