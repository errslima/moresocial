"""Operator event-identity repair: `python -m app.event_reconcile <command> ...`.

Dry-run is the default: the operation runs through the same tested service as the web UI
inside a transaction that is rolled back, and the before/after report is printed as JSON.
`--apply` commits. Reports contain IDs, counts, times and digests, never source bodies.
No ad hoc SQL: every change goes through app.event_identity.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid

from sqlalchemy import func, select

from . import db, event_identity as ei, event_resolver as er, events as ev
from .models import Account, Event, EventIdentityDecision, EventPairScan, Job
from .repo import Scoped


def workspace(s, args) -> uuid.UUID:
    if args.workspace:
        return uuid.UUID(args.workspace)
    wid = s.scalar(select(Account.workspace_id).where(func.lower(Account.email) == args.email.strip().lower()))
    if wid is None:
        raise SystemExit('no account with that email')
    return wid


def ids(value: str) -> list[uuid.UUID]:
    return [uuid.UUID(x.strip()) for x in (value or '').split(',') if x.strip()]


def local(d):
    return d.astimezone(ev.TZ).isoformat() if d else None


def group(ws: Scoped, eid) -> dict:
    e = ws.get(Event, eid)
    if e is None:
        return {'event': str(eid), 'missing': True}
    rep = ei.representative(ws, e)
    members = ei.members(ws, rep.id)
    union = ei.source_union(ws, rep.id)
    d = rep.details or {}
    statuses = {}
    for p in d.get('participants', []):
        statuses[p['status']] = statuses.get(p['status'], 0) + 1
    return {'representative': str(rep.id), 'members': [str(m.id) for m in members], 'title': rep.title,
            'starts_at': local(rep.starts_at), 'ends_at': local(rep.ends_at), 'place': d.get('place'),
            'role': d.get('role'), 'status': rep.status, 'dismissed': any(m.dismissed for m in members),
            'needs_clarification': bool(d.get('needs_clarification')), 'attendance': statuses,
            'sources': len(union), 'source_digest': ev.digest(union)[:16],
            'history_times': [h['starts_at'] for h in d.get('history', [])]}


def listing(ws: Scoped, day: str | None) -> list[dict]:
    out = []
    for e in ws.all(ws.q(Event).where(Event.origin != 'legacy').order_by(Event.starts_at, Event.id)):
        if day and (not e.starts_at or ev.day(e.starts_at).isoformat() != day):
            continue
        out.append({'id': str(e.id), 'representative': str(e.canonical_event_id or e.id), 'title': e.title,
                    'starts_at': local(e.starts_at), 'ends_at': local(e.ends_at), 'status': e.status, 'origin': e.origin,
                    'created_at': e.created_at.isoformat(), 'dismissed': e.dismissed,
                    'own_sources': len(ei.member_sources(ws, e.id)), 'group_sources': len(ei.source_union(ws, e.canonical_event_id or e.id))})
    return out


def report(s, wid=None) -> dict:
    def q(model, *cols):
        stmt = select(*cols).select_from(model)
        return stmt.where(model.workspace_id == wid) if wid else stmt
    decisions = s.execute(q(EventIdentityDecision, EventIdentityDecision.origin, EventIdentityDecision.via,
                            EventIdentityDecision.verdict, EventIdentityDecision.active, EventIdentityDecision.applied,
                            func.count()).group_by(EventIdentityDecision.origin, EventIdentityDecision.via,
                            EventIdentityDecision.verdict, EventIdentityDecision.active, EventIdentityDecision.applied)).all()
    scans = s.execute(q(EventPairScan, EventPairScan.status, EventPairScan.verdict, func.count())
                      .group_by(EventPairScan.status, EventPairScan.verdict)).all()
    jobs = s.execute(q(Job, Job.status, func.count()).where(Job.kind == 'resolve_events').group_by(Job.status)).all()
    errors = s.execute(q(Job, Job.last_error, func.count()).where(Job.kind == 'resolve_events', Job.last_error.is_not(None))
                       .group_by(Job.last_error)).all()
    grouped = s.scalar(q(Event, func.count()).where(Event.canonical_event_id.is_not(None)))
    reps = s.scalar(q(Event, func.count(func.distinct(Event.canonical_event_id))).where(Event.canonical_event_id.is_not(None)))
    return {'mode': ei.mode(s), 'resolver_version': er.RESOLVER_VERSION,
            'decisions': [dict(origin=o, via=v, verdict=x, active=a, applied=p, count=n) for o, v, x, a, p, n in decisions],
            'pair_scans': [dict(status=a, verdict=b, count=n) for a, b, n in scans],
            'resolve_jobs': {a: n for a, n in jobs}, 'resolve_errors': {a: n for a, n in errors},
            'grouped_members': grouped, 'multi_member_groups': reps}


def main(argv=None):
    p = argparse.ArgumentParser(prog='python -m app.event_reconcile', description=__doc__)
    p.add_argument('command', choices=['list', 'same', 'separate', 'split', 'undo', 'mode', 'withdraw-automatic',
                                       'candidates', 'resolve', 'report', 'recompute'])
    p.add_argument('--email')
    p.add_argument('--workspace')
    p.add_argument('--date', help='YYYY-MM-DD in Europe/Amsterdam (list)')
    p.add_argument('--events', default='', help='comma-separated original event IDs')
    p.add_argument('--parts', default='', help='split partition: IDs separated by commas, parts by |')
    p.add_argument('--decision')
    p.add_argument('--value', choices=list(ei.MODES))
    p.add_argument('--apply', action='store_true', help='commit (default is a rolled-back dry run)')
    args = p.parse_args(argv)
    out = {'command': args.command, 'applied': bool(args.apply)}
    with db.session() as s:
        if args.command == 'mode':
            out['before'] = ei.mode(s)
            if args.value:
                out['rebuild_workspaces'] = [str(w) for w in ei.set_mode(s, args.value, 'operator')]
                for w in out['rebuild_workspaces']:
                    ws = Scoped(s, uuid.UUID(w))
                    ev.lock(ws)
                    ei.refresh(ws)
            out['after'] = ei.mode(s)
            return finish(s, args, out)
        if args.command == 'report' and not (args.email or args.workspace):
            out['report'] = report(s)
            return finish(s, args, out)
        if not (args.email or args.workspace):
            p.error('--email or --workspace is required')
        wid = workspace(s, args)
        ws = Scoped(s, wid)
        out['workspace'] = str(wid)
        selected = ids(args.events)
        if args.command == 'list':
            out['events'] = listing(ws, args.date)
        elif args.command == 'report':
            out['report'] = report(s, wid)
        elif args.command in ('same', 'separate', 'split', 'undo', 'withdraw-automatic'):
            touched = selected or [x for part in args.parts.split('|') for x in ids(part)]
            if args.command == 'undo':
                d = ws.get(EventIdentityDecision, args.decision)
                touched = [d.event_a, d.event_b] if d else []
            out['before'] = [group(ws, x) for x in touched]
            if args.command == 'same':
                r = ei.same(ws, selected, via='operator')
            elif args.command == 'separate':
                if len(selected) != 2:
                    p.error('separate takes exactly two --events')
                r = ei.separate(ws, *selected, via='operator')
            elif args.command == 'split':
                parts = [ids(x) for x in args.parts.split('|')]
                first = ws.get(Event, parts[0][0])
                r = ei.split(ws, ei.representative(ws, first).id, parts, via='operator')
            elif args.command == 'undo':
                r = ei.undo(ws, args.decision)
            else:
                rows = ei.withdraw_automatic(ws, selected or None, dry_run=False)
                r = None
                out['withdrawn'] = [str(d.id) for d in rows]
            if r is not None:
                out['operation'] = str(r.operation)
                out['decisions_written'] = [str(x) for x in r.decisions]
            out['after'] = [group(ws, x) for x in touched]
        elif args.command == 'recompute':
            # Re-project group cards after a projection change; decisions and evidence are untouched.
            ev.lock(ws)
            reps = ws.all(ws.q(Event).where(Event.canonical_event_id.is_(None), Event.origin != 'legacy'))
            before = {e.id: ev.digest([e.title, e.activity, e.starts_at, e.ends_at, e.status, e.details]) for e in reps}
            for e in reps:
                ev.recompute_group(ws, e)
            ws.s.flush()
            out['groups'] = len(reps)
            out['changed'] = [str(e.id) for e in reps
                              if before[e.id] != ev.digest([e.title, e.activity, e.starts_at, e.ends_at, e.status, e.details])]
        elif args.command in ('candidates', 'resolve'):
            todo, stats = er.candidates(ws, selected)
            out['stats'] = stats
            out['candidates'] = [{'a': str(c.a), 'b': str(c.b), 'priority': c.priority, 'signals': c.signals} for c in todo]
            if args.command == 'resolve' and args.apply:
                from . import jobs
                token = ev.digest([[str(x) for x in selected], ev.now().isoformat()])[:32]
                # A targeted payload includes identities outside the default recent window.
                out['enqueued'] = jobs.enqueue(s, wid, 'resolve_events', 'resolve:operator:' + token,
                                               {'events': [str(x) for x in selected]} if selected else {})
        return finish(s, args, out)


def finish(s, args, out):
    if args.apply:
        s.commit()
    else:
        s.rollback()
    json.dump(out, sys.stdout, indent=2, default=str)
    sys.stdout.write('\n')
    return out


if __name__ == '__main__':
    try:
        main()
    except (ei.Conflict, ei.Stale, LookupError, ValueError) as exc:
        json.dump({'error': str(exc)}, sys.stdout)
        sys.stdout.write('\n')
        sys.exit(2)
