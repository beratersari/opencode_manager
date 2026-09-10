"""Dashboard login: username/password from settings, httpOnly session cookie.

n8n uses the same secret as Bearer or X-Amir-Mini-Token (Creasy also
accepts X-Creasy-Token).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

from opencode_manager.settings import Settings

SESSION_COOKIE = "amir_mini_session"
SESSION_MAX_AGE = 12 * 60 * 60
_SESSION_VERSION = "v1"


def dashboard_auth_required(settings: Settings) -> bool:
    return bool(settings.dashboard_token or settings.dashboard_password)


def has_username(settings: Settings) -> bool:
    return bool(settings.dashboard_user)


def _digest_eq(left: str, right: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(left.encode("utf-8")).digest(),
        hashlib.sha256(right.encode("utf-8")).digest(),
    )


def _session_secret(settings: Settings) -> bytes:
    raw = settings.dashboard_password or settings.dashboard_token or ""
    return hashlib.sha256(raw.encode("utf-8")).digest()


def credentials_ok(settings: Settings, username: str, password: str) -> bool:
    if not dashboard_auth_required(settings):
        return True
    if not password:
        return False
    if settings.dashboard_user and not _digest_eq(username, settings.dashboard_user):
        return False
    if settings.dashboard_password:
        return _digest_eq(password, settings.dashboard_password)
    if settings.dashboard_token:
        return _digest_eq(password, settings.dashboard_token)
    return False


def make_session(settings: Settings, now: int | None = None) -> str:
    ts = str(int(now if now is not None else time.time()))
    msg = f"{_SESSION_VERSION}|{ts}".encode("utf-8")
    sig = hmac.new(_session_secret(settings), msg, hashlib.sha256).hexdigest()
    return f"{_SESSION_VERSION}.{ts}.{sig}"


def session_ok(settings: Settings, value: str, *, now: int | None = None) -> bool:
    if not value or not dashboard_auth_required(settings):
        return False
    parts = value.split(".")
    if len(parts) != 3 or parts[0] != _SESSION_VERSION:
        return False
    version, ts, sig = parts
    try:
        issued = int(ts)
    except ValueError:
        return False
    current = int(now if now is not None else time.time())
    if current < issued or current - issued > SESSION_MAX_AGE:
        return False
    msg = f"{version}|{ts}".encode("utf-8")
    expected = hmac.new(_session_secret(settings), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def provided_api_token(headers: Any) -> str:
    header = (
        headers.get("x-amir-mini-token")
        or headers.get("X-Amir-Mini-Token")
        or headers.get("x-creasy-token")
        or headers.get("X-Creasy-Token")
        or ""
    ).strip()
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    return (header or bearer).strip()


def api_token_ok(settings: Settings, token: str) -> bool:
    if not settings.dashboard_token or not token:
        return False
    return _digest_eq(token, settings.dashboard_token)


def request_authenticated(settings: Settings, *, cookie: str, headers: Any) -> bool:
    if not dashboard_auth_required(settings):
        return True
    if session_ok(settings, cookie):
        return True
    return api_token_ok(settings, provided_api_token(headers))
