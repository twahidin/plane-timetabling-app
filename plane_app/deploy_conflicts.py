"""A level's deployment conflicts, each with its ways out (spec docs/superpowers/specs/
2026-10-25-deployment-file-design.md, goal 3). Pure functions, no database or web imports.

`conflicts` reads a level's stored `levels` entry beside its proposal (`classes.propose`) and lists what
the deployment leaves wrong: a Runs-with subject with more groups than its named teachers take
("groups_short"), a named teacher given two or more of a Runs-with subject's groups, which all run at once
("double_booked"), a named teacher left with no class or group ("teacher_no_group"), named Classes adding
up to more than the level has ("overshoot"), a named teacher outside their subjects or levels
("outside_specialisation") or past their allowance ("over_allowance"), a lesson pattern that no longer
adds up to the periods ("stale_pattern"). A level whose rows carry no `deploy` has none. Each conflict is
{id, kind, subject, text, options}; each option {id, text, recommended, change}, where `change` is what
`apply_option` does to the stored entry. The options come from the app's own rules (department,
`specialisation.covers`, allowance left), never from the AI, so the same facts give the same list;
`_recommend` marks exactly one, listed first. A give, move or swap is weighed by what it would really do
(`_Sim`: made, proposed again, its loads and conflicts): one that changes nothing is not offered, and one is
recommended only when it clears its conflict (a groups shortfall: narrows it by a group) and takes no one
past their allowance. Nothing is written here: the school picks an option and says yes elsewhere.

What it says carries subject words, level names, staff names and counts only, never a student's name."""
from __future__ import annotations

import copy
import math
from itertools import accumulate

from . import classes as C
from .plan import model as M
from .plan import specialisation as SP
from .students import norm

KINDS = ("groups_short", "double_booked", "teacher_no_group", "overshoot", "outside_specialisation",
         "over_allowance", "stale_pattern")     # the order a subject's conflicts are listed in


# ---------------------------------------------------------------------------
# the conflicts
# ---------------------------------------------------------------------------

def conflicts(plan: dict, settings: dict, key: str, entry: dict, prop: dict, students: list[dict], *,
              simulate: bool = True, only: str | None = None) -> list[dict]:
    """The level's conflicts, in its subject rows' order, then `KINDS` order, then the deployment's order.
    `prop` is the level's proposal from `entry`, `students` the ones it was proposed from. A level with no
    `deploy` on any row has none; in one that has, a row with no `deploy` can still have a stale lesson
    pattern (a file may give only a pattern), and a Banded row has none. A subject written on two rows is
    said once. Each give, move or swap is weighed by what it would really do (`_Sim`). `simulate` False
    gives each conflict's id, kind, subject and text only, with no options (what `_Sim` itself asks of an
    option's result); `only` gives the conflict of that id alone, as it reads in the full list (none when
    there is no such conflict), the others' options not weighed."""
    key = SP.level_key(key)
    entry = entry or {}
    if not any(row.get("deploy") for row in entry.get("subjects") or []):
        return []
    left = C._allowance_left(plan, settings, key)            # worked out once, for every proposal weighed
    sim = _Sim(plan, settings, key, entry, prop, students, left, only) if simulate else None
    out = _found(plan, key, entry, prop, students, _loads(plan, settings, key, prop, left), sim)
    return out if only is None else [c for c in out if c["id"] == only]


def _found(plan: dict, key: str, entry: dict, prop: dict, students: list[dict], loads: dict,
           sim: _Sim | None) -> list[dict]:
    """`conflicts`' list from the proposal's `loads` (`_loads`); with no `sim`, ids and texts only."""
    name = entry.get("name") or key
    size = int(entry.get("class_size") or 40)
    warnings = prop.get("warnings") or []
    found: list[tuple[int, int, int, dict]] = []
    rows = entry.get("subjects") or []
    for i, row in enumerate(rows):
        if row.get("taught_as") == "banded":
            continue
        reqs = [r for r in prop.get("requirements") or [] if r["subject"] == row["subject"]]
        mine = []
        if row.get("deploy"):
            if row.get("runs_with"):
                mine.append(_groups_short(plan, key, name, size, row, len(reqs), students, loads, sim))
                mine += _double_booked(plan, key, name, row, reqs, loads, sim)
            mine += _no_group(name, row, warnings)
            mine += _overshoot(name, row, reqs, warnings)
            mine += _outside(plan, key, name, row, reqs, loads, sim)
            mine += _over_allowance(plan, key, name, row, reqs, loads, sim)
        mine.append(_stale_pattern(name, row, warnings))
        for j, c in enumerate(c for c in mine if c):
            found.append((i, KINDS.index(c["kind"]), j, c))
    out, seen = [], set()
    for *_, c in sorted(found, key=lambda f: f[:3]):
        if c["id"] not in seen:
            seen.add(c["id"])
            out.append(c if sim else {**c, "options": []})
    return out


