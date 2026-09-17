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

DEFAULT_SETTINGS = {
    "time": {"slot_minutes": 40, "slots_per_day": 8, "start": "07:30",
             "labels": ["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8"], "window": "day"},
    "rules": {"max_load": 6, "max_run": 4, "mandatory_rest": []},
    "provider": {"kind": "anthropic", "base_url": "", "api_key": "", "model": "claude-opus-5"},
    "engine": {"url": "", "key": ""},
}

SCHEMA = """
create table if not exists kv (k text primary key, v text not null);
create table if not exists messages (id integer primary key autoincrement, session_id text not null, role text not null,
  content text not null, created_at real not null);
create table if not exists uploads (id integer primary key autoincrement, session_id text not null, name text not null,
  path text not null, kind text not null, text text not null, created_at real not null);
create table if not exists timetables (id text primary key, name text not null, created_at real not null);
"""
DEFAULT_TIMETABLE = "default"
SCOPED_KEYS = ("org:live", "org:draft", "last_check", "tt")   # kv keys that live per timetable


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
            con.execute("delete from kv where k in (?,?,?,?)",
                        (f"org:{tid}:live", f"org:{tid}:draft", f"last_check:{tid}", f"tt:{tid}"))
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
        """Global provider and engine settings plus the current timetable's time and rules."""
        base = self._get("settings")
        s = copy.deepcopy(DEFAULT_SETTINGS) if base is None else copy.deepcopy(base)
        per = self._get(f"tt:{self._tid()}")
        if per:
            s["time"], s["rules"] = per.get("time", s["time"]), per.get("rules", s["rules"])
        return s

    def set_settings(self, settings: dict) -> None:
        settings = copy.deepcopy(settings)
        per = {"time": settings.get("time", DEFAULT_SETTINGS["time"]), "rules": settings.get("rules", DEFAULT_SETTINGS["rules"])}
        self._set(f"tt:{self._tid()}", per)
        self._set("settings", settings)      # the global copy keeps time/rules as defaults for new timetables

    def get_value(self, key: str):
        return self._get(f"{key}:{self._tid()}" if key == "last_check" else key)

    def set_value(self, key: str, value) -> None:
        self._set(f"{key}:{self._tid()}" if key == "last_check" else key, value)

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
        """Drop every upload of this session with that filename; returns the stored paths so the caller can unlink them."""
        with self._con() as con:
            rows = con.execute("select path from uploads where session_id=? and name=? and timetable_id=?", (session_id, name, self._tid())).fetchall()
            con.execute("delete from uploads where session_id=? and name=? and timetable_id=?", (session_id, name, self._tid()))
        return [r["path"] for r in rows]

    def clear_uploads(self, session_id: str) -> list[str]:
        with self._con() as con:
            rows = con.execute("select path from uploads where session_id=? and timetable_id=?", (session_id, self._tid())).fetchall()
            con.execute("delete from uploads where session_id=? and timetable_id=?", (session_id, self._tid()))
        return [r["path"] for r in rows]
