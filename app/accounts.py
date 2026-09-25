"""Accounts, sessions and the Google connection lifecycle."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session as DB
from sqlalchemy.dialects.postgresql import insert

from . import config, jobs, security
from .google import GoogleError, Identity, TokenResult, expiry, provider
from .models import Account, Connection, Session, SyncStream, WhatsAppConnector, Workspace, SelfProfile, ConnectorCleanup
from .repo import Scoped

STREAM_SCOPES = {'gmail': config.GMAIL_SCOPE, 'calendar': config.CALENDAR_SCOPE}
SYNC_KINDS = ['sync_gmail', 'sync_calendar']


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def allowed(email: str) -> bool:
    settings = config.get()
    return email.lower() in settings.beta_allowlist


def account_for(s: DB, identity: Identity) -> Account:
    acct = s.scalars(select(Account).where(Account.google_sub == identity.sub).with_for_update()).first()
    if acct:
        acct.email = identity.email
        acct.display_name = identity.name or acct.display_name
    else:
        ws = Workspace(id=uuid.uuid4(), status='active')
        s.add(ws)
        s.flush()
        acct = Account(id=uuid.uuid4(), workspace_id=ws.id, google_sub=identity.sub, email=identity.email,
                       display_name=identity.name)
        s.add(acct)
        s.add(SelfProfile(workspace_id=ws.id))
    acct.last_login_at = utcnow()
    s.flush()
    return acct


def create_session(s: DB, acct: Account) -> str:
    raw = security.token(32)
    s.add(Session(id=uuid.uuid4(), workspace_id=acct.workspace_id, account_id=acct.id, token_hash=security.digest(raw),
                  csrf_token=security.token(24), expires_at=utcnow() + timedelta(days=config.get().session_days)))
    return raw


def session_for(s: DB, raw: str | None):
    if not raw or len(raw) > 200:
        return None
    row = s.execute(select(Session, Account, Workspace).join(Account, Account.id == Session.account_id)
                    .join(Workspace, Workspace.id == Session.workspace_id)
                    .where(Session.token_hash == security.digest(raw), Session.revoked_at.is_(None),
                           Session.expires_at > utcnow(), Workspace.status == 'active')).first()
    return row


def revoke_session(s: DB, raw: str | None) -> None:
    if raw:
        s.execute(update(Session).where(Session.token_hash == security.digest(raw)).values(revoked_at=utcnow()))


def store_grant(s: DB, acct: Account, identity: Identity, tokens: TokenResult) -> Connection:
    """Persist granted scopes and tokens. Never erase a stored refresh token Google omitted."""
    ws = Scoped(s, acct.workspace_id)
    conn = ws.first(ws.q(Connection).where(Connection.provider == 'google').with_for_update())
    if conn is None:
        conn = ws.add(Connection(id=uuid.uuid4(), provider='google', provider_account=identity.sub, scopes=[]))
    elif conn.provider_account != identity.sub:
        raise GoogleError('invalid', reason='different_account')
    conn.email = identity.email
    data_scopes = sorted(sc for sc in tokens.scopes if sc in STREAM_SCOPES.values())
    conn.scopes = sorted(set(tokens.scopes))
    if tokens.refresh_token:
        conn.refresh_token_enc = security.encrypt(tokens.refresh_token)
    conn.access_token_enc = security.encrypt(tokens.access_token)
    conn.access_expires_at = expiry(tokens.expires_in)
    if data_scopes and not conn.refresh_token_enc:
        conn.state, conn.detail = 'reconnect_required', 'offline_access_missing'
    else:
        conn.state, conn.detail = 'active', None
    s.flush()
    for stream, scope in STREAM_SCOPES.items():
        row = ws.first(ws.q(SyncStream).where(SyncStream.connection_id == conn.id, SyncStream.stream == stream))
        if row is None:
            row = ws.add(SyncStream(id=uuid.uuid4(), connection_id=conn.id, stream=stream, status='never_synced', progress={}))
        if scope in tokens.scopes and conn.state == 'active':
            if row.status in ('disabled', 'paused'):
                row.status = 'never_synced' if not row.last_success_at else 'ok'
            row.next_run_at = utcnow()
            row.failures = 0
        elif scope not in tokens.scopes:
            row.status = 'disabled'
            row.next_run_at = None
    s.flush()
    return conn


def access_token(s: DB, conn: Connection) -> str:
    """Valid access token, refreshing if needed. Revoked/expired grants become reconnect_required."""
    if conn.state != 'active':
        raise GoogleError('auth', reason='not_active')
    token = security.decrypt(conn.access_token_enc)
    if token and conn.access_expires_at and conn.access_expires_at > utcnow():
        return token
    refresh = security.decrypt(conn.refresh_token_enc)
    if not refresh:
        mark_reconnect(s, conn, 'offline_access_missing')
        raise GoogleError('auth', reason='no_refresh_token')
    try:
        result = provider().refresh(refresh)
    except GoogleError as exc:
        if exc.kind == 'auth':
            mark_reconnect(s, conn, 'grant_revoked_or_expired')
        raise
    conn.access_token_enc = security.encrypt(result.access_token)
    conn.access_expires_at = expiry(result.expires_in)
    if result.scopes:
        # Google reports the currently granted scopes; a scope the user withdrew disables its sync.
        conn.scopes = sorted(result.scopes)
        for stream, scope in STREAM_SCOPES.items():
            if scope not in result.scopes:
                s.execute(update(SyncStream).where(SyncStream.workspace_id == conn.workspace_id,
                                                   SyncStream.connection_id == conn.id, SyncStream.stream == stream)
                          .values(status='disabled', next_run_at=None))
    s.flush()
    return result.access_token


def mark_reconnect(s: DB, conn: Connection, reason: str) -> None:
    conn.state = 'reconnect_required'
    conn.detail = reason
    conn.access_token_enc = None
    s.execute(update(SyncStream).where(SyncStream.workspace_id == conn.workspace_id, SyncStream.connection_id == conn.id)
              .values(next_run_at=None))
    jobs.cancel(s, conn.workspace_id, SYNC_KINDS)
    s.flush()


def disconnect_google(s: DB, workspace_id: uuid.UUID) -> bool:
    """Stop future jobs, revoke at Google (best effort) and remove stored credentials."""
    ws = Scoped(s, workspace_id)
    conn = ws.first(ws.q(Connection).where(Connection.provider == 'google').with_for_update())
    if conn is None:
        return False
    refresh = security.decrypt(conn.refresh_token_enc) or security.decrypt(conn.access_token_enc)
    jobs.cancel(s, workspace_id, SYNC_KINDS)
    ws.update(SyncStream, SyncStream.connection_id == conn.id, status='paused', next_run_at=None)
    conn.refresh_token_enc = None
    conn.access_token_enc = None
    conn.state = 'disconnected'
    conn.detail = None
    s.flush()
    return provider().revoke(refresh) if refresh else True


def delete_account(s: DB, workspace_id: uuid.UUID) -> None:
    """Disable sessions and jobs, revoke Google, then delete every owned row. The host
    provisioner removes connector containers and session storage for workspaces that no
    longer exist."""
    ws_row = s.get(Workspace, workspace_id, with_for_update=True)
    if ws_row is None:
        return
    ws_row.status = 'deleting'
    if s.scalar(select(WhatsAppConnector.id).where(WhatsAppConnector.workspace_id == workspace_id)):
        # Commit the cleanup instruction atomically with the cascade; it must survive
        # even when this is the final account and the connector row disappears.
        s.execute(insert(ConnectorCleanup).values(workspace_id=workspace_id).on_conflict_do_nothing())
    s.execute(update(Session).where(Session.workspace_id == workspace_id).values(revoked_at=utcnow()))
    jobs.cancel(s, workspace_id)
    s.flush()
    try:
        disconnect_google(s, workspace_id)
    except Exception as exc:  # revocation is best effort; deletion must proceed
        security.emit('account_delete_revoke_failed', exc)
    s.execute(delete(Workspace).where(Workspace.id == workspace_id))