def _id(kind: str, subject: str, who: str | None) -> str:
    return f"{kind}:{norm(subject)}:{norm(who or '')}"


def _conflict(kind: str, subject: str, who: str | None, text: str, options: list[dict]) -> dict:
    return {"id": _id(kind, subject, who), "kind": kind, "subject": subject, "text": text, "options": options}


def _option(oid: str, text: str, change: dict, *, fallback: bool = False, never: bool = False) -> dict:
    """An option; `fallback` and `never` are `_recommend`'s flags, which it takes off again."""
    out = {"id": oid, "text": text, "recommended": False, "change": change}
    if fallback:
        out["fallback"] = True
    if never:
        out["never_recommend"] = True
    return out


class _Sim:
    """What a give, move or swap would really do: the entry with the option made (`apply_option`), proposed
    again as the route does (`classes.propose` with the same count, then `classes.keep_chosen` of the
    school's own choices in `prop`), its loads (`_loads`) and conflicts (ids only), against the entry
    proposed again unchanged (the baseline). `left` is the staff's allowance left (`classes._allowance_left`),
    the same for every proposal weighed, so worked out once; with `only`, the options of that conflict alone
    are weighed."""

    def __init__(self, plan: dict, settings: dict, key: str, entry: dict, prop: dict, students: list[dict],
                 left: dict[str, float], only: str | None = None):
        self.plan, self.settings, self.key, self.entry, self.prop, self.students = \
            plan, settings, key, entry, prop, students
        self.left, self.only = left, only
        self.count = len(prop.get("classes") or []) or None
        self.base = self._propose(entry)
        self.base_shape, self.base_loads = _shape(self.base), self._loads(self.base)

    def _propose(self, entry: dict) -> dict:
        new = C.propose(self.plan, self.settings, self.key, entry, self.students, count=self.count, left=self.left)
        return C.keep_chosen(self.prop, new, self.plan, self.settings, self.key, left=self.left)

    def _loads(self, prop: dict) -> dict[str, tuple[str, float, float]]:
        return _loads(self.plan, self.settings, self.key, prop, self.left)

    def weigh(self, cid: str, options: list[dict],
              short: str | None = None) -> dict[str, dict[str, tuple[float, float]]]:
        """Take off `options` (in place) each give, move or swap whose result is the baseline's (it changes
        nothing), and give `_recommend`'s loads for each that clears the conflict `cid` and takes no one past
        their allowance who was within it: {option id: {staff name: (used after, allowance)}}, naming each
        teacher whose load it changes. An option that does neither is still offered, never recommended.
        With `short` (a "groups_short" conflict's subject) an option need not clear it: one that leaves the
        subject at least one group fewer with no teacher the deployment names (`_unnamed`) qualifies, since
        one give cannot clear a shortfall of two. A conflict other than `only` (when given) is left as it is."""
        if self.only is not None and cid != self.only:
            return {}
        out, keep = {}, []
        for o in options:
            if o["change"]["op"] not in _WORK:
                keep.append(o)
                continue
            after = apply_option(self.entry, o)
            prop = self._propose(after)
            if _shape(prop) == self.base_shape:
                continue
            keep.append(o)
            loads = self._loads(prop)
            newly = any(used > allowance and self.base_loads.get(pid, ("", 0, allowance))[1] <= allowance
                        for pid, (_, used, allowance) in loads.items())
            if newly:
                continue
            if short is not None:
                if _unnamed(prop, short) >= _unnamed(self.base, short):
                    continue
            elif any(c["id"] == cid for c in _found(self.plan, self.key, after, prop, self.students, loads, None)):
                continue
            out[o["id"]] = {who: (used, allowance) for pid, (who, used, allowance) in loads.items()
                            if used != self.base_loads.get(pid, ("", None, 0))[1]}
        options[:] = keep
        return out


_WORK = ("add_teacher", "move_class", "replace_teacher")       # the options that give someone work


def _unnamed(prop: dict, subject: str) -> int:
    """The proposal's requirements of `subject` (a Runs-with subject: its groups) no teacher is named for."""
    return sum(1 for r in prop.get("requirements") or [] if r["subject"] == subject and not r.get("named"))


def _shape(prop: dict) -> list[tuple]:
    """Who teaches what in a proposal: each requirement's subject, grouping, classes and teacher."""
    return [(r["subject"], r.get("grouping"), tuple(r["classes"]), r.get("teacher"))
            for r in prop.get("requirements") or []]


