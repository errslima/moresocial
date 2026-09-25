"""Runtime configuration. Values come from the environment; secrets come from files.

Nothing here logs or returns secret values. `load()` validates the combination of
settings so that synthetic providers can never run on the production origin.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

PRODUCTION_ORIGIN = 'https://1f517.com'
GOOGLE_SCOPES_IDENTITY = ('openid', 'email', 'profile')
GMAIL_SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
CALENDAR_SCOPE = 'https://www.googleapis.com/auth/calendar.events.readonly'
GOOGLE_SCOPES = GOOGLE_SCOPES_IDENTITY + (GMAIL_SCOPE, CALENDAR_SCOPE)
USER_KEY_PROVIDERS = ('anthropic', 'openai')  # AI providers whose API keys users may add
DEFAULT_ANTHROPIC_MODEL = 'claude-sonnet-5'
DEFAULT_OPENAI_MODEL = 'gpt-5.6-terra'


class ConfigError(RuntimeError):
    pass


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == '':
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


def _int(env, name, default):
    raw = env.get(name)
    if raw in (None, ''):
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f'{name} must be an integer') from None


def read_secret(path: str | Path | None) -> str | None:
    if not path:
        return None
    try:
        value = Path(path).read_text().strip()
    except OSError:
        return None
    return value or None


@dataclass(frozen=True)
class GoogleClient:
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uris: tuple[str, ...] = ()


@dataclass(frozen=True)
class Settings:
    mode: str                      # production | development | test
    public_origin: str
    base_path: str
    database_url: str = field(repr=False)
    synthetic_providers: bool
    google_client_file: str | None
    encryption_key_file: str | None
    operator_key_file: str | None
    beta_allowlist: frozenset[str]
    admin_emails: frozenset[str]   # accounts that may open /admin (global AI settings)
    ai_provider: str               # anthropic | fake | none
    anthropic_api_key_file: str | None
    anthropic_model: str
    anthropic_fallbacks: bool
    voyage_api_key_file: str | None
    embedding_model: str
    embedding_dimension: int
    ai_daily_tokens_workspace: int
    ai_daily_tokens_global: int
    user_ai_keys: tuple[str, ...]  # providers users may add their own key for; empty = feature off
    openai_model: str
    openai_reasoning: bool
    ai_daily_tokens_user_key: int
    ai_concurrency: int
    connector_cap: int
    connector_image: str
    trusted_proxy_ips: frozenset[str]
    session_days: int = 30
    gmail_days: int = 90
    gmail_max_messages: int = 500
    calendar_past_days: int = 90
    calendar_future_days: int = 90
    calendar_max_events: int = 1000
    poll_seconds: int = 300
    backup_retention_days: int = 14

    @property
    def app_url(self) -> str:
        return self.public_origin + self.base_path

    @property
    def google_redirect_uri(self) -> str:
        return self.app_url + '/api/auth/google/callback'

    @property
    def cookie_path(self) -> str:
        return self.base_path + '/'

    @property
    def cookie_secure(self) -> bool:
        parts = urlsplit(self.public_origin)
        return parts.scheme == 'https' or parts.hostname in ('localhost', '127.0.0.1')

    @property
    def is_production(self) -> bool:
        return self.mode == 'production'

    def url(self, path: str = '/') -> str:
        """Browser-facing path under the public prefix. `path` must be app-relative."""
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('app paths must start with a single slash')
        return self.base_path + path

    def google_client(self) -> GoogleClient | None:
        return load_google_client(self.google_client_file)


def load_google_client(path: str | None) -> GoogleClient | None:
    """Load Google's downloaded web-client JSON (the nested `web` object)."""
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text())['web']
        return GoogleClient(client_id=str(data['client_id']), client_secret=str(data['client_secret']),
                            redirect_uris=tuple(str(u) for u in data.get('redirect_uris') or ()))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def load(env: dict | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    mode = env.get('MORESOCIAL_MODE', 'development')
    if mode not in ('production', 'development', 'test'):
        raise ConfigError('MORESOCIAL_MODE must be production, development or test')
    origin = env.get('PUBLIC_ORIGIN', PRODUCTION_ORIGIN if mode == 'production' else 'http://localhost:8772').rstrip('/')
    parts = urlsplit(origin)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.path or parts.query:
        raise ConfigError('PUBLIC_ORIGIN must be a scheme and host only, e.g. https://1f517.com')
    base_path = env.get('BASE_PATH', '/moresocial').rstrip('/')
    if not base_path.startswith('/') or '//' in base_path or any(c in base_path for c in '?#'):
        raise ConfigError('BASE_PATH must look like /moresocial')
    database_url = env.get('DATABASE_URL', '')
    if not database_url:
        pw = read_secret(env.get('DATABASE_PASSWORD_FILE'))
        if pw and env.get('DATABASE_HOST'):
            database_url = (f"postgresql+psycopg://{env.get('DATABASE_USER', 'moresocial')}:{pw}@"
                            f"{env['DATABASE_HOST']}:{env.get('DATABASE_PORT', '5432')}/{env.get('DATABASE_NAME', 'moresocial')}")
    if not database_url:
        raise ConfigError('DATABASE_URL (or DATABASE_HOST with DATABASE_PASSWORD_FILE) is required')
    synthetic = _bool(env.get('SYNTHETIC_PROVIDERS'))
    if synthetic and (mode == 'production' or origin == PRODUCTION_ORIGIN):
        raise ConfigError('Synthetic providers cannot be enabled in production or on the production URL')
    if mode == 'production':
        if origin.startswith('http://'):
            raise ConfigError('Production requires an https PUBLIC_ORIGIN')
        for name in ('GOOGLE_CLIENT_FILE', 'ENCRYPTION_KEY_FILE', 'OPERATOR_KEY_FILE'):
            if not env.get(name):
                raise ConfigError(f'{name} is required in production')
    ai_provider = env.get('AI_PROVIDER', 'fake' if synthetic else 'none')
    if ai_provider not in ('anthropic', 'fake', 'none'):
        raise ConfigError('AI_PROVIDER must be anthropic, fake or none')
    if ai_provider == 'fake' and (mode == 'production' or origin == PRODUCTION_ORIGIN):
        raise ConfigError('The deterministic fake AI provider is for tests and development only')
    user_ai_keys = tuple(dict.fromkeys(p.strip().lower() for p in env.get('USER_AI_KEYS', '').split(',') if p.strip()))
    if any(p not in USER_KEY_PROVIDERS for p in user_ai_keys):
        raise ConfigError('USER_AI_KEYS may only list ' + ', '.join(USER_KEY_PROVIDERS))
    admins = frozenset(e.strip().lower() for e in env.get('ADMIN_EMAILS', '').split(',') if e.strip())
    allow = frozenset(e.strip().lower() for e in env.get('BETA_ALLOWLIST', '').split(',') if e.strip())
    proxies = frozenset(p.strip() for p in env.get('TRUSTED_PROXY_IPS', '127.0.0.1').split(',') if p.strip())
    return Settings(
        mode=mode, public_origin=origin, base_path=base_path, database_url=database_url,
        synthetic_providers=synthetic,
        google_client_file=env.get('GOOGLE_CLIENT_FILE') or None,
        encryption_key_file=env.get('ENCRYPTION_KEY_FILE') or None,
        operator_key_file=env.get('OPERATOR_KEY_FILE') or None,
        beta_allowlist=allow,
        admin_emails=admins,
        ai_provider=ai_provider,
        anthropic_api_key_file=env.get('ANTHROPIC_API_KEY_FILE') or None,
        anthropic_model=env.get('ANTHROPIC_MODEL') or DEFAULT_ANTHROPIC_MODEL,
        anthropic_fallbacks=_bool(env.get('ANTHROPIC_REFUSAL_FALLBACKS'), True),
        voyage_api_key_file=env.get('VOYAGE_API_KEY_FILE') or None,
        embedding_model=env.get('EMBEDDING_MODEL', 'fake-hash-64' if ai_provider == 'fake' else 'voyage-3.5'),
        embedding_dimension=_int(env, 'EMBEDDING_DIMENSION', 64 if ai_provider == 'fake' else 1024),
        ai_daily_tokens_workspace=_int(env, 'AI_DAILY_TOKENS_PER_WORKSPACE', 400_000),
        ai_daily_tokens_global=_int(env, 'AI_DAILY_TOKENS_GLOBAL', 2_000_000),
        user_ai_keys=user_ai_keys,
        openai_model=env.get('OPENAI_MODEL') or DEFAULT_OPENAI_MODEL,
        openai_reasoning=_bool(env.get('OPENAI_REASONING'), True),
        ai_daily_tokens_user_key=_int(env, 'AI_DAILY_TOKENS_USER_KEY', 2_000_000),
        ai_concurrency=_int(env, 'AI_CONCURRENCY', 2),
        connector_cap=_int(env, 'WHATSAPP_CONNECTOR_CAP', 2),
        connector_image=env.get('CONNECTOR_IMAGE', 'moresocial-connector:current'),
        trusted_proxy_ips=proxies,
        session_days=_int(env, 'SESSION_DAYS', 30),
        poll_seconds=_int(env, 'SYNC_POLL_SECONDS', 300),
    )


def validate_google(settings: Settings) -> list[str]:
    """Readiness problems with the Google client configuration (no secret values)."""
    if settings.synthetic_providers:
        return []
    client = settings.google_client()
    if client is None:
        return ['Google client file is missing or not a web-client JSON']
    if settings.google_redirect_uri not in client.redirect_uris:
        return ['Configured callback is not an allowed redirect URI in the Google client file']
    return []


_settings: Settings | None = None


def get() -> Settings:
    global _settings
    if _settings is None:
        _settings = load()
    return _settings


def override(settings: Settings | None) -> None:
    global _settings
    _settings = settings
