"""Suggestion inbox, evidence, copyable drafts and per-chat pause controls."""
from datetime import timedelta
import uuid

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import String, cast, func, or_, select

from . import ai, assistant_context as ac, suggestions, whatsapp_health, whatsapp_send
from .whatsapp_pages import page_url
from .models import (Event, Job, Person, PersonIdentifier, Source, Suggestion, SuggestionReview, WhatsAppConnector,
                     Workspace)
from .pages import ai_error, ai_failure, found
from .web import Ctx, Problem, redirect, render, require_mutation, require_user

router = APIRouter()


def locked_review(ctx, ident):
    return found(ctx.ws.first(ctx.ws.q(SuggestionReview).where(SuggestionReview.id == ident).with_for_update()), 'That conversation')


def locked_item(ctx, sid):
    row = found(ctx.ws.get(Suggestion, sid), 'That suggestion')
    review = locked_review(ctx, row.review_id)
    ctx.s.refresh(row, with_for_update=True)
    return row, review


def event_sources(ws, event):
    """Visible messages establishing the linked event's schedule and place."""
    prov = (event.details or {}).get('provenance') or {}
    ids = list(dict.fromkeys((prov.get('schedule') or [])[:1] + (prov.get('place') or [])[:1]))
    rows = [ws.get(Source, sid) for sid in ids]
    return [r for r in rows if r and r.included and not r.deleted_at]


def view_data(ctx, row, review):
    target = whatsapp_send.suggestion_target(ctx.ws, row, review)
    event = ac.linked_event(ctx.ws, row)
    return {'conversation_url': page_url(**target, suggestion=row.id) if target else None,
            'row': row, 'person': ctx.ws.get(Person, row.person_id), 'review': review,
            'evidence': suggestions.evidence(ctx.ws, row), 'evidence_ok': suggestions.evidence_ok(ctx.ws, row),
            'event': event, 'event_sources': event_sources(ctx.ws, event) if event else []}


@router.get('/suggestions')
def inbox(request: Request, tab: str = 'active', page: int = 1, ctx: Ctx = Depends(require_user)):
    if tab not in ('active', 'snoozed', 'history') or page < 1 or page > 10000:
        raise Problem(400, 'Choose a valid suggestions view.')
    ws, now = ctx.ws, suggestions.utcnow()
    reviews = {r.id: r for r in ws.all(ws.q(SuggestionReview))}
    q = ws.q(Suggestion).where(Suggestion.review_id.in_([r.id for r in reviews.values() if not r.paused]))
    if tab == 'active':
        q = q.where(or_(Suggestion.state == 'active', (Suggestion.state == 'snoozed') & (Suggestion.snoozed_until <= now)))
    elif tab == 'snoozed':
        q = q.where(Suggestion.state == 'snoozed', Suggestion.snoozed_until > now)
    else:
        q = q.where(Suggestion.state.in_(suggestions.TERMINAL))
    rows = ws.all(q.order_by(Suggestion.due_at.asc().nulls_last(), Suggestion.created_at.desc(), Suggestion.id)
                  .offset((page - 1) * 5).limit(6))
    cards = [view_data(ctx, r, reviews[r.review_id]) for r in rows[:5]]
    # Heartbeat-based health: row age (updated_at) is not a sync signal.
    health = whatsapp_health.classify(ws.first(ws.q(WhatsAppConnector)), now)
    pending = ws.s.scalar(ws.q(Job, func.count()).where(Job.kind == 'review_suggestions', Job.status.in_(['queued', 'running'])))
    # Count unresolved conversations, not every historical failed attempt. A later
    # queued/successful review supersedes an old failure without erasing its audit trail.
    latest = (ws.q(Job, Job.payload['review_id'].astext.label('review_id'), Job.status, Job.updated_at,
        func.row_number().over(partition_by=Job.payload['review_id'].astext,
                               order_by=(Job.created_at.desc(), Job.id.desc())).label('rank'))
        .where(Job.kind == 'review_suggestions').subquery())
    failures = ws.s.scalar(select(func.count()).select_from(latest).join(SuggestionReview,
        (latest.c.review_id == cast(SuggestionReview.id, String)) &
        (SuggestionReview.workspace_id == ws.wid)).where(latest.c.rank == 1, latest.c.status == 'failed',
        latest.c.updated_at > now - timedelta(days=1), SuggestionReview.paused.is_(False),
        or_(SuggestionReview.checked_at.is_(None), SuggestionReview.checked_at < latest.c.updated_at)))
    labels = dict(ws.s.execute(ws.q(PersonIdentifier, PersonIdentifier.value, Person.display_name)
        .join(Person, (Person.id == PersonIdentifier.person_id) & (Person.workspace_id == PersonIdentifier.workspace_id))
        .where(PersonIdentifier.kind == 'whatsapp')).all())
    chats = [{'review': r, 'name': labels.get(r.conversation_id, 'WhatsApp conversation')} for r in reviews.values()]
    last = max((r.checked_at for r in reviews.values() if r.checked_at), default=None)
    return render(request, 'suggestions.html', ctx, cards=cards, kinds=suggestions.KIND_LABELS, tab=tab, page=page,
        more=len(rows) > 5, chats=sorted(chats, key=lambda x: x['name']), pending=pending, failures=failures,
        health=health, last_checked=last, ai_ready=ai.provider().can_generate(),
        timezone=ctx.s.get(Workspace, ws.wid).timezone)