def _loads(plan: dict, settings: dict, key: str, prop: dict,
           left: dict[str, float] | None = None) -> dict[str, tuple[str, float, float]]:
    """Staff id -> (name, periods used, allowance): the effective allowance, and what is used of it with
    this level's own earlier output aside (`classes._allowance_left`, or `left` when it is known) and the
    proposal's periods counted."""
    left = C._allowance_left(plan, settings, key) if left is None else left
    given: dict[str, float] = {}
    for r in prop.get("requirements") or []:
        if r.get("teacher"):
            given[r["teacher"]] = given.get(r["teacher"], 0) + (r.get("periods") or 0)
    out = {}
    for p in plan.get("staff") or []:
        allowance = M.effective_allowance(p, settings)
        out[p["id"]] = (str(p.get("name") or "").strip() or p["id"],
                        allowance - left.get(p["id"], allowance) + given.get(p["id"], 0), allowance)
    return out


def _with_room(plan: dict, key: str, row: dict, loads: dict, periods: float,
               skip: set) -> list[tuple[float, str, str, float, float]]:
    """(used ÷ allowance after, name, staff id, used, allowance) of each specialist (`covers` True) of the
    row's department not in `skip` whose allowance holds `periods` more, the most room left after first,
    then by name; none when the row names no department. A name two staff share would name no one, so
    such a teacher is left out."""
    out = []
    for p in plan.get("staff") or []:
        who, used, allowance = loads[p["id"]]
        if (p["id"] in skip or not row.get("dept") or p.get("dept") != row["dept"]
                or SP.covers(p, row["subject"], key) is not True or used + periods > allowance
                or C.resolve_teacher(plan, who) != p["id"]):
            continue
        out.append((_ratio(used + periods, allowance), who, p["id"], used, allowance))
    return sorted(out)


def _written(plan: dict, row: dict, pid: str) -> str | None:
    """The name the row's deployment gives the teacher `pid`, as first written; None when it names them
    nowhere (another row of the subject did)."""
    return next((d["teacher"] for d in row.get("deploy") or []
                 if d.get("teacher") and C.resolve_teacher(plan, d["teacher"]) == pid), None)


def _not_targets(plan: dict, row: dict, pid: str) -> set:
    """Who a swap or move from `pid` may not go to: `pid`, and on a Runs-with row every teacher already
    named for it, since its groups all run at once (a second group would double-book them)."""
    if row.get("runs_with"):
        return {pid, *C.deployed_ids(plan, {"subjects": [row]}, row["subject"])}
    return {pid}


def _classes_of(deploy: list[dict], reqs: list[dict], written: str, pid: str | None) -> tuple[int, bool]:
    """(classes, counted) of a teacher on a Class or Combined row: their lines' counts when one has a
    count, else the classes of the requirements the proposal named them for (0 with no line)."""
    lines = [d for d in deploy if norm(d.get("teacher")) == norm(written)]
    if any(d.get("classes") for d in lines):
        return sum(d.get("classes") or 0 for d in lines), True
    return (sum(len(r["classes"]) for r in _named_to(reqs, pid)) if lines and pid else 0), False


def _freed(row: dict, reqs: list[dict], written: str, pid: str | None) -> tuple[int, int | None]:
    """(groups, line) for one class (a Runs-with row: one group) moved off the teacher: the groups their
    load loses, and the index of the counted line `_move` edits (None when it edits no counted line). Each
    counted line starts a fresh group, so it takes ceil(count / N) groups on a Combined N row (one per class
    or group otherwise); the line is the last one whose count less one frees a group. Lines with no count
    become one counted line of the classes the proposal gave them (`_classes_of`), against the groups it
    named them for. What the move then does to every load is `_Sim`'s."""
    field = "groups" if row.get("runs_with") else "classes"
    n = int(row.get("combine") or 1) if row.get("taught_as") == "combined" else 1
    deploy = row.get("deploy") or []
    lines = [i for i, d in enumerate(deploy) if norm(d.get("teacher")) == norm(written)]
    counted = [i for i in lines if deploy[i].get(field)]
    if counted:
        steps = {i: math.ceil(deploy[i][field] / n) - math.ceil((deploy[i][field] - 1) / n) for i in counted}
        i = ([i for i in counted if steps[i]] or counted)[-1]
        return steps[i], i
    if not lines or row.get("runs_with"):        # a Runs-with line is one group
        return 1, None
    has = _classes_of(deploy, reqs, written, pid)[0]
    return abs(math.ceil((has - 1) / n) - len(_named_to(reqs, pid))), None


def _named_to(reqs: list[dict], pid: str) -> list[dict]:
    """The requirements of `reqs` the deployment names `pid` for."""
    return [r for r in reqs if r.get("named") and r.get("teacher") == pid]


def _counted(row: dict) -> int:
    """The groups the row's named teachers take, as `classes._grouped` counts them (one without a count)."""
    return sum(int(d.get("groups") or 1) for d in row.get("deploy") or [] if d.get("teacher"))


def _teachers(row: dict) -> int:
    """The teachers the row's deployment names, a name written on two lines (under `norm`) once."""
    return len({norm(d["teacher"]) for d in row.get("deploy") or [] if d.get("teacher")})


