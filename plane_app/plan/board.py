"""The deployment board (spec docs/superpowers/specs/2026-09-26-deployment-board-design.md §4): one
department and level of the plan as a grid of subject rows and class columns, a tray of teachers
with their loads, and the edits the board makes.

`view` reads a normalised plan in one pass over its requirements and one over its staff, so a
school's plan (thousands of requirements) renders without per-cell scans. Every mutation is pure:
plan in, a new normalised plan out, or `BoardError(status, message)`. Each re-runs the plan's lock
check against the plan it was given, so no board edit can change a locked requirement.

Department accounts (spec docs/superpowers/specs/2026-09-27-department-accounts-design.md §4): `view`
and every mutation take the `principal` (auth.Principal, or None for the admin). A head of
department sees and changes only their own department, within their teachers' department shares,
and not while it is submitted; a mutation that carries the plan `rev` its view was built from is
refused (StaleError) when a requirement it touches changed after that rev."""
from __future__ import annotations

import copy
import re
from collections import Counter
from datetime import datetime, timezone

from . import importer as IMP
from . import model as M


class BoardError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class StaleError(BoardError):
    """409: `req` (a requirement, or a staff entry named by `what`) changed at plan rev `rev_at`,
    after the rev the edit's view was built from (or, for an undo, after the change being undone).
    The route names who changed it and sends the current board with the error."""
    def __init__(self, req: dict, rev_at: int, what: str | None = None):
        self.what = what or named(req)
        super().__init__(409, f"{self.what} was changed a moment ago")
        self.req = req
        self.rev_at = rev_at


class UndoGone(BoardError):
    """409: something an undo row changed (not a requirement or a teacher, whose rev_at makes it a
    StaleError naming who) has been changed again since. Like a StaleError, the row can never be
    undone, so the route drops it."""


def is_hod(principal) -> bool:
    """A department account (the admin, or no principal at all, is not)."""
    return principal is not None and not principal.is_admin


def _who(principal) -> str:
    return principal.name if principal is not None else "Timetabler"


# ---------------------------------------------------------------------------
# keys and levels
# ---------------------------------------------------------------------------

_DIGITS = re.compile(r"\d+")


def cell_key(req: dict) -> str:
    """"dept|level|subject|grouping|class1,class2": every part of a split shares it."""
    return "|".join((str(req.get("dept") or ""), str(req.get("level") or ""), str(req.get("subject") or ""),
                     str(req.get("grouping") or "class"), ",".join(str(c) for c in req.get("classes") or ())))


def row_key(req: dict) -> str:
    return "|".join((str(req.get("dept") or ""), str(req.get("level") or ""), str(req.get("subject") or "")))


def level_tab(level, classes=()) -> str:
    """The board's level tab: the leading digits of a level ("1G3" -> "1"); a level without them is
    its own tab; an empty level takes the digits its first class code starts with."""
    text = str(level or "").strip()
    m = _DIGITS.match(text)
    if m:
        return m.group()
    if text:
        return text
    for code in classes or ():
        m = _DIGITS.match(str(code))
        if m:
            return m.group()
    return ""


def _stream(level) -> str:
    """What follows a level's leading digits ("1G3" -> "G3"), or nothing."""
    text = str(level or "").strip()
    m = _DIGITS.match(text)
    return text[m.end():].strip() if m else ""


def _tab_order(tab: str):
    return (0, int(tab), "") if tab.isdigit() else (1, 0, tab)


def _periods(req: dict) -> int:
    periods = req.get("periods")
    if periods:
        return int(periods)
    return sum(int(k) * int(v) for k, v in (req.get("lessons") or {}).items())


def _pattern(lessons: dict) -> str:
    """The lesson lengths, longest first: {"2": 2, "1": 1} -> "2,2,1"."""
    lengths = sorted((int(k) for k, n in lessons.items() for _ in range(int(n or 0))), reverse=True)
    return ",".join(str(n) for n in lengths)


def _add_lessons(total: dict, lessons: dict) -> dict:
    for k, n in (lessons or {}).items():
        total[str(k)] = total.get(str(k), 0) + int(n or 0)
    return total


def _plain(number):
    number = float(number)
    return int(number) if number.is_integer() else number


# ---------------------------------------------------------------------------
# view
# ---------------------------------------------------------------------------

def _cell_view(key: str, reqs: list[dict], col_of: dict[str, int]) -> dict:
    reqs = sorted(reqs, key=lambda r: r["id"])
    classes = list(reqs[0].get("classes") or [])
    cols = sorted(col_of[c] for c in classes if c in col_of)
    col = cols[0] if cols else 0
    contiguous = cols and cols == list(range(cols[0], cols[0] + len(cols))) and len(cols) == len(classes)
    lessons: dict = {}
    parts, unassigned = [], []
    needed = assigned = 0
    for r in reqs:
        p = _periods(r)
        needed += p
        _add_lessons(lessons, r.get("lessons"))
        if r.get("teachers"):
            assigned += p
            parts.append({"req": r["id"], "teachers": list(r["teachers"]), "periods": p,
                          "locked": bool(r.get("locked"))})
        else:
            unassigned.append({"req": r["id"], "periods": p, "locked": bool(r.get("locked"))})
    return {"key": key, "classes": classes, "col": col, "span": len(cols) if contiguous else 1,
            "grouping": reqs[0].get("grouping") or "class", "needed": needed, "assigned": assigned,
            "pattern": _pattern(lessons), "locked": all(r.get("locked") for r in reqs), "split": len(reqs) > 1,
            "parts": parts, "unassigned": unassigned}


def _part_time(staff: dict, n_slots: int) -> bool:
    avail = staff.get("avail")
    if not avail or not n_slots:
        return False
    try:
        covered = sum(max(0, min(int(end), n_slots) - max(int(start), 0)) for start, end in avail)
    except (TypeError, ValueError):
        return False
    return covered < n_slots


