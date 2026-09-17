"""Import aSc Timetables PDF exports deterministically, without a model.

An aSc export prints one grid per teacher (and one per venue): rows are days, possibly across an
odd/even fortnight, and columns are numbered slots with their start times. Each lesson is a block
with a subject code (top left), a venue code (top right, small), the class group (centred) and a
lesson code (bottom). The PDF keeps every word's position, so blocks can be mapped to exact
day and slot from the geometry: no guessing, no size limit.

The public entry points work on plain word lists (text, x0, x1, top, size) so they can be tested
without a PDF; `read_pdf_words` produces those lists with pdfplumber.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field, replace

DAY_WORDS = ("Mon", "Tue", "Tues", "Wed", "Thu", "Thurs", "Fri", "Sat", "Sun")
CYCLE_WORDS = ("Odd", "Even", "A", "B", "Week")
_TIME = re.compile(r"^\d{1,2}:\d{2}$")
_ID_BAD = re.compile(r"[^a-z0-9-]+")


class AscFormatError(ValueError):
    pass


@dataclass
class Block:
    day: int                 # row index
    start: int               # first slot within the day
    dur: int                 # slots
    subject: str
    venues: list[str]        # codes, possibly several (co-located groups)
    classes: list[str]       # class codes from the centred group text
    code: str                # lesson code at the bottom, may be empty
    special: bool            # no venue and no classes: flag raising, assemblies, duty
    teacher: str = ""        # the teacher's id, set when pages are combined


@dataclass
class TeacherPage:
    name: str
    days: list[str]          # row labels in order
    slot_times: list[str]    # start time per slot
    blocks: list[Block] = field(default_factory=list)


# ---------------------------------------------------------------------------
# PDF to words
# ---------------------------------------------------------------------------

def read_pdf_words(data: bytes) -> list[list[dict]]:
    """One list of word boxes per page: text, x0, x1, top, size."""
    import pdfplumber
    pages = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            pages.append([{"text": w["text"], "x0": float(w["x0"]), "x1": float(w["x1"]), "top": float(w["top"]),
                           "size": float(w["size"])} for w in page.extract_words(extra_attrs=["size"])])
    return pages


def is_asc_page(words: list[dict]) -> bool:
    texts = [w["text"] for w in words]
    return "aSc" in texts and "Timetables" in texts and any(t.isdigit() for t in texts)


# ---------------------------------------------------------------------------
# Grid geometry
# ---------------------------------------------------------------------------

def _title(words: list[dict]) -> str:
    big = sorted((w for w in words if w["size"] >= 25), key=lambda w: w["x0"])
    return " ".join(w["text"] for w in big).strip()


def _grid(words: list[dict]):
    """Column edges from the numbered header, slot start times, and the day rows."""
    header = sorted((w for w in words if 11 <= w["size"] <= 20 and w["text"].isdigit()), key=lambda w: int(w["text"]))
    if len(header) < 2:
        raise AscFormatError("no numbered slot header found")
    centres = [(w["x0"] + w["x1"]) / 2 for w in header]
    pitch = (centres[-1] - centres[0]) / (len(centres) - 1)
    edges = [centres[0] - pitch / 2] + [(a + b) / 2 for a, b in zip(centres, centres[1:])] + [centres[-1] + pitch / 2]
    header_bottom = max(w["top"] for w in header) + 4
    # start times: the first time line under the header, one per column
    times = [w for w in words if _TIME.match(w["text"]) and w["top"] > header_bottom - 8 and w["top"] < header_bottom + 40]
    by_col: dict[int, list[dict]] = {}
    for w in times:
        c = _col_of((w["x0"] + w["x1"]) / 2, edges)
        if c is not None:
            by_col.setdefault(c, []).append(w)
    slot_times = []
    for c in range(len(centres)):
        ws = sorted(by_col.get(c, []), key=lambda w: w["top"])
        slot_times.append(ws[0]["text"] if ws else "")
    # day rows: a cycle word (Odd/Even) or a day word at the left of the grid
    left = edges[0]
    anchors = sorted((w for w in words if 11 <= w["size"] <= 20 and w["x1"] < left and w["top"] > header_bottom
                      and (w["text"] in CYCLE_WORDS or w["text"] in DAY_WORDS)), key=lambda w: w["top"])
    rows: list[tuple[float, str]] = []
    for w in anchors:
        if rows and abs(w["top"] - rows[-1][0]) < 4:
            rows[-1] = (rows[-1][0], rows[-1][1] + " " + w["text"])
        else:
            rows.append((w["top"], w["text"]))
    if not rows:
        raise AscFormatError("no day rows found")
    tops = [r[0] for r in rows]
    pitch_y = (tops[-1] - tops[0]) / (len(tops) - 1) if len(tops) > 1 else 46.8
    bands = [(t - 0.34 * pitch_y, t + 0.66 * pitch_y) for t in tops]
    return edges, slot_times, [r[1] for r in rows], bands, pitch_y


def _col_of(x: float, edges: list[float]) -> int | None:
    for i in range(len(edges) - 1):
        if edges[i] <= x < edges[i + 1]:
            return i
    return None


def _snap(x: float, edges: list[float]) -> int:
    """Index of the nearest column edge."""
    return min(range(len(edges)), key=lambda i: abs(edges[i] - x))


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------

def _merge_line(words: list[dict], gap: float = 3.5) -> list[dict]:
    """Join words that sit on one line with tiny gaps ("CP Tim" "e", "G1 Hum", "DR 1,DR 2")."""
    out: list[dict] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        joined = out and abs(out[-1]["top"] - w["top"]) < 2.5 and (
            0 <= w["x0"] - out[-1]["x1"] < gap or out[-1]["text"].endswith(",") or w["text"].startswith(","))
        if joined:
            out[-1] = {**out[-1], "text": out[-1]["text"] + " " + w["text"], "x1": w["x1"]}
        else:
            out.append(dict(w))
    return out


_CLASS = re.compile(r"^\d{1,2}[A-Z]{1,2}\d{0,2}$")


def _is_class_group(text: str) -> bool:
    toks = [t for t in re.split(r"[/\s]+", text.strip("/")) if t]
    return bool(toks) and all(_CLASS.match(t) for t in toks)


def parse_teacher_page(words: list[dict]) -> TeacherPage:
    """Read every block on one teacher page from the grid geometry.

    Font sizes are only a hint: aSc shrinks any text that would not fit, so a long class list or a
    long venue list can be printed small. The reliable signals are position: a block's subject is
    the leftmost word on its first line, its venue is the word that ends at the block's right edge,
    the class group and lesson code are centred under them, and a special block (flag raising,
    assembly, duty) is a stack of left-aligned lines with no class group."""
    title = _title(words)
    name = re.sub(r"^(Teacher|Class|Room|Venue)\s+", "", title).strip() or title
    edges, slot_times, days, bands, pitch_y = _grid(words)
    page = TeacherPage(name=name, days=days, slot_times=slot_times)
    left, col_w = edges[0], edges[1] - edges[0]
    grid_words = [w for w in words if w["x0"] >= left - 1 and w["size"] < 11]

    def centre(w: dict) -> float:
        return (w["x0"] + w["x1"]) / 2

    for d, (top, bottom) in enumerate(bands):
        band = [w for w in grid_words if top <= w["top"] < bottom]
        if not band:
            continue
        first_top = min(w["top"] for w in band)
        line1 = _merge_line([w for w in band if w["top"] < first_top + 4])
        lower = [w for w in band if w["top"] >= first_top + 4]
        line1.sort(key=lambda w: w["x0"])

        def left_pad(w):   # distance inside the column edge on the word's left
            return min((w["x0"] - e for e in edges if e <= w["x0"] + 0.01), default=99)

        def right_pad(w):  # distance to the column edge on the word's right
            return min((e - w["x1"] for e in edges if e >= w["x1"] - 0.01), default=99)

        i = 0
        while i < len(line1):
            sub = line1[i]
            if left_pad(sub) > 6 and right_pad(sub) <= 8 and page.blocks and page.blocks[-1].day == d:
                i += 1                      # a stray right-aligned word: a venue whose subject was consumed
                continue
            start_edge = _snap(sub["x0"] - left_pad(sub) + 0.01, edges) if left_pad(sub) <= 6 else _snap(sub["x0"], edges)
            if start_edge >= len(edges) - 1:
                i += 1
                continue
            # continuation lines of the subject share its left edge; a block with no class group
            # anywhere near and no venue is a special (flag raising, assembly, duty)
            stack = [w for w in lower if abs(w["x0"] - sub["x0"]) < 2.5 and not _is_class_group(w["text"])]
            near_class = any(_is_class_group(w["text"]) and sub["x0"] - 2 <= centre(w) <= sub["x0"] + 1.2 * col_w for w in lower)
            special = bool(stack) and not near_class
            venue = None
            if not special and i + 1 < len(line1):
                cand = line1[i + 1]
                r_aligned = right_pad(cand) <= 8 and cand["x0"] > sub["x1"]
                l_aligned = left_pad(cand) <= 6
                has_letter = any(ch.isalpha() for ch in cand["text"])
                if r_aligned and has_letter:
                    right_edge = _snap(cand["x1"] + right_pad(cand) - 0.01, edges)
                    # a left-aligned word of the same size is the next block's subject, not a venue
                    if right_edge > start_edge and (not l_aligned or cand["size"] < sub["size"] - 0.3):
                        venue = cand
            if venue is not None:
                end_edge = _snap(venue["x1"] + right_pad(venue) - 0.01, edges)
                i += 2
            else:
                centred = sorted((w for w in lower if centre(w) > sub["x0"] and abs(w["x0"] - sub["x0"]) >= 2.5), key=lambda w: centre(w))
                if centred and not special:
                    end_edge = _snap(edges[start_edge] + 2 * (centre(centred[0]) - edges[start_edge]), edges)
                else:
                    end_edge = start_edge + 1
                i += 1
            end_edge = max(min(end_edge, len(edges) - 1), start_edge + 1)
            x0, x1 = edges[start_edge], edges[end_edge]
            inside = [w for w in lower if x0 - 1 <= centre(w) <= x1 + 1 and not (special and abs(w["x0"] - sub["x0"]) < 2.5)]
            class_words = sorted((w for w in inside if _is_class_group(w["text"]) or w["size"] >= 8.2), key=lambda w: (w["top"], w["x0"]))
            class_codes = [c for c in re.split(r"[/\s]+", "/".join(w["text"].strip("/") for w in class_words)) if c]
            code_words = sorted((w for w in inside if w not in class_words and any(ch.isalnum() for ch in w["text"])),
                                key=lambda w: (w["top"], w["x0"]))
            venue_codes = [v.strip() for v in re.split(r"[,;]", venue["text"])] if venue else []
            venue_codes = [v for v in venue_codes if v]
            subject = sub["text"]
            if special:
                subject = " ".join([sub["text"]] + [w["text"] for w in sorted(stack, key=lambda w: w["top"]) if w["text"] != "/"])
                code_words = []
            elif stack:
                # a wrapped subject such as "CP Tim" + "e": join, and keep those words out of the lesson code
                cont = [w for w in sorted(stack, key=lambda w: w["top"]) if w["top"] < top + 0.45 * pitch_y]
                subject = "".join([sub["text"]] + [w["text"] for w in cont]) if all(len(w["text"]) <= 2 for w in cont) \
                    else " ".join([sub["text"]] + [w["text"] for w in cont])
                code_words = [w for w in code_words if w not in cont]
            page.blocks.append(Block(day=d, start=start_edge, dur=end_edge - start_edge, subject=subject,
                                     venues=venue_codes, classes=class_codes,
                                     code=" ".join(c["text"] for c in code_words), special=special))
    return page


def parse_venue_page(words: list[dict]) -> tuple[str, int | None]:
    """A venue page's title is "CODE (capacity)"."""
    m = re.match(r"^(.+?)\s*\((\d+)\)\s*$", _title(words))
    if not m:
        return _title(words), None
    return m.group(1).strip(), int(m.group(2))


