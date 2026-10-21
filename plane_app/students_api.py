"""Students over HTTP (spec docs/superpowers/specs/2026-10-16-students-design.md §1, §2, §4): the
timetable's student list (`students`), added, edited and removed one at a time or all at once, and the
option groups a class can take.

Admin only: every route is `Depends(auth.require_session)`, which answers a department account with 403.
A reason for a 400 names IDs, classes and option codes, never a student's name."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from . import auth, students as S
from .plan import model as M
from .plan.reader import applying


def make_router(db) -> APIRouter:
    r = APIRouter()

    def _plan() -> dict:
        return M.normalise(db.get_value("plan") or M.empty_plan())

    def _stored() -> list[dict]:
        return list(db.get_value("students") or [])

    def _org():
        return db.get_org("live") or db.get_org("draft")

    def _checked(body, plan: dict, others: list[dict], sid: str | None = None) -> dict:
        """The record `body` describes, or a 400 whose reason is built from IDs, classes and codes."""
        if not isinstance(body, dict):
            raise HTTPException(400, "send the student as an object with id, name, class and takes")
        sid = str(sid if sid is not None else body.get("id") or "").strip()
        if not sid:
            raise HTTPException(400, "give the student an ID")
        if any(o["id"] == sid for o in others):
            raise HTTPException(400, f"ID {sid} is already used")
        takes_in = body.get("takes", [])
        if not isinstance(takes_in, list) or not all(
                isinstance(t, dict) and isinstance(t.get("division"), str) and isinstance(t.get("grouping"), str)
                for t in takes_in):
            raise HTTPException(400, f"{sid}: takes must be a list of {{division, grouping}}")
        cls = str(body.get("class") or "").strip()
        level = str(body.get("level") or "").strip() if not cls else ""     # a class decides the level
        takes: list[dict] = []
        for t in takes_in:
            t = {"division": t["division"], "grouping": t["grouping"]}
            if t not in takes:
                takes.append(t)
        if not cls and level:               # a level and no class yet: classes are formed later, so no takes
            if takes:
                raise HTTPException(400, f"{sid}: choose a class to give subjects, or leave them out until classes are formed")
            record = {"id": sid, "name": str(body.get("name") or "").strip(), "level": level, "takes": [],
                      "placed_by_app": []}
        else:
            record = {"id": sid, "name": str(body.get("name") or "").strip(), "class": cls, "takes": takes,
                      "placed_by_app": []}
        found = [p for p in S.problems([record], plan) if p["kind"] != "duplicate_id"]
        for p in found:
            if p["kind"] == "unknown_class":
                raise HTTPException(400, f"{p['text']} ({p['detail']})")
        if found:
            raise HTTPException(400, found[0]["text"])
        return record

    @r.get("/api/students")
    def listing(sid: str = Depends(auth.require_session)):
        students, plan = _stored(), _plan()
        found = S.problems(students, plan, _org())
        # `subjects` names each option group's subject by "division|grouping", for the chips; no student in it
        subjects = {f"{g['division']}|{g['grouping']}": g["subject"] for g in S.option_groups(plan).values()}
        return {"students": students,
                "summary": S.summary(students, plan, found),
                "problems": found, "subjects": subjects}

    @r.get("/api/students/clashes")
    def clashes(sid: str = Depends(auth.require_session)):
        """Where students' lessons overlap in the live timetable, with each clash's students (ID and name):
        the same list /api/solid counts for department accounts. Admin only."""
        students, live = _stored(), db.get_org("live")
        if not students or not live:
            return {"clashes": []}
        return {"clashes": S.clash_report(students, live, _plan(), names=True)["clashes"]}

    @r.get("/api/students/options")
    def options(class_: str = Query("", alias="class"), sid: str = Depends(auth.require_session)):
        plan = _plan()
        if class_ not in {c["code"] for c in plan["classes"]}:
            raise HTTPException(400, f"the class {class_ or '(none)'} isn't known")
        groups = [g for g in S.option_groups(plan).values() if class_ in g["classes"]]
        order = [b["id"] for b in plan["bands"]]
        bands: dict = {}
        for g in sorted(groups, key=lambda g: (g["grouping"], g["id"])):
            bands.setdefault(g["band"], []).append({k: g[k] for k in ("id", "division", "grouping", "subject")})
        keys = sorted(bands, key=lambda b: (b is None, order.index(b) if b in order else len(order), str(b)))
        out, n = [], 0
        for b in keys:
            label = None
            if b is not None:          # "Band 1 · Physics / History": numbered within this class's bands, never the raw id
                n += 1
                names = list(dict.fromkeys(g["subject"] for g in bands[b]))
                label = f"Band {n} \u00b7 " + " / ".join(names)
            out.append({"band": b, "label": label, "groups": bands[b]})
        return out

    @r.post("/api/students")
    def add(body: dict, sid: str = Depends(auth.require_session)):
        record = _checked(body, _plan(), _stored())
        db.set_value("students", _stored() + [record])
        return {"student": record}

    @r.put("/api/students/{student_id}")
    def edit(student_id: str, body: dict, sid: str = Depends(auth.require_session)):
        stored = _stored()
        old = next((s for s in stored if s["id"] == student_id), None)
        if old is None:
            raise HTTPException(404, f"no student {student_id}")
        if isinstance(body, dict) and str(body.get("id") or student_id).strip() != student_id:
            raise HTTPException(400, f"{student_id}: a student's ID can't be changed; remove and add instead")
        record = _checked(body, _plan(), [s for s in stored if s["id"] != student_id], student_id)
        taken = {t["grouping"] for t in record["takes"]}
        record["placed_by_app"] = [g for g in old.get("placed_by_app", []) if g in taken]
        for k in ("merit", "choices"):          # the form doesn't edit these: an edit keeps what the file gave
            if k in old and k not in body:
                record[k] = old[k]
        db.set_value("students", [record if s["id"] == student_id else s for s in stored])
        return {"student": record}

    @r.delete("/api/students/{student_id}")
    def remove(student_id: str, sid: str = Depends(auth.require_session)):
        stored = _stored()
        if not any(s["id"] == student_id for s in stored):
            raise HTTPException(404, f"no student {student_id}")
        db.set_value("students", [s for s in stored if s["id"] != student_id])
        return {"removed": student_id}

    @r.delete("/api/students")
    def remove_all(sid: str = Depends(auth.require_session)):
        """Every student gone, and with them the names the student-list readings keep (their students and
        their undo): a list read or applied before cannot bring them back."""
        tid = db.working_timetable()
        with db.plan_lock(tid):             # an Apply of a student list waits, and is then refused
            removed = len(db.get_value("students", tid) or [])
            db.set_value("students", [], tid)
            applying.forget_students(db, tid)
        return {"removed": removed}

    return r