@router.post('/suggestions/review')
def refresh(ctx: Ctx = Depends(require_mutation)):
    suggestions.bootstrap(ctx.s)
    now = suggestions.utcnow()
    rows = ctx.ws.all(ctx.ws.q(SuggestionReview).where(SuggestionReview.paused.is_(False),
        or_(SuggestionReview.checked_at.is_(None), SuggestionReview.checked_at < now - timedelta(minutes=15)))
        .order_by(SuggestionReview.checked_at.asc().nulls_first()).limit(50).with_for_update())
    for row in rows:
        row.next_review_at = now
    ctx.s.flush()
    suggestions.schedule(ctx.s)
    return redirect('/suggestions')


@router.post('/suggestions/conversations/{rid}')
def pause(rid: uuid.UUID, action: str = Form(...), ctx: Ctx = Depends(require_mutation)):
    review = locked_review(ctx, rid)
    if action not in ('pause', 'resume'):
        raise Problem(400, 'Choose pause or resume.')
    review.paused = action == 'pause'
    review.revision += 1
    review.next_review_at = suggestions.utcnow() if action == 'resume' else None
    ctx.ws.update(Suggestion, Suggestion.review_id == review.id, stale=True)
    return redirect('/suggestions')


@router.get('/suggestions/{sid}')
def detail(request: Request, sid: uuid.UUID, ctx: Ctx = Depends(require_user)):
    row = found(ctx.ws.get(Suggestion, sid), 'That suggestion')
    return detail_page(request, ctx, row)


def event_choices(ws, row, message_ids):
    """Events the user may link: groups sharing this conversation's messages or where the
    person takes part, upcoming ones, and the current link. Never legacy or dismissed plans."""
    primary = ws.get(Event, row.event_id) if row.event_id else None
    found = {c['rep'].id: c['rep'] for c in ac.event_candidates(ws, source_ids=message_ids, person_id=row.person_id,
                                                                primary=primary)}
    from . import events as automatic_events
    for rep in automatic_events.cards(ws)[:10]:
        found.setdefault(rep.id, rep)
    return list(found.values())[:10]


def detail_page(request, ctx, row, error=None, status=200):
    review = ctx.ws.get(SuggestionReview, row.review_id)
    snap = suggestions.snapshot(ctx.ws, review) if not review.paused else None
    if row.producer == 'proactive' and not review.paused:
        from . import proactive
        messages = proactive.recent_messages(ctx.ws, row)
    else:
        messages = snap[0]['messages'][-30:] if snap else []
    current, fingerprint = suggestions.draft_current(ctx.ws, row, review)
    data = view_data(ctx, row, review)
    evidence_ids = [m.id for m in data['evidence']] + ([row.anchor_id] if row.anchor_id else [])
    possible = [c['rep'] for c in ac.related_candidates(ctx.ws, row, evidence_ids)] if not data['event'] else []
    choices = event_choices(ctx.ws, row, [m['id'] for m in messages])
    return render(request, 'suggestion.html', ctx, status=status, **data,
        kinds=suggestions.KIND_LABELS, messages=messages, error=error, draft_is_current=current, context_fingerprint=fingerprint,
        possible_events=possible, event_choices=choices, timezone=ctx.s.get(Workspace, ctx.ws.wid).timezone)


