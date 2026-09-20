"""App state in SQLite: settings, timetable instances, live and draft organisations, chat messages, uploads.

Everything that belongs to one timetable (its organisations, last check, time and rest rules,
messages and uploads) is keyed by the timetable id. Provider and engine settings are global."""
from __future__ import annotations

import copy
import json
import secrets
import sqlite3
import time
from pathlib import Path

from . import calendar as cal_mod

# The six soft rules and their default weights. A literal copy of plane_timetabling.model.DEFAULT_SOFT:
# the app never imports the engine's scoring code (see tests/test_no_engine_import.py).
RULES = ("spread", "stability", "compact", "even_days", "edge", "venue")
DEFAULT_SOFT = {"spread": 10, "stability": 8, "compact": 6, "even_days": 3, "edge": 2, "venue": 1}
# Solve presets: "close" keeps the new timetable near the live one (and warm-starts from it),
# "balanced" is the engine's default, "quality" ignores stability and pushes the students' rules.
PRESETS = {
    "close": {"stability": 40, "spread": 6, "compact": 4, "even_days": 3, "edge": 2, "venue": 1},
    "balanced": dict(DEFAULT_SOFT),
    "quality": {"stability": 0, "spread": 14, "compact": 8, "even_days": 5, "edge": 3, "venue": 1},
}
MIN_TIME_LIMIT, MAX_TIME_LIMIT = 10, 900     # the engine clamps one solve to this range, seconds

DEFAULT_SETTINGS = {
    # `days_per_week` is where the cycle's week turns over: it names the days ("Odd Mon", "Even Tue")
    # and is what reads an Availability cell written as weekdays ("Mon-Wed") onto the right slots.
    "time": {"slot_minutes": 40, "slots_per_day": 8, "start": "07:30", "days_per_week": 5,
             "labels": ["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8"], "window": "day"},
    "rules": {"max_load": 6, "max_run": 4, "mandatory_rest": []},
    "solve": {"preset": "balanced", "time_limit": 300, "weights": dict(DEFAULT_SOFT)},
    "provider": {"kind": "anthropic", "base_url": "", "api_key": "", "model": "claude-opus-5"},
    "engine": {"url": "", "key": ""},
    "calendar": {"term_start": "", "first_week": "odd", "non_teaching_dates": []},
}
PER_TIMETABLE_SETTINGS = ("time", "rules", "solve", "calendar")      # the rest (provider, engine) is global

SCHEMA = """
create table if not exists kv (k text primary key, v text not null);
create table if not exists messages (id integer primary key autoincrement, session_id text not null, role text not null,
  content text not null, created_at real not null);
create table if not exists uploads (id integer primary key autoincrement, session_id text not null, name text not null,
  path text not null, kind text not null, text text not null, created_at real not null);
create table if not exists timetables (id text primary key, name text not null, created_at real not null);
"""
DEFAULT_TIMETABLE = "default"
SCOPED_KEYS = ("org:live", "org:draft", "last_check", "solve", "tt")   # kv keys that live per timetable
PER_TIMETABLE_VALUES = ("last_check", "solve", "bookings", "changes", "change_seq", "pending", "plan", "wizard")  # get_value/set_value keys that are scoped
# The one chat thread of a timetable. Messages, uploads and pending proposals are keyed by this
# rather than by the browser's login session, so a user who logs in on another device (or after
# the cookie expired) continues the same conversation. The login session id still authenticates
# and still keys the chat rate limit.
THREAD = "timetable"
CHANGE_SNAP_PREFIX = "change_snap:"    # one kv row per change snapshot: f"{CHANGE_SNAP_PREFIX}{n}" — also scoped, by prefix
PRINT_CUSTOM_PREFIX = "print_custom:"  # one kv row per saved custom timetable: f"{PRINT_CUSTOM_PREFIX}{token}" — also scoped, by prefix


def mask_settings(settings: dict) -> dict:
    """Return a copy with the provider and engine keys replaced by "***" plus their last four characters."""
    s = copy.deepcopy(settings)
    for path in (("provider", "api_key"), ("engine", "key")):
        group, field = path
        v = s.get(group, {}).get(field, "")
        s.setdefault(group, {})[field] = ("***" + v[-4:]) if v else ""
    return s