# ---------------------------------------------------------------------------
# Organisation
# ---------------------------------------------------------------------------

def slug(text: str) -> str:
    s = _ID_BAD.sub("-", text.lower()).strip("-")
    s = re.sub(r"-+", "-", s)
    return (s or "x")[:31]


def _unique(base: str, taken: set[str]) -> str:
    """`base`, or the first of base-2, base-3, ... not in `taken` (kept within 31 characters); records the result."""
    out, n = base, 2
    while out in taken:
        tail = f"-{n}"
        out = base[:31 - len(tail)].rstrip("-") + tail
        n += 1
    taken.add(out)
    return out


# ---------------------------------------------------------------------------
# Groups and bands
# ---------------------------------------------------------------------------

MOTHER_TONGUE = {"CL", "HCL", "ML", "HML", "TL", "HTL", "Higher Tamil"}


def _band_name(options: list[str], n: int) -> str:
    """Mother Tongue for the language subjects; otherwise the options' common prefix; otherwise "Band n"."""
    if all(o in MOTHER_TONGUE or any(w in o for w in ("Tamil", "Malay", "Chinese")) for o in options):
        return "Mother Tongue"
    prefix = options[0]
    for o in options[1:]:
        while not o.startswith(prefix):
            prefix = prefix[:-1]
    prefix = prefix.rstrip(" (-/_:.&")
    return prefix if len(prefix) >= 2 else f"Band {n}"


