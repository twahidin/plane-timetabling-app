"""Organisation data: locations, person planes, events, rules. Ids are strings."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from importlib import resources
from pathlib import Path


def normalise_avail(value, n_slots: int) -> tuple[tuple[int, int], ...]:
    """Accept [a, b] or [[a, b], ...]; return sorted, merged, validated windows."""
    msg = f"avail must be windows [start, end) within 0..{n_slots}, sorted and non-overlapping"
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(isinstance(x, int) and not isinstance(x, bool) for x in value):
        value = [value]
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(msg)
    wins = []
    for w in value:
        if not isinstance(w, (list, tuple)) or len(w) != 2 or not all(isinstance(x, int) and not isinstance(x, bool) for x in w):
            raise ValueError(msg)
        a, b = w
        if not (0 <= a < b <= n_slots):
            raise ValueError(msg)
        wins.append((a, b))
    wins.sort()
    out: list[tuple[int, int]] = []
    for a, b in wins:
        if out and a < out[-1][1]:
            raise ValueError(msg)
        if out and a == out[-1][1]:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return tuple(out)


@dataclass(frozen=True)
class Location:
    id: str
    name: str
    cap: int
    shared: bool = False   # several events may share it at once
    rest: bool = False     # tiles here are rest, not load
    kind: str = ""         # "classroom" | "lab" | "studio" | "hall" | … ; "" when unset


@dataclass(frozen=True)
class Person:
    id: str
    name: str
    role: str
    avail: tuple[tuple[int, int], ...]   # sorted, non-overlapping [start, end) windows in slots
    eligible: frozenset[str]        # location ids: the plane's extent along L

    def avail_mask(self, n_slots: int) -> int:
        # Inlined span(a, b) arithmetic: model.py ships alone to the app image
        # and the public template (see the guard test in test_export_script.py),
        # so it must not import sibling modules like .masks.
        m = 0
        for a, b in self.avail:
            b = min(b, n_slots)
            if b > a:
                m |= ((1 << (b - a)) - 1) << a
        return m


@dataclass
class Event:
    id: str
    name: str
    members: list[str]
    dur: int
    loc: str | None = None
    t0: int | None = None
    sync: str | None = None
    eligible_locs: list[str] | None = None
    fixed: bool = False
    double: bool = False

    @property
    def placed(self) -> bool:
        return self.loc is not None and self.t0 is not None

    def slots(self) -> range:
        assert self.t0 is not None
        return range(self.t0, self.t0 + self.dur)


@dataclass(frozen=True)
class Group:
    id: str
    name: str
    classes: tuple[str, ...]
    band: str | None = None
    option: str | None = None
    size: int | None = None


@dataclass(frozen=True)
class Band:
    id: str
    name: str
    classes: tuple[str, ...]
    options: tuple[str, ...]


SHARE_SEP = "#"       # part group id + SHARE_SEP + class code: the class's share of a part group
SHARE_ROLE = "Group share"    # reserved for the synthesised share planes: internal, never in to_dict


def share_id(group_id: str, class_code: str) -> str:
    return f"{group_id}{SHARE_SEP}{class_code.casefold()}"


def is_whole_class(g: Group) -> bool:
    """A group that is a whole class: no band, no option, exactly one class. Every other group takes
    only part of its classes (a band option, a set, a half-class), so it needs share planes."""
    return g.band is None and g.option is None and len(g.classes) == 1


def _imply_option_members(groups: list[Group], events: list[Event]) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """A whole-class lesson occupies the class's option groups: those students are in class, so no option
    (or set, or any other part group) of theirs may sit at the same time.

    The plane an overlap lands on is the class's *share* of a part group — one plane per (part group,
    class) — not the part group itself: a part group spans several classes, and EL 1A and EL 1B may of
    course run together even though both classes take the same mother-tongue option. So a whole-class
    lesson of class c takes the share plane of c in every part group holding c, and a part-group lesson
    takes the share plane of each of its classes. Two options of one band, and two sets of one class,
    keep their own share planes and still run together.

    Members are appended in place (originals first); returns (share id -> its display name, event id ->
    every share the event holds) so `from_dict` can synthesise the planes and `to_dict` strip them again.
    The shares are derived from the event's group members alone, so one that the input already lists is
    still recorded as implied — and so still stripped by `to_dict` and left out of `bodies`."""
    whole: dict[str, str] = {}          # whole-class group id -> its class code
    class_names: dict[str, str] = {}    # class code -> the whole-class group's name
    parts: dict[str, Group] = {}        # part group id -> the group
    by_class: dict[str, list[Group]] = {}    # class code -> the part groups holding it
    for g in groups:
        if is_whole_class(g):
            whole[g.id] = g.classes[0].casefold()
            class_names.setdefault(whole[g.id], g.name)
        else:
            parts[g.id] = g
            for c in g.classes:
                by_class.setdefault(c.casefold(), []).append(g)
    if not by_class:
        return {}, {}
    names: dict[str, str] = {}
    implied: dict[str, tuple[str, ...]] = {}
    for e in events:
        held: list[str] = []
        seen: set[str] = set()
        for m in e.members:
            g = parts.get(m)
            if g is not None:
                pairs = [(g, c.casefold()) for c in g.classes]
            elif m in whole:
                pairs = [(pg, whole[m]) for pg in by_class.get(whole[m], ())]
            else:
                continue
            for pg, c in pairs:
                sid = share_id(pg.id, c)
                if sid not in seen:
                    seen.add(sid)
                    held.append(sid)
                    names[sid] = f"{pg.name} ({class_names.get(c, c.upper())})"
        if held:
            e.members.extend(sid for sid in held if sid not in e.members)
            implied[e.id] = tuple(held)
    return names, implied


def _fresh(items: list, cache: dict, marker):
    """An id -> item index for a list that is rebound rather than edited: it is rebuilt when the list is
    a different object, a different length, or ends in a different item."""
    if marker is not items or len(cache) != len(items) or (items and cache.get(items[-1].id) is not items[-1]):
        return {x.id: x for x in items}, items
    return cache, marker


DEFAULT_SOFT = {"spread": 10, "stability": 8, "compact": 6, "even_days": 3, "edge": 2, "venue": 1}


@dataclass(frozen=True)
class Rules:
    max_load: int
    max_run: int
    mandatory_rest: tuple[int, ...] = ()
    slots_per_day: int | None = None    # when set, load, run and mandatory rest are judged within each day
    soft: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_SOFT))
    soft_edge_subjects: tuple[str, ...] = ()   # subjects penalised for a day's first/last slot


@dataclass
class Organisation:
    name: str
    time_labels: list[str]
    time_unit: str
    rules: Rules
    locations: list[Location]
    persons: list[Person]
    events: list[Event] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    bands: list[Band] = field(default_factory=list)
    _synth_persons: set[str] = field(default_factory=set, repr=False, compare=False)
    _implied_members: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False, compare=False)
    _share_ids: set[str] = field(default_factory=set, repr=False, compare=False)
    _by_id: dict[str, Person] = field(default_factory=dict, repr=False, compare=False)
    _indexed: list[Person] | None = field(default=None, repr=False, compare=False)
    _locs_by_id: dict[str, Location] = field(default_factory=dict, repr=False, compare=False)
    _locs_indexed: list[Location] | None = field(default=None, repr=False, compare=False)
    _groups_by_id: dict[str, Group] = field(default_factory=dict, repr=False, compare=False)
    _groups_indexed: list[Group] | None = field(default=None, repr=False, compare=False)

    @property
    def n_slots(self) -> int:
        return len(self.time_labels)

    def location(self, lid: str) -> Location:
        self._locs_by_id, self._locs_indexed = _fresh(self.locations, self._locs_by_id, self._locs_indexed)
        l = self._locs_by_id.get(lid)
        if l is None:
            raise KeyError(lid)
        return l

    def person(self, pid: str) -> Person:
        """O(1), like `location` and `group`: the share planes make all three lists long and they are
        asked once per member, per tile, per review."""
        self._by_id, self._indexed = _fresh(self.persons, self._by_id, self._indexed)
        p = self._by_id.get(pid)
        if p is None:
            raise KeyError(pid)
        return p

    def is_share(self, pid: str) -> bool:
        """A share plane this organisation synthesised: internal to the engine, free every slot and
        eligible everywhere. A person the input supplied is an ordinary plane whatever its role says."""
        return pid in self._share_ids

    def event(self, eid: str) -> Event:
        e = next((e for e in self.events if e.id == eid), None)
        if e is None:
            raise KeyError(eid)
        return e

    def group(self, gid: str) -> Group:
        self._groups_by_id, self._groups_indexed = _fresh(self.groups, self._groups_by_id, self._groups_indexed)
        g = self._groups_by_id.get(gid)
        if g is None:
            raise KeyError(gid)
        return g

    def implied_members(self, e: Event) -> tuple[str, ...]:
        """The share planes `_imply_option_members` derived for this event (see that function). An id
        here that the input supplied as a person of its own is an ordinary plane, not a share."""
        return self._implied_members.get(e.id, ())

    def bodies(self, e: Event) -> list[str]:
        """The members that put people in a room: a synthesised share plane is the same students as the
        class group already counted, so it must not inflate occupancy against a location's capacity."""
        return [m for m in e.members if m not in self._share_ids] if self._share_ids else list(e.members)

    def band_of(self, e: Event) -> str | None:
        """The band an event is an option of: the band of its first member group that has one."""
        self._groups_by_id, self._groups_indexed = _fresh(self.groups, self._groups_by_id, self._groups_indexed)
        for m in e.members:
            g = self._groups_by_id.get(m)
            if g is not None and g.band:
                return g.band
        return None

    def band(self, bid: str) -> Band:
        b = next((b for b in self.bands if b.id == bid), None)
        if b is None:
            raise KeyError(bid)
        return b

    def rest_location(self) -> Location | None:
        return next((l for l in self.locations if l.rest), None)

    def label(self, t: int) -> str:
        return self.time_labels[t]

    @classmethod
    def from_dict(cls, d: dict) -> "Organisation":
        n_slots = len(d["time_labels"])
        locations = [Location(**l) for l in d["locations"]]
        persons = [
            Person(id=p["id"], name=p["name"], role=p["role"],
                   avail=normalise_avail(p["avail"], n_slots), eligible=frozenset(p["eligible"]))
            for p in d["persons"]
        ]
        groups = [
            Group(id=g["id"], name=g["name"], classes=tuple(g["classes"]),
                  band=g.get("band"), option=g.get("option"), size=g.get("size"))
            for g in d.get("groups", [])
        ]
        bands = [
            Band(id=b["id"], name=b["name"], classes=tuple(b["classes"]),
                 options=tuple(b["options"]))
            for b in d.get("bands", [])
        ]

        person_ids = {p.id for p in persons}
        all_loc_ids = frozenset(l.id for l in locations)
        synth_persons: set[str] = set()
        for g in groups:
            if g.id not in person_ids:
                persons.append(Person(id=g.id, name=g.name, role="Group",
                                       avail=((0, n_slots),), eligible=all_loc_ids))
                person_ids.add(g.id)
                synth_persons.add(g.id)

        events = [
            Event(id=e["id"], name=e["name"], members=list(e["members"]), dur=e["dur"],
                  loc=e.get("loc"), t0=e.get("t0"), sync=e.get("sync"),
                  eligible_locs=e.get("eligible_locs"), fixed=e.get("fixed", False),
                  double=e.get("double", False))
            for e in d["events"]
        ]
        share_names, implied = _imply_option_members(groups, events)
        share_ids: set[str] = set()
        for sid, name in share_names.items():
            if sid not in person_ids:        # an id the input already owns is that person's plane, not a share
                persons.append(Person(id=sid, name=name, role=SHARE_ROLE,
                                      avail=((0, n_slots),), eligible=all_loc_ids))
                person_ids.add(sid)
                synth_persons.add(sid)
                share_ids.add(sid)

        return cls(
            name=d["name"],
            time_labels=list(d["time_labels"]),
            time_unit=d["time_unit"],
            rules=Rules(
                max_load=d["rules"]["max_load"],
                max_run=d["rules"]["max_run"],
                mandatory_rest=tuple(d["rules"].get("mandatory_rest", [])),
                slots_per_day=d["rules"].get("slots_per_day"),
                soft={**DEFAULT_SOFT, **d["rules"].get("soft", {})},
                soft_edge_subjects=tuple(d["rules"].get("soft_edge_subjects", [])),
            ),
            locations=locations,
            persons=persons,
            events=events,
            groups=groups,
            bands=bands,
            _synth_persons=synth_persons,
            _implied_members=implied,
            _share_ids=share_ids,
            _by_id={p.id: p for p in persons},
            _indexed=persons,
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "time_labels": list(self.time_labels),
            "time_unit": self.time_unit,
            "rules": {"max_load": self.rules.max_load, "max_run": self.rules.max_run,
                      "mandatory_rest": list(self.rules.mandatory_rest),
                      **({"slots_per_day": self.rules.slots_per_day} if self.rules.slots_per_day else {}),
                      "soft": dict(self.rules.soft),
                      **({"soft_edge_subjects": list(self.rules.soft_edge_subjects)} if self.rules.soft_edge_subjects else {})},
            "locations": [asdict(l) for l in self.locations],
            "persons": [
                {"id": p.id, "name": p.name, "role": p.role, "avail": [list(w) for w in p.avail],
                 "eligible": sorted(p.eligible)}
                for p in self.persons
                if p.id not in self._synth_persons
            ],
            "events": [
                {"id": e.id, "name": e.name,
                 "members": [m for m in e.members if m not in self._share_ids],
                 "dur": e.dur,
                 "loc": e.loc, "t0": e.t0, "sync": e.sync, "eligible_locs": e.eligible_locs,
                 "fixed": e.fixed, "double": e.double}
                for e in self.events
            ],
            "groups": [
                {"id": g.id, "name": g.name, "classes": list(g.classes),
                 "band": g.band, "option": g.option, "size": g.size}
                for g in self.groups
            ],
            "bands": [
                {"id": b.id, "name": b.name, "classes": list(b.classes), "options": list(b.options)}
                for b in self.bands
            ],
        }

    @classmethod
    def from_json(cls, path: str | Path) -> "Organisation":
        return cls.from_dict(json.loads(Path(path).read_text()))

    @classmethod
    def load_builtin(cls, name: str) -> "Organisation":
        text = resources.files("plane_timetabling").joinpath(f"datasets/{name}.json").read_text()
        return cls.from_dict(json.loads(text))
