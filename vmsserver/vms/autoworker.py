"""autoworker — the seventh subsystem's worker: the one that READS.

Every worker before it observed the world and wrote down what it saw. This one
reads what they wrote, decides that a scenario fired, and asks somebody else to
act — by filing a request, never by touching a device.

    AUTO_NAME / SLOT_INDEX     -> the slot to claim: a-<index>
    SERVER_NAME (or hostname)  -> `server` in the heartbeat
    ARCHIVE                    -> where its own events and its cursor live

Three things carry the whole design, and each one is a decision that could have
gone the other way:

  1. NO STATE MACHINE. "A and B within thirty seconds" is not "saw A, waiting
     for B" — it is a question asked of the last thirty seconds of the log,
     re-asked every pass. There is nothing to keep, nothing to restore and
     nothing to get wrong after a crash.
  2. A CURSOR, not a queue. `Frontier` — the survey's own number, the same file
     — says how far this scenario has been considered. It is the only thing
     written down.
  3. A DETERMINISTIC REQUEST ID. Delivery is at-least-once by construction, so
     a firing computed twice must be the same row: `<scenario>-<millisecond of
     the event that completed it>`. Written twice is written once; performed
     once by the holder.
"""
from __future__ import annotations

import logging
import os
import time

from w2cplatform import runtime
from w2cplatform.contract import Worker
from w2cplatform.eventdatabase import MergedIndex
from w2cplatform.events import EventLog
from w2cplatform.variables import Variables

from .auto import fires
from .config import AUTO_SPEC
from .scan import Frontier

log = logging.getLogger("autoworker")
AUTO = AUTO_SPEC.sub