def view(plan: dict, settings: dict, dept: str | None = None, level: str | None = None,
         principal=None) -> dict:
    """The board JSON of spec §4 for one department and level tab (default: the first of each).
    `level` may be a tab ("1") or a full level ("1G3", read as its tab). BoardError 404 for a
    department or level the plan does not have. For a head of department (`principal`) the
    department is always theirs, and the tray lists only the teachers available to it."""
    index: dict[str, dict[str, dict[str, dict[str, list]]]] = {}
    tab_classes: dict[str, set] = {}
    load: Counter = Counter()
    load_dept: Counter = Counter()
    unlocked_depts: set[str] = set()
    for r in plan.get("requirements") or []:
        d = str(r.get("dept") or "")
        if not r.get("locked"):
            unlocked_depts.add(d)
        tab = level_tab(r.get("level"), r.get("classes"))
        (index.setdefault(d, {}).setdefault(tab, {}).setdefault(row_key(r), {})
         .setdefault(cell_key(r), []).append(r))
        tab_classes.setdefault(tab, set()).update(r.get("classes") or ())
        p = _periods(r)
        for t in r.get("teachers") or ():
            load[t] += p
            load_dept[(t, d)] += p

    staff = plan.get("staff") or []
    departments = sorted(set(index) | {str(s.get("dept")) for s in staff if s.get("dept")})
    hod = is_hod(principal)
    if hod:
        dept = str(principal.dept or "")
        departments = [dept]
    elif dept is None:
        dept = departments[0] if departments else None
    elif dept not in departments:
        raise BoardError(404, f"no department {dept!r}")
    tabs = index.get(dept, {}) if dept is not None else {}
    levels = sorted(tabs, key=_tab_order)
    if level is None:
        level = levels[0] if levels else None
    elif level not in levels:
        if level_tab(level) in levels:
            level = level_tab(level)
        else:
            raise BoardError(404, f"{dept} has no level {level!r}")

    # columns: the level's classes, in plan order
    order = {c["code"]: i for i, c in enumerate(plan.get("classes") or [])}
    codes = set(tab_classes.get(level, ())) if level is not None else set()
    for c in plan.get("classes") or []:
        if level is not None and level_tab(c.get("level"), [c["code"]]) == level:
            codes.add(c["code"])
    columns = sorted(codes, key=lambda c: (order.get(c, len(order)), c))
    col_of = {c: i for i, c in enumerate(columns)}
    tags: dict[str, list[str]] = {c: [] for c in columns}
    for d in plan.get("divisions") or []:
        members = list(d.get("classes") or [])
        if not members or not any(c in col_of for c in members):
            continue
        label = members[0] if len(members) == 1 else f"{members[0]}-{members[-1]}"
        for c in members:
            if c in col_of:
                tags[c].append(label)
    classes = [{"code": c, "divisions": tags[c]} for c in columns]

    rows = []
    for rk, cells in (tabs.get(level) or {}).items():
        first = next(iter(cells.values()))[0]
        cell_views = sorted((_cell_view(k, reqs, col_of) for k, reqs in cells.items()),
                            key=lambda c: (c["col"], c["grouping"], c["key"]))
        (periods, pattern), _ = Counter((c["needed"], c["pattern"]) for c in cell_views).most_common(1)[0]
        stream = _stream(first.get("level"))
        subject = str(first.get("subject") or "")
        rows.append({"key": rk, "dept": dept, "level": str(first.get("level") or ""), "subject": subject,
                     "title": f"{subject} ({stream})" if stream else subject,
                     "periods": periods, "pattern": pattern,
                     "locked": all(c["locked"] for c in cell_views), "cells": cell_views})
    rows.sort(key=lambda r: (r["title"].lower(), r["level"]))

    n_slots = len(((settings or {}).get("time") or {}).get("labels") or [])
    tray = []
    for s in staff:
        available = dept is not None and M.available_in(s, dept)
        if hod and not available:
            continue
        home = (s.get("dept") or "") == dept
        if hod and not home:
            # another department's teacher shared in: only what the card needs to place them here,
            # nothing of their own department's (reductions, allowance, load elsewhere)
            tray.append({"id": s["id"], "name": s.get("name") or "", "short": s.get("short") or "",
                         "dept": s.get("dept") or "", "home": False, "available": available,
                         "shares": {k: n for k, n in (s.get("shares") or {}).items() if k == dept},
                         "capacity_here": _plain(M.capacity_in(s, dept, settings)),
                         "assigned_here": load_dept[(s["id"], dept)]})
            continue
        tray.append({"id": s["id"], "name": s.get("name") or "", "short": s.get("short") or "",
                     "dept": s.get("dept") or "",
                     "allowance": _plain(M.base_allowance(s, settings)),
                     # the stored figure, None when the allowance is worked out from the load factor
                     "allowance_given": s.get("allowance"),
                     "reductions": [dict(x) for x in s.get("reductions") or []],
                     "effective": _plain(M.effective_allowance(s, settings)),
                     "assigned": load[s["id"]], "assigned_here": load_dept[(s["id"], dept)],
                     "provisional": bool(s.get("provisional")), "part_time": _part_time(s, n_slots),
                     "home": home,
                     # a head of department sees only the share given to their own department
                     "shares": ({k: n for k, n in (s.get("shares") or {}).items() if k == dept} if hod
                                else dict(s.get("shares") or {})),
                     "available": available,
                     # periods a cycle this teacher may be given in the department on screen
                     "capacity_here": _plain(M.capacity_in(s, dept, settings)) if dept is not None else 0})
    tray.sort(key=lambda t: (not t["home"], t["dept"] if not t["home"] else "", t["name"].lower(), t["id"]))

    # every requirement of the department locked, whichever level is on screen: "Unlock department"
    return {"departments": departments, "levels": levels, "dept": dept, "level": level,
            "dept_locked": bool(tabs) and dept not in unlocked_depts,
            "classes": classes, "rows": rows, "tray": tray,
            "rev": int(plan.get("rev") or 0),
            "department_status": M.department_status(plan, dept),
            "department_statuses": {d: M.department_status(plan, d)["status"] for d in departments}}


def where(plan: dict, body: dict) -> tuple[str | None, str | None]:
    """The department and level tab a board request is about, for the board it returns: the body's
    own `view` ({dept, level}), else what its cell, row, requirement or scope names, else its
    dept/level fields. Read against the plan before the change (a merged requirement's id is gone
    after it)."""
    body = body if isinstance(body, dict) else {}
    shown = body.get("view")
    if isinstance(shown, dict) and shown.get("dept"):
        return str(shown["dept"]), (str(shown["level"]) if shown.get("level") else None)
    scope = body.get("scope") if isinstance(body.get("scope"), dict) else {}
    for key in (body.get("cell"), scope.get("cell"), scope.get("row")):
        if isinstance(key, str) and key:
            parts = key.split("|")
            classes = parts[4].split(",") if len(parts) > 4 else ()
            return parts[0], level_tab(parts[1] if len(parts) > 1 else "", classes)
    req_id = body.get("req")
    if isinstance(req_id, str):
        r = next((r for r in plan.get("requirements") or [] if r.get("id") == req_id), None)
        if r is not None:
            return str(r.get("dept") or ""), level_tab(r.get("level"), r.get("classes"))
    source = scope if scope.get("dept") else body
    dept = source.get("dept")
    if isinstance(dept, str) and dept:
        level = source.get("level")
        return dept, (level_tab(level) if level not in (None, "") else None)
    return None, None


