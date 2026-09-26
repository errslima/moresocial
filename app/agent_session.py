"""Operator-only temporary browser sessions. This module registers no HTTP routes.

Run with the app's database configuration (normally inside the web container).
The output is a private Playwright storage-state file, never a printed credential.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import uuid
from urllib.parse import urlsplit

from sqlalchemy import func, select

from app import accounts, config, db, security
from app.models import Account, Session, Workspace


def issue(email: str, output: Path, minutes: int = 120) -> dict:
    """Mint one session for an existing active account; never create accounts/grants."""
    if not 1 <= minutes <= 1440:
        raise ValueError('Session lifetime must be between 1 and 1440 minutes.')
    settings = config.get()
    origin = urlsplit(settings.public_origin)
    if origin.scheme != 'https' and origin.hostname not in ('localhost', '127.0.0.1'):
        raise ValueError('Agent sessions require HTTPS except on localhost.')
    email = email.strip().lower()
    if not accounts.allowed(email):
        raise ValueError('Account is not enabled for sign-in.')
    expires = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    created_file = False
    try:
        with db.session() as s:
            acct = s.scalars(select(Account).join(Workspace, Workspace.id == Account.workspace_id)
                             .where(func.lower(Account.email) == email, Workspace.status == 'active')).one_or_none()
            if acct is None:
                raise ValueError('An existing active account is required.')
            raw = accounts.create_session(s, acct)
            s.flush()
            session = s.scalars(select(Session).where(Session.token_hash == security.digest(raw))).one()
            session.expires_at = expires
            state = {'cookies': [{
                'name': 'moresocial_session', 'value': raw, 'domain': origin.hostname,
                'path': settings.cookie_path, 'expires': int(expires.timestamp()),
                'httpOnly': True, 'secure': settings.cookie_secure, 'sameSite': 'Lax',
            }], 'origins': []}
            # Exclusive creation refuses existing files and symlinks. Never print the state.
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created_file = True
            with os.fdopen(fd, 'w') as f:
                json.dump(state, f)
                f.write('\n')
                f.flush()
                os.fsync(f.fileno())
            result = {'session_id': str(session.id), 'expires_at': expires.isoformat(),
                      'output': str(output)}
            workspace_id = acct.workspace_id
    except BaseException:
        if created_file:
            output.unlink(missing_ok=True)
        raise
    security.emit('agent_session_issued', audit=True, workspace_id=workspace_id,
                  window_s=minutes * 60)
    return result


def revoke(session_id: uuid.UUID) -> dict:
    """Revoke just the named session, leaving the user's other browsers signed in."""
    with db.session() as s:
        session = s.get(Session, session_id)
        if session is None:
            raise ValueError('Session was not found.')
        session.revoked_at = datetime.now(timezone.utc)
        workspace_id = session.workspace_id
    security.emit('agent_session_revoked', audit=True, workspace_id=workspace_id)
    return {'session_id': str(session_id), 'revoked': True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    new = commands.add_parser('issue', help='Write a private Playwright storage-state file.')
    new.add_argument('--email', required=True)
    new.add_argument('--output', type=Path, required=True)
    new.add_argument('--minutes', type=int, default=120)
    stop = commands.add_parser('revoke', help='Revoke one session by its non-secret ID.')
    stop.add_argument('--session-id', type=uuid.UUID, required=True)
    args = parser.parse_args()
    try:
        result = (issue(args.email, args.output, args.minutes) if args.command == 'issue'
                  else revoke(args.session_id))
    except Exception as exc:
        # Do not dump exception details, configuration, cookies or SQL parameters.
        parser.exit(1, f'Agent session command failed ({type(exc).__name__}). '
                    'Check account, lifetime and output path. Existing files are never overwritten.\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