def _double_booked(plan: dict, key: str, name: str, row: dict, reqs: list[dict], loads: dict,
                   sim: _Sim | None) -> list[dict]:
    """A teacher the Runs-with row's deployment gives two or more of its groups (`classes._grouped`: a
    Groups count, or two lines), which all run at the same time in one band. Ways out: move a group to each
    specialist of the department not already named for the subject with room for its periods (`_with_room`,
    weighed by `_Sim`); leave the extra group(s) unassigned, the teacher keeping one and the subject its
    groups ("one_group"; its text names any teacher with no group who comes off with it, `_dropped`), the
    declared fallback. There is no leaving it as is: one teacher cannot teach two
    groups at once."""
    subject, step = row["subject"], int(row["periods"])
    out = []
    for pid in dict.fromkeys(r["teacher"] for r in reqs if r.get("named") and r.get("teacher")):
        n, who = len(_named_to(reqs, pid)), _written(plan, row, pid)
        if pid not in loads or who is None or n < 2:
            continue
        text = f"{name}: {subject}: {who} has {n} groups that run at the same time"
        if sim is None:
            out.append(_conflict("double_booked", subject, who, text, []))
            continue
        at = _freed(row, reqs, who, pid)[1]
        options = []
        for _, alt, *_ in _with_room(plan, key, row, loads, step, _not_targets(plan, row, pid)):
            change = {"op": "move_class", "subject": subject, "from": who, "to": alt}
            if at is not None and sum(norm(d.get("teacher")) == norm(who) for d in row["deploy"]) > 1:
                change["at"] = at
            options.append(_option(f"move:{norm(alt)}", f"Move a group to {alt}", change))
        extra = "the extra group" if n == 2 else f"the {n - 1} extra groups"
        change = {"op": "one_group", "subject": subject, "teacher": who, "groups": len(reqs)}
        options.append(_option("unassigned", f"Leave {extra} unassigned" + _dropped(row, change),
                               change, fallback=True))
        _recommend(options, sim.weigh(_id("double_booked", subject, who), options) if sim else {})
        out.append(_conflict("double_booked", subject, who, text, options))
    return out


def _dropped(row: dict, change: dict) -> str:
    """What a "one_group" change also does, said: each other teacher named on the row whose lines got no
    group comes off the subject (`_one_group` cannot keep a line at no group), "" when no one does."""
    after = copy.deepcopy(row.get("deploy") or [])
    _one_group(after, change)
    still = {norm(d.get("teacher")) for d in after}
    gone = [t for t in dict.fromkeys(d["teacher"] for d in row.get("deploy") or [] if d.get("teacher"))
            if norm(t) not in still and norm(t) != norm(change["teacher"])]
    if not gone:
        return ""
    names = gone[0] if len(gone) == 1 else ", ".join(gone[:-1]) + " and " + gone[-1]
    one = len(gone) == 1
    return (f"; {names}, who {'has' if one else 'have'} no group, {'comes' if one else 'come'} off "
            f"{change['subject']}")


def _groups_short(plan: dict, key: str, name: str, size: int, row: dict, g: int, students: list[dict],
                  loads: dict, sim: _Sim | None) -> dict | None:
    """A Runs-with row whose `g` groups are more than its named teachers take (t). Ways out: give a group
    to each specialist (`covers` True) of the row's department not yet named, with room for its periods,
    the most room left first; allow bigger groups, one per named teacher, which knowingly goes above the
    class size (said, and so `_recommend` never picks it); leave the rest unassigned. A give is weighed by
    `_Sim`, and qualifies when it leaves one group fewer with no teacher (not only when it clears them all)."""
    subject, periods = row["subject"], int(row["periods"])
    t = _counted(row)
    if g <= t:
        return None
    n = C.taking(students, key, subject, plan)
    text = (f"{name}: {subject}: {g} groups from {n} students, {t} teacher(s) named; "
            f"{g - t} group(s) have no teacher")
    if sim is None:
        return _conflict("groups_short", subject, None, text, [])
    named = set(C.deployed_ids(plan, {"subjects": [row]}, subject))
    deploy = row.get("deploy") or []
    # the count comes from Groups, one line naming no teacher among them: a give fills one of its groups
    unnamed = all(d.get("groups") for d in deploy) and any(not d.get("teacher") for d in deploy)
    options = []
    for _, who, _, used, allowance in _with_room(plan, key, row, loads, periods, named):
        change = {"op": "add_teacher", "subject": subject, "teacher": who, "groups": 1}
        if unnamed:
            change["from_unnamed"] = True
        options.append(_option(f"give:{norm(who)}", f"Give a group to {who}: {C._num(used)} → "
                                                    f"{C._num(used + periods)} periods, within {C._num(allowance)}",
                               change))
    one_each = _teachers(row)                   # "set_groups" gives each named teacher one group
    if one_each and n:
        most = math.ceil(n / one_each)          # only a group row naming no teacher leaves it within the size
        enough = "1 group is enough" if one_each == 1 else f"{one_each} groups are enough"
        options.append(_option("bigger", f"Allow groups of up to {most}, so {enough}"
                                         + (f" (above the class size of {size})" if most > size else ""),
                               {"op": "set_groups", "subject": subject, "per_teacher": 1}, never=True))
    options.append(_option("accept", "Leave it unassigned", {"op": "accept"}, fallback=True))
    _recommend(options, sim.weigh(_id("groups_short", subject, None), options, short=subject) if sim else {})
    return _conflict("groups_short", subject, None, text, options)


