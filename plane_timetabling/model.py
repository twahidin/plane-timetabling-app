"""Organisation data: locations, person planes, events, rules. Ids are strings."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from importlib import resources
from pathlib import Path


@dataclass(frozen=True)
class Location:
    id: str
    name: str
    cap: int
    shared: bool = False   # several events may share it at once
    rest: bool = False     # tiles here are rest, not load


@dataclass(frozen=True)
class Person:
    id: str
    name: str
    role: str
    avail: tuple[int, int]          # [start, end) in slots: the plane's extent along T
    eligible: frozenset[str]        # location ids: the plane's extent along L


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

    @property
    def n_slots(self) -> int:
        return len(self.time_labels)

    def location(self, lid: str) -> Location:
        l = next((l for l in self.locations if l.id == lid), None)
        if l is None:
            raise KeyError(lid)
        return l

    def person(self, pid: str) -> Person:
        p = next((p for p in self.persons if p.id == pid), None)
        if p is None:
            raise KeyError(pid)
        return p

    def event(self, eid: str) -> Event:
        e = next((e for e in self.events if e.id == eid), None)
        if e is None:
            raise KeyError(eid)
        return e

    def group(self, gid: str) -> Group:
        g = next((g for g in self.groups if g.id == gid), None)
        if g is None:
            raise KeyError(gid)
        return g

    def band_of(self, e: Event) -> str | None:
        """The band an event is an option of: the band of its first member group that has one."""
        for m in e.members:
            g = next((g for g in self.groups if g.id == m), None)
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
                   avail=tuple(p["avail"]), eligible=frozenset(p["eligible"]))
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
                                       avail=(0, n_slots), eligible=all_loc_ids))
                person_ids.add(g.id)
                synth_persons.add(g.id)

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
            events=[
                Event(id=e["id"], name=e["name"], members=list(e["members"]), dur=e["dur"],
                      loc=e.get("loc"), t0=e.get("t0"), sync=e.get("sync"),
                      eligible_locs=e.get("eligible_locs"), fixed=e.get("fixed", False),
                      double=e.get("double", False))
                for e in d["events"]
            ],
            groups=groups,
            bands=bands,
            _synth_persons=synth_persons,
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
                {"id": p.id, "name": p.name, "role": p.role, "avail": list(p.avail),
                 "eligible": sorted(p.eligible)}
                for p in self.persons
                if p.id not in self._synth_persons
            ],
            "events": [
                {"id": e.id, "name": e.name, "members": list(e.members), "dur": e.dur,
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
