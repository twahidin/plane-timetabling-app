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

    @property
    def placed(self) -> bool:
        return self.loc is not None and self.t0 is not None

    def slots(self) -> range:
        assert self.t0 is not None
        return range(self.t0, self.t0 + self.dur)


@dataclass(frozen=True)
class Rules:
    max_load: int
    max_run: int
    mandatory_rest: tuple[int, ...] = ()
    slots_per_day: int | None = None    # when set, load, run and mandatory rest are judged within each day


@dataclass
class Organisation:
    name: str
    time_labels: list[str]
    time_unit: str
    rules: Rules
    locations: list[Location]
    persons: list[Person]
    events: list[Event] = field(default_factory=list)

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

    def rest_location(self) -> Location | None:
        return next((l for l in self.locations if l.rest), None)

    def label(self, t: int) -> str:
        return self.time_labels[t]

    @classmethod
    def from_dict(cls, d: dict) -> "Organisation":
        return cls(
            name=d["name"],
            time_labels=list(d["time_labels"]),
            time_unit=d["time_unit"],
            rules=Rules(
                max_load=d["rules"]["max_load"],
                max_run=d["rules"]["max_run"],
                mandatory_rest=tuple(d["rules"].get("mandatory_rest", [])),
                slots_per_day=d["rules"].get("slots_per_day"),
            ),
            locations=[Location(**l) for l in d["locations"]],
            persons=[
                Person(id=p["id"], name=p["name"], role=p["role"],
                       avail=tuple(p["avail"]), eligible=frozenset(p["eligible"]))
                for p in d["persons"]
            ],
            events=[
                Event(id=e["id"], name=e["name"], members=list(e["members"]), dur=e["dur"],
                      loc=e.get("loc"), t0=e.get("t0"), sync=e.get("sync"),
                      eligible_locs=e.get("eligible_locs"), fixed=e.get("fixed", False))
                for e in d["events"]
            ],
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "time_labels": list(self.time_labels),
            "time_unit": self.time_unit,
            "rules": {"max_load": self.rules.max_load, "max_run": self.rules.max_run,
                      "mandatory_rest": list(self.rules.mandatory_rest),
                      **({"slots_per_day": self.rules.slots_per_day} if self.rules.slots_per_day else {})},
            "locations": [asdict(l) for l in self.locations],
            "persons": [
                {"id": p.id, "name": p.name, "role": p.role, "avail": list(p.avail),
                 "eligible": sorted(p.eligible)}
                for p in self.persons
            ],
            "events": [
                {"id": e.id, "name": e.name, "members": list(e.members), "dur": e.dur,
                 "loc": e.loc, "t0": e.t0, "sync": e.sync, "eligible_locs": e.eligible_locs,
                 "fixed": e.fixed}
                for e in self.events
            ],
        }

    @classmethod
    def from_json(cls, path: str | Path) -> "Organisation":
        return cls.from_dict(json.loads(Path(path).read_text()))

    @classmethod
    def load_builtin(cls, name: str) -> "Organisation":
        text = resources.files("plane_timetabling").joinpath(f"datasets/{name}.json").read_text()
        return cls.from_dict(json.loads(text))
