"""Department accounts (heads of department): a global list under the kv key `users`.

Passwords are kept only as scrypt hashes (`scrypt$<salt hex>$<hash hex>`); nothing here returns a
hash except `get_user` and `find_by_username`, which the login and the password change need. Every
route answers with `public(user)`."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
from datetime import datetime, timezone

KEY = "users"                              # global kv key (not in PER_TIMETABLE_VALUES)
USERNAME_RE = re.compile(r"^[a-z0-9.-]{3,32}$")
RESERVED = {"admin"}
MIN_PASSWORD, MAX_PASSWORD = 10, 256
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
DKLEN = 32
EDITABLE = ("name", "dept", "active")

_lock = threading.Lock()                   # every read-modify-write of the list


class UserError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


class WrongPassword(UserError):
    """The current password given to `set_password` is wrong (the route counts these)."""
    def __init__(self):
        super().__init__("the current password is wrong", 400)


# ---- hashing -------------------------------------------------------------
def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(pw.encode(), salt=salt, dklen=DKLEN, **SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(pw: str, stored: str | None) -> bool:
    try:
        scheme, salt_hex, digest_hex = str(stored).split("$")
        salt, digest = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
    except ValueError:
        return False
    if scheme != "scrypt" or len(salt) != 16 or len(digest) != DKLEN:
        return False
    got = hashlib.scrypt(str(pw).encode(), salt=salt, dklen=DKLEN, **SCRYPT)
    return hmac.compare_digest(got, digest)


# A fixed hash to check against when the username is unknown, so a miss costs the same as a hit.
_DUMMY = hash_password(secrets.token_urlsafe(16))


def burn_time(pw: str) -> None:
    verify_password(pw, _DUMMY)


# ---- the store -----------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(db) -> list[dict]:
    return list(db.get_value(KEY) or [])


def _save(db, rows: list[dict]) -> None:
    db.set_value(KEY, rows)


def public(user: dict) -> dict:
    return {k: v for k, v in user.items() if k != "password_hash"}


def _find(rows: list[dict], uid: str) -> dict:
    for u in rows:
        if u["id"] == uid:
            return u
    raise UserError("no such account", 404)


def _text(value, what: str, limit: int) -> str:
    if value is not None and not isinstance(value, str):
        raise UserError(f"{what} must be text")
    s = (value or "").strip()
    if not s:
        raise UserError(f"{what} is required")
    if len(s) > limit:
        raise UserError(f"{what} is too long (at most {limit} characters)")
    return s


def _check_new_password(pw) -> str:
    if not isinstance(pw, str) or len(pw) < MIN_PASSWORD:
        raise UserError(f"the new password needs at least {MIN_PASSWORD} characters")
    if len(pw) > MAX_PASSWORD:
        raise UserError(f"the new password is too long (at most {MAX_PASSWORD} characters)")
    return pw


def any_active(db) -> bool:
    return any(u.get("active") for u in _load(db))


def deployment_timetable(db) -> str:
    """The timetable department accounts work on. Accounts made before the setting was stored fix
    it here, on the first read after they exist (the admin's selection at that moment)."""
    if _load(db):
        db.ensure_deployment_timetable()
    return db.deployment_timetable()


def list_users(db) -> list[dict]:
    return [public(u) for u in _load(db)]


def get_user(db, uid: str) -> dict | None:
    """The stored record, hash included: for the login and the session check only."""
    return next((u for u in _load(db) if u["id"] == uid), None)


def find_by_username(db, username: str) -> dict | None:
    name = str(username or "").strip().lower()
    return next((u for u in _load(db) if u["username"] == name), None)


def create_user(db, username: str, name: str, dept: str) -> tuple[dict, str]:
    if username is not None and not isinstance(username, str):
        raise UserError("a username is text")
    uname = (username or "").strip().lower()
    if not USERNAME_RE.match(uname):
        raise UserError("a username is 3 to 32 lowercase letters, digits, dots or hyphens")
    if uname in RESERVED:
        raise UserError(f"the username {uname!r} is reserved")
    name, dept = _text(name, "name", 80), _text(dept, "department", 40)
    temp = secrets.token_urlsafe(9)
    hashed = hash_password(temp)                   # scrypt outside the lock
    with _lock:
        rows = _load(db)
        if any(u["username"] == uname for u in rows):
            raise UserError(f"the username {uname!r} is taken", 409)
        user = {"id": "u" + secrets.token_hex(6), "username": uname, "name": name, "dept": dept, "role": "hod",
                "password_hash": hashed, "must_change": True, "active": True, "session_version": 0,
                "created": _now(), "last_login": None}
        rows.append(user)
        _save(db, rows)
    db.ensure_deployment_timetable()               # the first account fixes it to the selection (design §3)
    return public(user), temp


def update_user(db, uid: str, **fields) -> dict:
    unknown = set(fields) - set(EDITABLE)
    if unknown:
        raise UserError(f"cannot change {', '.join(sorted(unknown))}")
    clean = {}
    if "name" in fields:
        clean["name"] = _text(fields["name"], "name", 80)
    if "dept" in fields:
        clean["dept"] = _text(fields["dept"], "department", 40)
    if "active" in fields:
        if not isinstance(fields["active"], bool):
            raise UserError("active is true or false")
        clean["active"] = fields["active"]
    with _lock:
        rows = _load(db)
        user = _find(rows, uid)
        if clean.get("active") is False and user.get("active"):
            # deactivating ends every session, so reactivating later does not revive old cookies
            user["session_version"] = int(user.get("session_version", 0)) + 1
        user.update(clean)
        _save(db, rows)
    return public(user)


def reset_password(db, uid: str) -> str:
    temp = secrets.token_urlsafe(9)
    hashed = hash_password(temp)                   # scrypt outside the lock
    with _lock:
        rows = _load(db)
        user = _find(rows, uid)
        user.update(password_hash=hashed, must_change=True,
                    session_version=int(user.get("session_version", 0)) + 1)
        _save(db, rows)
    return temp


def set_password(db, uid: str, old: str, new: str) -> dict:
    """The user's own change: checks the old password; logs out every other session. The scrypt
    work runs outside the lock; the write then goes through only if the account is unchanged
    since it was read (no reset, deactivation or other change in between)."""
    new = _check_new_password(new)
    user = get_user(db, uid)
    if user is None:
        raise UserError("no such account", 404)
    if not verify_password(old if isinstance(old, str) else "", user["password_hash"]):
        raise WrongPassword()
    if new == old:
        raise UserError("choose a password different from the current one")
    hashed = hash_password(new)
    with _lock:
        rows = _load(db)
        current = _find(rows, uid)
        if (current.get("session_version", 0) != user.get("session_version", 0)
                or current["password_hash"] != user["password_hash"]):
            raise UserError("the account changed a moment ago; try again", 409)
        current.update(password_hash=hashed, must_change=False,
                       session_version=int(current.get("session_version", 0)) + 1)
        _save(db, rows)
    return public(current)


def delete_user(db, uid: str) -> None:
    with _lock:
        rows = _load(db)
        _find(rows, uid)
        _save(db, [u for u in rows if u["id"] != uid])


def touch_login(db, uid: str) -> None:
    with _lock:
        rows = _load(db)
        try:
            _find(rows, uid)["last_login"] = _now()
        except UserError:
            return
        _save(db, rows)
