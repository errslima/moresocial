"""Suggestion inbox, evidence, copyable drafts and per-chat pause controls."""
from datetime import timedelta
import uuid

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, or_, select

from . import ai, suggestions
from .models import Job, Person, PersonIdentifier, Suggestion, SuggestionReview, WhatsAppConnector, Workspace
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


def view_data(ctx, row, review):
    return {'row': row, 'person': ctx.ws.get(Person, row.person_id), 'review': review,
            'evidence': suggestions.evidence(ctx.ws, row)}


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
    connector = ws.first(ws.q(WhatsAppConnector))
    stale_sync = not connector or connector.observed != 'ready' or connector.updated_at < now - timedelta(minutes=10)
    pending = ws.s.scalar(ws.q(Job, func.count()).where(Job.kind == 'review_suggestions', Job.status.in_(['queued', 'running'])))
    failures = ws.s.scalar(ws.q(Job, func.count()).where(Job.kind == 'review_suggestions', Job.status == 'failed',
        Job.updated_at > now - timedelta(days=1)))
    labels = dict(ws.s.execute(ws.q(PersonIdentifier, PersonIdentifier.value, Person.display_name)
        .join(Person, (Person.id == PersonIdentifier.person_id) & (Person.workspace_id == PersonIdentifier.workspace_id))
        .where(PersonIdentifier.kind == 'whatsapp')).all())
    chats = [{'review': r, 'name': labels.get(r.conversation_id, 'WhatsApp conversation')} for r in reviews.values()]
    last = max((r.checked_at for r in reviews.values() if r.checked_at), default=None)
    return render(request, 'suggestions.html', ctx, cards=cards, kinds=suggestions.KINDS, tab=tab, page=page,
        more=len(rows) > 5, chats=sorted(chats, key=lambda x: x['name']), pending=pending, failures=failures,
        stale_sync=stale_sync, last_checked=last, ai_ready=ai.provider().can_generate(),
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


def detail_page(request, ctx, row, error=None, status=200):
    review = ctx.ws.get(SuggestionReview, row.review_id)
    snap = suggestions.snapshot(ctx.ws, review) if not review.paused else None
    return render(request, 'suggestion.html', ctx, status=status, **view_data(ctx, row, review),
        kinds=suggestions.KINDS, messages=snap[0]['messages'][-30:] if snap else [], error=error,
        timezone=ctx.s.get(Workspace, ctx.ws.wid).timezone)


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
    except (ai.AIUnavailable, ai.InvalidOutput, ai.BudgetExhausted) as exc:
        ref = ai_failure('suggestion_draft', exc)
        ctx.s.expire_all()
        return detail_page(request, ctx, found(ctx.ws.get(Suggestion, sid)), ai_error(exc) + (f' Reference: {ref}' if ref else ''), 503)
    return redirect(f'/suggestions/{sid}')


@router.post('/suggestions/{sid}/draft/save')
def save(sid: uuid.UUID, text: str = Form(''), revision: int = Form(...), review_revision: int = Form(...),
         ctx: Ctx = Depends(require_mutation)):
    row, review = locked_item(ctx, sid)
    if row.draft_revision != revision or review.revision != review_revision:
        raise Problem(409, 'This draft or conversation changed. Reload before saving your edit.')
    if row.stale or review.paused or row.state not in suggestions.OPEN or not suggestions.evidence(ctx.ws, row):
        raise Problem(409, 'This suggestion needs a fresh review before editing its draft.')
    if not text.strip() or len(text) > 4000:
        raise Problem(400, 'Enter a message of 1–4000 characters.')
    row.draft, row.draft_revision, row.draft_review_revision = text.strip(), revision + 1, review.revision
    return redirect(f'/suggestions/{sid}')