def department_of(plan: dict, body: dict) -> str | None:
    """The department a board request changes, by what it names (never by its `view`, which only
    picks the board it returns): a teacher's own department for a staff edit, else `where`'s."""
    body = body if isinstance(body, dict) else {}
    staff_id = body.get("id")
    if isinstance(staff_id, str):
        person = next((s for s in plan.get("staff") or [] if s.get("id") == staff_id), None)
        if person is not None:
            return str(person.get("dept") or "")
    return where(plan, {k: v for k, v in body.items() if k != "view"})[0]


# ---------------------------------------------------------------------------
# department scope, shares and revs (spec 2026-09-27 §4)
# ---------------------------------------------------------------------------

# what a head of department may change about a teacher of their own department ("provisional" only
# from yes to no: naming a placeholder)
HOD_STAFF_FIELDS = ("name", "short", "reductions", "provisional")


def _not_submitted(plan: dict, dept) -> None:
    if M.department_status(plan, dept)["status"] == "submitted":
        raise BoardError(403, f"{dept} is submitted: ask the timetabler to reopen it")


def _key_name(key: str, row: bool = False) -> str:
    parts = key.split("|")
    fields = dict(zip(("dept", "level", "subject", "grouping"), parts))
    if not row and len(parts) > 4:
        fields["classes"] = parts[4].split(",")
    return named(fields)


def _body_label(plan: dict, body: dict) -> str | None:
    """What a request names, for a person to read, or None."""
    scope = body.get("scope") if isinstance(body.get("scope"), dict) else {}
    for key, row in ((body.get("cell"), False), (scope.get("cell"), False), (scope.get("row"), True)):
        if isinstance(key, str) and key:
            return _key_name(key, row)
    for field in ("req", "id"):
        value = body.get(field)
        if isinstance(value, str):
            section = "requirements" if field == "req" else "staff"
            item = next((x for x in plan.get(section) or [] if x.get("id") == value), None)
            if item is not None:
                return named(item) if field == "req" else _staff_name(plan, value)
    return None


def _scope_of_body(plan: dict, body: dict, dept: str) -> None:
    other = department_of(plan, body)
    if other is not None and other != dept:
        label = _body_label(plan, body)
        raise BoardError(403, f"{label} belongs to {other or 'no department'}" if label
                         else f"This belongs to {other or 'no department'}: you can change only {dept}")


def _belongs(what: str, other) -> BoardError:
    return BoardError(403, f"{what} belongs to {other or 'no department'}")


def _scope_of_changes(before: dict, after: dict, changes: dict, dept: str) -> None:
    """Everything a head of department's edit changed is their department's (spec §4)."""
    for b, a in changes.get("requirements", {}).values():
        for r in (b, a):
            if r is not None and r["dept"] != dept:
                raise _belongs(named(r), r["dept"])
    for staff_id, (b, a) in changes.get("staff", {}).items():
        person = a or b
        name = str(person.get("name") or "").strip() or staff_id
        if b is None:
            if a["dept"] != dept:
                raise _belongs(name, a["dept"])
            if not a["provisional"]:
                raise BoardError(403, "only the timetabler adds a named teacher: add a provisional one")
            if a["allowance"] is not None or a["shares"]:
                raise BoardError(403, "only the timetabler sets a teacher's allowance and shares")
            continue
        if b["dept"] != dept:
            raise _belongs(name, b["dept"])
        if a is None:
            raise BoardError(403, "only the timetabler removes a teacher")
        for field in sorted((set(a) | set(b)) - {"rev_at"}):
            if field not in HOD_STAFF_FIELDS and a.get(field) != b.get(field):
                raise BoardError(403, f"only the timetabler changes a teacher's {field.replace('_', ' ')}")
        if a["provisional"] and not b["provisional"]:
            raise BoardError(403, f"only the timetabler makes {name} provisional")
    for code, (b, _a) in changes.get("classes", {}).items():
        if b is None:
            raise BoardError(403, f"ask the timetabler to add class {code}")
        raise BoardError(403, f"only the timetabler changes class {code}")
    for div_id, (b, a) in changes.get("divisions", {}).items():
        if b is None:
            raise BoardError(403, f"ask the timetabler to add a division of {', '.join(map(str, a['classes']))}")
        raise BoardError(403, f"only the timetabler changes division {div_id}")
    depts = {r["id"]: r["dept"] for r in (*before["requirements"], *after["requirements"])}
    for band_id, (b, a) in changes.get("bands", {}).items():
        options = [o for band in (b, a) if band is not None for o in band["options"]]
        other = next((depts.get(o) for o in options if depts.get(o) != dept), None)
        if other is not None or any(o not in depts for o in options):
            raise _belongs(f"band {band_id}", other)
    for other in changes.get("departments", {}):
        if other != dept:
            raise _belongs(other, other)


def _dept_loads(plan: dict) -> Counter:
    load: Counter = Counter()
    for r in plan["requirements"]:
        for t in r["teachers"]:
            load[(t, r["dept"])] += _periods(r)
    return load


def _check_shares(before: dict, after: dict, settings: dict) -> None:
    """A head of department gives their teachers periods only within what each may be given in the
    department (model.capacity_in); a teacher not available to it at all is not theirs to give."""
    staff = {s["id"]: s for s in after["staff"]}
    was = _dept_loads(before)
    for (t, d), n in sorted(_dept_loads(after).items()):
        prev = was.get((t, d), 0)
        person = staff.get(t)
        if n <= prev or person is None:
            continue
        name = _staff_name(after, t)
        if not M.available_in(person, d):
            raise BoardError(403, f"{name} is not available to {d}: ask the timetabler for a share")
        cap = M.capacity_in(person, d, settings)
        if n > cap:
            if prev >= cap:
                raise BoardError(409, f"{name}'s {d} share is full: {_plain(prev)} of {_plain(cap)}")
            raise BoardError(409, f"{name}'s {d} share would be over: {_plain(n)} of {_plain(cap)}")


def _client_rev(body: dict, required: bool, current: int = 0) -> int | None:
    """The plan rev the client's board was built from. A rev ahead of the plan's own could only
    come from a made-up body and would skip the stale check, so it is refused."""
    rev = body.get("rev")
    if rev is None:
        if required:
            raise BoardError(400, "rev is required: reload the board")
        return None
    if isinstance(rev, bool) or not isinstance(rev, int) or rev < 0:
        raise BoardError(400, "rev must be a whole number")
    if rev > current:
        raise BoardError(400, "rev is ahead of the plan: reload the board")
    return rev


