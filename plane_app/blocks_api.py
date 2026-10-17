"""Option blocks over HTTP (spec docs/superpowers/specs/2026-10-17-option-blocks-design.md §1, §4): a year
group's combinations and limits (`blocks_setup`, keyed by year key), placing its students, the engine run on
counts (`blocks_run`, one run per timetable at a time), the proposal and its teachers, and Apply / Undo of the
sets, bands and students' takes. The pure work is in blocks.py; this is the routes and their storage.

Admin only: every route is `Depends(auth.require_session)`, which answers a department account with 403.
The engine gets opaque combination ids, subject codes and counts, never a name, student ID or merit rank; a
reason for a 400 or 409 names IDs and codes only; the log carries counts only. A timetable without
`blocks_setup` never reaches the engine from here."""
from __future__ import annotations

import copy
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query

from . import auth, blocks as K, students as S
from .engine_client import EngineError
from .plan import model as M
from .plan.reader import applying

SETUP, RUN = "blocks_setup", "blocks_run"
MAX_BLOCKS, MAX_COMBINATIONS, MAX_SUBJECTS, MAX_CODE = K.MAX_BLOCKS, K.MAX_COMBINATIONS, K.MAX_SUBJECTS, K.MAX_CODE
DEFAULT_LIMITS = K.DEFAULT_LIMITS
DEFAULT_TIME_LIMIT, MAX_TIME_LIMIT = 60, 300
DONE = ("optimal", "feasible")
RESERVED = K.RESERVED
STUDENTS_CHANGED = "The students have changed since this run; build again."
SETUP_CHANGED = "The setup has changed since this run; build again."

log = logging.getLogger("plane_app")


def _int(value, name: str, lo: int, hi: int | None = None) -> int:
    try:
        return K.check_int(value, name, lo, hi)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _text(value, name: str, required: bool = False) -> str:
    try:
        return K.check_text(value, name, required)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _combinations(raw) -> list[dict]:
    try:
        return K.check_combinations(raw)       # the one validation, shared with the workbook's sheet
    except ValueError as e:
        raise HTTPException(400, str(e))


def _limits(raw) -> dict:
    if not isinstance(raw, dict):
        raise HTTPException(400, "limits must give blocks, min_size and max_size")
    out = {"blocks": _int(raw.get("blocks"), "the number of blocks", 1, MAX_BLOCKS),
           "min_size": _int(raw.get("min_size"), "the smallest set", 0),
           "max_size": _int(raw.get("max_size"), "the largest set", 1)}
    if out["min_size"] > out["max_size"]:
        raise HTTPException(400, "the smallest set must not be larger than the largest")
    return out


def _subjects(raw) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > MAX_SUBJECTS:
        raise HTTPException(400, f"subjects must map at most {MAX_SUBJECTS} subject codes to their details")
    out = {}
    for code, given in raw.items():
        if not isinstance(given, dict):
            raise HTTPException(400, f"{code}: give its details as an object")
        item = {}
        for k in ("name", "dept", "room_kind"):
            if given.get(k) not in (None, ""):
                item[k] = _text(given[k], f"{code}'s {k.replace('_', ' ')}")
        if given.get("periods") not in (None, ""):
            item["periods"] = _int(given["periods"], f"{code}'s periods a cycle", 1, 200)
        out[str(code).strip()] = item
    return out


def _overrides(raw, combinations: list[dict]) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(400, "overrides must map a student ID to a combination code")
    by_key = {S.norm(c["code"]): c["code"] for c in combinations}
    out = {}
    for sid, code in raw.items():
        if code in (None, ""):
            continue                                    # cleared
        if not isinstance(code, str) or S.norm(code) not in by_key:
            raise HTTPException(400, f"{sid}: {code} isn't a combination")
        out[str(sid).strip()] = by_key[S.norm(code)]
    return out


def _spec(key: str, classes: list[str]) -> dict:
    kind, _, rest = key.partition(":")
    return {"level": rest} if kind == "level" else {"classes": list(classes)}


