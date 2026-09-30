"""Share the school's saved templates with other schools through the engine library (spec
docs/superpowers/specs/2026-09-30-learning-design.md §2.2).

What is sent is exactly the stored template (`templates.py` already keeps names, class codes, requirement ids
and the timetable's name out of it); the engine checks it against its own whitelist and holds it for the
operator's review. The local template remembers `shared = {id, status, note}`; `refresh` follows the review.
Other schools' approved templates reach the wizard as `shared-<item id>`, each checked by
`library.validate_template` first, with how many schools used it and how many kept it. Choosing one reports
`use`; the first successful Generate draft on a timetable made with it reports `kept`."""
from __future__ import annotations

import copy
import re
import threading
import time as _time

from ..engine_client import EngineError
from ..wizard import library as L
from . import templates as T

CACHE_SECONDS = 300
FAILED_SECONDS = 45            # after the engine could not be reached, the wizard does not ask again for this long
KEPT_RETRY_SECONDS = 600       # a failed `kept` report is tried again after a Generate draft this long later
_ITEM_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
# an old export named the timetable in its trade-off line ("Made from <name> on 3 Oct 2026."); the current one says
# "Made from a finished timetable on <date>."
_MADE_FROM = re.compile(r"Made from .+ on (\d{1,2} \w{3} \d{4})\.")
_MADE_FROM_NOW = "Made from a finished timetable on "
_UNEXPECTED = "the shared library answered in an unexpected shape"

# engine identity -> (when fetched, the engine's `GET /v1/library?kind=template` answer, or the EngineError it
# failed with); per process. A failure is remembered for FAILED_SECONDS, an answer for CACHE_SECONDS.
_cache: dict[str, tuple[float, dict | EngineError]] = {}
_cache_lock = threading.Lock()
# publish, refresh and withdraw each read the engine and then write the local record: one at a time, so a refresh
# that listed before a publish finished never drops the record that publish is about to write
_lock = threading.Lock()


class SharingError(ValueError):
    """A request that does not fit the template's sharing state (sharing it twice, withdrawing one not shared)."""


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _shared_record(entry: dict) -> dict | None:
    record = entry.get("shared")
    return record if isinstance(record, dict) and isinstance(record.get("id"), str) else None


def _anonymous_tradeoffs(tradeoffs):
    """The trade-off lines with any "Made from <something> on <date>." reduced to the timetable-free wording."""
    if not isinstance(tradeoffs, list):
        return tradeoffs
    out = []
    for line in tradeoffs:
        m = _MADE_FROM.fullmatch(line) if isinstance(line, str) and not line.startswith(_MADE_FROM_NOW) else None
        out.append(f"{_MADE_FROM_NOW}{m.group(1)}." if m else line)
    return out


def publish(db, engine, template_id: str) -> dict:
    """Send the saved template to the engine library as `kind="template"`; `{id, status}`. `KeyError` when there
    is no such saved template, `SharingError` when it is already shared, `library.WizardError` when it no longer
    passes validation, `EngineError` when the engine refuses or cannot be reached (nothing is recorded then)."""
    with _lock:
        entry = T.local_entry(db, template_id)
        if _shared_record(entry):
            raise SharingError("this template is already shared; stop sharing it first")
        body = copy.deepcopy(entry)
        if "tradeoffs" in body:
            body["tradeoffs"] = _anonymous_tradeoffs(body["tradeoffs"])
        L.validate_template(body)
        got = engine.library_publish("template", body)
        if not isinstance(got, dict) or not isinstance(got.get("id"), str) or not got["id"]:
            raise EngineError(502, _UNEXPECTED)
        record = {"id": got["id"], "status": str(got.get("status") or "pending"), "note": ""}

        def store(stored: dict) -> None:
            if isinstance(stored.get(template_id), dict):
                if "tradeoffs" in body:              # what was sent is what stays stored
                    stored[template_id]["tradeoffs"] = copy.deepcopy(body["tradeoffs"])
                stored[template_id]["shared"] = record
        T.edit_local(db, store)
    clear_cache()
    return {"id": record["id"], "status": record["status"]}


def refresh(db, engine) -> None:
    """Follow the operator's review: each shared template's status and note from the engine's list of this
    school's own items. One the engine no longer has (withdrawn elsewhere, removed) is no longer shared."""
    with _lock:
        got = engine.library_list("template")
        # a malformed answer must never read as "this school has nothing shared": it would drop every record
        if (not isinstance(got, dict) or not isinstance(got.get("own"), list)
                or not all(isinstance(o, dict) and isinstance(o.get("id"), str) for o in got["own"])):
            raise EngineError(502, _UNEXPECTED)
        own = {o["id"]: o for o in got["own"]}

        def follow(stored: dict) -> None:
            for entry in stored.values():
                if not isinstance(entry, dict) or "shared" not in entry:
                    continue
                record = _shared_record(entry)
                item = own.get(record["id"]) if record else None
                if item is None:
                    del entry["shared"]
                else:
                    entry["shared"] = {"id": record["id"], "status": str(item.get("status") or "pending"),
                                       "note": str(item.get("note") or "")}
        T.edit_local(db, follow)
    clear_cache()