def _lesson_key(b: Block) -> tuple[tuple[str, ...], str]:
    return tuple(sorted(dict.fromkeys(b.classes))), b.subject


def derive_bands(blocks) -> tuple[list[dict], list[dict]]:
    """Groups and bands from lessons: lessons that share a class and overlap in time are options of one band.

    A teaching group is keyed by (class list, subject, teachers): the same subject is often taught to one
    set of classes by several teachers at once (four EL teachers for 1A1-1A4), and those are distinct
    groups of students. Two keys conflict when they share a class and any of their sittings overlap; the
    connected components of that graph with two or more keys are bands, their subjects the options (a
    subject that appears more than once in a band is told apart by its lesson code, else by a number).
    Every class gets a whole-class group (id = the class code); every (class list, band, option) gets an
    option group. Groups are planes: a class is split between the options of a band, so only the option
    groups clash, never the class."""
    groups, bands, _ = _derive(blocks)
    return groups, bands


def _derive(blocks) -> tuple[list[dict], list[dict], dict[tuple, str]]:
    """As `derive_bands`, plus the map from (classes, subject, teachers) to the option group id."""
    from collections import Counter
    from itertools import combinations
    # co-taught blocks (one per teacher page) are one lesson
    lessons: dict[tuple, dict] = {}
    for b in blocks:
        if b.special or not b.classes:
            continue
        classes, subject = _lesson_key(b)
        L = lessons.setdefault((b.day, b.start, b.dur, b.venues[0] if b.venues else "", classes, subject),
                               {"teachers": set(), "codes": Counter()})
        L["teachers"].add(b.teacher)
        if b.code:
            L["codes"][b.code] += 1
    sittings: dict[tuple, list[tuple[int, int, int]]] = {}
    codes: dict[tuple, Counter] = {}
    for (day, start, dur, _, classes, subject), L in lessons.items():
        k = (classes, subject, tuple(sorted(L["teachers"])))
        sittings.setdefault(k, []).append((day, start, start + dur))
        codes.setdefault(k, Counter()).update(L["codes"])
    keys = list(sittings)
    adj: dict[tuple, set] = {k: set() for k in keys}
    by_class: dict[str, list[tuple]] = {}
    for k in keys:
        for c in k[0]:
            by_class.setdefault(c, []).append(k)
    for ks in by_class.values():
        for a, b in combinations(ks, 2):
            if b in adj[a]:
                continue
            if any(d1 == d2 and s1 < e2 and s2 < e1 for d1, s1, e1 in sittings[a] for d2, s2, e2 in sittings[b]):
                adj[a].add(b); adj[b].add(a)
    seen: set[tuple] = set()
    comps: list[list[tuple]] = []
    for k in keys:
        if k in seen:
            continue
        stack, comp = [k], []
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x); comp.append(x); stack.extend(sorted(adj[x] - seen))
        comps.append(sorted(comp))
    # bands are named and numbered in an order that depends on their content, not on page order,
    # so the same export with its teacher pages shuffled yields the same ids
    comps.sort(key=lambda comp: (sorted({c for k in comp for c in k[0]}), sorted({k[1] for k in comp}), sorted(k[2] for k in comp)))
    taken: set[str] = set()
    groups: list[dict] = []
    for c in sorted({c for k in keys for c in k[0]}):
        groups.append({"id": _unique(slug(c), taken), "name": c, "classes": [c], "band": None, "option": None})
    bands: list[dict] = []
    gid_of: dict[tuple, str] = {}
    for comp in comps:
        if len(comp) < 2:
            continue
        classes = sorted({c for k in comp for c in k[0]})
        subjects = sorted({k[1] for k in comp})
        name = _band_name(subjects, len(bands) + 1)
        bid = _unique(slug("-".join(classes[:2]) + "-" + name), taken)
        per_subject = Counter(k[1] for k in comp)
        options: list[str] = []
        for k in comp:
            option = k[1]
            if per_subject[k[1]] > 1:                # same subject, several teaching groups: tell them apart
                option = f"{k[1]} ({codes[k].most_common(1)[0][0] if codes[k] else len(options) + 1})"
            while option in options:
                option = f"{option} ({len(options) + 1})"
            options.append(option)
            gid = _unique(slug(option + "-" + "-".join(k[0])), taken)
            gid_of[k] = gid
            groups.append({"id": gid, "name": f"{'/'.join(k[0])} · {name} · {option}", "classes": list(k[0]), "band": bid, "option": option})
        bands.append({"id": bid, "name": name, "classes": classes, "options": options})
    return groups, bands, gid_of