def _fresh(reqs, rev: int) -> None:
    """StaleError for the most recently changed of `reqs` whose rev_at is past `rev`."""
    stale = [r for r in reqs if r is not None and int(r.get("rev_at") or 0) > rev]
    if stale:
        newest = max(stale, key=lambda r: (int(r.get("rev_at") or 0), r["id"]))
        raise StaleError(newest, int(newest["rev_at"]))


def _fresh_staff(people, rev: int) -> None:
    """StaleError for the most recently changed of the staff entries `people` whose rev_at is past
    `rev` (a teacher edited since the view the edit was made from)."""
    stale = [s for s in people if s is not None and int(s.get("rev_at") or 0) > rev]
    if stale:
        newest = max(stale, key=lambda s: (int(s.get("rev_at") or 0), s["id"]))
        raise StaleError(newest, int(newest["rev_at"]), str(newest.get("name") or "").strip() or newest["id"])


def _staff_named_by_body(plan: dict, body: dict) -> list[dict]:
    """The staff entry a request names outright (a teacher edit's `id`)."""
    staff_id = body.get("id")
    return [s for s in plan["staff"] if isinstance(staff_id, str) and s["id"] == staff_id]


def _named_by_body(plan: dict, body: dict) -> list[dict]:
    """The requirements a request names outright: its cell's or its req."""
    scope = body.get("scope") if isinstance(body.get("scope"), dict) else {}
    keys = {k for k in (body.get("cell"), scope.get("cell")) if isinstance(k, str)}
    req = body.get("req")
    return [r for r in plan["requirements"] if cell_key(r) in keys or (isinstance(req, str) and r["id"] == req)]


# ---------------------------------------------------------------------------
# mutations
# ---------------------------------------------------------------------------

def _mutation(fn=None, *, takes_principal: bool = False):
    """`run(plan, settings, body, principal=None)`: `fn(work, settings, **body)` on a deep copy of
    the normalised plan (`fn(work, settings, principal, **body)` with `takes_principal`), then the
    result normalised (PlanError: 400) and the lock check re-run against the plan as it was
    (PlanError: 409). The body is one dict (a JSON object); `fn` takes `work` and `settings`
    positional-only, so no body key can land on them.

    Around it (spec 2026-09-27 §4): for a head of department, 403 while their department is
    submitted and for anything outside it, and 403/409 past a teacher's department share; for
    anyone whose body carries `rev` (a head of department's must), StaleError when a requirement or
    teacher the edit names or changes has changed since that rev."""
    if fn is None:
        return lambda f: _mutation(f, takes_principal=takes_principal)

    def run(plan: dict, settings: dict, body: dict | None = None, principal=None) -> dict:
        if body is None:
            body = {}
        if not isinstance(body, dict) or not all(isinstance(k, str) for k in body):
            raise BoardError(400, "the body must be a JSON object")
        try:
            before = M.normalise(plan)
        except M.PlanError as e:
            raise BoardError(400, f"the plan does not read: {e}")
        hod = is_hod(principal)
        rev = _client_rev(body, required=hod, current=int(before.get("rev") or 0))
        body = {k: v for k, v in body.items() if k != "rev"}
        if hod:
            _not_submitted(before, principal.dept)
            _scope_of_body(before, body, principal.dept)
        if rev is not None:
            _fresh(_named_by_body(before, body), rev)
            _fresh_staff(_staff_named_by_body(before, body), rev)
        work = copy.deepcopy(before)
        if takes_principal:
            fn(work, settings, principal, **body)
        else:
            fn(work, settings, **body)
        try:
            after = M.normalise(work)
        except M.PlanError as e:
            raise BoardError(400, str(e))
        try:
            M.check_locks(before, after)
        except M.LockError as e:
            raise BoardError(409, f"{named(e.req)} is locked: unlock it first")
        except M.PlanError as e:
            raise BoardError(409, str(e))
        changes = M.plan_changes(before, after)
        if hod:
            _scope_of_changes(before, after, changes, principal.dept)
        if rev is not None:
            _fresh((b for b, _ in changes.get("requirements", {}).values()), rev)
            _fresh_staff((b for b, _ in changes.get("staff", {}).values()), rev)
        if hod:
            _check_shares(before, after, settings)
        return after
    run.__name__ = fn.__name__
    run.__doc__ = fn.__doc__
    return run


