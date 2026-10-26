"""Forming classes over HTTP (spec docs/superpowers/specs/2026-10-20-csv-pack-design.md §3, Calls §1 and §2):
each level's facts in the `levels` store (expected students, class size, naming), a proposal per level kept
in `classes_run`, a teacher changed in it, and Apply / Undo of the classes, their requirements and the
students' classes and combined-group takes. A proposal comes with the level's deployment conflicts
(`deploy_conflicts.conflicts`), and the option the school picks for one is made to the `levels` entry and
the level proposed again; a pick never writes the plan. The pure work is in classes.py and
deploy_conflicts.py; this is the routes and their storage, following blocks_api.py.

Admin only: every route is `Depends(auth.require_session)`, which answers a department account with 403.
Names reach the page only, in a proposal's `names`; `classes_run` keeps student IDs, never a name. A reason
for a 400 or 409 names codes, IDs, groupings and level names; the log carries counts and conflict kinds
only. Nothing is written to the plan or the students before Apply.

`classes_run` is {level_key: {level, proposal, digest, count, applied, undo?, applied_entry?, accepted?}}.
`digest` is what the proposal rests on (the level's `levels` entry, its students' IDs, levels, choices and
merit, and its option-block combinations and overrides); `applied_entry` is the digest of the `levels`
entry the last Apply used, so a formed level whose entry has changed since reads as stale; `accepted` is
{conflict id: its text} of the conflicts the school chose to leave as they are: one stays settled while a
conflict of that id reads the same, whatever else changes, and is asked again when its text changes (an
older store's list of ids reads as none)."""
from __future__ import annotations

import copy
import logging
import threading

from fastapi import APIRouter, Depends, HTTPException, Query

from . import auth, blocks as K, classes as C, deploy_conflicts as D, levels as LV, students as S
from .plan import model as M
from .plan import specialisation as SP
from .plan.reader import applying

RUN = "classes_run"
CHANGED = "requirements or students changed since this proposal; propose again"
STALE = "this conflict has changed — look again"
MAX_EXPECTED, MAX_CLASS_SIZE, MAX_CLASSES, MAX_NAMING = 100_000, 1_000, 100, 40

log = logging.getLogger("plane_app")


def _int(value, name: str, lo: int, hi: int) -> int:
    try:
        return K.check_int(value, name, lo, hi)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _naming(value) -> str:
    """A naming pattern ("{n}{x}" by default): text of at most MAX_NAMING characters, without "," (a year
    key joins class codes with it) or "+" (a combined grouping does)."""
    if not isinstance(value, str):
        raise HTTPException(400, "naming must be text, such as {n}{x}")
    value = value.strip()
    if len(value) > MAX_NAMING:
        raise HTTPException(400, f"naming must be at most {MAX_NAMING} characters")
    if "," in value or "+" in value:
        raise HTTPException(400, "naming must not hold a comma or a plus sign")
    return value


def _shown(text: str) -> str:
    return text if len(text) <= MAX_NAMING else text[:MAX_NAMING] + "..."


def _key(level) -> str:
    if not isinstance(level, str) or not level.strip():
        raise HTTPException(400, "give the level")
    return SP.level_key(level)


def _digest(entry: dict, students: list[dict], setup: dict | None) -> str:
    """What a proposal rests on: the level's `levels` entry, each of its students' ID, own level, choices,
    merit and Runs-with subjects (only when they have some, so an older digest still matches; never a name,
    a class or takes, which Apply writes), and the level's option-block combinations and overrides (the
    placement that groups the students)."""
    rows = sorted([str(s.get("id")), str(s.get("level") or ""), [str(c) for c in s.get("choices") or []],
                   s.get("merit"), *([[str(x) for x in s["subjects"]]] if s.get("subjects") else [])]
                  for s in students)
    setup = setup or {}
    return applying._hash({"entry": entry, "students": rows, "combinations": setup.get("combinations"),
                           "overrides": setup.get("overrides") or {}})