def withdraw(db, engine, template_id: str) -> None:
    """Take the template out of the engine library. `KeyError` when there is no such saved template,
    `SharingError` when it is not shared. An item the engine no longer has counts as withdrawn."""
    with _lock:
        record = _shared_record(T.local_entry(db, template_id))
        if record is None:
            raise SharingError("this template is not shared")
        try:
            engine.library_withdraw(record["id"])
        except EngineError as e:
            if e.status != 404:
                raise

        def forget(stored: dict) -> None:
            if isinstance(stored.get(template_id), dict):
                stored[template_id].pop("shared", None)
        T.edit_local(db, forget)
    clear_cache()


def _listing(engine) -> dict:
    key = engine.identity
    now = _time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None:
        at, got = hit
        if isinstance(got, EngineError) and now - at < FAILED_SECONDS:
            raise got
        if not isinstance(got, EngineError) and now - at < CACHE_SECONDS:
            return got
    try:
        got = engine.library_list("template")
        if not isinstance(got, dict) or not isinstance(got.get("items"), list):
            raise EngineError(502, "the shared library sent an answer the app cannot read")
    except EngineError as e:
        with _cache_lock:
            _cache[key] = (_time.monotonic(), e)
        raise
    with _cache_lock:
        _cache[key] = (now, got)
    return got


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def shared_templates(db, engine) -> list[dict]:
    """Approved templates from other schools, newest first, as wizard templates with ids `shared-<item id>` and
    `shared = {id, schools, kept}`. This school's own items are left out; a body that does not pass
    `validate_template` is skipped. `EngineError` when the engine cannot be reached."""
    got = _listing(engine)
    mine = {o.get("id") for o in got.get("own") or [] if isinstance(o, dict)}
    mine |= {r["id"] for entry in T.local_templates(db) if (r := _shared_record(entry))}
    out = []
    for item in got["items"]:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if not (isinstance(item_id, str) and _ITEM_ID.fullmatch(item_id)) or item_id in mine:
            continue
        body = item.get("body")
        if not isinstance(body, dict):
            continue
        template = copy.deepcopy(body)
        template["id"] = L.SHARED_PREFIX + item_id
        template.pop("shared", None)
        try:
            L.validate_template(template)
        except L.WizardError:
            continue
        template["shared"] = {"id": item_id, "schools": _count(item.get("schools")), "kept": _count(item.get("kept"))}
        out.append(template)
    return out


def report_use(engine, template: dict) -> None:
    """Tell the engine a school chose this shared template; a failure is ignored (the choice stands)."""
    record = template.get("shared") if isinstance(template, dict) else None
    if engine is None or not isinstance(record, dict) or not record.get("id"):
        return
    try:
        engine.library_use(record["id"], False)
    except Exception:       # noqa: BLE001 - the engine being unreachable must never stop the wizard
        pass


def report_kept(db, get_engine, tid: str | None = None) -> None:
    """After a successful Generate draft: when the timetable (`tid`, else the working one) was started from a
    shared template and that has not been reported yet, tell the engine it was kept. `get_engine` is called only
    then. A failure is ignored and noted (`kept_tried`), except that the engine answering 404 (the item was
    withdrawn or never approved) is final (`kept_gone`); the report is tried again after a Generate draft at least
    KEPT_RETRY_SECONDS later. Run after the response has gone (the routes use a background task)."""
    try:
        record = db.get_value("wizard", tid)
    except KeyError:                                     # the timetable was deleted meanwhile
        return
    if not isinstance(record, dict) or not isinstance(record.get("shared_id"), str) or record.get("kept_reported") or record.get("kept_gone"):
        return
    tried = record.get("kept_tried")
    if isinstance(tried, (int, float)) and 0 <= _time.time() - tried < KEPT_RETRY_SECONDS:
        return
    item_id = record["shared_id"]
    try:
        get_engine().library_use(item_id, True)
        outcome = {"kept_reported": True}
    except EngineError as e:
        # 404: the item was withdrawn or never approved: there is nothing to report to, so stop for good
        outcome = {"kept_gone": True} if e.status == 404 else {"kept_tried": _time.time()}
    except Exception:       # noqa: BLE001 - a lost report is retried later; the draft stands either way
        outcome = {"kept_tried": _time.time()}
    try:
        now = db.get_value("wizard", tid)
        if isinstance(now, dict) and now.get("shared_id") == item_id:
            db.set_value("wizard", {**{k: v for k, v in now.items() if k != "kept_tried"}, **outcome}, tid)
    except KeyError:
        pass