def _text(value, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise BoardError(400, f"{name} is required")
    return str(value).strip()


def _key_text(value, name: str) -> str:
    """_text for a field that becomes part of a cell or row key ("dept|level|subject|grouping|1A,1B"):
    it may not hold the key's own separators."""
    text = _text(value, name)
    if "|" in text or "," in text:
        raise BoardError(400, f"{name} {text!r} must not contain '|' or ','")
    return text


def _class_list(value, name: str = "classes") -> list[str]:
    if not isinstance(value, list) or not value:
        raise BoardError(400, f"{name} must be a list of one or more class codes")
    out = []
    for c in value:
        code = _key_text(c, name)
        if code in out:
            raise BoardError(400, f"{name} names {code} twice")
        out.append(code)
    return out


def _lesson_pattern(value) -> tuple[dict, int]:
    try:
        lessons = M.normalise_lessons(value if isinstance(value, dict) else M.as_dict(value, "lessons"))
    except M.PlanError as e:
        raise BoardError(400, str(e))
    periods = sum(int(k) * n for k, n in lessons.items())
    if not periods:
        raise BoardError(400, "lessons must add up to at least one period")
    return lessons, periods


def _cell(work: dict, key) -> list[dict]:
    if not isinstance(key, str) or not key:
        raise BoardError(400, "cell is required")
    reqs = sorted((r for r in work["requirements"] if cell_key(r) == key), key=lambda r: r["id"])
    if not reqs:
        raise BoardError(404, f"no cell {key!r}")
    return reqs


def named(req: dict) -> str:
    """A requirement as the board shows it, for a person to read: "MATH Mathematics (G3) 1A2", with
    the group code of an option ("EL English Language (G3) 1ELP 1A1,1A2")."""
    stream = _stream(req.get("level"))
    subject = str(req.get("subject") or "")
    grouping = str(req.get("grouping") or "class")
    parts = (str(req.get("dept") or ""), f"{subject} ({stream})" if stream else subject,
             "" if grouping == "class" else grouping, ",".join(str(c) for c in req.get("classes") or ()))
    return " ".join(p for p in parts if p) or str(req.get("id") or "")


def _unlocked(*reqs: dict) -> None:
    for r in reqs:
        if r["locked"]:
            raise BoardError(409, f"{named(r)} is locked: unlock it first")


def _staff_name(work: dict, staff_id: str) -> str:
    person = next((s for s in work["staff"] if s["id"] == staff_id), None)
    return str((person or {}).get("name") or "").strip() or staff_id


def _teacher(work: dict, value) -> str:
    if not isinstance(value, str) or not value:
        raise BoardError(400, "teacher is required")
    if not any(s["id"] == value for s in work["staff"]):
        raise BoardError(404, f"no teacher {value!r}")
    return value


def _cell_name(reqs: list[dict]) -> str:
    return named(reqs[0])


def _base_id(reqs: list[dict]) -> str:
    """The id a cell has as one requirement: its own id, or its parts' `<base>-a` without the suffix."""
    if len(reqs) == 1:
        return reqs[0]["id"]
    m = re.match(r"^(.*)-[a-z]$", reqs[0]["id"])
    if m and all(re.fullmatch(re.escape(m.group(1)) + r"-[a-z]", r["id"]) for r in reqs):
        return m.group(1)
    r = reqs[0]
    return M.requirement_id(r["dept"], r["level"], r["grouping"], r["classes"])


def _renamed(work: dict, mapping: dict[str, list[str]]) -> None:
    """Band options and sync_with follow requirements whose ids changed (old id -> new ids)."""
    if not mapping:
        return
    for b in work["bands"]:
        options: list[str] = []
        for o in b["options"]:
            for new in mapping.get(o, [o]):
                if new not in options:
                    options.append(new)
        b["options"] = options
    for r in work["requirements"]:
        if r.get("sync_with") in mapping and mapping[r["sync_with"]]:
            r["sync_with"] = mapping[r["sync_with"]][0]


@_mutation
def assign(work: dict, settings: dict, /, *, cell=None, teacher=None, mode=None, req=None, **_) -> None:
    """A teacher onto a cell: its first unassigned requirement by default; `mode: "replace"` swaps
    the teacher of a single-teacher part (the cell's only assigned part, or `req`); `"coteach"`
    adds the teacher to every part that already has a teacher (an unassigned share stays so)."""
    if mode not in (None, "", "replace", "coteach"):
        raise BoardError(400, f"mode {mode!r} must be replace or coteach")
    reqs = _cell(work, cell)
    teacher = _teacher(work, teacher)
    if not mode:
        empty = [r for r in reqs if not r["teachers"]]
        if not empty:
            raise BoardError(409, f"{_cell_name(reqs)} is fully assigned: replace, co-teach or split it")
        free = [r for r in empty if not r["locked"]]
        if not free:
            _unlocked(empty[0])
        free[0]["teachers"] = [teacher]
    elif mode == "replace":
        if req is not None:
            target = next((r for r in reqs if r["id"] == req), None)
            if target is None:
                raise BoardError(404, f"{req!r} is not part of {_cell_name(reqs)}")
        else:
            taught = [r for r in reqs if r["teachers"]]
            if len(reqs) == 1:
                target = reqs[0]
            elif len(taught) == 1:
                target = taught[0]
            else:
                raise BoardError(409, f"{_cell_name(reqs)} is split: choose the part to replace")
        _unlocked(target)
        if len(target["teachers"]) > 1:
            raise BoardError(409, f"{named(target)} is co-taught: remove a teacher first")
        target["teachers"] = [teacher]
    else:
        taught = [r for r in reqs if r["teachers"]]
        if not taught:
            raise BoardError(409, f"nobody teaches {_cell_name(reqs)} yet: assign a teacher instead")
        _unlocked(*taught)
        missing = [r for r in taught if teacher not in r["teachers"]]
        if not missing:
            raise BoardError(409, f"{_staff_name(work, teacher)} already teaches every taught part of "
                                  f"{_cell_name(reqs)}")
        for r in missing:
            r["teachers"].append(teacher)


def _merge_empty(work: dict, key: str) -> None:
    """Adjacent unassigned, unlocked parts of a split cell become one (lessons summed). When no part
    of the cell is locked, the parts are then rebuilt as a fresh split: lessons shared out by
    `split_lessons` over the parts' periods, ids re-lettered `<base>-a`, `-b`, ... in order (each
    part keeps its teachers), so the cell stays one the workbook's Split column can say. A single
    part left takes back the base id. Band options and sync_with follow every id that changed."""
    parts = sorted((r for r in work["requirements"] if cell_key(r) == key), key=lambda r: r["id"])
    if len(parts) < 2:
        return
    base = _base_id(parts)
    kept: list[dict] = []
    members: list[list[str]] = []         # the old ids each kept part stands for
    for r in parts:
        prev = kept[-1] if kept else None
        if (prev is not None and not prev["teachers"] and not r["teachers"]
                and not prev["locked"] and not r["locked"]):
            _add_lessons(prev["lessons"], r["lessons"])
            prev["periods"] = _periods(prev) + _periods(r)
            members[-1].append(r["id"])
        else:
            kept.append(r)
            members.append([r["id"]])
    dropped = {old for group in members for old in group[1:]}
    work["requirements"] = [r for r in work["requirements"] if r["id"] not in dropped]
    new_ids = [group[0] for group in members]
    if not any(r["locked"] for r in kept):
        taken = {r["id"] for r in work["requirements"]} - {r["id"] for r in kept}
        try:
            wanted = [base] if len(kept) == 1 else [M.split_part_id(base, i) for i in range(len(kept))]
            total: dict = {}
            for r in kept:
                _add_lessons(total, r["lessons"])
            patterns = M.split_lessons(total, [_periods(r) for r in kept])
        except M.PlanError:
            wanted = patterns = None
        if wanted is not None and not set(wanted) & taken:
            new_ids = wanted
            for r, new_id, pattern in zip(kept, wanted, patterns):
                r["id"] = new_id
                r["lessons"] = pattern
    mapping = {old: [new_id] for group, new_id in zip(members, new_ids) for old in group if old != new_id}
    _renamed(work, mapping)


@_mutation
def unassign(work: dict, settings: dict, /, *, req=None, teacher=None, **_) -> None:
    """A teacher off a requirement (every teacher when `teacher` is not given); a split part left
    empty merges with its empty neighbours."""
    if not isinstance(req, str) or not req:
        raise BoardError(400, "req is required")
    r = next((x for x in work["requirements"] if x["id"] == req), None)
    if r is None:
        raise BoardError(404, f"no requirement {req!r}")
    _unlocked(r)
    if teacher is None:
        r["teachers"] = []
    else:
        if teacher not in r["teachers"]:
            who = _staff_name(work, teacher) if isinstance(teacher, str) else repr(teacher)
            raise BoardError(404, f"{who} does not teach {named(r)}")
        r["teachers"] = [t for t in r["teachers"] if t != teacher]
    if not r["teachers"]:
        _merge_empty(work, cell_key(r))


def _share(work: dict, share, i: int) -> tuple[list[str], int]:
    if not isinstance(share, dict):
        raise BoardError(400, f"shares[{i}] must be {{teacher, periods}}")
    periods = share.get("periods")
    if isinstance(periods, bool) or not isinstance(periods, (int, float, str)):
        raise BoardError(400, f"shares[{i}].periods must be a whole number")
    try:
        number = float(periods)
    except ValueError:
        raise BoardError(400, f"shares[{i}].periods must be a whole number")
    if not number.is_integer() or number <= 0:
        raise BoardError(400, f"shares[{i}].periods must be a whole number, 1 or more")
    if share.get("teachers") is not None:
        if not isinstance(share["teachers"], list):
            raise BoardError(400, f"shares[{i}].teachers must be a list")
        names = share["teachers"]
    else:
        names = [share["teacher"]] if share.get("teacher") else []
    teachers: list[str] = []
    for t in names:
        t = _teacher(work, t)
        if t not in teachers:
            teachers.append(t)
    return teachers, int(number)


@_mutation
def split(work: dict, settings: dict, /, *, cell=None, shares=None, **_) -> None:
    """The cell's requirements rebuilt, one per share, the lessons shared out by split_lessons; a
    share may have no teacher (unassigned) or several (co-taught)."""
    reqs = _cell(work, cell)
    _unlocked(*reqs)
    if not isinstance(shares, list) or not shares:
        raise BoardError(400, "shares must be a list of {teacher, periods}")
    parsed = [_share(work, s, i) for i, s in enumerate(shares)]
    lessons: dict = {}
    for r in reqs:
        _add_lessons(lessons, r["lessons"])
    try:
        patterns = M.split_lessons(lessons, [p for _, p in parsed])
        base = _base_id(reqs)
        ids = [base] if len(parsed) == 1 else [M.split_part_id(base, i) for i in range(len(parsed))]
    except M.PlanError as e:
        raise BoardError(400, str(e))
    old_ids = [r["id"] for r in reqs]
    others = {r["id"]: r for r in work["requirements"] if r["id"] not in old_ids}
    for new_id in ids:
        if new_id in others:
            raise BoardError(409, f"{_cell_name(reqs)} cannot be split: its part {new_id} would take the id of "
                                  f"{named(others[new_id])}")
    template = reqs[0]
    new = [{**copy.deepcopy(template), "id": new_id, "teachers": teachers, "lessons": pattern,
            "periods": periods, "locked": False}
           for new_id, (teachers, periods), pattern in zip(ids, parsed, patterns)]
    work["requirements"] = [r for r in work["requirements"] if r["id"] not in old_ids] + new
    _renamed(work, {old: ids for old in old_ids if old not in ids or len(ids) > 1})


@_mutation
def lock(work: dict, settings: dict, /, *, scope=None, locked=None, **_) -> None:
    """`locked` on every requirement of a cell, a row, a department's level, or a department."""
    if not isinstance(locked, bool):
        raise BoardError(400, "locked must be true or false")
    if not isinstance(scope, dict):
        raise BoardError(400, "scope must be {cell}, {row}, {dept, level} or {dept}")
    if scope.get("cell"):
        match = [r for r in work["requirements"] if cell_key(r) == scope["cell"]]
        what = f"cell {scope['cell']!r}"
    elif scope.get("row"):
        match = [r for r in work["requirements"] if row_key(r) == scope["row"]]
        what = f"row {scope['row']!r}"
    elif scope.get("dept"):
        dept, level = scope["dept"], scope.get("level")
        match = [r for r in work["requirements"] if r["dept"] == dept and (
            level in (None, "") or r["level"] == level or level_tab(r["level"], r["classes"]) == str(level))]
        what = f"{dept} level {level}" if level not in (None, "") else f"department {dept!r}"
    else:
        raise BoardError(400, "scope must be {cell}, {row}, {dept, level} or {dept}")
    if not match:
        raise BoardError(404, f"no requirements in {what}")
    for r in match:
        r["locked"] = locked


def _add_classes(work: dict, codes: list[str]) -> None:
    known = {c["code"] for c in work["classes"]}
    for code in codes:
        if code not in known:
            work["classes"].append({"code": code, "level": "", "size": None})
            known.add(code)


def _requirement(dept, level, subject, grouping, classes, lessons, periods, venue_kind, req_id) -> dict:
    return {"id": req_id, "dept": dept, "level": level, "subject": subject, "periods": periods,
            "lessons": dict(lessons), "grouping": grouping, "classes": list(classes), "teachers": [],
            "size": None, "venue": {"kind": venue_kind or None, "room": None}, "sync_with": None,
            "locked": False, "source_hash": None}


@_mutation
def add_row(work: dict, settings: dict, /, *, dept=None, level=None, subject=None, lessons=None, classes=None,
            venue_kind=None, **_) -> None:
    """A subject row: one unassigned entire-class requirement per class. A second subject for the
    same department, level and class takes the subject-qualified id (model.requirement_id_for), as
    the workbook importer does."""
    dept, level, subject = _key_text(dept, "dept"), _key_text(level, "level"), _key_text(subject, "subject")
    classes = _class_list(classes)
    lessons, periods = _lesson_pattern(lessons)
    if venue_kind is not None and not isinstance(venue_kind, str):
        raise BoardError(400, "venue_kind must be text")
    cells: dict[str, dict] = {}
    for r in work["requirements"]:
        if r["locked"] or cell_key(r) not in cells:
            cells[cell_key(r)] = r
    taken = {r["id"] for r in work["requirements"]}
    for code in classes:
        req = _requirement(dept, level, subject, "class", [code], lessons, periods, venue_kind, "")
        existing = cells.get(cell_key(req))
        if existing is not None:
            _unlocked(existing)
            raise BoardError(409, f"{subject} already has a row for {code}")
        req_id = M.requirement_id_for(dept, level, "class", [code], subject, taken)
        req["id"] = req_id
        taken.add(req_id)
        work["requirements"].append(req)
    _add_classes(work, classes)


# a generated option code's last letter: "1ELP", "1ELQ", ... as schools write them
_OPTION_LETTERS = "PQRSTUVWXYZABCDEFGHIJKLMNO"


def subject_code(subject: str) -> str:
    """A short code for a subject, as schools write group codes: the initials of its words
    ("English Language" -> "EL"), or the first two letters of a one-word subject ("Science" -> "SC")."""
    words = re.findall(r"[A-Za-z]+", subject)
    if not words:
        return "OP"
    if len(words) == 1:
        return words[0][:2].upper()
    return "".join(w[0] for w in words[:3]).upper()


@_mutation
def add_band(work: dict, settings: dict, /, *, dept=None, level=None, classes=None, subject=None, groups=None,
             lessons=None, venue_kind=None, **_) -> None:
    """Option requirements that run together: a band over a division of the classes (an existing
    division with the same classes is reused). Group codes and the band id are the importer's, so
    the band comes back the same through the workbook: codes share one family (importer.family:
    "1ELP", "1ELR" -> EL), a group without a code gets `<level digits><subject code><letter>`, and
    the band id is band_id(division, family)."""
    dept, level, subject = _key_text(dept, "dept"), _key_text(level, "level"), _key_text(subject, "subject")
    classes = _class_list(classes)
    lessons, periods = _lesson_pattern(lessons)
    if not isinstance(groups, list) or not groups:
        raise BoardError(400, "groups must be a list of {code, classes}")
    for i, g in enumerate(groups):
        if not isinstance(g, dict):
            raise BoardError(400, f"groups[{i}] must be {{code, classes}}")
    given = [_key_text(g["code"], f"groups[{i}].code") if g.get("code") is not None else None
             for i, g in enumerate(groups)]
    families = {IMP.family(c) for c in given if c}
    if len(families) > 1 or "" in families:
        raise BoardError(400, "option codes must share a subject code, e.g. 1ELP / 1ELR")
    family = next(iter(families)) if families else subject_code(subject)
    digits = _DIGITS.match(level)
    prefix = (digits.group() if digits else "") + family
    used = {r["grouping"] for r in work["requirements"]} | {c for c in given if c}
    letters = iter(c for c in _OPTION_LETTERS if prefix + c not in used)
    codes: list[str] = []
    for code in given:
        if code is None:
            code = prefix + next(letters, "")
            if code == prefix:
                raise BoardError(400, "too many groups to name: give each group its code")
        if code in codes:
            raise BoardError(400, f"group {code} is named twice")
        codes.append(code)

    options = []
    for i, (code, g) in enumerate(zip(codes, groups)):
        g_classes = classes if g.get("classes") is None else _class_list(g.get("classes"), f"groups[{i}].classes")
        if not set(g_classes) <= set(classes):
            raise BoardError(400, f"group {code} names classes outside the band")
        options.append((code, g_classes))

    by_cell: dict[str, dict] = {}
    for r in work["requirements"]:
        if r["locked"] or cell_key(r) not in by_cell:
            by_cell[cell_key(r)] = r
    taken = {r["id"] for r in work["requirements"]}
    groupings = {r["grouping"] for r in work["requirements"]}
    new_reqs = []
    for code, g_classes in options:
        req = _requirement(dept, level, subject, code, g_classes, lessons, periods, venue_kind,
                           M.requirement_id(dept, level, code, g_classes))
        existing = by_cell.get(cell_key(req))
        if existing is not None:
            _unlocked(existing)
            raise BoardError(409, f"{subject} {code} already exists")
        if code in groupings:
            raise BoardError(409, f"group code {code} is already used: choose another")
        if req["id"] in taken:
            other = next(r for r in work["requirements"] if r["id"] == req["id"])
            raise BoardError(409, f"{named(req)} would take the id of {named(other)}")
        taken.add(req["id"])
        new_reqs.append(req)

    division = next((d for d in work["divisions"] if set(d["classes"]) == set(classes)), None)
    if division is None:
        div_id = M.division_id(classes)
        if any(d["id"] == div_id for d in work["divisions"]):
            raise BoardError(409, f"division {div_id} already names other classes")
        division = {"id": div_id, "classes": list(classes)}
        work["divisions"].append(division)
    b_id = M.band_id(division["id"], family)
    if any(b["id"] == b_id for b in work["bands"]):
        raise BoardError(409, f"{division['id']} already has a {family} band")
    work["requirements"].extend(new_reqs)
    work["bands"].append({"id": b_id, "division": division["id"], "options": [r["id"] for r in new_reqs]})
    _add_classes(work, classes)


_STAFF_FIELDS = ("name", "short", "dept", "allowance", "reductions", "provisional", "shares")

# staff ids become organisation person ids unchanged (issues block past intake's 31 characters)
_STAFF_ID_MAX = 31


def _initials(name: str) -> str:
    return "".join(w[0] for w in name.split() if w[:1].isalnum()).upper()


def _staff_text(body: dict, field: str, required: bool) -> str:
    """A teacher's name, short name or department: text (a number is refused, not turned into one);
    a department becomes part of the board's keys, so it may not hold their separators."""
    value = body.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise BoardError(400, f"{field} is required")
        return ""
    if not isinstance(value, str):
        raise BoardError(400, f"{field} must be text")
    return _key_text(value, field) if field == "dept" else value.strip()


@_mutation
def save_staff(work: dict, settings: dict, /, **body) -> None:
    """Add a teacher (no `id`) or edit one: only the fields the body carries change. A new
    provisional teacher without a name is "New <dept> teacher N"."""
    staff_id = body.get("id")
    if staff_id is not None:
        person = next((s for s in work["staff"] if s["id"] == staff_id), None)
        if person is None:
            raise BoardError(404, f"no teacher {staff_id!r}")
        for field in _STAFF_FIELDS:
            if field in body:
                person[field] = (_staff_text(body, field, field != "short") if field in ("name", "short", "dept")
                                 else body[field])
        return
    dept = _staff_text(body, "dept", True)
    names = {s["name"] for s in work["staff"]}
    name = _staff_text(body, "name", False)
    if not name:
        if not body.get("provisional"):
            raise BoardError(400, "name is required (or make the teacher provisional)")
        n = 1
        while f"New {dept} teacher {n}" in names:
            n += 1
        name = f"New {dept} teacher {n}"
    taken = {s["id"] for s in work["staff"]}
    person = {"id": M.unique_id(M.plan_slug(name), taken, _STAFF_ID_MAX), "name": name,
              "short": _staff_text(body, "short", False) or _initials(name), "dept": dept,
              "allowance": body.get("allowance"), "reductions": body.get("reductions"),
              "provisional": body.get("provisional"), "shares": body.get("shares")}
    work["staff"].append(person)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _department_reqs(work: dict, dept) -> tuple[str, list[dict]]:
    dept = _key_text(dept, "dept")
    match = [r for r in work["requirements"] if r["dept"] == dept]
    if not match:
        raise BoardError(404, f"no requirements in department {dept!r}")
    return dept, match


@_mutation(takes_principal=True)
def submit(work: dict, settings: dict, principal, /, *, dept=None, **_) -> None:
    """A head of department's sign-off: every requirement of the department locked and the
    department `submitted` (read-only to its head until the timetabler reopens it). The admin
    names the department; a head of department submits their own."""
    if is_hod(principal):
        dept = principal.dept
    dept, match = _department_reqs(work, dept)
    for r in match:
        r["locked"] = True
    work["departments"][dept] = {"status": "submitted", "by": _who(principal), "at": _now()}


@_mutation(takes_principal=True)
def reopen(work: dict, settings: dict, principal, /, *, dept=None, **_) -> None:
    """The timetabler's answer to a submission: every requirement of the department unlocked and
    the department open again."""
    if is_hod(principal):
        raise BoardError(403, "only the timetabler reopens a department")
    dept, match = _department_reqs(work, dept)
    for r in match:
        r["locked"] = False
    work["departments"][dept] = {"status": "open", "by": _who(principal), "at": _now()}


# ---------------------------------------------------------------------------
# per-user undo and the activity text (spec 2026-09-27 §4)
# ---------------------------------------------------------------------------

def undo(plan: dict, row: dict, principal=None, own=frozenset(), settings: dict | None = None) -> dict:
    """The plan with one history row's changes reverted (`row`: {user, rev, changes}, the changes
    as model.plan_changes gave them when the edit was made at plan rev `rev`), leaving every other
    change since alone. StaleError when a requirement or teacher the row changed has changed again
    since (rev_at past `rev`); UndoGone (409) for any other item changed since (both: the row can
    never be undone); 403 for a head of department whose
    department is submitted. `own`: the plan revs this user's own undos made. Undoing a newer change
    of theirs moves a requirement's rev_at past `rev` while leaving it as this row left it, which is
    not someone else's change. A head of department's undo is held to what their edits are held to
    now (their department today, the shares today, with `settings`): 403/409 as an edit would be."""
    try:
        before = M.normalise(plan)
    except M.PlanError as e:
        raise BoardError(400, f"the plan does not read: {e}")
    if is_hod(principal):
        _not_submitted(before, principal.dept)
    rev = int(row.get("rev") or 0)
    changes = row.get("changes") or {}
    work = copy.deepcopy(before)
    for section, key in M.KEYED_SECTIONS:
        current = {x[key]: x for x in work[section]}
        for k, (b, a) in (changes.get(section) or {}).items():
            cur = current.get(k)
            if section == "requirements":
                changed = (cur or a or b)
                cur_rev = int((cur or {}).get("rev_at") or 0)
                if M.content(cur) != M.content(a) or (cur is not None and cur_rev > rev and cur_rev not in own):
                    raise StaleError(changed, int((cur or {}).get("rev_at") or rev + 1))
            elif M.content(cur) != M.content(a):
                what = named(cur or a or b) if section != "staff" else str((cur or a or b).get("name") or k)
                if section == "staff" and cur is not None and int(cur.get("rev_at") or 0) > rev:
                    raise StaleError(cur, int(cur["rev_at"]), str(cur.get("name") or "").strip() or k)
                raise UndoGone(409, f"{what} was changed after your change: it cannot be undone")
    for dept, (b, a) in (changes.get("departments") or {}).items():
        if work["departments"].get(dept) != a:
            raise UndoGone(409, f"{dept} was submitted or reopened after your change: it cannot be undone")

    for section, key in M.KEYED_SECTIONS:
        for k, (b, _a) in (changes.get(section) or {}).items():
            work[section] = [x for x in work[section] if x[key] != k]
            if b is not None:
                work[section].append(copy.deepcopy(b))
    for dept, (b, _a) in (changes.get("departments") or {}).items():
        if b is None:
            work["departments"].pop(dept, None)
        else:
            work["departments"][dept] = copy.deepcopy(b)
    # a class or division the change added stays while something added since still names it
    used = {c for r in work["requirements"] for c in r["classes"]}
    used_divisions = {b["division"] for b in work["bands"]}
    for code, (b, a) in (changes.get("classes") or {}).items():
        if b is None and a is not None and code in used:
            work["classes"].append(copy.deepcopy(a))
    for div_id, (b, a) in (changes.get("divisions") or {}).items():
        if b is None and a is not None and div_id in used_divisions:
            work["divisions"].append(copy.deepcopy(a))
    staff_ids = {s["id"] for s in work["staff"]}
    for r in work["requirements"]:
        for t in r["teachers"]:
            if t not in staff_ids:
                raise BoardError(409, f"{t} teaches {named(r)} now: unassign them before undoing")
    try:
        after = M.normalise(work)
        M.check_locks(before, after)
    except M.LockError as e:
        raise BoardError(409, f"{named(e.req)} is locked: unlock it first")
    except M.PlanError as e:
        raise BoardError(409, str(e))
    if is_hod(principal):
        _scope_of_changes(before, after, M.plan_changes(before, after), principal.dept)
        _check_shares(before, after, settings)
    return after


def _lessons_text(shares) -> str:
    try:
        return "/".join(str(int(float(x.get("periods")))) for x in shares)
    except (TypeError, ValueError, AttributeError):
        return ""


def summary(action: str, plan: dict, body: dict, dept: str | None = None) -> str:
    """One line for the activity panel: what a board edit (already made, so its body is valid) did,
    read against the plan before it. `dept`: the department it changed (a submission names none)."""
    body = body if isinstance(body, dict) else {}
    scope = body.get("scope") if isinstance(body.get("scope"), dict) else {}
    teacher = body.get("teacher")
    who = _staff_name(plan, teacher) if isinstance(teacher, str) else ""
    if action == "assign":
        cell = _body_label(plan, body) or ""
        mode = body.get("mode")
        if mode == "coteach":
            return f"added {who} to {cell} (co-teaching)"
        return f"gave {cell} to {who}" if mode == "replace" else f"assigned {who} to {cell}"
    if action == "unassign":
        what = _body_label(plan, body) or str(body.get("req"))
        return f"removed {who} from {what}" if who else f"cleared {what}"
    if action == "split":
        return f"split {_body_label(plan, body) or ''} {_lessons_text(body.get('shares') or [])}".strip()
    if action == "lock":
        verb = "locked" if body.get("locked") else "unlocked"
        what = _body_label(plan, body)
        if what is None:
            level = scope.get("level")
            what = f"{scope.get('dept')} level {level}" if level not in (None, "") else str(scope.get("dept"))
        return f"{verb} {what}"
    if action == "row":
        return f"added {body.get('subject')} for {', '.join(str(c) for c in body.get('classes') or [])}"
    if action == "band":
        return f"added {body.get('subject')} option groups for {', '.join(str(c) for c in body.get('classes') or [])}"
    if action == "staff":
        if isinstance(body.get("id"), str):
            return f"edited {_staff_name(plan, body['id'])}"
        return f"added {body.get('name') or 'a provisional teacher'}"
    if action in ("submit", "reopen"):
        verb = "submitted" if action == "submit" else "reopened"
        return f"{verb} {dept or body.get('dept') or ''}".strip()
    return action
