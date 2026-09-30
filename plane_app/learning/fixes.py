"""Fix records (spec docs/superpowers/specs/2026-09-30-learning-design.md §3.1-§3.2): which kind of change cleared
which kind of clash. A record is a signature `{clash, dur, edge}` and a kind of fix, nothing else: no ids,
names or text. The sentences the assistant reads are written here from counts, in the timetable's own words."""
from __future__ import annotations

import logging
import threading
import time as _time
from collections import Counter

from ..engine_client import EngineError
from . import log

_log = logging.getLogger(__name__)

CLASHES = ("person", "location", "sync", "plane", "load")          # tie order for the commonest type
FIXES = ("same_day", "other_day", "room", "swap")
MAX_DUR = 8
MAX_COUNT = 100000                  # the engine's whitelist takes a count of 1 to this
SHARING_KEY = "learning_sharing"    # global kv: {"fixes": bool}; absent means off
CACHE_SECONDS = 300                 # another school's counts, per clash, are asked for at most this often
MAX_SCHOOLS = 100000                # a listing that claims more schools or a bigger count than this is not believed
BATCH = 20                          # publish_all sends at most this many, then waits BATCH_PAUSE seconds
BATCH_PAUSE = 60
FAILED_SECONDS = 45                 # after the engine could not be reached, it is not asked again for this long

_CLASH_TEXT = {
    "person": "a {person} was in two places at once",
    "location": "a {venue} was double-booked or over capacity",
    "sync": "{requirement}s that must start together did not",
    "plane": "someone was timetabled outside their hours or {venue}s",
    "load": "a {person} was overloaded or had no rest",
}
_FIX_TEXT = {
    "same_day": "moving the {requirement} to another {period} the same day",
    "other_day": "moving the {requirement} to another day",
    "room": "keeping the time and changing the {venue}",
    "swap": "swapping the {requirement} with another {requirement}",
}


def classify(frm, to, slots_per_day: int, swap: bool) -> str:
    """The kind of change from `frm` to `to` (each [t0, loc])."""
    if swap:
        return "swap"
    (t_from, loc_from), (t_to, loc_to) = frm, to
    if t_from == t_to and loc_from != loc_to:
        return "room"
    return "same_day" if t_from != t_to and t_from // slots_per_day == t_to // slots_per_day else "other_day"


def signature(clashes_before_touching: list[dict], dur, frm_t0, slots_per_day: int) -> dict | None:
    """`{clash, dur, edge}` of a lesson: the commonest clash type among those that named it before the
    change (ties in the order of CLASHES), its length clipped to 1-8 and whether it sat first or last in a day."""
    counts = Counter(c.get("type") for c in clashes_before_touching or () if c.get("type") in CLASHES)
    if not counts:
        return None
    top = max(counts.values())
    clash = next(t for t in CLASHES if counts.get(t) == top)
    try:
        length = max(1, min(MAX_DUR, int(dur)))
        slot = int(frm_t0) % int(slots_per_day)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return {"clash": clash, "dur": length, "edge": slot in (0, int(slots_per_day) - 1)}


def local_counts(db, clash: str | None = None) -> list[dict]:
    """[{signature, fix, count}] over the fix rows of every timetable of this deployment, most frequent first."""
    seen: Counter = Counter()
    for r in log.rows_all(db, kinds=("fix",)):
        d = r["data"]
        sig, fix = d.get("signature") or {}, d.get("fix")
        if sig.get("clash") not in CLASHES or fix not in FIXES or (clash and sig["clash"] != clash):
            continue
        seen[(sig["clash"], sig.get("dur"), bool(sig.get("edge")), fix)] += 1
    order = sorted(seen.items(), key=lambda kv: (-kv[1], CLASHES.index(kv[0][0]), kv[0][1] or 0, kv[0][2], FIXES.index(kv[0][3])))
    return [{"signature": {"clash": c, "dur": n, "edge": e}, "fix": f, "count": k} for (c, n, e, f), k in order]


def _times(n: int) -> str:
    return "once" if n == 1 else f"{n} times"


