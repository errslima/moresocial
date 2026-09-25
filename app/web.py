"""Public web application (served behind the /moresocial prefix).

Caddy's handle_path strips /moresocial; routes here start at /. Browser URLs are built
with `settings.url()` so every link, asset, redirect and cookie keeps the prefix.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session as DB

from . import accounts, config, db, security
from .models import Account, Session
from .repo import Scoped

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / 'templates'))
# Fail loudly on a missing template variable (e.g. a macro imported without context).
import jinja2 as _jinja2
templates.env.undefined = _jinja2.StrictUndefined
SESSION_COOKIE = 'moresocial_session'
SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')

NOTICES = {
    'signed-out': 'You are signed out.',
    'google-cancelled': 'Google sign-in was cancelled. Nothing was connected.',
    'google-failed': 'Google sign-in could not be completed. Please try again.',
    'google-expired': 'That sign-in attempt expired or was already used. Please start again.',
    'not-invited': 'Moresocial is in a closed beta and this Google account is not on the list yet.',
    'different-account': 'That was a different Google account. Reconnect with the account you signed up with.',
    'unconfigured': 'Google sign-in is temporarily unavailable.',
    'account-deleted': 'Your account and its data were deleted.',
    'reconnected': 'Google access was updated.',
    'ai-key-saved': 'Your API key works and is saved. The assistant now uses it.',
    'ai-key-removed': 'Your API key was removed from Moresocial. Revoke it in the provider console if you no longer need it.',
    'admin-saved': 'Global AI settings saved. The worker picks them up within a minute.',
}


def is_admin(ctx) -> bool:
    return bool(ctx) and ctx.account.email.strip().lower() in config.get().admin_emails


class Problem(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


def get_db() -> Iterator[DB]:
    with db.session() as s:
        yield s


@dataclass
class Ctx:
    s: DB
    ws: Scoped
    account: Account
    session: Session
    raw_token: str

    @property
    def csrf(self) -> str:
        return self.session.csrf_token


def optional_user(request: Request, s: DB = Depends(get_db)) -> Ctx | None:
    row = accounts.session_for(s, request.cookies.get(SESSION_COOKIE))
    if not row:
        return None
    sess, acct, _ws = row
    return Ctx(s=s, ws=Scoped(s, sess.workspace_id), account=acct, session=sess,
               raw_token=request.cookies.get(SESSION_COOKIE))


class LoginRequired(Exception):
    pass


def require_user(ctx: Ctx | None = Depends(optional_user)) -> Ctx:
    if ctx is None:
        raise LoginRequired()
    return ctx


async def require_mutation(request: Request, ctx: Ctx = Depends(require_user)) -> Ctx:
    """State-changing requests need a session CSRF token (form field or header)."""
    token = request.headers.get('x-csrf-token')
    if token is None and request.headers.get('content-type', '').startswith(('application/x-www-form-urlencoded', 'multipart/form-data')):
        token = (await request.form()).get('csrf')
    if not security.same(str(token or ''), ctx.csrf):
        raise Problem(403, 'This form expired. Reload the page and try again.')
    return ctx


def origin_ok(request: Request) -> bool:
    settings = config.get()
    origin = request.headers.get('origin')
    if origin:
        return origin.rstrip('/') == settings.public_origin
    referer = request.headers.get('referer')
    if referer:
        parts = urlsplit(referer)
        return f'{parts.scheme}://{parts.netloc}' == settings.public_origin
    return False


def redirect(path: str, **params) -> RedirectResponse:
    settings = config.get()
    url = settings.url(path)
    if params:
        from urllib.parse import urlencode
        url += ('&' if '?' in url else '?') + urlencode(params)
    return RedirectResponse(url, status_code=303)


def render(request: Request, name: str, ctx: Ctx | None = None, status: int = 200, **data) -> HTMLResponse:
    settings = config.get()
    notice = NOTICES.get(request.query_params.get('notice', ''))
    return templates.TemplateResponse(request, name, {'ctx': ctx, 'url': settings.url, 'settings': settings,
                                                       'is_admin': is_admin(ctx),
                                                       'notice': notice, 'csrf': ctx.csrf if ctx else '', **data},
                                      status_code=status)


def csp(settings: config.Settings) -> str:
    form = "'self'" if settings.synthetic_providers else "'self' https://accounts.google.com"
    return ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
            f"font-src 'self'; form-action {form}; base-uri 'none'; frame-ancestors 'none'")


def create_app() -> FastAPI:
    settings = config.get()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, root_path=settings.base_path)
    static_files = {f.name: f for f in (HERE / 'static').iterdir() if f.is_file()}
    media = {'.css': 'text/css', '.js': 'text/javascript', '.svg': 'image/svg+xml'}

    # A plain route instead of StaticFiles: with root_path set and the prefix already stripped
    # by the proxy, Starlette's static mount resolves paths incorrectly.
    @app.get('/static/{name}')
    def static(name: str):
        f = static_files.get(name)
        if f is None:
            raise HTTPException(404)
        return FileResponse(f, media_type=media.get(f.suffix, 'application/octet-stream'),
                            headers={'Cache-Control': 'public, max-age=3600'})

    @app.middleware('http')
    async def guard(request: Request, call_next):
        path = request.url.path
        root = request.scope.get('root_path', '')
        if root and path.startswith(root):
            path = path[len(root):] or '/'
        if path.startswith('/internal'):
            return PlainTextResponse('Not found', status_code=404)
        if request.method not in SAFE_METHODS and not origin_ok(request):
            return PlainTextResponse('Cross-origin request refused', status_code=403)
        response = await call_next(request)
        response.headers.setdefault('Content-Security-Policy', csp(settings))
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('Referrer-Policy', 'same-origin')
        response.headers.setdefault('X-Frame-Options', 'DENY')
        response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        if not path.startswith('/static/'):
            response.headers.setdefault('Cache-Control', 'no-store')
        return response

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired):
        if request.url.path.endswith('.json') or request.headers.get('accept', '').startswith('application/json'):
            return JSONResponse({'error': 'Sign in required'}, status_code=401)
        return redirect('/')

    @app.exception_handler(Problem)
    async def problem(request: Request, exc: Problem):
        if request.headers.get('accept', '').startswith('application/json'):
            return JSONResponse({'error': exc.message}, status_code=exc.status)
        return render(request, 'error.html', None, status=exc.status, message=exc.message, reference=None)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        message = {404: 'Not found.', 405: 'Not allowed.'}.get(exc.status_code, 'Request refused.')
        return render(request, 'error.html', None, status=exc.status_code, message=message, reference=None)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        ref = security.emit('request_failed', exc, route=request.url.path.split('/')[1][:40] if '/' in request.url.path else None)
        return render(request, 'error.html', None, status=500, message='Something went wrong.', reference=ref)

    @app.get('/health')
    def health():
        return {'status': 'ok'}

    @app.get('/ready')
    def ready():
        checks = {'database': False, 'migrations': False, 'google_client': not config.validate_google(settings),
                  'encryption_key': (not settings.is_production) or bool(config.read_secret(settings.encryption_key_file)),
                  'ai_generation': False, 'ai_embeddings': False}
        try:
            at_head, _ = db.migration_state()
            checks['database'] = True
            checks['migrations'] = at_head
            if at_head:
                from . import ai
                operator = ai.provider()
                checks['ai_generation'] = operator.can_generate()
                checks['ai_embeddings'] = bool(operator.embedding_dimension)
        except Exception as exc:
            security.emit('readiness_failed', exc)
        # External providers (Google, AI) are reported but do not make the app unready.
        ok = checks['database'] and checks['migrations'] and checks['encryption_key']
        return JSONResponse({'status': 'ready' if ok else 'not_ready', 'checks': checks}, status_code=200 if ok else 503)

    from . import admin, auth_routes, pages
    app.include_router(auth_routes.router)
    app.include_router(pages.router)
    app.include_router(admin.router)
    if settings.synthetic_providers:
        from . import dev_routes
        app.include_router(dev_routes.router)
    return app