def _no_group(name: str, row: dict, warnings: list[str]) -> list[dict]:
    """A named teacher the proposal left with no class (`classes._named`) or group (`classes._grouped`),
    found by the proposal's own warning, which is the text. Ways out: drop them from the subject; for a
    Runs-with row, split into as many groups as the named teachers take; leave as is."""
    subject = row["subject"]
    word = "group" if row.get("runs_with") else "class"
    out = []
    deploy = row.get("deploy") or []
    for who in dict.fromkeys(d["teacher"] for d in deploy if d.get("teacher")):
        text = f"{name}: {subject}: {who} has no {word}"
        if text not in warnings:
            continue
        # the lines that got nothing: said once each, and the last of theirs that could (shares go in file
        # order; a Class row's counted lines are served first, its uncounted ones share what is left)
        lines = [i for i, d in enumerate(deploy) if d.get("teacher") == who
                 and (row.get("runs_with") or not d.get("classes"))]
        change = {"op": "remove_teacher", "subject": subject, "teacher": who}
        surplus = lines[-warnings.count(text):]
        if len(surplus) < sum(d.get("teacher") == who for d in deploy):
            change["at"] = surplus
        options = [_option("drop", f"Drop {who} from {subject}", change)]
        if row.get("runs_with"):
            options.append(_option("split", f"Split into {_teachers(row)} smaller groups",
                                   {"op": "set_groups", "subject": subject, "per_teacher": 1}))
        options.append(_option("accept", "Leave as is", {"op": "accept"}, fallback=True))
        _recommend(options, {})
        out.append(_conflict("teacher_no_group", subject, who, text, options))
    return out


def _overshoot(name: str, row: dict, reqs: list[dict], warnings: list[str]) -> list[dict]:
    """A Class or Combined row's counted teacher whose count `classes._named` trimmed, walking the counted
    lines over the row's requirements `reqs` (code order) as it does, found by its warning, which is the
    text. Ways out: trim the line to what it got (drop it when that is nothing), which is recommended; form
    one more class, which the route does by proposing again with that count (not stored here)."""
    deploy = row.get("deploy") or []
    if row.get("runs_with") or not reqs:
        return []
    subject = row["subject"]
    starts = [0, *accumulate(len(r["classes"]) for r in reqs)]      # class position each group starts at
    k = starts[-1]
    counted = [(i, d) for i, d in enumerate(deploy) if d.get("teacher") and d.get("classes")]
    asked = sum(d["classes"] for _, d in counted)
    out, g = [], 0
    for i, d in counted:
        who, through, h = d["teacher"], starts[g] + d["classes"], g
        while h < len(reqs) and starts[h] < through:
            h += 1
        got, g = starts[h] - starts[g], h
        if through <= k:
            continue
        if asked > k:
            text = (f"{name}: {subject} names {asked} classes for teachers but the level has {k}"
                    + (f"; {who}'s count is trimmed" if got else f", so {who} has no class"))
        else:
            text = (f"{name}: {subject} is combined {int(row.get('combine') or 1)} at a time, "
                    + (f"so {who}'s count is trimmed to {got}" if got else f"so {who} has no group left"))
        if text not in warnings:
            continue
        change = {"op": "set_count", "subject": subject, "teacher": who, "classes": got}
        if sum(norm(x.get("teacher")) == norm(who) for x in deploy) > 1:
            change["at"] = i
        options = [_option("trim", f"Trim {who} to {got}" if got else f"Drop {who} from {subject}", change,
                           fallback=True),
                   _option("count", f"Form {k + 1} classes", {"op": "count", "count": k + 1})]
        _recommend(options, {})
        out.append(_conflict("overshoot", subject, who, text, options))
    return out


