"""Assistant settings, per-person preferences, event preparation controls and shareable
updates. Every form is CSRF-protected and scoped to the signed-in workspace. Nothing here
sends a message; preparation only produces editable drafts in Suggestions."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request

from . import ai, assistant, assistant_context as ac, proactive
from .models import Event, Person, ShareableUpdate, Workspace
from .pages import ai_error, ai_failure, found
from .web import Ctx, Problem, redirect, render, require_mutation, require_user

router = APIRouter()


def settings_page(request: Request, ctx: Ctx, *, preview=None, error=None, status=200):
    ws = ctx.ws
    return render(request, 'assistant.html', ctx, status=status, settings=ac.settings(ws), style=ac.STYLE,
                  initiatives=ac.INITIATIVES, state=proactive.status(ws), preview=preview, error=error,
                  ai_ready=ai.provider().can_generate(), window=proactive.WINDOW,
                  timezone=ctx.s.get(Workspace, ws.wid).timezone)


@router.get('/assistant')
def assistant_page(request: Request, ctx: Ctx = Depends(require_user)):
    return settings_page(request, ctx)


@router.post('/assistant')
async def save_assistant(request: Request, ctx: Ctx = Depends(require_mutation)):
    form = dict((await request.form()).items())
    try:
        assistant.save_settings(ctx.ws, form)
    except assistant.Invalid as exc:
        return settings_page(request, ctx, error=str(exc), status=400)
    return redirect('/assistant', notice='assistant-saved')


@router.post('/assistant/pause')
def pause_assistant(action: str = Form(...), ctx: Ctx = Depends(require_mutation)):
    if action not in ('pause', 'resume'):
        raise Problem(400, 'Choose pause or resume.')
    assistant.set_paused(ctx.ws, action == 'pause')
    return redirect('/assistant')


@router.post('/assistant/preview')
def preview_assistant(request: Request, ctx: Ctx = Depends(require_mutation)):
    ctx.s.commit()  # the provider call runs outside the request transaction
    try:
        text = assistant.preview(ctx.ws.wid)
    except (ai.AIUnavailable, ai.InvalidOutput) as exc:
        ref = ai_failure('assistant_preview', exc)
        return settings_page(request, ctx, error=ai_error(exc) + (f' Reference: {ref}' if ref else ''), status=503)
    return settings_page(request, ctx, preview=text)


@router.post('/assistant/prepare')
def prepare_all(ctx: Ctx = Depends(require_mutation)):
    if ac.settings(ctx.ws).paused:
        raise Problem(409, 'The assistant is paused. Resume it to prepare messages.')
    proactive.request(ctx.ws)
    return redirect('/assistant', notice='assistant-preparing')


@router.post('/people/{pid}/assistant')
async def person_preferences(pid: str, request: Request, ctx: Ctx = Depends(require_mutation)):
    person = found(ctx.ws.get(Person, pid), 'That person')
    try:
        assistant.save_preferences(ctx.ws, person, dict((await request.form()).items()))
    except assistant.Invalid as exc:
        raise Problem(400, str(exc))
    return redirect(f'/people/{person.id}')


def representative(ctx, eid):
    from . import event_identity
    e = found(ctx.ws.get(Event, eid), 'That event')
    if e.origin == 'legacy':
        raise Problem(404, 'That event was not found.')
    return event_identity.representative(ctx.ws, e)


@router.post('/events/{eid}/preparation')
def event_preparation(eid: str, action: str = Form(...), ctx: Ctx = Depends(require_mutation)):
    from . import events
    if action not in ('pause', 'resume'):
        raise Problem(400, 'Choose pause or resume.')
    rep = representative(ctx, eid)
    events.set_preparation_paused(ctx.ws, rep, action == 'pause')
    return redirect(f'/events/{rep.id}')


@router.post('/events/{eid}/prepare')
def event_prepare(eid: str, ctx: Ctx = Depends(require_mutation)):
    from . import events
    rep = representative(ctx, eid)
    if ac.settings(ctx.ws).paused or events.group_preparation_paused(ctx.ws, rep) or events.group_dismissed(ctx.ws, rep):
        raise Problem(409, 'Message preparation is paused for this event or for the assistant.')
    proactive.request(ctx.ws, event_id=rep.id)
    return redirect(f'/events/{rep.id}', notice='assistant-preparing')


@router.post('/me/updates')
async def create_update(request: Request, ctx: Ctx = Depends(require_mutation)):
    form = await request.form()
    try:
        assistant.create_update(ctx.ws, dict(form.items()), form.getlist('people'))
    except assistant.Invalid as exc:
        raise Problem(400, str(exc))
    return redirect('/me', notice='update-saved')


@router.post('/me/updates/{uid}')
async def edit_update(uid: str, request: Request, ctx: Ctx = Depends(require_mutation)):
    update = found(ctx.ws.get(ShareableUpdate, uid), 'That update')
    form = await request.form()
    try:
        assistant.edit_update(ctx.ws, update, dict(form.items()), form.getlist('people'))
    except assistant.Invalid as exc:
        raise Problem(400, str(exc))
    return redirect('/me', notice='update-saved')


@router.post('/me/updates/{uid}/revoke')
def revoke_update(uid: str, ctx: Ctx = Depends(require_mutation)):
    update = found(ctx.ws.get(ShareableUpdate, uid), 'That update')
    assistant.revoke_update(ctx.ws, update)
    return redirect('/me', notice='update-revoked')
