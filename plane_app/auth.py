"""One password, one signed cookie."""
from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE = "session"
MAX_AGE = 30 * 24 * 3600


def make_session_cookie(secret: str, session_id: str) -> str:
    return URLSafeTimedSerializer(secret, salt="session").dumps(session_id)


def read_session_cookie(secret: str, value: str | None) -> str | None:
    if not value:
        return None
    try:
        return URLSafeTimedSerializer(secret, salt="session").loads(value, max_age=MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def new_session_id() -> str:
    return secrets.token_urlsafe(24)


def require_session(request: Request) -> str:
    sid = read_session_cookie(request.app.state.config.secret_key, request.cookies.get(COOKIE))
    if sid:
        return sid
    if request.url.path.startswith("/api/"):
        raise HTTPException(401, "not logged in")
    raise HTTPException(303, headers={"Location": "/login"})