@router.post('/suggestions/{sid}/state')
def state(sid: uuid.UUID, action: str = Form(...), days: int = Form(1), ctx: Ctx = Depends(require_mutation)):
    row, review = locked_item(ctx, sid)
    try:
        suggestions.act(ctx.ws, row, action, days)
    except ValueError as exc:
        raise Problem(400, str(exc))
    return redirect('/suggestions')


@router.post('/suggestions/{sid}/draft')
def generate(request: Request, sid: uuid.UUID, ctx: Ctx = Depends(require_mutation)):
    row = found(ctx.ws.get(Suggestion, sid), 'That suggestion')
    # Release the request transaction before provider generation and its own publication transaction.
    ctx.s.commit()
    try:
        suggestions.draft(ctx.ws.wid, sid)
    except ValueError as exc:
        ctx.s.expire_all()
        return detail_page(request, ctx, found(ctx.ws.get(Suggestion, sid)), str(exc), 409)
    except (ai.AIUnavailable, ai.InvalidOutput) as exc:
        ref = ai_failure('suggestion_draft', exc)
        ctx.s.expire_all()
        return detail_page(request, ctx, found(ctx.ws.get(Suggestion, sid)), ai_error(exc) + (f' Reference: {ref}' if ref else ''), 503)
    return redirect(f'/suggestions/{sid}')


@router.post('/suggestions/{sid}/draft/save')
def save(sid: uuid.UUID, text: str = Form(''), revision: int = Form(...), review_revision: int = Form(...),
         context: str = Form(''), ctx: Ctx = Depends(require_mutation)):
    row, review = locked_item(ctx, sid)
    if row.draft_revision != revision or review.revision != review_revision:
        raise Problem(409, 'This draft or conversation changed. Reload before saving your edit.')
    if row.stale or review.paused or row.state not in suggestions.OPEN or not suggestions.evidence_ok(ctx.ws, row):
        raise Problem(409, 'This suggestion needs a fresh review before editing its draft.')
    if not text.strip() or len(text) > 4000:
        raise Problem(400, 'Enter a message of 1–4000 characters.')
    # Saving means the user reviewed the draft against the context shown on the page.
    built = suggestions.draft_context(ctx.ws, row, review)
    if not built or built[1] != context:
        raise Problem(409, 'The context of this suggestion changed. Reload, check it and save again.')
    row.draft, row.draft_revision, row.draft_review_revision = text.strip(), revision + 1, review.revision
    row.draft_origin, row.draft_fingerprint = 'user', built[1]
    # The edit was written against this context: excluding any of it later erases the edit too.
    row.context_sources = sorted(set(row.context_sources or []) | set(built[0]['_sources']))
    return redirect(f'/suggestions/{sid}')


@router.post('/suggestions/{sid}/event')
def correct_event(sid: uuid.UUID, event: str = Form(...), ctx: Ctx = Depends(require_mutation)):
    """Correct the related event among eligible ones; a user choice is never overridden."""
    row, review = locked_item(ctx, sid)
    if event == 'none':
        row.event_id, row.event_link = None, 'user'
        return redirect(f'/suggestions/{sid}')
    snap = suggestions.snapshot(ctx.ws, review) if not review.paused else None
    ids = [m['id'] for m in snap[0]['messages']] if snap else []
    chosen = next((e for e in event_choices(ctx.ws, row, ids) if str(e.id) == event), None)
    if chosen is None:
        raise Problem(400, 'Choose one of the listed events.')
    row.event_id, row.event_link = chosen.id, 'user'
    return redirect(f'/suggestions/{sid}')
