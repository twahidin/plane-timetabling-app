"""A reading's records as a plan patch (spec docs/superpowers/specs/2026-10-08-smart-workbook-reader-
design.md §2, §4): the plan's own patch operations (model.apply_patch), so a reading is validated like
a hand edit and only ever adds or updates.

Only the fields a recipe read are sent: a venue whose capacity the workbook does not give keeps the
one stored. A venue is matched to the plan's by name (case and spaces aside) before its slug id; a
person by name or slug id; a requirement by its cell (department, level, subject, class), comparing
only the parts the recipe read. A requirement row naming several classes is one requirement a class
(the "Entire Class" of the deployment workbook), and one with no lesson pattern of its own is all
single lessons. Doubts name what was left out or added unasked."""
from __future__ import annotations

from .. import model as M
from ..importer import _resolve_staff


def _key(text) -> str:
    return " ".join(str(text).split()).lower()


class _Patch:
    def __init__(self, plan: dict):
        self.plan = plan
        self.doubts: list[str] = []
        self.venues: dict[str, dict] = {}                 # id -> upsert item, in reading order
        self.venue_names = {_key(v["name"]): v["id"] for v in plan["venues"]}
        self.venue_by_id = {v["id"]: v["name"] for v in plan["venues"]}     # the name holding each id
        self.collided: set[str] = set()
        self.classes: dict[str, dict] = {}                # code -> upsert item
        self.class_codes = {c["code"]: c["code"] for c in plan["classes"]}
        self.class_codes.update({c["code"].lower(): c["code"] for c in plan["classes"]})
        self.plan_codes = {c["code"] for c in plan["classes"]}
        self.doubted: set[str] = set()
        self.homes: dict[str, tuple[str, str]] = {}       # code -> (venue id, room as written)
        self.staff_patch: dict[str, dict] = {}
        self.staff = [{"id": s["id"], "name": s["name"]} for s in plan["staff"]]
        self.staff_ids = {s["id"] for s in plan["staff"]}
        self.requirements: dict[str, dict] = {}
        self.new_cells: dict[tuple, str] = {}             # cell -> id of a requirement this reading adds

    # -- venues --------------------------------------------------------------------------------

    def venue(self, name, at: str, **fields) -> str | None:
        """Upsert a venue named `name` with `fields` (only those read); its id, None when it does not read."""
        name = " ".join(str(name).split())
        try:
            M.normalise_venue({"name": name})
        except M.PlanError as e:
            self.doubts.append(f"{at}: {e}; the room was left out")
            return None
        vid = self.venue_names.get(_key(name))
        renames = True
        if vid is None:
            vid = M.venue_id(name)
            holder = self.venue_by_id.get(vid)
            if holder is not None and _key(holder) != _key(name):
                # "B4/1" where the plan has "B4-1": one id, two names. Never a silent rename.
                renames = False
                if name not in self.collided:
                    self.collided.add(name)
                    self.doubts.append(f"{at}: {name} has the same id as {holder}; {holder} is updated "
                                       f"and keeps its name")
            else:
                self.venue_names[_key(name)] = vid
                self.venue_by_id.setdefault(vid, name)
        item = self.venues.get(vid)
        if item is None:
            item = self.venues[vid] = {"id": vid, "name": name} if renames else {"id": vid}
        for field, value in fields.items():
            try:
                M.normalise_venue({"name": name, field: value})
            except M.PlanError as e:
                self.doubts.append(f"{at}: {e}; left as it was")
                continue
            item[field] = value
        return vid

    # -- classes -------------------------------------------------------------------------------

    def klass(self, code, at: str, doubt_new: bool, **fields) -> str:
        """Upsert a class (code as the plan writes it when the plan has it); its code."""
        code = str(code).strip()
        known = self.class_codes.get(code) or self.class_codes.get(code.lower())
        if known is None:
            self.class_codes[code] = self.class_codes[code.lower()] = code
            known = code
        # a plan that already lists its classes: one the reading names but the plan lacks may be a typo
        if doubt_new and self.plan_codes and known not in self.plan_codes and known not in self.doubted:
            self.doubted.add(known)
            self.doubts.append(f"{at}: class {known} is not in the plan; it was added")
        item = self.classes.setdefault(known, {"code": known})
        item.update(fields)
        return known

    def home(self, record: dict) -> None:
        at = record.get("at", "")
        room = record["room"]
        vid = self.venue(room, at)
        if vid is None or not record.get("class"):
            return
        fields = {"size": record["size"]} if "size" in record else {}
        given = str(record["class"]).strip()
        known = self.class_codes.get(given) or self.class_codes.get(given.lower())
        # a plan that lists its classes: a home room for a class it lacks is skipped, never a new class
        if self.plan_codes and known not in self.plan_codes and known not in self.classes:
            if given not in self.doubted:
                self.doubted.add(given)
                self.doubts.append(f"{at}: class {given} is not in the plan; its home room was not set")
            return
        code = self.klass(record["class"], at, doubt_new=True, **fields)
        sharing = [c for c, (v, _) in self.homes.items() if v == vid and c != code]
        if sharing:
            self.doubts.append(f"{at}: {room} is home to {', '.join(sharing)} and {code}")
        if code in self.homes and self.homes[code][0] != vid:
            self.doubts.append(f"{at}: {code} is in {self.homes[code][1]} and {room}; {room} is kept")
        self.homes[code] = (vid, room)

    # -- staff ---------------------------------------------------------------------------------

    def person(self, name, at: str, dept: str | None = None, doubt_new: bool = False) -> str | None:
        """The id of the person named `name`, added as new staff when nobody has the name."""
        known = len(self.staff)
        pid = _resolve_staff(str(name), None, self.staff)
        if pid is None:
            return None
        if len(self.staff) > known:                     # _resolve_staff added them
            if doubt_new:
                self.doubts.append(f"{at}: {name} is not on the staff list; added as new staff")
            entry = self.staff_patch.setdefault(pid, {"name": self.staff[-1]["name"]})
            if dept:
                entry["dept"] = dept
        return pid

    def staff_record(self, record: dict) -> None:
        at = record.get("at", "")
        dept = record.get("department")
        pid = self.person(record["name"], at, dept)
        if pid is None or not dept:
            return
        current = next((s.get("dept") for s in self.plan["staff"] if s["id"] == pid), None)
        if current != dept:             # a new person, or one whose department the workbook changes
            self.staff_patch.setdefault(pid, {})["dept"] = dept

    # -- requirements --------------------------------------------------------------------------

    def _existing(self, record: dict, code: str) -> list[dict]:
        out = []
        for r in self.plan["requirements"]:
            if (r["grouping"] or "class") != "class" or r["classes"] != [code] or r["subject"] != record["subject"]:
                continue
            if "department" in record and r["dept"] != record["department"]:
                continue
            if "level" in record and r["level"] != record["level"]:
                continue
            out.append(r)
        return out

    def requirement(self, record: dict) -> None:
        at = record.get("at", "")
        subject = record["subject"]
        dept = record.get("department", "")
        periods = record.get("periods")
        if periods is not None and periods > M.SPLIT_LESSONS_MAX:
            self.doubts.append(f"{at}: {subject}: {periods} periods is more than a requirement holds; left out")
            return
        teachers = [t for t in (self.person(n, at, dept or None, doubt_new=True) for n in record.get("teachers") or [])
                    if t]
        teachers = list(dict.fromkeys(teachers))
        for cls in record["classes"]:
            code = self.klass(cls, at, doubt_new=True)
            existing = self._existing(record, code)
            if existing:
                self._update(existing, record, code, teachers, at)
            else:
                self._add(record, code, teachers, at)

    def _update(self, existing: list[dict], record: dict, code: str, teachers: list[str], at: str) -> None:
        name = f"{record['subject']} {code}"
        if len({M.cell_key(r) for r in existing}) > 1:
            self.doubts.append(f"{at}: {name} matches more than one requirement; left as it is")
            return
        if any(r["locked"] for r in existing):
            self.doubts.append(f"{at}: {name} is locked; left as it is")
            return
        if len(existing) > 1:
            self.doubts.append(f"{at}: {name} is split between teachers; left as it is")
            return
        old = existing[0]
        fields: dict = {}
        if teachers:
            fields["teachers"] = teachers
        periods = record.get("periods")
        if periods is not None:
            fields["periods"] = periods
            # a lesson pattern that already makes up the periods is the school's own: kept
            if sum(int(k) * n for k, n in old["lessons"].items()) != periods:
                fields["lessons"] = {**{k: 0 for k in old["lessons"]}, "1": periods}
        if fields:
            self.requirements.setdefault(old["id"], {}).update(fields)

    def _add(self, record: dict, code: str, teachers: list[str], at: str) -> None:
        dept, level, subject = record.get("department", ""), record.get("level", ""), record["subject"]
        cell = (dept, level, subject, "class", (code,))
        periods = record.get("periods")
        if cell in self.new_cells:
            self.doubts.append(f"{at}: {subject} {code} is listed twice; the later row is used")
            rid = self.new_cells[cell]
        else:
            taken = {r["id"] for r in self.plan["requirements"]} | set(self.requirements)
            rid = M.requirement_id_for(dept, level, "class", [code], subject, taken)
            self.new_cells[cell] = rid
        if periods is None:
            self.doubts.append(f"{at}: {subject} {code} has no periods; it was added without lessons")
        self.requirements[rid] = {
            "dept": dept, "level": level, "subject": subject, "grouping": "class", "classes": [code],
            "teachers": teachers, "periods": periods or 0, "lessons": {"1": periods} if periods else {},
        }

    # -- the patch -----------------------------------------------------------------------------

    def patch(self) -> dict:
        out: dict = {}
        if self.venues:
            out["venues"] = {"upsert": list(self.venues.values())}
        if self.staff_patch:
            out["staff"] = self.staff_patch
        classes: dict = {}
        if self.classes:
            classes["upsert"] = list(self.classes.values())
        if self.homes:
            classes["homes"] = {code: vid for code, (vid, _) in self.homes.items()}
        if classes:
            out["classes"] = classes
        if self.requirements:
            out["requirements"] = self.requirements
        return out


def to_patch(records: dict, plan: dict) -> tuple[dict, list[str]]:
    """(patch, doubts): `records` ({target: [record]}, as recipes.run gives them) as one patch for
    model.apply_patch on `plan`. Venues first, then classes, homes, staff and requirements, so a
    requirement's teachers are found among the people the same reading lists."""
    b = _Patch(M.normalise(plan or M.empty_plan()))
    for r in records.get("venues") or []:
        b.venue(r["name"], r.get("at", ""), **{f: r[f] for f in ("block", "level", "capacity", "kind") if f in r})
    for r in records.get("classes") or []:
        b.klass(r["code"], r.get("at", ""), doubt_new=False, **{f: r[f] for f in ("level", "size") if f in r})
    for r in records.get("homes") or []:
        b.home(r)
    for r in records.get("staff") or []:
        b.staff_record(r)
    for r in records.get("requirements") or []:
        b.requirement(r)
    return b.patch(), b.doubts