def _outside(plan: dict, key: str, name: str, row: dict, reqs: list[dict], loads: dict,
             sim: _Sim | None) -> list[dict]:
    """A teacher the row's deployment names (`named` on its requirements) who lists subjects, but not
    this one at this level (`covers` False). Ways out: swap to each specialist of the department with room
    for the teacher's periods on the subject (`_with_room`; on a Runs-with row, not one already named for
    it); keep them. A swap is weighed by `_Sim`."""
    subject = row["subject"]
    staff = {p["id"]: p for p in plan.get("staff") or []}
    out = []
    for pid in dict.fromkeys(r["teacher"] for r in reqs if r.get("named") and r.get("teacher")):
        who = _written(plan, row, pid)
        if pid not in staff or who is None or SP.covers(staff[pid], subject, key) is not False:
            continue
        text = f"{name}: {subject}: {who} doesn't list {subject} at {name}"
        if sim is None:
            out.append(_conflict("outside_specialisation", subject, who, text, []))
            continue
        periods = sum(r.get("periods") or 0 for r in _named_to(reqs, pid))
        options = [_option(f"swap:{norm(alt)}", f"Swap to {alt}", {"op": "replace_teacher", "subject": subject,
                                                                   "teacher": who, "with": alt})
                   for _, alt, *_ in _with_room(plan, key, row, loads, periods, _not_targets(plan, row, pid))]
        options.append(_option("accept", f"Keep {who}", {"op": "accept"}, fallback=True))
        _recommend(options, sim.weigh(_id("outside_specialisation", subject, who), options) if sim else {})
        out.append(_conflict("outside_specialisation", subject, who, text, options))
    return out


def _over_allowance(plan: dict, key: str, name: str, row: dict, reqs: list[dict], loads: dict,
                    sim: _Sim | None) -> list[dict]:
    """A teacher the row's deployment names whose named requirement carries `classes.rewarn`'s "would be N
    periods over allowance". Ways out: move one class (a Runs-with row: one group) to each specialist of
    the department with room for it (`_with_room`; on a Runs-with row, not one already named for it); keep
    it. None is offered when no line of the giver's frees a group (`_freed`), nor one that would change
    nothing (`_Sim`); a move is recommended only when, made and proposed again, it brings the teacher within
    allowance and takes no one else past theirs."""
    subject, step = row["subject"], int(row["periods"])
    word = "group" if row.get("runs_with") else "class"
    deploy = row.get("deploy") or []
    out = []
    for pid in dict.fromkeys(r["teacher"] for r in reqs if r.get("named") and r.get("teacher")):
        if pid not in loads:                    # no longer on the staff: no conflict is built on them
            continue
        me, used, allowance = loads[pid]
        said = f"{me} would be {C._num(used - allowance)} periods over allowance"
        who = _written(plan, row, pid)
        if who is None or not any(said in r.get("warnings") or [] for r in _named_to(reqs, pid)):
            continue
        text = f"{name}: {subject}: {who} would be {C._num(used - allowance)} periods over allowance"
        if sim is None:
            out.append(_conflict("over_allowance", subject, who, text, []))
            continue
        options = []
        frees, at = _freed(row, reqs, who, pid)
        moves = _with_room(plan, key, row, loads, step, _not_targets(plan, row, pid)) if frees else []
        for _, alt, apid, *_ in moves:
            change = {"op": "move_class", "subject": subject, "from": who, "to": alt}
            if at is not None and sum(norm(d.get("teacher")) == norm(who) for d in deploy) > 1:
                change["at"] = at
            if not row.get("runs_with"):        # a Class line with no count: the classes the proposal gave it
                for field, written, tid in (("from_has", who, pid), ("to_has", alt, apid)):
                    has, counted = _classes_of(deploy, reqs, written, tid)
                    if not counted and any(norm(d.get("teacher")) == norm(written) for d in deploy):
                        change[field] = has
            options.append(_option(f"move:{norm(alt)}", f"Move a {word} to {alt}", change))
        options.append(_option("accept", "Keep it", {"op": "accept"}, fallback=True))
        _recommend(options, sim.weigh(_id("over_allowance", subject, who), options) if sim else {})
        out.append(_conflict("over_allowance", subject, who, text, options))
    return out


def _stale_pattern(name: str, row: dict, warnings: list[str]) -> dict | None:
    """A row whose stored lesson pattern no longer adds up to its periods (`classes._pattern`), found by
    its warning, which is the text. Ways out: single periods, so the school's periods win (recommended);
    the periods the pattern makes."""
    subject, periods, lessons = row["subject"], int(row["periods"]), row.get("lessons")
    text = (f"{name}: {subject}: the lesson pattern no longer adds up to {periods} periods, "
            f"so single periods are used")
    if not lessons or text not in warnings:
        return None
    total = C._total(lessons)
    options = [_option("singles", f"Use {periods} single periods", {"op": "clear_lessons", "subject": subject},
                       fallback=True),
               _option("periods", f"Make it {total} periods a week",
                       {"op": "set_periods", "subject": subject, "periods": total})]
    _recommend(options, {})
    return _conflict("stale_pattern", subject, None, text, options)


# ---------------------------------------------------------------------------
# the recommendation
# ---------------------------------------------------------------------------

def _ratio(used: float, allowance: float) -> float:
    if allowance:
        return used / allowance
    return 0.0 if used <= 0 else math.inf