def describe(counts: list[dict], vocabulary: dict, dur: int | None = None, shared: bool = False) -> list[str]:
    """One plain sentence per clash and fix, in the timetable's words, most frequent first. With `dur`, only
    fixes recorded for a lesson of that length (clipped to 1-8) count, and the sentence says so. With `shared`
    (other schools' aggregates, each with `schools`) the sentence opens "Across N schools," instead of naming the
    clash (the caller asked for one clash), N being the most schools behind any one length and edge of that fix
    (a school that sent several lengths is not counted twice)."""
    words = {**vocabulary, "period": "period"}
    length = None if dur is None else max(1, min(MAX_DUR, int(dur)))
    totals: Counter = Counter()
    schools: Counter = Counter()
    for c in counts or ():
        sig = c.get("signature") or {}
        if sig.get("clash") in _CLASH_TEXT and c.get("fix") in _FIX_TEXT and (length is None or sig.get("dur") == length):
            key = (sig["clash"], c["fix"])
            totals[key] += int(c.get("count") or 0)
            schools[key] = max(schools[key], int(c.get("schools") or 0))
    out = []
    for (clash, fix), n in sorted(totals.items(), key=lambda kv: (-kv[1], CLASHES.index(kv[0][0]), FIXES.index(kv[0][1]))):
        if n <= 0:
            continue
        change = _FIX_TEXT[fix].format(**words)
        if shared:
            k = max(1, schools[(clash, fix)])
            span = f"Across {k} school{'' if k == 1 else 's'}"
            head = f"{span}, for a {length}-{words['period']} {words['requirement']}" if length else span
            out.append(f"{head}, {change} worked {_times(n)}.")
            continue
        cause = _CLASH_TEXT[clash].format(**words)
        head = f"For a {length}-{words['period']} {words['requirement']}, when {cause}" if length else f"When {cause}"
        out.append(f"{head}, {change} worked {_times(n)}.")
    return out


def advice(counts: list[dict], vocabulary: dict, dur: int | None = None, shared: bool = False) -> tuple[list[str], str | None]:
    """`describe` for the assistant: when `dur` is given and nothing is recorded for that length, every length
    is described instead and the note says so."""
    lines = describe(counts, vocabulary, dur, shared)
    if lines or dur is None:
        return lines, None
    lines = describe(counts, vocabulary, None, shared)
    if not lines:
        return [], None
    words = {**vocabulary, "period": "period"}
    length = max(1, min(MAX_DUR, int(dur)))
    return lines, f"Nothing recorded for {length}-{words['period']} {words['requirement']}s; here is every length."


# ---- sharing with other schools (spec §3.3) ---------------------------------------------------------------------
# Opt-in, off by default, and with it off no fix call is ever made to the engine (not even a listing). What is
# sent is exactly {signature, fix, count}: this school's total for that clash signature and kind of fix. What
# comes back is numbers only; the sentences the assistant reads are written here, in the timetable's words.

def sharing_on(db) -> bool:
    value = db.get_value(SHARING_KEY)
    return isinstance(value, dict) and value.get("fixes") is True


def set_sharing(db, on: bool) -> None:
    # Switching off sets _wake so that a run that is pausing between batches notices at once. An off-then-on toggle
    # while a run is pausing therefore ends that pause early: the run goes on (the switch reads on again) and sends
    # its next batch sooner than BATCH_PAUSE. That is bounded (one early batch per toggle, and a toggle is a
    # deliberate act by an admin) and stays under the request rate limit the pacing is there to respect.
    db.set_value(SHARING_KEY, {"fixes": bool(on)})
    clear_cache()
    if not on:
        _wake.set()


_lock = threading.Lock()                # counting a pair and sending it are one step, or a lower count could land last
_cache: dict[tuple[str, str], tuple[float, list | EngineError]] = {}
_cache_lock = threading.Lock()
_threads: set[threading.Thread] = set()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _pair_count(counts: list[dict], signature: dict, fix: str) -> int:
    return sum(c["count"] for c in counts if c["fix"] == fix and c["signature"] == signature)