class Db:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._con() as con:
            con.executescript(SCHEMA)
            self._migrate(con)

    # ---- timetable instances ----------------------------------------------
    def _migrate(self, con: sqlite3.Connection) -> None:
        """Bring a database from before timetable instances up to date: one 'Default' timetable owns
        the old unscoped keys, messages and uploads."""
        for table in ("messages", "uploads"):
            cols = [r["name"] for r in con.execute(f"pragma table_info({table})")]
            if "timetable_id" not in cols:
                con.execute(f"alter table {table} add column timetable_id text not null default '{DEFAULT_TIMETABLE}'")
        if con.execute("select count(*) from timetables").fetchone()[0] == 0:
            con.execute("insert into timetables values (?,?,?)", (DEFAULT_TIMETABLE, "Default", time.time()))
        for old, new in (("org:live", f"org:{DEFAULT_TIMETABLE}:live"), ("org:draft", f"org:{DEFAULT_TIMETABLE}:draft"),
                         ("last_check", f"last_check:{DEFAULT_TIMETABLE}")):
            row = con.execute("select v from kv where k=?", (old,)).fetchone()
            if row is not None:
                con.execute("insert into kv (k, v) values (?, ?) on conflict(k) do nothing", (new, row["v"]))
                con.execute("delete from kv where k=?", (old,))
        if con.execute("select 1 from kv where k='current_timetable'").fetchone() is None:
            con.execute("insert into kv values ('current_timetable', ?)", (json.dumps(DEFAULT_TIMETABLE),))
        # Fold every browser session's chat rows into the timetable's one thread (see THREAD). A
        # steady-state boot already has every row under THREAD, so check before scanning either
        # table for something to write.
        if (con.execute("select 1 from messages where session_id!=? limit 1", (THREAD,)).fetchone()
                or con.execute("select 1 from uploads where session_id!=? limit 1", (THREAD,)).fetchone()):
            for table in ("messages", "uploads"):
                con.execute(f"update {table} set session_id=? where session_id!=?", (THREAD, THREAD))
            # The fold can land two old sessions' uploads of the same name under one thread; keep only
            # the newest (highest id) per (timetable_id, name) and drop the rest. Their files are not
            # unlinked here — _migrate has no caller to hand the paths back to for cleanup, and a
            # leftover upload file is harmless (clear_uploads only ever removes rows it knows about).
            con.execute("""
                delete from uploads
                where session_id=? and id not in (
                    select max(id) from uploads where session_id=? group by timetable_id, name
                )
            """, (THREAD, THREAD))
        for row in con.execute("select k, v from kv where k like 'pending:%'").fetchall():
            try:
                rec = json.loads(row["v"])
                if not isinstance(rec, dict):
                    continue
                cards = {k: v for k, v in (rec.get("cards") or {}).items() if k == THREAD}
                runs = {k: v for k, v in (rec.get("runs") or {}).items() if k == THREAD}
                new = {"cards": cards, "runs": runs} if cards else None
                if new != rec:
                    if new is None:
                        con.execute("delete from kv where k=?", (row["k"],))
                    else:
                        con.execute("update kv set v=? where k=?", (json.dumps(new), row["k"]))
            except (ValueError, AttributeError, TypeError):
                continue

    def timetables(self) -> list[dict]:
        with self._con() as con:
            rows = con.execute("select * from timetables order by created_at, id").fetchall()
            out = []
            for r in rows:
                has = lambda k: con.execute("select 1 from kv where k=?", (k,)).fetchone() is not None
                out.append({"id": r["id"], "name": r["name"], "created_at": r["created_at"],
                            "has_live": has(f"org:{r['id']}:live"), "has_draft": has(f"org:{r['id']}:draft")})
        return out

    def current_timetable(self) -> str:
        return self._get("current_timetable") or DEFAULT_TIMETABLE

    def select_timetable(self, tid: str) -> None:
        with self._con() as con:
            if con.execute("select 1 from timetables where id=?", (tid,)).fetchone() is None:
                raise KeyError(tid)
        self._set("current_timetable", tid)

    def create_timetable(self, name: str) -> str:
        """Create an empty timetable, make it current, and return its id."""
        tid = secrets.token_urlsafe(6).lower().replace("_", "x").replace("-", "y")
        with self._con() as con:
            con.execute("insert into timetables values (?,?,?)", (tid, name.strip() or "Untitled", time.time()))
        self._set("current_timetable", tid)
        return tid

    def rename_timetable(self, tid: str, name: str) -> None:
        with self._con() as con:
            con.execute("update timetables set name=? where id=?", (name.strip() or "Untitled", tid))

    def delete_timetable(self, tid: str) -> list[str]:
        """Delete a timetable and everything scoped to it; returns upload paths to unlink.
        The last timetable can never be deleted. Deleting the current one selects another."""
        with self._con() as con:
            ids = [r["id"] for r in con.execute("select id from timetables order by created_at, id")]
            if tid not in ids:
                raise KeyError(tid)
            if len(ids) == 1:
                raise ValueError("cannot delete the last timetable")
            paths = [r["path"] for r in con.execute("select path from uploads where timetable_id=?", (tid,))]
            con.execute("delete from uploads where timetable_id=?", (tid,))
            con.execute("delete from messages where timetable_id=?", (tid,))
            con.execute("delete from kv where k in (?,?,?,?,?,?,?,?,?,?,?)",
                        (f"org:{tid}:live", f"org:{tid}:draft", f"last_check:{tid}", f"solve:{tid}", f"tt:{tid}", f"bookings:{tid}", f"changes:{tid}", f"change_seq:{tid}", f"pending:{tid}", f"plan:{tid}", f"wizard:{tid}"))
            con.execute("delete from kv where k like ?", (f"{CHANGE_SNAP_PREFIX}%:{tid}",))   # one row per snapshot; not enumerable by exact key
            con.execute("delete from kv where k like ?", (f"{PRINT_CUSTOM_PREFIX}%:{tid}",))  # one row per saved custom timetable
            con.execute("delete from timetables where id=?", (tid,))
            if self.current_timetable() == tid:
                other = next(i for i in ids if i != tid)
                con.execute("insert into kv (k, v) values ('current_timetable', ?) on conflict(k) do update set v=excluded.v",
                            (json.dumps(other),))
        return paths

    def _tid(self) -> str:
        return self.current_timetable()

    def _con(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    # ---- kv with atomic JSON writes -------------------------------------
    def _get(self, k: str):
        with self._con() as con:
            row = con.execute("select v from kv where k=?", (k,)).fetchone()
        return None if row is None else json.loads(row["v"])

    def _set(self, k: str, value) -> None:
        # SQLite writes are transactional; the "atomic" guarantee the spec asks for is that a
        # crash mid-write never leaves a half-written value, which a single UPSERT in one
        # transaction provides. No temp files are created.
        with self._con() as con:
            if value is None:
                con.execute("delete from kv where k=?", (k,))
            else:
                con.execute("insert into kv (k, v) values (?, ?) on conflict(k) do update set v=excluded.v", (k, json.dumps(value)))

    def get_settings(self) -> dict:
        """Global provider and engine settings plus the current timetable's time, rules and solve settings.
        Groups a stored document lacks (a database from before they existed) come from the defaults."""
        base = self._get("settings") or {}
        s = {k: copy.deepcopy(base.get(k, v)) for k, v in DEFAULT_SETTINGS.items()}
        per = self._get(f"tt:{self._tid()}") or {}
        for k in PER_TIMETABLE_SETTINGS:
            if k in per:
                s[k] = copy.deepcopy(per[k])
        return s

    def set_settings(self, settings: dict) -> None:
        settings = copy.deepcopy(settings)
        per = {k: settings.get(k, DEFAULT_SETTINGS[k]) for k in PER_TIMETABLE_SETTINGS}
        self._set(f"tt:{self._tid()}", per)
        self._set("settings", settings)      # the global copy keeps time/rules/solve as defaults for new timetables

    def _clean_solve(self, given: dict, current: dict) -> dict:
        """Keep the solve group well-formed whatever the caller sent: a known preset, a time limit
        within the engine's cap, and integer weights for the six rules (anything else falls back to
        the stored value)."""
        out = {}
        preset = given.get("preset", current.get("preset", "balanced"))
        out["preset"] = preset if preset in PRESETS or preset == "custom" else current.get("preset", "balanced")
        try:
            limit = int(given.get("time_limit", current.get("time_limit", 300)))
        except (TypeError, ValueError):
            limit = int(current.get("time_limit", 300))
        out["time_limit"] = min(max(limit, MIN_TIME_LIMIT), MAX_TIME_LIMIT)
        weights = given.get("weights")
        base = dict(current.get("weights") or DEFAULT_SOFT)
        if isinstance(weights, dict):
            for r in RULES:
                v = weights.get(r, base.get(r, DEFAULT_SOFT[r]))
                base[r] = max(int(v), 0) if isinstance(v, (int, float)) and not isinstance(v, bool) else base.get(r, DEFAULT_SOFT[r])
        out["weights"] = {r: base.get(r, DEFAULT_SOFT[r]) for r in RULES}
        return out

    def store_settings(self, given: dict) -> dict:
        """Clean and merge a settings body the same way `PUT /api/settings` does, store it and return
        the merged (unmasked) settings. A caller may send only some of the six groups (the wizard sends
        just `time` and `rules`); every group it leaves out keeps its current value untouched. Shared
        by the settings route and the start wizard, which must never disturb the solver, provider,
        engine or calendar groups it did not ask to change."""
        current = self.get_settings()
        if not isinstance(given, dict):
            given = {}
        groups = {k: (dict(g) if isinstance(g := given.get(k), dict) else {}) for k in ("time", "rules", "solve", "provider", "engine", "calendar")}
        groups["solve"] = self._clean_solve(groups["solve"], current.get("solve", {}))
        groups["calendar"] = cal_mod.clean(groups["calendar"], current.get("calendar", {}))
        for group, field in (("provider", "api_key"), ("engine", "key")):
            v = groups[group].get(field, "")
            if not isinstance(v, str):
                groups[group].pop(field, None)
            elif v.startswith("***"):
                groups[group][field] = current.get(group, {}).get(field, "")
        # A provider key belongs to one provider: switching kind without a fresh key stores no key.
        new_kind = groups["provider"].get("kind")
        if isinstance(new_kind, str) and new_kind != current.get("provider", {}).get("kind"):
            submitted = given.get("provider", {}).get("api_key", "") if isinstance(given.get("provider"), dict) else ""
            if not isinstance(submitted, str) or not submitted or submitted.startswith("***"):
                groups["provider"]["api_key"] = ""
        merged = {k: {**current.get(k, {}), **groups[k]} for k in groups}
        self.set_settings(merged)
        return merged

    def _is_scoped(self, key: str) -> bool:
        return key in PER_TIMETABLE_VALUES or key.startswith(CHANGE_SNAP_PREFIX) or key.startswith(PRINT_CUSTOM_PREFIX)

    def get_value(self, key: str):
        return self._get(f"{key}:{self._tid()}" if self._is_scoped(key) else key)

    def set_value(self, key: str, value) -> None:
        self._set(f"{key}:{self._tid()}" if self._is_scoped(key) else key, value)

    def get_org(self, kind: str) -> dict | None:
        assert kind in ("live", "draft")
        return self._get(f"org:{self._tid()}:{kind}")

    def set_org(self, kind: str, org: dict | None) -> None:
        assert kind in ("live", "draft")
        self._set(f"org:{self._tid()}:{kind}", org)

    def promote(self, live_org: dict, check: dict | None = None) -> None:
        """Set the live organisation, drop the draft and record the last check in one transaction."""
        tid = self._tid()
        check = check if check is not None else {"ok": True, "clashes": []}
        with self._con() as con:
            con.execute("insert into kv (k, v) values (?, ?) on conflict(k) do update set v=excluded.v",
                        (f"org:{tid}:live", json.dumps(live_org)))
            con.execute("delete from kv where k=?", (f"org:{tid}:draft",))
            con.execute("insert into kv (k, v) values (?, ?) on conflict(k) do update set v=excluded.v",
                        (f"last_check:{tid}", json.dumps(check)))

    # ---- messages and uploads -------------------------------------------
    def add_message(self, session_id: str, role: str, content: dict) -> None:
        with self._con() as con:
            con.execute("insert into messages (session_id, role, content, created_at, timetable_id) values (?,?,?,?,?)",
                        (session_id, role, json.dumps(content), time.time(), self._tid()))

    def messages(self, session_id: str) -> list[dict]:
        with self._con() as con:
            rows = con.execute("select role, content, created_at from messages where session_id=? and timetable_id=? order by id", (session_id, self._tid())).fetchall()
        return [{"role": r["role"], "content": json.loads(r["content"]), "created_at": r["created_at"]} for r in rows]

    def clear_messages(self, session_id: str) -> None:
        with self._con() as con:
            con.execute("delete from messages where session_id=? and timetable_id=?", (session_id, self._tid()))

    def add_upload(self, session_id: str, name: str, path: str, kind: str, text: str) -> int:
        with self._con() as con:
            cur = con.execute("insert into uploads (session_id, name, path, kind, text, created_at, timetable_id) values (?,?,?,?,?,?,?)",
                              (session_id, name, path, kind, text, time.time(), self._tid()))
            assert cur.lastrowid is not None
            return cur.lastrowid

    def uploads(self, session_id: str) -> list[dict]:
        with self._con() as con:
            rows = con.execute("select * from uploads where session_id=? and timetable_id=? order by id", (session_id, self._tid())).fetchall()
        return [dict(r) for r in rows]

    def remove_upload_by_name(self, session_id: str, name: str) -> list[str]:
        """Drop every upload of the timetable's thread with that filename; returns the stored paths so the caller can unlink them."""
        with self._con() as con:
            rows = con.execute("select path from uploads where session_id=? and name=? and timetable_id=?", (session_id, name, self._tid())).fetchall()
            con.execute("delete from uploads where session_id=? and name=? and timetable_id=?", (session_id, name, self._tid()))
        return [r["path"] for r in rows]

    def clear_uploads(self, session_id: str) -> list[str]:
        with self._con() as con:
            rows = con.execute("select path from uploads where session_id=? and timetable_id=?", (session_id, self._tid())).fetchall()
            con.execute("delete from uploads where session_id=? and timetable_id=?", (session_id, self._tid()))
        return [r["path"] for r in rows]