def _recommend(options: list[dict], loads: dict[str, dict[str, tuple[float, float]]]) -> None:
    """Mark exactly one of `options` recommended and move it first, in place (the rest keep their order).
    `loads` is {option id: {staff name: (periods used after it, allowance)}} for each option that gives
    work to someone (a give, move or swap `_Sim.weigh` found clears its conflict), naming every teacher
    whose load it changes.
    Such an option that keeps them all within allowance wins, the smallest highest used ÷ allowance first
    (then the next highest, so a move whose giver ends the same either way goes to the one with most room),
    then by staff name; else the first option flagged `fallback` (a kind's accept, or its own preferred
    way out), else the first. An option flagged `never_recommend` (one that breaks a Must rule, as "Allow
    bigger groups" breaks the group-size limit, the stand-in until piece 3) is never picked unless it is
    the only one; nor is one past a teacher's allowance (the only other Must rule until piece 3). Both
    flags are taken off, so an option keeps its four keys."""
    if not options:
        return
    allowed = [o for o in options if not o.get("never_recommend")] or options
    fits = [(sorted((_ratio(u, a) for u, a in loads[o["id"]].values()), reverse=True), sorted(loads[o["id"]]), i)
            for i, o in enumerate(allowed)
            if loads.get(o["id"]) and all(u <= a for u, a in loads[o["id"]].values())]
    if fits:
        pick = allowed[min(fits)[2]]
    else:
        pick = next((o for o in allowed if o.get("fallback")), allowed[0])
    for o in options:
        o["recommended"] = o is pick
        o.pop("fallback", None)
        o.pop("never_recommend", None)
    options.sort(key=lambda o: not o["recommended"])


# ---------------------------------------------------------------------------
# applying a picked option
# ---------------------------------------------------------------------------

_OPS = ("add_teacher", "remove_teacher", "set_groups", "replace_teacher", "move_class", "set_count",
        "clear_lessons", "set_periods", "one_group")


def apply_option(entry: dict, option: dict) -> dict:
    """A new levels entry with `option`'s change made to its subject row; `entry` is not changed. "accept"
    changes nothing, nor does "count" (the route proposes again with that class count). "add_teacher" adds
    {teacher, groups} to the subject's deployment, and with
    `from_unnamed` takes as many groups off the first line naming no teacher that has groups (the line
    goes when that leaves it nothing), so a count made of Groups stays the same; "remove_teacher" takes
    the lines at `at` (indexes into the row's deployment), else every line naming the teacher (under
    `norm`), off it;
    "set_groups" gives each named teacher `per_teacher` groups on their first line, their other lines
    taken off (a teacher takes at most that many, since a Runs-with subject's groups all run at once), and
    drops the groups of a row naming no teacher (the row too, when that leaves it nothing), so the subject
    has as many groups as its named teachers take. "one_group" (`_one_group`) leaves `teacher` one group,
    the subject keeping the proposal's `groups`, those taken off left unassigned. "replace_teacher" writes
    `with` for `teacher` on every line naming them.
    "move_class" (`_move`) takes one class (a Runs-with row: one group) from `from` and gives it to `to`.
    "set_count" sets the classes of the line at `at`, else of the first naming `teacher`, to `classes`,
    taking the line off for 0. "clear_lessons" takes the row's lesson pattern off, so its periods are
    single periods; "set_periods" sets its periods. A deployment left empty is taken off (the store keeps
    `deploy` only when non-empty). ValueError("bad_option") for an op it does not know or a subject the
    entry has no row for. The result passes `levels.normalise_levels`."""
    out = copy.deepcopy(entry)
    change = option.get("change") or {}
    op = change.get("op")
    if op in ("accept", "count"):
        return out
    row = next((r for r in out.get("subjects") or [] if norm(r.get("subject")) == norm(change.get("subject"))), None)
    if row is None or op not in _OPS:
        raise ValueError("bad_option")
    deploy = row.setdefault("deploy", [])
    if op == "add_teacher":
        spare = next((d for d in deploy if not d.get("teacher") and d.get("groups")), None)
        if change.get("from_unnamed") and spare:
            spare["groups"] = max(0, spare["groups"] - int(change.get("groups") or 1)) or None
        deploy.append({"teacher": change["teacher"], "classes": change.get("classes"),
                       "groups": change.get("groups")})
        deploy[:] = [d for d in deploy if d.get("teacher") or d.get("classes") or d.get("groups")]
    elif op == "remove_teacher" and change.get("at") is not None:
        deploy[:] = [d for i, d in enumerate(deploy) if i not in set(change["at"])]
    elif op == "remove_teacher":
        deploy[:] = [d for d in deploy if norm(d.get("teacher")) != norm(change["teacher"])]
    elif op == "set_groups":
        seen: set[str] = set()
        for d in deploy:
            if d.get("teacher"):
                d["groups"] = None if norm(d["teacher"]) in seen else int(change["per_teacher"])
                seen.add(norm(d["teacher"]))
            else:
                d["groups"] = None
        deploy[:] = [d for d in deploy if d.get("groups") or (not d.get("teacher") and d.get("classes"))]
    elif op == "one_group":
        _one_group(deploy, change)
    elif op == "replace_teacher":
        for d in deploy:
            if norm(d.get("teacher")) == norm(change["teacher"]):
                d["teacher"] = change["with"]
    elif op == "move_class":
        _move(row, deploy, change)
    elif op == "set_count":
        at = change.get("at")
        if at is None:
            line = next((d for d in deploy if norm(d.get("teacher")) == norm(change["teacher"])), None)
        else:
            line = deploy[at] if isinstance(at, int) and 0 <= at < len(deploy) else None
        if line is None:
            raise ValueError("bad_option")
        line["classes"] = int(change["classes"]) or None
        if not line["classes"]:
            deploy[:] = [d for d in deploy if d is not line]
    elif op == "clear_lessons":
        row.pop("lessons", None)
    else:
        row["periods"] = int(change["periods"])
    if not deploy:
        del row["deploy"]
    return out