def is_asc_pdf(data: bytes) -> bool:
    """Cheap check on the first page only."""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if not pdf.pages:
                return False
            words = [{"text": w["text"], "size": float(w["size"])} for w in pdf.pages[0].extract_words(extra_attrs=["size"])]
        return is_asc_page(words)
    except Exception:
        return False


def build_organisation(pages: list[list[dict]], mode: str = "keep", default_cap: int = 40,
                       classes_as_planes: bool = False) -> tuple[dict, list[dict]]:
    """Turn every aSc page into one organisation.

    mode "keep": every lesson is fixed where the export placed it, so the timetable can be viewed
    and checked as it is. mode "rebuild": lessons are unplaced (their venue becomes the eligible
    location) and only special blocks stay fixed, so the engine re-places everything.

    Students are planes too, as groups. Secondary classes are banded across subjects (part of 4P1
    is in Higher Mother Tongue while the rest is in Malay), so a class is not one body: by default
    `derive_bands` splits every class into its whole-class group plus one option group per band it
    takes part in, and a lesson's members are its teachers plus its group(s). The options of a band
    at one sitting share a sync id, so a rebuild keeps them concurrent. classes_as_planes=True
    keeps one group per class and no bands, so the banding shows up as clashes to inspect."""
    if mode not in ("keep", "rebuild"):
        raise ValueError(mode)
    teachers: list[TeacherPage] = []
    caps: dict[str, int] = {}
    notes: list[dict] = []
    for i, words in enumerate(pages):
        if not is_asc_page(words):
            continue
        title = _title(words)
        if title.startswith("Teacher "):
            teachers.append(parse_teacher_page(words))
        else:
            code, cap = parse_venue_page(words)
            if cap is not None:
                caps[code] = cap
    if not teachers:
        raise AscFormatError("No teacher pages found. Upload the teacher timetable export (pages titled 'Teacher ...').")

    ref = teachers[0]
    days, spd = ref.days, len(ref.slot_times)
    for t in teachers[1:]:
        if t.days != days or len(t.slot_times) != spd:
            notes.append({"section": "time", "source": t.name, "note": f"{t.name}'s grid ({len(t.slot_times)} slots, {len(t.days)} rows) differs from {ref.name}'s; its lessons are mapped onto the first grid."})
    n_slots = len(days) * spd
    time_labels = [f"{day} {ref.slot_times[s] or s}" for day in days for s in range(spd)]
    slot_minutes = _slot_minutes(ref.slot_times)

    venue_ids: dict[str, str] = {}
    locations: list[dict] = []
    def venue(code: str) -> str:
        if code not in venue_ids:
            vid = slug(code)
            base, n = vid, 2
            while any(l["id"] == vid for l in locations):
                vid = f"{base}-{n}"; n += 1
            venue_ids[code] = vid
            locations.append({"id": vid, "name": code, "cap": caps.get(code, default_cap), "shared": False, "rest": False})
        return venue_ids[code]
    locations.append({"id": "campus", "name": "Whole school", "cap": 9999, "shared": True, "rest": False})
    locations.append({"id": "rest", "name": "Rest", "cap": 9999, "shared": True, "rest": True})

    persons: dict[str, dict] = {}
    used: dict[str, set] = {}
    def person(pid: str, name: str, role: str) -> None:
        if pid in persons and persons[pid]["role"] != role:
            raise AscFormatError(f"id {pid!r} is both a {persons[pid]['role'].lower()} and a {role.lower()}: "
                                 f"{persons[pid]['name']!r} and {name!r} would be one plane")
        if pid not in persons:
            persons[pid] = {"id": pid, "name": name, "role": role, "avail": [0, n_slots], "eligible": None}

    lessons = [replace(b, teacher=slug(t.name)) for t in teachers for b in t.blocks if not b.special and b.classes]
    if classes_as_planes:
        classes = sorted({c for b in lessons for c in b.classes})
        groups = [{"id": slug(c), "name": c, "classes": [c], "band": None, "option": None} for c in classes]
        bands: list[dict] = []
        gid_of: dict[tuple, str] = {}
    else:
        groups, bands, gid_of = _derive(lessons)
    whole = {g["classes"][0]: g["id"] for g in groups if g["band"] is None}
    band_of = {g["id"]: g["band"] for g in groups}

    # lessons keyed so that a lesson two teachers share (co-teaching) becomes one event with both
    events: dict[tuple, dict] = {}
    for t in teachers:
        tid = slug(t.name)
        person(tid, t.name, "Teacher")
        for b in t.blocks:
            t0 = b.day * spd + b.start
            if b.special:
                key = ("special", t0, b.dur, b.subject.lower())
                ev = events.setdefault(key, {"id": f"sp-{t0}-{slug(b.subject)[:12]}", "name": b.subject, "members": [], "dur": b.dur,
                                             "loc": "campus", "t0": t0, "sync": None, "eligible_locs": ["campus"], "fixed": True})
                if tid not in ev["members"]:
                    ev["members"].append(tid)
                continue
            loc = venue(b.venues[0]) if b.venues else "campus"
            used.setdefault(tid, set()).add(loc)
            key = ("lesson", t0, b.dur, loc, *_lesson_key(b))
            ev = events.setdefault(key, {"id": "", "name": f"{b.subject} {'/'.join(b.classes)}".strip(), "members": [], "dur": b.dur,
                                         "loc": loc, "t0": t0, "sync": None, "eligible_locs": [venue(v) for v in b.venues] or ["campus"],
                                         "fixed": mode == "keep", "_code": b.code, "_block": b})
            if tid not in ev["members"]:
                ev["members"].append(tid)
    # students: once every teacher page is in, a lesson's teachers are known, so its group is too
    for ev in events.values():
        b = ev.pop("_block", None)
        if b is None:
            continue
        classes, subject = _lesson_key(b)
        gid = gid_of.get((classes, subject, tuple(sorted(ev["members"]))))
        gids = [gid] if gid else [whole[c] for c in classes]
        if gid and band_of[gid]:
            ev["sync"] = f"{band_of[gid]}-{b.day}-{b.start}"
        for g in gids:
            used.setdefault(g, set()).add(ev["loc"])
            ev["members"].append(g)
    for g in groups:                        # student groups are planes too, listed after the teachers
        person(g["id"], g["name"], "Group")
    for code in caps:                       # venues with a page of their own exist even when unused here
        venue(code)
    # a plane covers the venues its person actually uses, plus the whole-school row and rest
    for pid, p in persons.items():
        p["eligible"] = sorted(used.get(pid, set()) | {"campus", "rest"})
    out_events = []
    for n, ev in enumerate(events.values(), 1):
        code = ev.pop("_code", "")
        if not ev["id"]:
            ev["id"] = f"{slug(code or ev['name'])[:16]}-{ev['t0']}-{n}"[:31]
        if mode == "rebuild" and not ev["fixed"]:
            ev["loc"], ev["t0"] = None, None
        out_events.append(ev)
    # ids must be distinct: whole-school blocks are keyed by their full subject but named by a prefix
    # of it, so two at one slot can share an id; the repeats get a deterministic numeric suffix
    taken = {ev["id"] for ev in out_events}
    seen: set[str] = set()
    for ev in out_events:
        if ev["id"] in seen:
            ev["id"] = _unique(ev["id"], taken)
        seen.add(ev["id"])

    rules = {"max_load": spd, "max_run": spd, "mandatory_rest": [], "slots_per_day": spd}
    org = {"name": f"aSc import: {len(teachers)} teachers, {len(days)} days",
           "time_labels": time_labels, "time_unit": f"{slot_minutes}-minute slot", "rules": rules,
           "locations": locations, "persons": list(persons.values()), "events": out_events,
           "groups": groups, "bands": bands}
    notes.insert(0, {"section": "time", "source": "grid header",
                     "note": f"{len(days)} day rows x {spd} slots of {slot_minutes} minutes = {n_slots} slots. Load and rest rules apply per day; they start unlimited, set them in Settings."})
    n_lessons = sum(1 for e in out_events if e["loc"] != "campus" or e["t0"] is None)
    notes.insert(1, {"section": "events", "source": "teacher pages",
                     "note": f"{len(teachers)} teachers, {len(venue_ids)} venues, {n_lessons} lessons"
                             + (f" and {len(out_events) - n_lessons} whole-school blocks." if len(out_events) > n_lessons else ".")
                             + (" Every lesson is fixed where the export placed it; Build checks it." if mode == "keep" else " Lessons are unplaced; Build re-places them, keeping each in a venue it used.")})
    n_whole, n_opt = sum(1 for g in groups if g["band"] is None), sum(1 for g in groups if g["band"])
    notes.insert(2, {"section": "groups", "source": "teacher pages",
                     "note": f"{n_whole} whole-class groups, {n_opt} option groups in {len(bands)} band{'s' if len(bands) != 1 else ''}"
                             + (". Each class is one plane; the banding shows up as clashes to inspect." if classes_as_planes
                                else ". Lessons that share a class at the same time are options of one band; each option is its own plane, so only real clashes remain.")})
    missing = sorted({l["name"] for l in locations if l["id"] not in ("campus", "rest") and l["name"] not in caps})
    if missing:
        notes.append({"section": "locations", "source": "venue pages", "note": f"No capacity found for {len(missing)} venue(s), set to {default_cap}: {', '.join(missing[:12])}{'...' if len(missing) > 12 else ''}"})
    return org, notes


def _slot_minutes(times: list[str]) -> int:
    mins = []
    for a, b in zip(times, times[1:]):
        try:
            ha, ma = map(int, a.split(":")); hb, mb = map(int, b.split(":"))
            mins.append((hb * 60 + mb) - (ha * 60 + ma))
        except ValueError:
            continue
    if not mins:
        return 20
    return max(set(mins), key=mins.count)
