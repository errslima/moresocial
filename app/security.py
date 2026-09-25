"""Token hashing, symmetric encryption for provider credentials, and diagnostics.

Credentials are encrypted with a Fernet key held outside the database. Diagnostics
(`diagnostics.emit`) carry an opaque reference and field-validated metadata only: never
tokens, codes, QR payloads, message bodies or prompts.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken

from . import config


def token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def same(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')


class CryptoUnavailable(RuntimeError):
    pass


_fernet: Fernet | None = None
_fernet_source: str | None = None


def _cipher() -> Fernet:
    global _fernet, _fernet_source
    settings = config.get()
    source = settings.encryption_key_file or ('synthetic' if settings.mode != 'production' else None)
    if _fernet is not None and _fernet_source == source:
        return _fernet
    key = config.read_secret(settings.encryption_key_file)
    if key is None:
        if settings.mode == 'production':
            raise CryptoUnavailable('Encryption key file is not configured')
        # Deterministic development/test key derived from the database URL; never used in production.
        key = base64.urlsafe_b64encode(hashlib.sha256(('dev-key:' + settings.database_url).encode()).digest()).decode()
    _fernet, _fernet_source = Fernet(key.encode()), source
    return _fernet


def encrypt(value: str) -> str:
    return _cipher().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _cipher().decrypt(value.encode()).decode()
    except InvalidToken:
        return None


def reset_cipher() -> None:
    global _fernet, _fernet_source
    _fernet = None
    _fernet_source = None


def emit(event: str, exc: BaseException | None = None, **fields) -> str:
    """Write one sanitized diagnostic event (see `diagnostics`); return its reference."""
    from . import diagnostics
    return diagnostics.emit(event, exc, **fields)