def _one_group(deploy: list[dict], change: dict) -> None:
    """`teacher` left one group, in place, the subject keeping the `groups` the proposal had. First every
    line's count is made what the proposal gave it (`classes._grouped`: in file order each teacher's line
    takes its count, one without a count, of the `groups`; a line that got none goes); then the teacher
    keeps one group on their first line, their other lines going; then the groups no teacher takes go on
    one line naming no teacher (the first such line, made when there is none), so they stay unassigned and
    the count, now the sum of the Groups, is the same. ValueError("bad_option") when `teacher` got no group
    or `groups` is not a whole number above 0."""
    total = change.get("groups")
    if isinstance(total, bool) or not isinstance(total, int) or total < 1:
        raise ValueError("bad_option")
    lines, spare, t = [], None, 0
    for d in deploy:
        if not d.get("teacher"):
            if spare is None:
                spare = d
                lines.append(d)
            continue
        count = int(d.get("groups") or 1)
        share = max(0, min(count, total - t))
        t += count
        if share:
            d["groups"] = share
            lines.append(d)
    mine = [d for d in lines if norm(d.get("teacher")) == norm(change["teacher"])]
    if not mine:
        raise ValueError("bad_option")
    mine[0]["groups"] = 1
    lines = [d for d in lines if all(d is not x for x in mine[1:])]
    left = total - sum(d["groups"] for d in lines if d.get("teacher"))
    if spare is not None and left:
        spare["groups"] = left
    elif spare is not None:
        lines = [d for d in lines if d is not spare]
    elif left:
        lines.append({"teacher": None, "classes": None, "groups": left})
    deploy[:] = lines


def _move(row: dict, deploy: list[dict], change: dict) -> None:
    """One class (a Runs-with row: one group) from `from` to `to`, in place. On the giver, the line at
    `at` (one of theirs with a count), else the last of their lines with a count, loses one (the line goes
    at 0); with none, a Runs-with line (one group) goes, and a Class row's lines become one line of
    `from_has` - 1 classes. On the taker, the last of their lines with a count gains one; with none, a
    Runs-with teacher's lines (one group each) become one line of one more group, a Class row's lines one
    line of `to_has` + 1; a teacher with no line gets one of 1. ValueError("bad_option") when `from` has
    no line, `at` is not one of their counted lines, or a Class line with no count comes without its
    `from_has` / `to_has`."""
    field = "groups" if row.get("runs_with") else "classes"
    gone: list[dict] = []
    mine = [d for d in deploy if norm(d.get("teacher")) == norm(change["from"])]
    if not mine:
        raise ValueError("bad_option")
    counted = [d for d in mine if d.get(field)]
    at = change.get("at")
    if at is not None:
        if not (isinstance(at, int) and 0 <= at < len(deploy) and any(deploy[at] is d for d in counted)):
            raise ValueError("bad_option")
        line = deploy[at]
        line[field] -= 1
    elif counted:
        line = counted[-1]
        line[field] -= 1
    elif row.get("runs_with"):
        line = mine[-1]
        line["groups"] = None
    else:
        if change.get("from_has") is None:
            raise ValueError("bad_option")
        line, gone = mine[0], mine[1:]
        line["classes"] = int(change["from_has"]) - 1
    if not line[field]:
        gone.append(line)
    theirs = [d for d in deploy if norm(d.get("teacher")) == norm(change["to"])]
    counted = [d for d in theirs if d.get(field)]
    if counted:
        counted[-1][field] += 1
    elif theirs and row.get("runs_with"):         # each line with no count is one group
        theirs[0]["groups"] = len(theirs) + 1
        gone += theirs[1:]
    elif theirs:
        if change.get("to_has") is None:
            raise ValueError("bad_option")
        theirs[0]["classes"] = int(change["to_has"]) + 1
        gone += theirs[1:]
    else:
        deploy.append({"teacher": change["to"], "classes": None, "groups": None, field: 1})
    deploy[:] = [d for d in deploy if all(d is not x for x in gone)]
