"""Temporary operator-issued sessions use normal auth, expiry, isolation and logout."""
import dataclasses
from datetime import datetime, timedelta, timezone
import json
import stat
import uuid

import pytest
from sqlalchemy import func, select

from app import accounts, agent_session, config, db, security
from app.models import Account, Connection, Session, Workspace


@pytest.fixture(autouse=True)
def restore_settings():
    original = config.get()
    yield
    config.override(original)


def cookie(path):
    return json.loads(path.read_text())['cookies'][0]


def count_sessions():
    with db.session() as s:
        return s.scalar(select(func.count()).select_from(Session))


def test_issue_authenticates_existing_account_without_google(make_browser, tmp_path, monkeypatch):
    owner, agent, other = make_browser(), make_browser(), make_browser()
    owner.login('alex')
    other.login('blake')
    gathering = other.post('/gatherings', {'title': 'Private to Blake', 'csrf': other.csrf()}).headers['location']
    with db.session() as s:
        grants = [(c.id, c.refresh_token_enc) for c in s.scalars(select(Connection).order_by(Connection.id))]
    def no_google(*args, **kwargs):
        raise AssertionError('Agent login must not use Google')
    monkeypatch.setattr(accounts, 'provider', no_google)
    output = tmp_path / 'state.json'
    result = agent_session.issue('Alex@example.test', output, 30)
    c = cookie(output)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert c['domain'] == 'moresocial.test' and c['path'] == '/moresocial/'
    assert c['httpOnly'] and c['secure'] and c['sameSite'] == 'Lax'
    assert c['value'] not in json.dumps(result)
    assert 29 * 60 < c['expires'] - datetime.now(timezone.utc).timestamp() <= 30 * 60
    agent.c.cookies.set(c['name'], c['value'], domain=c['domain'], path=c['path'])
    assert 'Alex Synthetic' in agent.get('/home').text
    assert agent.c.get(gathering).status_code == 404
    assert agent.post('/gatherings', {'title': 'No CSRF'}).status_code == 403
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Account)) == 2
        assert grants == [(x.id, x.refresh_token_enc) for x in s.scalars(select(Connection).order_by(Connection.id))]
    agent_session.revoke(uuid.UUID(result['session_id']))
    assert agent.get('/home').status_code == 303
    assert owner.get('/home').status_code == 200
    assert agent_session.revoke(uuid.UUID(result['session_id']))['revoked']


def test_expired_session_rejected(browser, tmp_path):
    browser.login('alex')
    output = tmp_path / 'state.json'
    result = agent_session.issue('alex@example.test', output)
    with db.session() as s:
        row = s.get(Session, uuid.UUID(result['session_id']))
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    with db.session() as s:
        assert accounts.session_for(s, cookie(output)['value']) is None


@pytest.mark.parametrize('minutes', [0, -1, 1441])
def test_lifetime_bounds(browser, tmp_path, minutes):
    browser.login('alex')
    before = count_sessions()
    with pytest.raises(ValueError):
        agent_session.issue('alex@example.test', tmp_path / 'state.json', minutes)
    assert count_sessions() == before
    assert not (tmp_path / 'state.json').exists()


def test_existing_file_or_symlink_is_never_overwritten(browser, tmp_path):
    browser.login('alex')
    output = tmp_path / 'state.json'
    output.write_text('keep this')
    link = tmp_path / 'link.json'
    link.symlink_to(output)
    before = count_sessions()
    for path in (output, link):
        with pytest.raises(FileExistsError):
            agent_session.issue('alex@example.test', path)
    assert output.read_text() == 'keep this' and link.is_symlink()
    assert count_sessions() == before


def test_unknown_disallowed_or_inactive_account_refused(browser, tmp_path):
    output = tmp_path / 'state.json'
    with pytest.raises(ValueError):
        agent_session.issue('alex@example.test', output)  # allowlisted, but not created
    browser.login('alex')
    with pytest.raises(ValueError):
        agent_session.issue('stranger@example.test', output)
    with db.session() as s:
        s.scalars(select(Workspace)).one().status = 'deleting'
    with pytest.raises(ValueError):
        agent_session.issue('alex@example.test', output)
    assert not output.exists()


def test_http_remote_origin_refused(browser, tmp_path):
    browser.login('alex')
    config.override(dataclasses.replace(config.get(), public_origin='http://remote.example'))
    with pytest.raises(ValueError):
        agent_session.issue('alex@example.test', tmp_path / 'state.json')


def test_issue_works_for_https_production_config(browser, tmp_path):
    browser.login('alex')
    config.override(dataclasses.replace(config.get(), mode='production', public_origin='https://1f517.com'))
    output = tmp_path / 'state.json'
    agent_session.issue('alex@example.test', output)
    assert cookie(output)['domain'] == '1f517.com' and cookie(output)['secure']


def test_write_failure_rolls_back_session_and_removes_file(browser, tmp_path, monkeypatch):
    browser.login('alex')
    before = count_sessions()
    def fail(*args):
        raise OSError('synthetic disk failure')
    monkeypatch.setattr(agent_session.os, 'fsync', fail)
    output = tmp_path / 'state.json'
    with pytest.raises(OSError):
        agent_session.issue('alex@example.test', output)
    assert not output.exists() and count_sessions() == before


def test_transaction_failure_removes_credential_file(browser, tmp_path, monkeypatch):
    from contextlib import contextmanager
    browser.login('alex')
    before = count_sessions()
    original = db.session
    @contextmanager
    def failing_transaction():
        with original() as s:
            yield s
            raise RuntimeError('synthetic transaction failure')
    output = tmp_path / 'state.json'
    with monkeypatch.context() as patch:
        patch.setattr(db, 'session', failing_transaction)
        with pytest.raises(RuntimeError):
            agent_session.issue('alex@example.test', output)
    assert not output.exists() and count_sessions() == before


def test_logout_revokes_agent_session(make_browser, tmp_path):
    owner, agent = make_browser(), make_browser()
    owner.login('alex')
    output = tmp_path / 'state.json'
    agent_session.issue('alex@example.test', output)
    c = cookie(output)
    agent.c.cookies.set(c['name'], c['value'], domain=c['domain'], path=c['path'])
    assert agent.post('/api/auth/logout', {'csrf': agent.csrf()}).status_code == 303
    with db.session() as s:
        assert accounts.session_for(s, c['value']) is None
    assert owner.get('/home').status_code == 200
