"""Private internal API (container port 8001; never proxied publicly).

Roles, checked at every endpoint:
- connector: a per-workspace key; may only ingest into that key's workspace.
- operator: the host provisioner's key (loopback listener); manages connector desired state.
"""
from __future__ import annotations

import re
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from . import config, db, security, whatsapp
from .models import WhatsAppConnector, Workspace, ConnectorCleanup

WORKSPACE_HEX = re.compile(r'^[0-9a-f]{32}$')
OBSERVED = {'starting', 'pairing', 'loading', 'ready', 'disconnected', 'error', 'stopped', 'removed', 'auth_failure'}


def bearer(request: Request) -> str | None:
    value = request.headers.get('authorization', '')
    return value[7:] if value.startswith('Bearer ') else None


def operator_ok(request: Request) -> bool:
    expected = config.read_secret(config.get().operator_key_file)
    return security.same(bearer(request), expected)


async def body(request: Request, limit: int) -> dict:
    raw = await request.body()
    if len(raw) > limit:
        raise ValueError('too large')
    import json
    data = json.loads(raw or b'{}')
    if not isinstance(data, dict):
        raise ValueError('not an object')
    return data


def create_internal_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get('/internal/health')
    def health():
        return {'status': 'ok'}

    @app.post('/internal/connector/ingest')
    async def ingest(request: Request):
        def authenticate():
            with db.session() as s:
                row = whatsapp.connector_for_key(s, bearer(request))
                if row is None:
                    return None
                active = s.get(Workspace, row.workspace_id)
                if active is None or active.status != 'active':
                    return None
                return row.workspace_id, row.account_wid

        access = await run_in_threadpool(authenticate)
        if access is None:
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        workspace_id, account = access
        try:
            packet = await body(request, 2_000_000)
            # Any workspace field in the payload is ignored: the key determines the workspace.
            result = await run_in_threadpool(whatsapp.ingest, workspace_id, packet,
                                            packet.get('account') if account is None else account)
        except ValueError:
            return JSONResponse({'error': 'invalid packet'}, status_code=400)
        return {'ok': True, 'created': result.created, 'updated': result.updated, 'unchanged': result.unchanged,
                'skipped': result.skipped}

    @app.get('/internal/manage/connectors')
    def desired(request: Request):
        if not operator_ok(request):
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        with db.session() as s:
            rows = s.scalars(select(WhatsAppConnector)).all()
            out = [{'workspace': r.workspace_id.hex, 'desired': r.desired, 'observed': r.observed,
                    'key': security.decrypt(r.key_enc) if r.desired == 'running' else None} for r in rows]
            out.extend({'workspace': r.workspace_id.hex, 'desired': 'deleted', 'observed': 'pending', 'key': None}
                       for r in s.scalars(select(ConnectorCleanup)))
            count = s.scalar(select(func.count()).select_from(Workspace))
        return JSONResponse({'connectors': out, 'image': config.get().connector_image, 'complete': True,
                             'workspace_count': count}, headers={'Cache-Control': 'no-store'})

    @app.post('/internal/manage/connectors/{workspace}/observed')
    async def observed(workspace: str, request: Request):
        if not operator_ok(request):
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        if not WORKSPACE_HEX.match(workspace):
            return JSONResponse({'error': 'invalid workspace'}, status_code=400)
        try:
            data = await body(request, 4096)
        except ValueError:
            return JSONResponse({'error': 'invalid'}, status_code=400)
        state = data.get('state')
        if state not in OBSERVED:
            return JSONResponse({'error': 'invalid state'}, status_code=400)

        def persist():
            with db.session() as s:
                cleanup = s.get(ConnectorCleanup, uuid.UUID(workspace), with_for_update=True)
                if cleanup is not None:
                    if state == 'removed' and s.get(Workspace, cleanup.workspace_id) is None:
                        s.delete(cleanup)
                        return {'ok': True, 'deleted': True}
                    return {'ok': True, 'pending_cleanup': True}
                row = s.scalars(select(WhatsAppConnector).where(WhatsAppConnector.workspace_id == uuid.UUID(workspace))
                                .with_for_update()).first()
                if row is None:
                    return {'ok': True, 'known': False}
                if state == 'removed' and row.desired == 'deleted':
                    s.delete(row)
                    return {'ok': True, 'deleted': True}
                if row.desired == 'running' and state in ('stopped', 'removed'):
                    return {'ok': True}  # stale report; desired state wins
                row.observed = state
                detail = data.get('detail')
                row.detail = detail[:300] if isinstance(detail, str) and state == 'error' else row.detail
            return {'ok': True}

        return await run_in_threadpool(persist)

    return app