def _send(engine, signature: dict, fix: str, count: int) -> None:
    engine.library_publish("fix", {"signature": {"clash": signature["clash"], "dur": signature["dur"], "edge": signature["edge"]},
                                   "fix": fix, "count": max(1, min(MAX_COUNT, int(count)))})


def publish(db, engine, signature: dict, fix: str) -> None:
    """When sharing is on, send this school's total for the pair (the engine updates its item for the pair).
    Nothing when sharing is off, there is no engine or nothing is recorded for the pair. Errors are logged, never raised."""
    if engine is None:
        return
    try:
        with _lock:
            if not sharing_on(db):
                return
            count = _pair_count(local_counts(db), signature, fix)
            if count >= 1:
                _send(engine, signature, fix, count)
    except Exception:  # noqa: BLE001 - sharing is a nicety: a failed publish never breaks an apply or a turn
        _log.warning("fix aggregate not published", exc_info=True)


_wake = threading.Event()               # set when sharing is switched off, so a run that is pausing notices at once
_run_active = threading.Lock()          # one publish_all run at a time


def _sleep(seconds: float) -> None:
    """The pause between batches: cut short when sharing is switched off. A module-level function so that tests
    can patch the pacing away."""
    _wake.wait(seconds)
    _wake.clear()


def publish_all(db, engine) -> int:
    """Send every pair this school has, for when sharing is switched on; how many were sent. The library routes
    share the school's per-minute request bucket (60 by default), so the run is paced: at most BATCH sends, then
    a wait of BATCH_PAUSE seconds (about 20 a minute; `publish_all_soon` runs it off the request), and each send re-checks the switch (turning sharing off stops
    the run) and recounts its pair (a fix applied meanwhile is not overwritten with an older count). A failure is
    logged and the run goes on; pairs the engine answered 429 to are tried once more at the end; an engine that
    cannot be reached at all (three failures in a row) ends the run, the counts going with the next fix."""
    if engine is None:
        return 0
    pairs = [(c["signature"], c["fix"]) for c in _safely(local_counts, db) or ()] if sharing_on(db) else []
    sent, in_batch, unreachable, limited = 0, 0, 0, []

    def one(signature, fix) -> str:
        """'sent', 'stopped' (sharing is off), 'limited' (429) or 'failed'."""
        nonlocal unreachable
        try:
            with _lock:
                if not sharing_on(db):
                    return "stopped"
                count = _pair_count(local_counts(db), signature, fix)
                if count >= 1:
                    _send(engine, signature, fix, count)
            unreachable = 0
            return "sent"
        except EngineError as e:
            unreachable = unreachable + 1 if e.status == 0 else 0
            _log.warning("fix aggregate not published (%s)", e.status)
            return "limited" if e.status == 429 else "failed"
        except Exception:  # noqa: BLE001
            _log.warning("fix aggregate not published", exc_info=True)
            return "failed"

    for n, (signature, fix) in enumerate(pairs):
        if in_batch >= BATCH:
            _sleep(BATCH_PAUSE)
            in_batch = 0
        outcome = one(signature, fix)
        if outcome == "stopped":
            return sent
        in_batch += 1
        if outcome == "sent":
            sent += 1
        elif outcome == "limited":
            limited.append((signature, fix))
        if unreachable >= 3:
            _log.warning("the shared library cannot be reached; %d fix aggregates left for later", len(pairs) - n - 1)
            return sent
    if limited:
        _sleep(BATCH_PAUSE)
        for signature, fix in limited:
            outcome = one(signature, fix)
            if outcome == "stopped":
                return sent
            sent += outcome == "sent"
    return sent


