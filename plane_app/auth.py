"""Signed session cookies and who is asking: the admin (the timetabler) or a department account.

Deny by default: `require_session`, on almost every route, lets only the admin through. The few
routes a head of department may use (design §2) take `require_user` instead."""
from __future__ import annotations

import hmac
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import db as db_mod
from . import users

COOKIE = "session"
MAX_AGE = 30 * 24 * 3600
ADMIN = "admin"
# the routes an HOD who must still change their password may use
MUST_CHANGE_OPEN = frozenset({"/", "/api/me", "/api/me/password"})     # /logout needs no login at all


@dataclass(frozen=True)
class Principal:
    sid: str
    user_id: str               # "admin" or the account's id
    role: str                  # "admin" | "hod"
    dept: str | None
    name: str
    must_change: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN


def make_session_cookie(secret: str, sid: str, user: str = ADMIN, v: int = 0) -> str:
    return URLSafeTimedSerializer(secret, salt="session").dumps({"sid": sid, "user": user, "v": v})


def read_session_cookie(secret: str, value: str | None) -> dict | None:
    """{"sid", "user", "v"}; a cookie from before accounts (a bare session id) reads as the admin."""
    if not value:
        return None
    try:
        data = URLSafeTimedSerializer(secret, salt="session").loads(value, max_age=MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if isinstance(data, str) and data:
        return {"sid": data, "user": ADMIN, "v": 0}
    if (isinstance(data, dict) and isinstance(data.get("sid"), str) and data["sid"]
            and isinstance(data.get("user"), str) and isinstance(data.get("v"), int)):
        return {"sid": data["sid"], "user": data["user"], "v": data["v"]}
    return None


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def new_session_id() -> str:
    return secrets.token_urlsafe(24)


def _resolve(request: Request) -> Principal | None:
    data = read_session_cookie(request.app.state.config.secret_key, request.cookies.get(COOKIE))
    if data is None:
        return None
    if data["user"] == ADMIN:
        return Principal(data["sid"], ADMIN, ADMIN, None, "Timetabler")
    user = users.get_user(request.app.state.db, data["user"])
    if user is None or not user.get("active") or int(user.get("session_version", 0)) != data["v"]:
        return None                        # deleted, deactivated, or the password was reset or changed
    return Principal(data["sid"], user["id"], "hod", user["dept"], user["name"], bool(user.get("must_change")))


def principal(request: Request) -> Principal | None:
    """Who sent this request, or None when nobody valid is logged in. Worked out once per request."""
    if not hasattr(request.state, "principal"):
        request.state.principal = _resolve(request)
    return request.state.principal


def _not_logged_in(request: Request) -> HTTPException:
    if request.url.path.startswith("/api/"):
        return HTTPException(401, "not logged in")
    return HTTPException(303, headers={"Location": "/login"})


def require_session(request: Request) -> str:
    """Admin only. Returns the session id (it also keys the chat rate limit)."""
    p = principal(request)
    if p is None:
        raise _not_logged_in(request)
    if not p.is_admin:
        raise HTTPException(403, "not available to department accounts")
    return p.sid


def check_user(request: Request) -> Principal:
    """The admin or a department account; an account that must change its password reaches only
    the password change (and the page that shows it)."""
    p = principal(request)
    if p is None:
        raise _not_logged_in(request)
    if p.must_change and not p.is_admin and request.url.path not in MUST_CHANGE_OPEN:
        raise HTTPException(403, "change your password first")
    return p


def _user_and_pin(request: Request) -> tuple[Principal, str | None]:
    """(who, the timetable to pin their request to): the deployment timetable for a department
    account, None for the admin. Sync, so its database reads run in the threadpool."""
    p = check_user(request)
    return p, (None if p.is_admin else users.deployment_timetable(request.app.state.db))


async def require_user(pin: tuple[Principal, str | None] = Depends(_user_and_pin)) -> AsyncIterator[Principal]:
    """For the routes on the department allowlist (design §2) only. A department account's request
    works on the deployment timetable for its whole length (design §3); the admin's is left alone.

    Async on purpose: FastAPI runs a sync dependency in the threadpool on a *copy* of the request's
    context, so a ContextVar set there is gone before the endpoint runs (and its reset fails). An
    async dependency runs in the request's own task: the endpoint sees the pin (a sync endpoint gets
    a copy of that context in its worker thread) and the pin is reset in the same context once the
    response is done, on success or error."""
    who, tid = pin
    if tid is None:
        yield who
        return
    with db_mod.pinned(tid):
        yield who
