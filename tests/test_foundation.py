"""M0: configuration guards, migrations, health/readiness, prefix routing."""
import pytest

from app import config


BASE = {'DATABASE_URL': 'postgresql://x@localhost/x'}


def test_synthetic_mode_cannot_run_in_production_or_on_production_url():
    with pytest.raises(config.ConfigError):
        config.load({**BASE, 'MORESOCIAL_MODE': 'production', 'SYNTHETIC_PROVIDERS': '1', 'GOOGLE_CLIENT_FILE': 'x',
                     'ENCRYPTION_KEY_FILE': 'x', 'OPERATOR_KEY_FILE': 'x'})
    with pytest.raises(config.ConfigError):
        config.load({**BASE, 'MORESOCIAL_MODE': 'development', 'SYNTHETIC_PROVIDERS': '1', 'PUBLIC_ORIGIN': 'https://1f517.com'})
    with pytest.raises(config.ConfigError):
        config.load({**BASE, 'MORESOCIAL_MODE': 'development', 'AI_PROVIDER': 'fake', 'PUBLIC_ORIGIN': 'https://1f517.com'})


def test_production_requires_secret_files_and_https():
    with pytest.raises(config.ConfigError):
        config.load({**BASE, 'MORESOCIAL_MODE': 'production'})
    with pytest.raises(config.ConfigError):
        config.load({**BASE, 'MORESOCIAL_MODE': 'production', 'PUBLIC_ORIGIN': 'http://1f517.com', 'GOOGLE_CLIENT_FILE': 'x',
                     'ENCRYPTION_KEY_FILE': 'x', 'OPERATOR_KEY_FILE': 'x'})
    s = config.load({**BASE, 'MORESOCIAL_MODE': 'production', 'GOOGLE_CLIENT_FILE': 'x', 'ENCRYPTION_KEY_FILE': 'x',
                     'OPERATOR_KEY_FILE': 'x', 'AI_PROVIDER': 'anthropic'})
    assert s.google_redirect_uri == 'https://1f517.com/moresocial/api/auth/google/callback'
    assert s.cookie_path == '/moresocial/' and s.cookie_secure


def test_google_client_file_is_loaded_and_callback_validated(tmp_path):
    import json
    f = tmp_path / 'client.json'
    f.write_text(json.dumps({'web': {'client_id': 'id.apps.googleusercontent.com', 'client_secret': 'synthetic',
                                     'redirect_uris': ['https://1f517.com/moresocial/api/auth/google/callback']}}))
    s = config.load({**BASE, 'GOOGLE_CLIENT_FILE': str(f), 'PUBLIC_ORIGIN': 'https://1f517.com', 'MORESOCIAL_MODE': 'development'})
    assert config.validate_google(s) == []
    assert 'synthetic' not in repr(s.google_client())  # secret never in repr
    other = config.load({**BASE, 'GOOGLE_CLIENT_FILE': str(f), 'PUBLIC_ORIGIN': 'https://other.example', 'MORESOCIAL_MODE': 'development'})
    assert config.validate_google(other)


def test_health_ready_and_prefix(browser):
    assert browser.get('/health').json() == {'status': 'ok'}
    r = browser.get('/ready')
    assert r.status_code == 200 and r.json()['checks']['migrations'] is True
    assert 'email' not in r.text
    r = browser.c.get('/moresocial')
    assert r.status_code == 308 and r.headers['location'] == '/moresocial/'
    assert browser.c.get('/home').status_code == 404  # outside the prefix
    page = browser.get('/').text
    assert 'href="/moresocial/static/app.css"' in page
    assert 'action="/moresocial/api/auth/google/start"' in page
    assert browser.get('/static/app.css').status_code == 200


def test_public_app_rejects_internal_routes(browser):
    assert browser.get('/internal/manage/connectors').status_code == 404
    assert browser.post('/internal/connector/ingest', json={}).status_code == 404


def test_cross_origin_posts_are_refused(browser):
    r = browser.c.post('/moresocial/api/auth/google/start', data={'purpose': 'login'}, headers={'Origin': 'https://evil.example'})
    assert r.status_code == 403


def test_security_headers(browser):
    r = browser.get('/')
    assert "frame-ancestors 'none'" in r.headers['content-security-policy']
    assert "script-src 'self'" in r.headers['content-security-policy']
    assert r.headers['cache-control'] == 'no-store'


def test_runtime_role_is_not_superuser(database):
    from sqlalchemy import create_engine, text
    e = create_engine(database['runtime_url'].replace('postgresql://', 'postgresql+psycopg://', 1))
    with e.connect() as c:
        assert c.execute(text('SELECT rolsuper FROM pg_roles WHERE rolname = current_user')).scalar() is False
    e.dispose()