def publish_all_soon(db, get_engine) -> bool:
    """`publish_all` on a daemon thread (a paced run can last many minutes: it must not hold a request's worker
    or stall the server's shutdown). `get_engine` is called on that thread; if it fails the counts go with the next
    fix. Only one run at a time: while one is active this starts nothing (the active run re-checks the switch
    before each send, and recounts each pair) and returns False."""
    if not _run_active.acquire(blocking=False):
        return False

    def run():
        try:
            _wake.clear()
            try:
                engine = get_engine()
            except Exception:  # noqa: BLE001 - no engine can be made from the settings
                _log.warning("fix aggregates not published: no engine could be made", exc_info=True)
                return
            publish_all(db, engine)
        except Exception:  # noqa: BLE001
            _log.warning("fix aggregates not published", exc_info=True)
        finally:
            _threads.discard(threading.current_thread())
            _run_active.release()
    t = threading.Thread(target=run, name="fix-publish-all", daemon=True)
    _threads.add(t)
    try:
        t.start()
    except Exception:  # noqa: BLE001 - no thread could be made: free the guard, or no later toggle could ever start a run
        _threads.discard(t)
        _run_active.release()
        _log.warning("fix aggregates not published: the run could not be started", exc_info=True)
        return False
    return True


def _safely(fn, *args):
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001
        _log.warning("fix counts not read", exc_info=True)
        return None


def publish_soon(db, engine, signature: dict, fix: str) -> None:
    """`publish` on a daemon thread, so that an apply (called from a route and from a chat tool, both of which
    hold a person waiting) never waits for the engine. A publish still running when the process stops is lost;
    the next fix for the pair sends the whole count again."""
    if engine is None or not sharing_on(db):
        return

    def run():
        try:
            publish(db, engine, signature, fix)
        finally:
            _threads.discard(threading.current_thread())
    t = threading.Thread(target=run, name="fix-publish", daemon=True)
    _threads.add(t)
    t.start()


def drain(timeout: float = 10.0) -> None:
    """Wait for background publishes (for tests)."""
    for t in list(_threads):
        t.join(timeout)


def _whole(value, low: int, high: int | None = None) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= low and (high is None or value <= high)


def _clean_item(item) -> dict | None:
    """One item of the engine's answer rebuilt from checked fields only, or None when any of them is not what the
    engine's whitelist promises: nothing another school (or a hostile engine) wrote is passed on but numbers and
    the names of our own kinds of clash and fix."""
    if not isinstance(item, dict) or not isinstance(item.get("signature"), dict):
        return None
    sig = item["signature"]
    if not (isinstance(sig.get("clash"), str) and sig["clash"] in CLASHES and isinstance(item.get("fix"), str)
            and item["fix"] in FIXES and isinstance(sig.get("edge"), bool) and _whole(sig.get("dur"), 1, MAX_DUR)
            and _whole(item.get("schools"), 0, MAX_SCHOOLS) and _whole(item.get("count"), 0, MAX_COUNT)):
        return None
    return {"signature": {"clash": sig["clash"], "dur": sig["dur"], "edge": sig["edge"]}, "fix": item["fix"],
            "count": item["count"], "schools": item["schools"]}


def shared_counts(db, engine, clash: str) -> list[dict] | None:
    """[{signature, fix, count, schools}] other schools (and this one, once approved) have approved for `clash`;
    `None` when sharing is off or the library could not be reached. Cached per clash for CACHE_SECONDS, a failure
    for FAILED_SECONDS."""
    if engine is None or not sharing_on(db):
        return None
    key = (engine.identity, clash)
    now = _time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None:
        at, got = hit
        if isinstance(got, EngineError):
            if now - at < FAILED_SECONDS:
                return None
        elif now - at < CACHE_SECONDS:
            return got
    try:
        got = engine.library_list("fix", clash=clash)
        items = got.get("items") if isinstance(got, dict) else None
        if not isinstance(items, list):
            raise EngineError(502, "the shared library sent an answer the app cannot read")
        clean = [c for c in map(_clean_item, items) if c is not None]
    except EngineError as e:
        with _cache_lock:
            _cache[key] = (_time.monotonic(), e)
        return None
    except Exception:  # noqa: BLE001 - an answer of the wrong shape is a failure like any other
        with _cache_lock:
            _cache[key] = (_time.monotonic(), EngineError(502, "the shared library sent an answer the app cannot read"))
        return None
    with _cache_lock:
        _cache[key] = (now, clean)
    return clean