def _version(record: dict) -> str:
    """The token a page holds for the proposal it shows: the hash of the run record's digest, count and
    proposal, so any new proposal, teacher change or pick gives a new one. A pick sent with another is
    refused (409 STALE): sent twice, or from a second tab, it is made once."""
    return applying._hash({"digest": record.get("digest"), "count": record.get("count"),
                           "proposal": record.get("proposal")})


def _stale(why: str) -> HTTPException:
    """409 STALE, with the header X-Stale saying why, so the page knows how to look again: "version" when
    the stored proposal is still what the facts give and only the page's copy is older (the page reads it
    again), "facts" when the stored facts no longer match it (the page proposes again)."""
    return HTTPException(409, STALE, headers={"X-Stale": why})


def _accepted(record: dict) -> dict[str, str]:
    """The record's accepted conflicts, {id: text}; an older store's list of ids is none (its texts are not
    known, so each is asked again)."""
    found = record.get("accepted")
    if not isinstance(found, dict):
        return {}
    return {k: v for k, v in found.items() if isinstance(k, str) and isinstance(v, str)}


def make_router(db) -> APIRouter:
    r = APIRouter()
    lock = threading.Lock()             # classes_run read-modify-write; taken after the plan lock

    def _plan(tid: str) -> dict:
        return M.normalise(db.get_value("plan", tid) or M.empty_plan())

    def _students(tid: str) -> list[dict]:
        stored = db.get_value("students", tid)
        return list(stored) if isinstance(stored, list) else []

    def _runs(tid: str) -> dict:
        stored = db.get_value(RUN, tid)
        return dict(stored) if isinstance(stored, dict) else {}

    def _setup(tid: str, key: str, plan: dict, levels: dict) -> dict | None:
        """The level's option-block setup, under the key option blocks gives its formed classes (the levels
        entry's name, `K.setup_key_for`) or the old "level:<level_key>" one; 409 when two stored setups of the
        level differ (R20)."""
        name = (levels.get(key) or {}).get("name") or key
        try:
            return K.find_setup(db.get_value("blocks_setup", tid), K.setup_key_for(name),
                                K.level_names(plan, levels))[1]
        except K.SetupClash as e:
            raise HTTPException(409, str(e))

    def _entry(levels: dict, level) -> tuple[str, dict]:
        key = _key(level)
        if key not in levels:
            raise HTTPException(404, f"no level {_shown(level.strip())}: give requirements.csv for it first")
        return key, levels[key]

    def _record(tid: str, key: str) -> dict:
        found = _runs(tid).get(key)
        if not isinstance(found, dict) or not found.get("proposal"):
            raise HTTPException(404, "No proposal for this level: propose first.")
        return found

    def _formed(plan: dict, key: str) -> list[str]:
        return [c["code"] for c in plan["classes"] if c.get("formed") == key]

    def _level_view(plan: dict, levels: dict, key: str, students: list[dict], runs: dict) -> dict:
        entry, record = levels[key], runs.get(key) if isinstance(runs.get(key), dict) else {}
        formed = _formed(plan, key)
        applied = record.get("applied_entry")
        return {"level": key, "name": entry["name"], "students_expected": entry["students_expected"],
                "class_size": entry["class_size"], "naming": entry["naming"],
                "students": len(C._level_students(plan, key, students)),
                "formed": bool(formed), "classes": formed,
                "stale": bool(formed) and bool(applied) and applied != applying._hash(entry),
                "proposed": bool(record.get("proposal")), "can_undo": bool(record.get("undo"))}

    def _conflicts(tid: str, record: dict, prop: dict, plan: dict, levels: dict, students: list[dict],
                   only: str | None = None) -> list[dict]:
        """The level's deployment conflicts, worked out from its `levels` entry and `prop` (its proposal as the
        page sees it: warned against the plan as it is now), each `settled` when the school accepted it as it reads now.
        None for a level no longer in the store, and none for one whose rows carry no `deploy`. With `only`, the
        conflict of that id alone (`deploy_conflicts.conflicts`' `only`), as it reads in the full list."""
        key = record["level"]
        if key not in levels:
            return []
        accepted = _accepted(record)
        return [{**c, "settled": accepted.get(c["id"]) == c["text"]}
                for c in D.conflicts(plan, db.get_settings(tid), key, levels[key], prop,
                                     C._level_students(plan, key, students), only=only)]

    def _view(tid: str, record: dict, plan: dict | None = None) -> dict:
        """What the page sees of a proposal: its warnings worked out against the plan as it is now, its
        deployment conflicts, and the names of its students (for the page only; never stored with it)."""
        plan = plan if plan is not None else _plan(tid)
        key = record["level"]
        prop = C.rewarn(record["proposal"], plan, db.get_settings(tid), key)
        students = _students(tid)
        sids = {sid for c in prop.get("classes") or [] for sid in c.get("students") or []}
        names = {s["id"]: s.get("name") or "" for s in students if s.get("id") in sids}
        return {**prop, "count": record.get("count"), "version": _version(record), "names": names,
                "can_apply": not record.get("applied") and bool(prop.get("classes")),
                "can_undo": bool(record.get("undo")),
                "conflicts": _conflicts(tid, record, prop, plan, LV.get(db, tid), students)}

    def _propose(tid: str, key: str, count: int | None, plan: dict, levels: dict, students: list[dict],
                 expect: str | None = None, store_levels: bool = False, keep: dict | None = None) -> dict:
        """The level proposed (`classes.propose`) and stored with the digest of what it rests on, keeping the
        last Apply's Undo and entry, and the accepted conflicts (each settled while it reads the same); the record.
        With `expect` (a pick's version), 409 STALE ("version") when the stored record no longer has it,
        nothing stored;
        with `store_levels`, `levels` (a pick's changed entry) is stored with the record, under the same check;
        with `keep` (the proposal a pick was made on), the teachers the school chose there by hand are carried
        over (`classes.keep_chosen`)."""
        entry = levels[key]
        setup = _setup(tid, key, plan, levels)
        prop = C.propose(plan, db.get_settings(tid), key, entry, students, setup, count=count)
        if keep:
            prop = C.keep_chosen(keep, prop, plan, db.get_settings(tid), key)
        record = {"level": key, "proposal": prop, "count": count, "applied": False,
                  "digest": _digest(entry, C._level_students(plan, key, students), setup)}
        with lock:
            runs = _runs(tid)
            old = runs.get(key) if isinstance(runs.get(key), dict) else {}
            if expect is not None and _version(old) != expect:
                raise _stale("version")
            if store_levels:
                LV.put(db, tid, levels)
            for k in ("undo", "applied_entry"):     # the last Apply's Undo and its entry outlive a new proposal
                if old.get(k):
                    record[k] = old[k]
            if _accepted(old):                      # the school's say stands while its conflict reads the same
                record["accepted"] = _accepted(old)
            runs[key] = record
            db.set_value(RUN, runs, tid)
        log.info("classes proposed: %d classes, %d requirements, %d students", len(prop["classes"]),
                 len(prop["requirements"]), sum(c["size"] for c in prop["classes"]))
        return record

    @r.get("/api/classes/levels")
    def levels_view(sid: str = Depends(auth.require_session)):
        """Each level of the `levels` store, with its count from the student list and whether it is formed,
        stale (its entry changed since its Apply), proposed and undoable. Read only."""
        tid = db.working_timetable()
        plan, levels, students, runs = _plan(tid), LV.get(db, tid), _students(tid), _runs(tid)
        order = sorted(levels, key=lambda k: S._natural(levels[k]["name"]))
        return {"levels": [_level_view(plan, levels, k, students, runs) for k in order]}

    @r.put("/api/classes/levels/{level}")
    def put_level(level: str, body: dict, sid: str = Depends(auth.require_session)):
        """A level's expected students (null clears it), class size and naming; the keys not given stay."""
        tid = db.working_timetable()
        if not isinstance(body, dict) or not {"students_expected", "class_size", "naming"} & set(body):
            raise HTTPException(400, "give students_expected, class_size or naming")
        with db.plan_lock(tid):                     # the levels store is written under the plan lock (as Apply does)
            levels = LV.get(db, tid)
            key, entry = _entry(levels, level)
            new = dict(entry)
            if "students_expected" in body:
                given = body["students_expected"]
                new["students_expected"] = None if given is None else _int(
                    given, "the expected number of students", 0, MAX_EXPECTED)
            if "class_size" in body:
                new["class_size"] = _int(body["class_size"], "the class size", 1, MAX_CLASS_SIZE)
            if "naming" in body:
                new["naming"] = _naming(body["naming"])
            try:
                levels = LV.put(db, tid, {**levels, key: new})
            except ValueError as e:                 # a code, never a value
                raise HTTPException(400, f"the level could not be stored ({e})")
        return _level_view(_plan(tid), levels, key, _students(tid), _runs(tid))

    @r.get("/api/classes/proposal")
    def stored_proposal(level: str = Query(""), sid: str = Depends(auth.require_session)):
        """The level's proposal as Propose stored it, with names, for the page to show on opening. Read only."""
        tid = db.working_timetable()
        return _view(tid, _record(tid, _key(level)))

    @r.post("/api/classes/propose")
    def propose(body: dict, sid: str = Depends(auth.require_session)):
        """A level's classes, requirements and suggested teachers (`classes.propose`), stored with the digest
        of what they rest on; nothing written to the plan or the students. `count` is the school's own number
        of classes. With `keep` true (the page proposing again after a stale pick), the teachers the school
        chose by hand in the stored proposal are carried over (`classes.keep_chosen`); without, every teacher
        not named by the deployment is suggested afresh."""
        tid = db.working_timetable()
        plan, levels, students = _plan(tid), LV.get(db, tid), _students(tid)
        key, _ = _entry(levels, (body or {}).get("level"))
        count = body.get("count")
        count = None if count in (None, "") else _int(count, "the number of classes", 1, MAX_CLASSES)
        stored = _runs(tid).get(key) if body.get("keep") is True else None
        keep = stored.get("proposal") if isinstance(stored, dict) else None
        return _view(tid, _propose(tid, key, count, plan, levels, students, keep=keep), plan)

    @r.post("/api/classes/conflicts/apply")
    def apply_conflict(body: dict, sid: str = Depends(auth.require_session)):
        """The option the school picked for one of the level's deployment conflicts, after its yes. The
        conflicts are worked out again from the stored facts and the option looked up there by id (a change
        the page sends is never used); 409 STALE when the proposal no longer rests on the stored facts or
        the conflict or option is gone. "accept" records the conflict as settled; "count" proposes again
        with that number of classes; any other change is made to the `levels` entry (`apply_option`) and
        the level proposed again. Either way the teachers the school chose by hand are kept
        (`classes.keep_chosen`). The body carries the `version` of the view the pick was made on; another
        (sent twice, or from an older page) is 409 STALE. A 409 STALE says why in its X-Stale header
        (`_stale`): "facts" when the stored facts no longer match the proposal, else "version". The plan is
        never written."""
        tid = db.working_timetable()
        body = body if isinstance(body, dict) else {}
        with db.plan_lock(tid):                     # the levels store is written under the plan lock (as Apply does)
            plan, levels, students = _plan(tid), LV.get(db, tid), _students(tid)
            key, entry = _entry(levels, body.get("level"))
            record = _runs(tid).get(key)
            if not isinstance(record, dict) or not record.get("proposal"):
                raise HTTPException(409, "No proposal for this level: propose first.")
            version = body.get("version")
            if _digest(entry, C._level_students(plan, key, students),
                       _setup(tid, key, plan, levels)) != record["digest"]:
                raise _stale("facts")               # the proposal no longer rests on the stored facts
            if not isinstance(version, str) or version != _version(record):
                raise _stale("version")             # sent twice, or from a page showing an older proposal
            prop = C.rewarn(record["proposal"], plan, db.get_settings(tid), key)
            cid = body.get("conflict")              # the picked conflict alone is weighed: the new view weighs all
            conflict = next((c for c in _conflicts(tid, record, prop, plan, levels, students, only=cid)
                             if c["id"] == cid), None) if isinstance(cid, str) else None
            option = next((o for o in (conflict or {}).get("options") or [] if o["id"] == body.get("option")), None)
            if option is None:
                raise _stale("version")
            op = option["change"]["op"]
            if op == "accept":
                with lock:
                    runs = _runs(tid)
                    if not isinstance(runs.get(key), dict) or _version(runs[key]) != version:
                        raise _stale("version")             # proposed again or changed meanwhile
                    record = runs[key]
                    record = {**record, "accepted": {**_accepted(record), conflict["id"]: conflict["text"]}}
                    runs[key] = record
                    db.set_value(RUN, runs, tid)
            elif op == "count":
                count = _int(option["change"]["count"], "the number of classes", 1, MAX_CLASSES)
                record = _propose(tid, key, count, plan, levels, students, expect=version, keep=record["proposal"])
            else:
                try:
                    levels = LV.normalise_levels({**levels, key: D.apply_option(entry, option)})
                except ValueError as e:             # a code, never a value
                    raise HTTPException(409, f"the deployment could not be changed ({e})")
                record = _propose(tid, key, record.get("count"), plan, levels, students, expect=version,
                                  store_levels=True, keep=record["proposal"])
        log.info("classes conflict applied: %s %s", conflict["kind"], op)
        return _view(tid, record, plan)

    @r.put("/api/classes/teacher")
    def teacher(body: dict, sid: str = Depends(auth.require_session)):
        """One requirement's teacher changed (or cleared with null); the warnings are worked out again."""
        tid = db.working_timetable()
        plan = _plan(tid)
        body = body if isinstance(body, dict) else {}
        key, index, chosen = _key(body.get("level")), body.get("index"), body.get("teacher")
        with lock:
            record = _runs(tid).get(key)
            if not isinstance(record, dict) or not record.get("proposal"):
                raise HTTPException(409, "No proposal to change: propose first.")
            reqs = record["proposal"]["requirements"]
            if isinstance(index, bool) or not isinstance(index, int):
                raise HTTPException(400, "give the requirement's index as a whole number")
            if not 0 <= index < len(reqs):
                raise HTTPException(404, f"no requirement {index} in the proposal")
            if chosen is not None:
                person = next((p for p in plan["staff"] if p["id"] == chosen), None)
                if person is None:
                    raise HTTPException(400, f"no teacher {_shown(str(chosen))}")
                dept = reqs[index].get("dept")
                deployed = chosen in C.deployed_ids(plan, LV.get(db, tid).get(key), reqs[index]["subject"])
                if dept and person.get("dept") != dept and not deployed:
                    raise HTTPException(400, f"{chosen} isn't in the {dept} department")
            else:
                deployed = False
            proposal = copy.deepcopy(record["proposal"])
            proposal["requirements"][index]["teacher"] = chosen
            proposal["requirements"][index]["suggested"] = False
            # the school's own choice now: `named` (kept at Apply in another department) only if the file names them
            proposal["requirements"][index].pop("named", None)
            if deployed:
                proposal["requirements"][index]["named"] = True
            proposal = C.rewarn(proposal, plan, db.get_settings(tid), key)
            record = {**record, "proposal": proposal, "applied": False}
            runs = _runs(tid)
            runs[key] = record
            db.set_value(RUN, runs, tid)
        return _view(tid, record, plan)

    @r.post("/api/classes/apply")
    def apply(body: dict, sid: str = Depends(auth.require_session)):
        """The proposal written: its classes, division and requirements into the plan (`apply_proposal`), each
        placed student's class and combined-group takes (`students_after`); the plan and the changed
        students kept for Undo. Refused when the level's entry or students have changed since the proposal,
        when a requirement has no department ("say which department for <subject>", R27(b)), or when
        apply_proposal refuses (a lock, a clash). A refusal writes nothing."""
        tid = db.working_timetable()
        key = _key((body or {}).get("level"))
        with db.plan_lock(tid), lock:
            raw = db.get_value("plan", tid)
            plan = M.normalise(raw or M.empty_plan())
            runs = _runs(tid)
            record = runs.get(key)
            if not isinstance(record, dict) or not record.get("proposal"):
                raise HTTPException(409, "Nothing to apply: propose first.")
            if record.get("applied"):
                raise HTTPException(409, "Already applied.")
            levels = LV.get(db, tid)
            entry = levels.get(key)
            stored = _students(tid)
            if entry is None or _digest(entry, C._level_students(plan, key, stored),
                                        _setup(tid, key, plan, levels)) != record["digest"]:
                raise HTTPException(409, CHANGED)
            # each chosen teacher checked against the plan as it is now, as option blocks' Apply does: one gone,
            # or moved out of the requirement's department, is dropped (no teacher) and named in the answer;
            # a teacher the deployment names is kept in another department (the proposal said so)
            staff = {p["id"]: p for p in plan["staff"]}
            proposal = copy.deepcopy(record["proposal"])
            dropped = []
            for i, q in enumerate(proposal["requirements"]):
                t, dept = q.get("teacher"), q.get("dept")
                elsewhere = dept and t in staff and staff[t].get("dept") != dept and not q.get("named")
                if t and (t not in staff or elsewhere):
                    dropped.append({"index": i, "subject": q["subject"], "classes": list(q["classes"]), "teacher": t})
                    q["teacher"] = None
            unsaid = list(dict.fromkeys(q["subject"] for q in proposal["requirements"] if not q.get("dept")))
            if unsaid:                              # R27(b): a requirement with no department is never written
                raise HTTPException(409, f"say which department for {', '.join(unsaid)}")
            try:
                new = C.apply_proposal(plan, key, proposal, name=entry.get("name"))
                M.check_locks(plan, new)
            except ValueError as e:                 # PlanError and LockError are ValueErrors: codes only
                raise HTTPException(409, str(e))
            after = C.students_after(stored, plan, new, key, proposal)
            before = [[i, {k: s[k] for k in ("class", "takes") if k in s}]
                      for i, (s, a) in enumerate(zip(stored, after)) if s != a]
            db.set_value("plan", new, tid)
            db.set_value("students", after, tid)
            written = db.get_value("plan", tid) or {}
            undo = {"level": key, "plan_before": raw, "rev_after": int(written.get("rev") or 0),
                    "content_after": applying._content(written), "students_before": before,
                    "after_hash": applying._hash(db.get_value("students", tid) or []),
                    "applied_entry_before": record.get("applied_entry")}
            record = {**record, "proposal": C.rewarn(proposal, new, db.get_settings(tid), key) if dropped else proposal,
                      "applied": True, "undo": undo, "applied_entry": applying._hash(entry)}
            runs[key] = record
            db.set_value(RUN, runs, tid)
        log.info("classes applied: %d classes, %d requirements, %d students changed",
                 len(record["proposal"]["classes"]), len(record["proposal"]["requirements"]), len(before))
        return {"ok": True, **_view(tid, record), "teachers_dropped": dropped}

    @r.post("/api/classes/undo")
    def undo(body: dict, sid: str = Depends(auth.require_session)):
        """The level's last Apply put back, plan and students, when neither has changed since; else refused,
        nothing moved."""
        tid = db.working_timetable()
        key = _key((body or {}).get("level"))
        with db.plan_lock(tid), lock:
            runs = _runs(tid)
            record = runs.get(key) if isinstance(runs.get(key), dict) else {}
            done = record.get("undo")
            if not isinstance(done, dict):
                raise HTTPException(409, "Nothing to undo.")
            if not applying.plan_as_written(db.get_value("plan", tid), done):
                raise HTTPException(409, applying.PLAN_CHANGED)
            if not applying.students_as_written(db, done, tid):
                raise HTTPException(409, applying.STUDENTS_CHANGED)
            students = _students(tid)
            for i, saved in done["students_before"]:
                students[i] = {**{k: v for k, v in students[i].items() if k not in ("class", "takes")}, **saved}
            db.set_value("plan", done.get("plan_before") or M.empty_plan(), tid)      # the rev keeps moving forward
            db.set_value("students", students, tid)
            record = {k: v for k, v in record.items() if k not in ("undo", "applied_entry")}
            record["applied"] = False
            if done.get("applied_entry_before"):
                record["applied_entry"] = done["applied_entry_before"]
            runs[key] = record
            db.set_value(RUN, runs, tid)
        log.info("classes undone: %d students", len(done["students_before"]))
        return {"ok": True}

    return r