class AutoWorker(Worker):
    """`name` is a slot (`a-1`); `index` is the merged event log — the console's
    own reader, because a scenario watches what the console shows."""

    # How far back a scenario looks when it has never run, and the most it re-reads after being down. Not
    # "everything since the beginning": a scenario started this morning must not fire on yesterday's door.
    COLD_START = 300.0
    # Firings filed in one pass, per scenario. A sensor that bounced two hundred times while this worker
    # was restarting is not two hundred doors — it is a bounce, and the rate ceiling below says so; this
    # one keeps a single pass bounded whatever the log holds.
    PER_PASS = 4
    # How long a firing id is remembered after it is filed. Long enough that the same event, still inside
    # somebody's window, is not filed twice; short enough to be a handful of strings.
    REMEMBER = 900.0
    # How far BEHIND `now` the cursor is allowed to stop, and the reason there is a distance at all: the
    # merge is not a stream this process reads. A resource tails its files on a timer and the console
    # merges over HTTP, so an event written at T becomes visible some seconds later. A cursor set to `now`
    # is already past it, and `t <= since` then skips it FOR EVER — no refusal, no log line, just a
    # scenario that does not fire, which is the most expensive silence in this subsystem.
    #
    # The asymmetry decides the number: re-reading a stretch costs a comparison (the firing ids are
    # deterministic and the ones just filed are still remembered), running ahead costs a firing. Five
    # seconds is comfortably more than the tail period.
    SETTLE = 5.0
    # Rows per (subsystem, kind) query. The merge answers `ORDER BY t LIMIT ?`, so a full window drops the
    # NEWEST events — exactly the ones a scenario exists for.
    PER_KIND = 500

    def __init__(self, name: str | None, vars_: Variables, objects, index=None, capacity: int | None = None,
                 clock=time.monotonic, wall=time.time, server: str | None = None,
                 archive_root: str | None = None, env: dict | None = None):
        env = dict(os.environ if env is None else env)
        super().__init__(AUTO, None, vars_, objects, clock=clock, wall=wall)
        self.claim_slot(prefer=name if name is not None else runtime.slot(env, "AUTO_NAME", "a"))
        self.capacity = capacity if capacity is not None else int(env.get("CAPACITY", "50"))
        self.server = runtime.server(env, server)
        self.labels = runtime.labels(env)
        self.archive_root = archive_root or env.get("ARCHIVE", "/data/archive")
        # The same reader the console uses, for the same reason: a scenario watches what an operator would
        # see on the timeline. One merge, one definition of "the events of the last minute".
        self.index = index or MergedIndex(objects, wall=wall)
        self.fired: dict[str, float] = {}            # firing id -> when it was filed (the replay guard)
        self.recent: dict[str, list[float]] = {}     # scenario -> firing times inside the last minute
        self.filed = 0
        self.status_by_unit: dict[str, dict] = {}

    # -- the rows ---------------------------------------------------------------------------------
    def scenario(self, unit: str) -> dict | None:
        it, _ = self.vars.get(AUTO.config(AUTO_SPEC.rows, str(unit)))
        return AUTO_SPEC.row(it) if it and it.get("deleted") != "true" else None

    # -- the pass ---------------------------------------------------------------------------------
    # One pass over the scenarios this worker was assigned. Everything else in this file is called from
    # here, and the order is the argument: take the epoch (one evaluator per scenario, fenced like any
    # writer), read the window, decide, file, then move the cursor — never before the filing, or a crash
    # between the two would lose the firing instead of repeating it.
    def reconcile_once(self, now: float | None = None) -> list[str]:
        now = self.wall() if now is None else now
        acted: list[str] = []
        for unit in sorted(self.assignment().units):
            row = self.scenario(unit)
            if row is None:
                continue
            if not row["enabled"]:
                self.status_by_unit[unit] = {"id": unit, "phase": "pending", "why": "disabled"}
                continue
            if unit not in self.epochs:
                self.take_epoch(unit)                # its own events — "this scenario fired" — need one writer
            if not self.may_write(unit):
                continue                             # the lease says another instance has it: decide nothing
            try:
                n = self.evaluate(row, now)
            except Exception as e:                   # noqa: BLE001 — a resource that stopped answering mid-pass
                self.status_by_unit[unit] = {"id": unit, "phase": "waiting", "why": str(e)}
                log.warning("%s: %s not evaluated: %s", self.name, unit, e)
                continue
            self.status_by_unit[unit] = {"id": unit, "phase": "running", "fired": n}
            if n:
                acted.append(unit)
        return acted

    # One scenario against the log. Returns how many firings were filed.
    def evaluate(self, row: dict, now: float) -> int:
        unit = str(row["id"])
        front = Frontier(self.archive_root, unit, AUTO.name)
        since = front.read()
        since = now - self.COLD_START if since is None else max(since, now - self.COLD_START)
        window = float(row.get("within") or 0)
        # Read back far enough to answer the question, not far enough to answer it twice: the window is
        # how much history a firing may span, and `since` is how much of it is new.
        events = self.window(row, since - window, now)
        fired = 0
        for fid, at in self.firings(row, events, since):
            if fired >= self.PER_PASS:
                break
            if fid in self.fired:
                continue                             # already filed; the id is the same either way
            if not self.allowed(row, now):
                log.warning("%s: %s is over its ceiling of %s/min — not firing", self.name, unit, row["rate_per_minute"])
                break
            self.file(row, fid, at)
            fired += 1
        self.forget(now)
        front.set(max(since, now - self.SETTLE))     # …and never further than the log has caught up to
        return fired

    # The events to decide on — ONE QUERY PER KIND THE SCENARIO WATCHES, and that is not an optimisation.
    #
    # A single "everything in the last five minutes" comes back `ORDER BY t LIMIT n`: when it overflows,
    # what is dropped is the NEWEST end, which is the end a scenario cares about. Three cameras writing a
    # statistics line every few seconds fill a thousand rows in a five-minute window, and from that moment
    # the evaluator sees the beginning of the window and never the end. No refusal, no log line — the
    # language gets blamed.
    #
    # The trigger already names the subsystem and the kind. Letting the query name them too means the
    # window holds only what this scenario is looking at, and there are at most `MAX_TRIGGERS` of them.
    #
    # And when a window still comes back full, SAY SO: a decision taken on truncated data must not look
    # like a decision taken on all of it.
    def window(self, row: dict, t0: float, t1: float) -> list[dict]:
        kinds = sorted({(str(t.get("sub", "")), str(t.get("kind", ""))) for t in row["when"]})
        out, full = [], []
        for sub, kind in kinds:
            rep = self.index.query(max(0.0, t0), t1, subsystem=sub, kind=kind, limit=self.PER_KIND)
            evs = [e for e in rep.get("events", []) if not e.get("fenced")]
            if len(evs) >= self.PER_KIND:
                full.append(f"{sub}.{kind}")
            out += evs
        if full:
            log.warning("%s: %s — the window came back full for %s; a firing may have been cut off its "
                        "newest end", self.name, row["id"], ", ".join(full))
        out.sort(key=lambda e: float(e.get("t", 0)))
        return out

    # Which firings this scenario has, newest first — `(id, when)`.
    #
    # The completing event decides. For one trigger that is every matching event; for several it is an
    # event that matches one of them and has a partner for each of the others inside the window ending at
    # it. Asking it this way is what removes the state machine: the same question, asked of the same log,
    # gives the same answer on every pass, whoever is asking and however many times they have restarted.
    def firings(self, row: dict, events: list[dict], since: float) -> list[tuple[str, float]]:
        triggers, window = list(row["when"]), float(row.get("within") or 0)
        out: list[tuple[str, float]] = []
        for e in events:
            t = float(e.get("t", 0))
            if t <= since:
                continue                             # considered on an earlier pass
            if not any(fires(tr, e) for tr in triggers):
                continue
            if len(triggers) > 1:
                done = [tr for tr in triggers if fires(tr, e)]
                waiting = [tr for tr in triggers if tr not in done]
                if not all(any(fires(tr, o) and t - window <= float(o.get("t", 0)) <= t for o in events)
                           for tr in waiting):
                    continue                         # not all of them, not inside the window: not yet
            out.append((f"{row['id']}-{int(t * 1000)}", t))
        return out

    # The ceiling, per scenario, per minute. In memory on purpose: it is a guard against a sensor that
    # bounces, and a bounce does not survive a restart — while a number in the store would be one more
    # write on the path of the thing that must stay fast.
    def allowed(self, row: dict, now: float) -> bool:
        unit, cap = str(row["id"]), int(row.get("rate_per_minute") or 0)
        recent = [t for t in self.recent.get(unit, []) if now - t < 60.0]
        self.recent[unit] = recent
        return not cap or len(recent) < cap

    # File what the scenario asks for: one row per action, in the TARGET subsystem's request family.
    #
    # `valid_until` is the scenario's window, or thirty seconds — the same default the console uses for an
    # operator's own command, and for the same reason: an action that arrives after its moment is not a
    # late action, it is a wrong one.
    def file(self, row: dict, fid: str, at: float) -> None:
        unit = str(row["id"])
        for i, action in enumerate(row["then"]):
            sub, name = str(action.get("sub", "")), str(action.get("action", ""))
            rid = f"{fid}-{i}"
            fields = {k: str(v) for k, v in action.items() if k not in ("sub", "action")}
            if name == "record":                     # the recorder's own words for "record this from now"
                fields = {"unit": fields.get("cam", ""), **fields}
            self.vars.put(f"{sub}/requests/{rid}",
                          {**fields, "action": name, "at": str(at), "by": f"auto/{unit}",
                           "valid_until": str(at + (float(row.get("within") or 0) or 30.0))})
            self.filed += 1
        self.fired[fid] = self.wall()
        self.recent.setdefault(unit, []).append(at)
        # …and the scenario's own event, in its own bucket: what an operator sees on the timeline when
        # they ask why the door opened at 14:02.
        if unit in self.epochs:
            EventLog(self.archive_root, AUTO.name, unit, self.epochs[unit]).append(
                at, "fired", scenario=unit, actions=len(row["then"]))

    def forget(self, now: float) -> None:
        self.fired = {k: t for k, t in self.fired.items() if now - t < self.REMEMBER}

    # -- what this worker publishes -----------------------------------------------------------------
    def status(self) -> list[dict]:
        return [self.status_by_unit[u] for u in sorted(self.status_by_unit) if u in set(self.assignment().units)]

    def headroom(self) -> int:
        return max(0, self.capacity - len(self.assignment().units))

    def heartbeat_once(self) -> None:
        self.heartbeat(self.status(), server=self.server, instance=self.instance,
                       labels=",".join(self.labels), capacity=self.capacity, headroom=self.headroom(),
                       filed=self.filed)

    def pump_once(self) -> None:
        return None                                   # nothing to drain: this worker runs no pipelines