def make_router(db, engine) -> APIRouter:
    r = APIRouter()
    lock = K.SETUP_LOCK                 # blocks_setup / blocks_run read-modify-write (shared with the workbook's Apply); taken after the plan lock

    def _plan(tid: str) -> dict:
        return M.normalise(db.get_value("plan", tid) or M.empty_plan())

    def _year(plan: dict, year) -> tuple[str, list[str]]:
        if not isinstance(year, str) or not year:
            raise HTTPException(400, "give the year group")
        try:
            return year, K.year_classes(plan, year)
        except ValueError as e:
            raise HTTPException(400, str(e))

    def _setups(tid: str) -> dict:
        stored = db.get_value(SETUP, tid)
        return dict(stored) if isinstance(stored, dict) else {}

    def _setup(tid: str, key: str) -> dict:
        found = _setups(tid).get(key)
        if not found:
            raise HTTPException(409, "Set up the combinations for this year group first.")
        return found

    def _students(tid: str, classes: list[str]) -> list[dict]:
        wanted = set(classes)
        return [s for s in db.get_value("students", tid) or [] if s.get("class") in wanted]

    def _record(tid: str, key: str) -> dict:
        record = db.get_value(RUN, tid)
        if not isinstance(record, dict) or record.get("year") != key:
            raise HTTPException(404, "No blocks run for this year group.")
        return record

    def _view(tid: str, record: dict) -> dict:
        """What the page sees of the run: never the split (student IDs) or the opaque map. The proposal's
        teacher names and warnings are worked out against the plan as it is now."""
        status = record.get("status")
        ok = status in DONE and bool(record.get("proposal"))
        undo = record.get("undo")
        proposal = None
        if ok:
            proposal = K.rewarn(record["proposal"], _plan(tid), db.get_settings(tid), record["meta"], record["year"])
        return {"year": record["year"], "status": status, "reasons": list(record.get("reasons") or []),
                "time_limit": record.get("time_limit"), "started": record.get("started"), "proposal": proposal,
                "can_apply": ok and not record.get("applied"),
                "can_undo": bool(undo) and undo.get("year") == record["year"]}

    def _fingerprint(tid: str, key: str, classes: list[str]) -> str:
        return K.year_fingerprint(_students(tid, classes), (_setups(tid).get(key) or {}).get("overrides"))

    def _engine_error(e: EngineError) -> HTTPException:
        return HTTPException(502 if e.status in (0, 500) else e.status, e.detail)

    @r.get("/api/blocks/years")
    def years(sid: str = Depends(auth.require_session)):
        """The plan's levels and the year keys with a stored setup, for the picker."""
        tid = db.working_timetable()
        plan = _plan(tid)
        levels = sorted({S.level_of(plan, c["code"]) for c in plan["classes"]} - {""}, key=S._natural)
        # the last Apply's Undo outlives a run for another year, which the run's own view cannot show: each
        # year says whether the stored Undo is its own
        record = db.get_value(RUN, tid)
        undo = record.get("undo") if isinstance(record, dict) else None
        undo_year = undo.get("year") if isinstance(undo, dict) else None
        stored = []
        for key in sorted(_setups(tid)):
            try:
                stored.append({"key": key, "classes": K.year_classes(plan, key), "can_undo": key == undo_year})
            except ValueError:
                stored.append({"key": key, "classes": [], "can_undo": key == undo_year})   # its classes are gone
        return {"levels": [{"level": lv, "key": f"level:{lv}", "classes": K.year_classes(plan, f"level:{lv}"),
                            "can_undo": f"level:{lv}" == undo_year}
                           for lv in levels],
                "stored": stored}

    @r.get("/api/blocks/setup")
    def get_setup(year: str = Query(""), sid: str = Depends(auth.require_session)):
        tid = db.working_timetable()
        plan = _plan(tid)
        key, classes = _year(plan, year)
        setup = _setups(tid).get(key) or {"year": _spec(key, classes), "combinations": [],
                                          "limits": dict(DEFAULT_LIMITS), "subjects": {}, "overrides": {},
                                          "placement": None}
        meta, found = K.subjects_meta(plan, setup)
        return {"year": key, "classes": classes, "setup": setup, "subjects": meta, "checks": found}

    @r.put("/api/blocks/setup")
    def put_setup(body: dict, sid: str = Depends(auth.require_session)):
        """A year group's combinations, limits, subject details and overrides; a placement made before is
        dropped (place again)."""
        tid = db.working_timetable()
        plan = _plan(tid)
        year = body.get("year")
        try:
            if isinstance(year, str):
                year = {"classes": K.year_classes(plan, year)}
            key = K.year_key(plan, year)
            classes = K.year_classes(plan, key)
        except ValueError as e:
            raise HTTPException(400, str(e))
        combinations = _combinations(body.get("combinations", []))
        setup = {"year": _spec(key, classes), "combinations": combinations,
                 "limits": _limits(body.get("limits", DEFAULT_LIMITS)),
                 "subjects": _subjects(body.get("subjects")),
                 "overrides": _overrides(body.get("overrides"), combinations), "placement": None}
        with lock:
            setups = _setups(tid)
            setups[key] = setup
            db.set_value(SETUP, setups, tid)
        meta, found = K.subjects_meta(plan, setup)
        return {"year": key, "classes": classes, "setup": setup, "subjects": meta, "checks": found}

    @r.get("/api/blocks/place")
    def stored_placement(year: str = Query(""), sid: str = Depends(auth.require_session)):
        """The placement made last (as POST /api/blocks/place stored it), with the year's names and the checks
        worked out now, for the page to show on opening. Read only: nothing is placed or written; 404 when the
        year has no setup or no placement."""
        tid = db.working_timetable()
        plan = _plan(tid)
        key, classes = _year(plan, year)
        setup = _setups(tid).get(key)
        result = (setup or {}).get("placement")
        if not result:
            raise HTTPException(404, "No placement for this year group.")
        students = _students(tid, classes)
        _meta, missing = K.subjects_meta(plan, setup)
        found = K.checks(setup["combinations"], students, setup["limits"]) + missing
        return {**result, "checks": found, "names": {s["id"]: s.get("name") or "" for s in students}}

    @r.post("/api/blocks/place")
    def place(body: dict, sid: str = Depends(auth.require_session)):
        """The checks and each student's placement (with names, for the page); the placement is stored
        without them."""
        tid = db.working_timetable()
        plan = _plan(tid)
        key, classes = _year(plan, body.get("year"))
        setup = _setup(tid, key)
        students = _students(tid, classes)
        _meta, missing = K.subjects_meta(plan, setup)
        found = K.checks(setup["combinations"], students, setup["limits"]) + missing
        result = K.place(students, setup["combinations"], setup.get("overrides"))
        with lock:
            setups = _setups(tid)
            if key in setups:
                setups[key] = {**setups[key], "placement": result}
                db.set_value(SETUP, setups, tid)
        log.info("blocks placement: %d students, %d placed, %d unplaced", len(students), len(result["placed"]),
                 len(result["unplaced"]))
        return {**result, "checks": found, "names": {s["id"]: s.get("name") or "" for s in students}}

    @r.post("/api/blocks/run", status_code=202)
    def run(body: dict, sid: str = Depends(auth.require_session)):
        """Start the engine on the year's placement; a run of any year before it is cancelled and replaced."""
        tid = db.working_timetable()
        plan = _plan(tid)
        key, classes = _year(plan, body.get("year"))
        setup = _setup(tid, key)
        if not setup.get("placement"):
            raise HTTPException(409, "Place the students first.")
        time_limit = body.get("time_limit", DEFAULT_TIME_LIMIT)
        time_limit = _int(time_limit, "the time limit", 1, MAX_TIME_LIMIT)
        students = _students(tid, classes)
        meta, missing = K.subjects_meta(plan, setup)
        blocking = [c["text"] for c in K.checks(setup["combinations"], students, setup["limits"]) + missing
                    if c["blocking"]]
        if blocking:
            more = f" (and {len(blocking) - 5} more)" if len(blocking) > 5 else ""
            raise HTTPException(409, "Fix these first: " + "; ".join(blocking[:5]) + more)
        fresh = K.place(students, setup["combinations"], setup.get("overrides"))
        if fresh["placed"] != (setup["placement"] or {}).get("placed"):
            raise HTTPException(409, "The students or combinations have changed since placing; place them again.")
        if not fresh["placed"]:
            raise HTTPException(409, "Nobody is placed: no combination has places for these students.")
        request, mapping = K.engine_request(setup, fresh["placed"], meta, plan, db.get_settings(tid), key)
        short = K.no_teacher_left(request, meta)
        if short:                                       # the engine could only say "at most 0": say why here
            more = f" (and {len(short) - 5} more)" if len(short) > 5 else ""
            raise HTTPException(409, "Fix these first: " + "; ".join(short[:5]) + more)
        fingerprint = K.year_fingerprint(students, setup.get("overrides"))
        eng = engine()
        # the engine is called outside the lock (a slow engine never holds up a blocks write, nor through the
        # plan lock an Apply's wait); what was read is checked again before the run is stored
        with lock:
            old = db.get_value(RUN, tid)
            old_job = old.get("job") if isinstance(old, dict) and old.get("status") == "running" else None
        if old_job is not None:
            try:
                eng.cancel_job(old_job)
            except EngineError:
                pass                                    # the engine forgot it
        try:
            job = eng.blocks(request, time_limit)
        except EngineError as e:
            raise _engine_error(e)
        with lock:
            now = db.get_value(RUN, tid)
            now = now if isinstance(now, dict) else {}
            lost = now.get("status") == "running" and now.get("job") != old_job
            if not lost:
                record = {"year": key, "job": job["job"], "map": mapping, "status": "running", "reasons": [],
                          "proposal": None, "classes": classes, "meta": meta, "combinations": setup["combinations"],
                          "placed": fresh["placed"], "unplaced": len(fresh["unplaced"]), "fingerprint": fingerprint,
                          "setup_digest": K.setup_digest(setup),
                          "time_limit": time_limit, "started": time.time(), "applied": False}
                if now.get("undo"):
                    record["undo"] = now["undo"]        # Undo of the last Apply outlives a new run
                db.set_value(RUN, record, tid)
        if lost:                                        # another run was started meanwhile: it stays, this one goes
            try:
                eng.cancel_job(job["job"])
            except EngineError:
                pass
            raise HTTPException(409, "Another blocks run was started meanwhile; read it again.")
        log.info("blocks run started: %d combinations, %d subjects, %d students", len(request["combinations"]),
                 len(request["subjects"]), len(fresh["placed"]))
        return {"year": key, "job": job["job"], "status": "running"}

    def _finish(tid: str, record: dict, job: dict) -> dict:
        """The run once the engine is done with it: the proposal, or why there is none."""
        result = job.get("result")
        record = {**record, "status": "failed", "reasons": [], "proposal": None}
        if not isinstance(result, dict):
            record["status"] = "cancelled" if job.get("status") == "cancelled" else "failed"
            record["reasons"] = [] if record["status"] == "cancelled" else [str(job.get("error") or "the run failed")]
            return record
        answer = K.named_answer(result, record["map"])
        record["reasons"] = answer["reasons"]
        if answer.get("status") not in DONE:
            record["status"] = "cancelled" if job.get("status") == "cancelled" else "infeasible"
            return record
        try:
            split = K.split(record["placed"], record["combinations"], answer)
        except ValueError as e:
            record["reasons"] = [str(e)]
            return record
        plan, settings = _plan(tid), db.get_settings(tid)
        teachers = K.suggest_teachers(answer["sets"], plan, settings, record["classes"], record["meta"],
                                      year=record["year"])
        proposal = K.proposal(answer, teachers, plan, settings, record["meta"], record["year"],
                              placed=len(split), unplaced=record["unplaced"])
        return {**record, "status": answer["status"], "answer": answer, "split": split, "proposal": proposal}

    @r.get("/api/blocks/run")
    def run_status(year: str = Query(""), sid: str = Depends(auth.require_session)):
        tid = db.working_timetable()
        key, _classes = _year(_plan(tid), year)
        record = _record(tid, key)
        if record.get("status") != "running":
            return _view(tid, record)
        try:
            job = engine().job(record["job"])
        except EngineError as e:
            if e.status != 404:
                raise _engine_error(e)
            job = {"status": "failed", "error": "the engine no longer has this job"}
        if job.get("status") not in ("done", "failed", "cancelled"):
            return {**_view(tid, record), "status": job.get("status") or "running"}
        with lock:                                      # concurrent polls finish it once
            now = db.get_value(RUN, tid)
            if not isinstance(now, dict) or now.get("job") != record["job"] or now.get("status") != "running":
                return _view(tid, _record(tid, key))
            record = _finish(tid, now, job)
            db.set_value(RUN, record, tid)
        log.info("blocks run finished: %s, %d sets", record["status"],
                 (record.get("proposal") or {}).get("summary", {}).get("sets", 0))
        return _view(tid, record)

    @r.post("/api/blocks/cancel")
    def cancel(body: dict, sid: str = Depends(auth.require_session)):
        tid = db.working_timetable()
        key, _classes = _year(_plan(tid), body.get("year"))
        record = db.get_value(RUN, tid)
        if not isinstance(record, dict) or record.get("year") != key or record.get("status") != "running":
            raise HTTPException(409, "No blocks run is running for this year group.")
        try:
            engine().cancel_job(record["job"])
        except EngineError as e:
            if e.status != 404:
                raise _engine_error(e)
        return {"ok": True}

    @r.put("/api/blocks/teachers")
    def teachers(body: dict, sid: str = Depends(auth.require_session)):
        """One set's teacher changed (or cleared with null); the warnings are worked out again."""
        tid = db.working_timetable()
        plan = _plan(tid)
        key, _classes = _year(plan, body.get("year"))
        subject, number, teacher = body.get("subject"), body.get("set"), body.get("teacher")
        with lock:
            record = _record(tid, key)
            if record.get("status") not in DONE or not record.get("proposal"):
                raise HTTPException(409, "No proposal to change: build the blocks first.")
            proposal = record["proposal"]
            entry = next((s for col in proposal["blocks"] for s in col
                          if s["subject"] == subject and s["set"] == number), None)
            if entry is None:
                raise HTTPException(404, f"no set {number} of {subject} in the proposal")
            if teacher is not None:
                person = next((p for p in plan["staff"] if p["id"] == teacher), None)
                if person is None:
                    raise HTTPException(400, f"no teacher {teacher}")
                dept = (record["meta"].get(subject) or {}).get("dept")
                if dept and person.get("dept") != dept:
                    raise HTTPException(400, f"{teacher} isn't in the {dept} department")
            entry["teacher"], entry["suggested"] = teacher, False
            record = {**record, "proposal": K.rewarn(proposal, plan, db.get_settings(tid), record["meta"], key),
                      "applied": False}
            db.set_value(RUN, record, tid)
        return _view(tid, record)

    @r.post("/api/blocks/apply")
    def apply(body: dict, sid: str = Depends(auth.require_session)):
        """The proposal written: its sets and bands into the plan (`blocks.apply_plan`) and the year's students'
        takes (R12); the plan and the changed takes kept for Undo. Refused when the students or the setup
        (combinations, limits, subjects) have changed since the run. A refusal writes nothing."""
        tid = db.working_timetable()
        with db.plan_lock(tid), lock:
            raw = db.get_value("plan", tid)
            plan = M.normalise(raw or M.empty_plan())
            key, classes = _year(plan, body.get("year"))
            record = db.get_value(RUN, tid)
            if not isinstance(record, dict) or record.get("year") != key or record.get("status") not in DONE:
                raise HTTPException(409, "Nothing to apply: build the blocks first.")
            if record.get("applied"):
                raise HTTPException(409, "Already applied.")
            if classes != record["classes"]:
                raise HTTPException(409, "The year group's classes have changed since this run; build again.")
            if _fingerprint(tid, key, classes) != record.get("fingerprint"):
                raise HTTPException(409, STUDENTS_CHANGED)
            if K.setup_digest(_setups(tid).get(key)) != record.get("setup_digest"):
                raise HTTPException(409, SETUP_CHANGED)
            # each chosen teacher checked against the plan as it is now: one gone, or moved out of the
            # subject's department, is dropped (the set gets no teacher) and named in the answer
            staff = {p["id"]: p for p in plan["staff"]}
            proposal = copy.deepcopy(record["proposal"])
            chosen, dropped = {}, []
            for col in proposal["blocks"]:
                for s in col:
                    t, dept = s["teacher"], (record["meta"].get(s["subject"]) or {}).get("dept")
                    if t and (t not in staff or (dept and staff[t].get("dept") != dept)):
                        dropped.append({"subject": s["subject"], "set": s["set"], "teacher": t})
                        s["teacher"] = t = None
                    chosen[s["subject"], s["set"]] = t
            try:
                new = K.apply_plan(plan, key, classes, record["answer"], chosen, record["meta"])
                takes = K.takes_for(new, key, record["split"])
            except ValueError as e:
                raise HTTPException(409, str(e))
            stored = list(db.get_value("students", tid) or [])
            after = K.rebuild_takes(stored, classes, K.old_groups(plan, key), takes)
            before = [[i, {k: s[k] for k in ("takes", "placed_by_app") if k in s}]
                      for i, (s, a) in enumerate(zip(stored, after)) if s != a]
            db.set_value("plan", new, tid)
            db.set_value("students", after, tid)
            written = db.get_value("plan", tid) or {}
            undo = {"year": key, "plan_before": raw, "rev_after": int(written.get("rev") or 0),
                    "content_after": applying._content(written), "students_before": before,
                    "after_hash": applying._hash(db.get_value("students", tid) or [])}
            record = {**record, "proposal": proposal, "applied": True, "undo": undo}
            db.set_value(RUN, record, tid)
        log.info("blocks applied: %d sets, %d students", record["proposal"]["summary"]["sets"], len(takes))
        return {"ok": True, **_view(tid, record), "teachers_dropped": dropped}

    @r.post("/api/blocks/undo")
    def undo(body: dict, sid: str = Depends(auth.require_session)):
        """The last Apply put back, plan and takes, when neither has changed since; else refused, nothing moved."""
        tid = db.working_timetable()
        with db.plan_lock(tid), lock:
            key, _classes = _year(_plan(tid), body.get("year"))
            record = db.get_value(RUN, tid)
            done = record.get("undo") if isinstance(record, dict) else None
            if not isinstance(done, dict) or done.get("year") != key:
                raise HTTPException(409, "Nothing to undo.")
            if not applying.plan_as_written(db.get_value("plan", tid), done):
                raise HTTPException(409, applying.PLAN_CHANGED)
            if not applying.students_as_written(db, done, tid):
                raise HTTPException(409, applying.STUDENTS_CHANGED)
            students = list(db.get_value("students", tid) or [])
            for i, saved in done["students_before"]:
                students[i] = {**{k: v for k, v in students[i].items() if k not in ("takes", "placed_by_app")}, **saved}
            db.set_value("plan", done.get("plan_before") or M.empty_plan(), tid)      # the rev keeps moving forward
            db.set_value("students", students, tid)
            record = {k: v for k, v in record.items() if k != "undo"}
            record["applied"] = False
            db.set_value(RUN, record, tid)
        log.info("blocks undone: %d students", len(done["students_before"]))
        return {"ok": True}

    return r
