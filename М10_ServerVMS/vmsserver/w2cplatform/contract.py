"""The subsystem contract — what the platform knows about any subsystem,
and it is all of this:

    a config prefix        <name>/*                      writable by the controller only
    assignment rows        <name>/workers/<worker>       what each worker should run
    a heartbeat object     <name>/<worker>/heartbeat     {ts, status: [...]} — the worker's own report
    an epoch prefix        <name>/epoch/<unit>           fencing tokens the workers take by CAS
    an event log           <resource>/<name>/<unit>/e<epoch>/<start>Z.events.jsonl   (w2cplatform.events)
                                                         what a worker observed about a unit it holds the epoch for;
                                                         on its server's resource, under the subsystem's prefix
    a slot prefix          <name>/slots/<worker>         identity by claim: a worker's name is a slot it
                                                         holds by CAS and renews; a replacement process
                                                         takes the lapsed slot and inherits its assignment

Who decides how many workers there are: not the controller. The scheduler
runs `count` of them (Nomad, or `systemctl start vmsworker@w-N` on one box)
and an autoscaler moves `count` from a headroom metric the workers export.
The platform's part is to give `count` interchangeable processes stable
names — the slots — so that assignments survive a reschedule. A slot is
released on an orderly stop (scale-in); the controller then redistributes
what the slot held. A slot that merely lapses (a crash) is left alone: the
scheduler brings the process back, and it claims the same slot.

`Controller` and `Worker` are the two base classes. The platform never
imports anything from a subsystem; the live and det subsystems prove the
shape is generic by running a subsystem that counts seconds through it.
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # contract.py — the subsystem contract: Subsystem, Assignment, Heartbeat, Slot, and the Controller and
# Worker base classes
#
# **Role in the module.** Lesson 1. This file is the whole of what the platform knows about any subsystem,
# and it is deliberately small: a config prefix `<name>/*` writable by the controller only; assignment rows
# `<name>/workers/<worker>`; a heartbeat object `<name>/<worker>/heartbeat`; an epoch prefix
# `<name>/epoch/<unit>` the workers take by CAS; an event log on the resource (`events.py`); and a slot
# prefix `<name>/slots/<worker>` — identity by claim. It depends on `variables.py`, `objects.py` and
# `epoch.py` and nothing else. `spec.SpecController` extends `Controller`; `vms.worker.VmsWorker` and the
# gateway (`vms/liveworker.py`) and the detector (`vms/detworker.py`) extend `Worker`. The file's own docstring settles who
# decides how many workers there are: not the controller. The scheduler runs `count` of them; the platform's
# part is to give those interchangeable processes stable names — the slots — so assignments survive a
# reschedule. A slot released on an orderly stop is redistributed by the controller; a slot that merely
# lapses is a crash, left alone for the scheduler.
#
# ## Module-level names
# None (dataclasses and classes only).
#
# ### `__init__(self, sub, name, vars_, objects, lease_ttl=30.0, lease_margin=5.0, clock=time.monotonic,
# wall=time.time, instance=None, slot_ttl=45.0)` `name` is the slot name, or `None` until `claim_slot()`.
# `instance` identifies the process (`hostname:pid:6hex` by default) and is what a slot row records as
# `holder`. Keeps two clocks: monotonic for leases, wall for slot expiry. `epochs` and `leases` are per-unit
# dicts, empty at start.
#
# ## Notes
# - The tests enforce the boundary: `test_the_platform_knows_nothing_about_video` asserts no import from
#   `vms/` and not the word "camera" in this file; `test_lesson8_live.py` and `test_lesson9_det.py` run two more subsystems through the
#   same `Controller`/`Worker`.
# - Ordering that matters: a worker claims its slot before reading its assignment (the name is the row key);
#   it takes an epoch before writing anything for a unit; it renews slot and leases on a shorter period than
#   `slot_ttl` / `lease_ttl − margin`.
# - `until` is wall-clock while leases are monotonic: slots are compared across processes and boxes, leases
#   only within one process.
# ================================================================================================
from __future__ import annotations

import json
import os
import socket
import time
import uuid
from dataclasses import dataclass, field

from .epoch import Lease, next_epoch
from .objects import ObjectStore
from .variables import Conflict, Variables


# A name, and the key layout derived from it. Every path the platform touches for a subsystem is produced
# here, so the layout is in one place.
# - `name` — the prefix (`vms`, `live`, `det`).
@dataclass
class Subsystem:
    name: str

    # `<name>/<part>/<part>…` — the generic path builder used by `SpecController` for rows, `next_id`,
    # `placement/<id>`, derived rows and the snapshot key.
    def config(self, *parts: str) -> str:
        return "/".join((self.name,) + parts)

    # `<name>/workers/<worker>`.
    def assignment(self, worker: str) -> str:
        return f"{self.name}/workers/{worker}"

    # `<name>/<worker>/heartbeat` — an object-store key, not a Variable.
    def heartbeat_key(self, worker: str) -> str:
        return f"{self.name}/{worker}/heartbeat"

    # `<name>/epoch/<unit>`.
    def epoch_key(self, unit: str) -> str:
        return f"{self.name}/epoch/{unit}"

    # `<name>/slots/<worker>`.
    def slot_key(self, worker: str) -> str:
        return f"{self.name}/slots/{worker}"

    # `[<name>/*]` — the whole prefix. This is Lesson 1's coarse ACL; `SubsystemSpec.acl_controller` narrows
    # it in Lesson 6 once the console gets its own token.
    def acl_controller(self) -> list[str]:
        return [f"{self.name}/*"]

    # `[<name>/epoch/*, <name>/slots/*]` — a worker writes only epochs and its slot, never configuration.
    def acl_worker(self) -> list[str]:
        return [f"{self.name}/epoch/*", f"{self.name}/slots/*"]


# What one worker should run: the row at `<name>/workers/<worker>`.
# - `worker` — the slot name.
# - `units` — the subsystem's unit ids as strings; the platform does not know what they are.
# - `rev` — bumped on every change, so a worker can tell a new assignment from the one it already applied
#   (`assignment_rev` in the VMS heartbeat).
@dataclass
class Assignment:
    worker: str
    units: list[str]                 # what the subsystem calls its units of work; the platform does not know
    rev: int = 0

    # `{"units": "1,2,3", "rev": n}` — the Variables row form (strings only).
    def to_items(self) -> dict:
        return {"units": ",".join(self.units), "rev": self.rev}

    # The inverse; a missing row is an empty assignment with `rev 0`. Empty strings in the list are dropped.
    @classmethod
    def from_items(cls, worker: str, items: dict | None) -> "Assignment":
        if not items:
            return cls(worker, [])
        return cls(worker, [u for u in items.get("units", "").split(",") if u], int(items.get("rev", 0)))


# A worker's own report, written as one JSON object.
# - `worker` — the name; `ts` — wall-clock time of the write; `status` — a list of per-unit dicts (the read
#   model: `{id, phase, epoch, …}` in the VMS); `extra` — every other top-level key (`server`, `labels`,
#   `capacity`, `headroom`, `conflicts`, `started`, `previous_hb`, …). The platform reads `extra` by name in
#   `SpecController` and the console's `/metrics`; it never defines the keys.
@dataclass
class Heartbeat:
    worker: str
    ts: float
    status: list[dict] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    # `{"worker", "ts", "status", **extra}` as JSON.
    def to_bytes(self) -> bytes:
        return json.dumps({"worker": self.worker, "ts": self.ts, "status": self.status, **self.extra}).encode()

    # The inverse; everything that is not `worker`/`ts`/`status` lands in `extra`.
    @classmethod
    def from_bytes(cls, raw: bytes) -> "Heartbeat":
        d = json.loads(raw)
        extra = {k: v for k, v in d.items() if k not in ("worker", "ts", "status")}
        return cls(d["worker"], float(d["ts"]), list(d.get("status", [])), extra)


# A worker's name as a row: who holds it, until when (wall clock), and whether the last holder let go on
# purpose. This is the mechanism behind identity by claim.
# - `name` — `w-1`; `holder` — the claiming process's `instance` string; `until` — wall-clock expiry of the
#   claim; `released` — True when the holder stopped on purpose (default True for a row that does not exist
#   yet); `gen` — a generation counter bumped on every claim.
@dataclass
class Slot:
    """A worker's name, as a row: who holds it, until when (wall clock), and
    whether the last holder let go of it on purpose."""
    name: str
    holder: str = ""
    until: float = 0.0
    released: bool = True
    gen: int = 0

    # Row conversion; `released` is stored as `"true"`/`"false"`. A missing row is `Slot(name)` — released,
    # no holder.
    def to_items(self) -> dict:
        return {"holder": self.holder, "until": self.until, "released": "true" if self.released else "false", "gen": self.gen}

    @classmethod
    def from_items(cls, name: str, items: dict | None) -> "Slot":
        if not items:
            return cls(name)
        return cls(name, items.get("holder", ""), float(items.get("until", 0)), items.get("released") == "true",
                   int(items.get("gen", 0)))

    # Held, not released, and past `until`: the holder went silent — a crash.
    def lapsed(self, now: float) -> bool:
        return not self.released and self.holder != "" and now > self.until

    # Released, or never held, or past `until`. (A lapsed slot is claimable; a released one is too.)
    def claimable(self, now: float) -> bool:
        return self.released or self.holder == "" or now > self.until


# The integer after the last `-` in a slot name (`w-3` → 3), 0 if not numeric. Used to order free slots and
# to pick the next unused number.
def slot_number(name: str) -> int:
    tail = name.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else 0


# The only writer of `<name>/*`. It holds nothing: every method reads the store, decides, and writes by CAS,
# so two instances are harmless — this is the property `spec.SpecController` and the VMS controller inherit,
# and the reason the controller is never on the recovery path.
class Controller:
    """The only writer of <name>/*. Holds nothing: every method reads the
    store, decides, and writes by CAS. Two instances are harmless."""

    # Keeps the `Subsystem`, the two stores and a wall clock (tests inject a fake).
    def __init__(self, sub: Subsystem, vars_: Variables, objects: ObjectStore, wall=time.time):
        self.sub, self.vars, self.objects, self.wall = sub, vars_, objects, wall

    # The one write primitive: read `(items, idx)`, call `mutate(dict(items or {}))`; if it returns `None`
    # nothing is written and the current items are returned; otherwise `put(cas=idx)`; on `Conflict` re-read
    # and repeat, up to `retries`, then `RuntimeError`. Every mutation in `SpecController` goes through
    # this, which is what makes "two controllers agree by CAS" true.
    def write(self, path: str, mutate, retries: int = 10) -> dict:
        """Read-modify-write by CAS: `mutate(items or {}) -> new items`.
        A conflict means another instance wrote; re-read and go again."""
        for _ in range(retries):
            items, idx = self.vars.get(path)
            new = mutate(dict(items or {}))
            if new is None:
                return dict(items or {})
            try:
                self.vars.put(path, new, cas=idx)
                return new
            except Conflict:
                continue
        raise RuntimeError(f"{path}: {retries} conflicts")

    # Which workers exist: those whose heartbeat object under `<name>/` is at most `max_age` old. Never a
    # list the controller keeps — a fact it reads.
    # `test_controller_and_worker_bases_speak_only_the_contract`: after the wall clock advances 100 s a
    # silent worker is not a worker.
    def workers_seen(self, max_age: float = 45.0) -> dict[str, Heartbeat]:
        """Which workers exist: those that heartbeat recently. Never a list
        the controller keeps — a fact it reads."""
        out = {}
        now = self.wall()
        for key in self.objects.list(self.sub.name + "/"):
            if key.endswith("/heartbeat"):
                raw = self.objects.get(key)
                if raw:
                    hb = Heartbeat.from_bytes(raw)
                    if now - hb.ts <= max_age:
                        out[hb.worker] = hb
        return out

    # Reads one worker's row.
    def assignment(self, worker: str) -> Assignment:
        items, _ = self.vars.get(self.sub.assignment(worker))
        return Assignment.from_items(worker, items)

    # Replaces the worker's assignment with the sorted, de-duplicated list and bumps `rev`.
    def assign(self, worker: str, units: list[str]) -> Assignment:
        def mutate(items):
            rev = int(items.get("rev", 0)) + 1
            return Assignment(worker, sorted(set(units), key=str), rev).to_items()
        return Assignment.from_items(worker, self.write(self.sub.assignment(worker), mutate))

    # Read-modify-write adding one unit; returns `None` from the mutator (no write) if already present. Two
    # controllers adding different units to one worker at once both land.
    def assign_add(self, worker: str, unit: str) -> Assignment:
        """Read-modify-write: two controllers adding different units to one
        worker at once both land."""
        def mutate(items):
            a = Assignment.from_items(worker, items)
            if unit in a.units:
                return None
            return Assignment(worker, sorted(set(a.units) | {unit}, key=str), a.rev + 1).to_items()
        return Assignment.from_items(worker, self.write(self.sub.assignment(worker), mutate))

    # The mirror of `assign_add`.
    def assign_remove(self, worker: str, unit: str) -> Assignment:
        def mutate(items):
            a = Assignment.from_items(worker, items)
            if unit not in a.units:
                return None
            return Assignment(worker, [u for u in a.units if u != unit], a.rev + 1).to_items()
        return Assignment.from_items(worker, self.write(self.sub.assignment(worker), mutate))

    # Every row under `<name>/workers/`.
    def assignments(self) -> dict[str, Assignment]:
        out = {}
        for path in self.vars.list(self.sub.name + "/workers/"):
            worker = path.rsplit("/", 1)[1]
            out[worker] = self.assignment(worker)
        return out

    # -- slots: read them, never hand them out ------------------------------------
    # Every row under `<name>/slots/`, read only — the controller never hands slots out.
    def slots(self) -> dict[str, Slot]:
        out = {}
        for path in self.vars.list(self.sub.name + "/slots/"):
            name = path.rsplit("/", 1)[1]
            items, _ = self.vars.get(path)
            out[name] = Slot.from_items(name, items)
        return out

    # Slots whose holder let go on purpose (scale-in, or `retire`) and that still have units assigned: what
    # a subsystem redistributes. A slot that merely lapsed is not here — that is a crash, and the scheduler
    # brings the process back under the same name. Sorted by slot number.
    def released_slots(self) -> list[str]:
        """Slots whose holder let go on purpose (scale-in, or `retire`) and
        that still have an assignment: what a subsystem redistributes. A slot
        that merely lapsed is NOT here — that is a crash, and the scheduler
        brings its process back under the same name."""
        return sorted((n for n, s in self.slots().items() if s.released and self.assignment(n).units),
                      key=slot_number)

    # An operator's statement that a slot is gone for good: marks it `released` by CAS (no-op if already
    # released). The controller never decides this on its own from a silence.
    def retire(self, worker: str) -> Slot:
        """An operator's statement that a slot is gone for good (the process
        that held it will not return). Marks it released; the subsystem's
        redistribution takes it from there. The controller never decides this
        on its own from ONE silence — a subsystem that requires a resource may
        act on two, the slot's and its server's resource's (spec.gone_servers)."""
        def mutate(items):
            s = Slot.from_items(worker, items)
            if s.released:
                return None
            return Slot(worker, s.holder, s.until, True, s.gen).to_items()
        return Slot.from_items(worker, self.write(self.sub.slot_key(worker), mutate))


# Runs its assignment and reports. Reads `<name>/workers/<me>` and the units it names; writes its heartbeat
# object and, when it starts a unit, that unit's epoch by CAS. Never writes configuration. A fresh worker
# rediscovers everything from the store. Subsystems subclass it and implement `reconcile_once`.
class Worker:
    """Runs its assignment and reports. Reads <name>/workers/<me> and the
    units it names; writes its heartbeat object and, when it starts a unit,
    that unit's epoch by CAS. Never writes configuration. A fresh worker
    rediscovers everything from the store."""

    def __init__(self, sub: Subsystem, name: str | None, vars_: Variables, objects: ObjectStore,
                 lease_ttl: float = 30.0, lease_margin: float = 5.0, clock=time.monotonic, wall=time.time,
                 instance: str | None = None, slot_ttl: float = 45.0):
        self.sub, self.vars, self.objects = sub, vars_, objects
        self.clock, self.wall = clock, wall
        self.lease_ttl, self.lease_margin = lease_ttl, lease_margin
        self.epochs: dict[str, int] = {}          # unit -> epoch this worker holds
        self.leases: dict[str, Lease] = {}
        self.instance = instance or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"   # the process; a name is a slot
        self.slot_ttl = slot_ttl
        self.slot: Slot | None = None
        self.name = name                          # None until claim_slot(); a fixed name is a slot claimed by that name

    # -- identity by claim ----------------------------------------------------------
    # Become somebody. Lists the slot rows; with `prefer` (Nomad's `NOMAD_ALLOC_INDEX`, systemd's `%i`) the
    # candidate list is just that name and it is taken by CAS even from a holder that has not lapsed — the
    # scheduler is the authority on which process is the current one, and the old holder finds out on its
    # next `renew_slot`. Without `prefer`, candidates are: lapsed slots first (oldest `until` first — their
    # assignment is waiting), then free (released or never held) slots by number, then a fresh `w-<max+1>`.
    # For each candidate, re-read, skip if not claimable (only in the no-`prefer` case), and `put` a new
    # `Slot(cand, instance, now + slot_ttl, released=False, gen+1)` with `cas=idx`; a `Conflict` means
    # someone took it between read and write, move on. Sets `self.slot` and `self.name`.
    # `test_identity_by_claim_is_a_platform_piece`: two nameless workers get `w-1` and `w-2`; after `w-1`
    # lapses a third gets `w-1` back and its assignment with it; `prefer="w-7"` creates and takes `w-7`.
    def claim_slot(self, prefer: str | None = None, retries: int = 50) -> str:
        """Become somebody. With `prefer` (whatever `runtime.slot` made of
        SLOT_INDEX or a <ROLE>_NAME) take that slot, by CAS, even from a holder
        that has not lapsed — the runtime is the authority on which process is
        the current one,
        and the old holder finds out on its next renewal. Without it, take a
        lapsed slot — its assignment is waiting — before an unused number. Holding is renewed by `renew_slot`; losing it fences the
        instance. The controller never hands names out; a process takes one."""
        prefix = self.sub.name + "/slots/"
        now = self.wall()
        for _ in range(retries):
            names = [p[len(prefix):] for p in self.vars.list(prefix)]
            known = {n: Slot.from_items(n, self.vars.get(prefix + n)[0]) for n in names}
            if prefer is not None:
                order = [prefer]
            else:
                lapsed = sorted((n for n, s in known.items() if s.lapsed(now)), key=lambda n: known[n].until)
                free = sorted((n for n, s in known.items() if s.claimable(now) and not s.lapsed(now)), key=slot_number)
                nxt = f"w-{max([slot_number(n) for n in names] + [0]) + 1}"
                order = lapsed + free + [nxt]
            for cand in order:
                items, idx = self.vars.get(prefix + cand)
                cur = Slot.from_items(cand, items)
                if prefer is None and not cur.claimable(now):
                    continue                                   # a preferred slot is taken regardless: the scheduler
                                                               # said this index is mine; the old holder fences on renewal
                new = Slot(cand, self.instance, now + self.slot_ttl, False, cur.gen + 1)
                try:
                    self.vars.put(prefix + cand, new.to_items(), cas=idx)
                except Conflict:
                    continue                                   # somebody took it between the read and the write
                self.slot, self.name = new, cand
                return cand
        raise RuntimeError(f"{self.instance}: could not claim a slot in {retries} tries")

    # Still me? Read the slot; if `holder` is another instance, return False — the instance is fenced as a
    # whole (the VMS worker stops recording on this). Otherwise extend `until` by CAS; a `Conflict` is also
    # False. A worker with no slot (fixed name without claim) returns True.
    def renew_slot(self) -> bool:
        """Still me? Read the slot; if another instance holds it now, the
        instance is fenced as a whole. Extends `until` by CAS otherwise."""
        if self.slot is None:
            return True
        items, idx = self.vars.get(self.sub.slot_key(self.name))
        cur = Slot.from_items(self.name, items)
        if cur.holder != self.instance:
            return False
        new = Slot(self.name, self.instance, self.wall() + self.slot_ttl, False, cur.gen)
        try:
            self.vars.put(self.sub.slot_key(self.name), new.to_items(), cas=idx)
        except Conflict:
            return False
        self.slot = new
        return True

    # An orderly stop (SIGTERM from the scheduler: scale-in, or a drain). Writes the row with
    # `released=True` and `until=now`, if this instance still holds it; a `Conflict` is ignored. This flag
    # is what tells scale-in from a crash: a crash says nothing and the slot merely lapses.
    def release_slot(self) -> None:
        """An orderly stop (SIGTERM from the scheduler: scale-in, or a drain).
        Says so in the row — `released` — which is what tells scale-in from a
        crash. A crash says nothing, and the slot merely lapses."""
        if self.slot is None:
            return
        items, idx = self.vars.get(self.sub.slot_key(self.name))
        cur = Slot.from_items(self.name, items)
        if cur.holder == self.instance:
            try:
                self.vars.put(self.sub.slot_key(self.name), Slot(self.name, self.instance, self.wall(), True, cur.gen).to_items(), cas=idx)
            except Conflict:
                pass
        self.slot = None

    # Reads my row.
    def assignment(self) -> Assignment:
        items, _ = self.vars.get(self.sub.assignment(self.name))
        return Assignment.from_items(self.name, items)

    # Called when the worker starts a unit: `next_epoch` on `<name>/epoch/<unit>`, record it in `epochs`,
    # and open a `Lease` on it. A second worker starting the same unit gets the next number, and the first
    # one's lease fences on renewal.
    def take_epoch(self, unit: str) -> int:
        """Called when the worker STARTS a unit: a new epoch, by CAS, and a
        lease on it. A second worker starting the same unit gets the next
        number, and the first one's lease will fence on renewal."""
        epoch, _ = next_epoch(self.vars, self.sub.epoch_key(unit))
        self.epochs[unit] = epoch
        self.leases[unit] = Lease(self.vars, self.sub.epoch_key(unit), epoch, self.lease_ttl, self.lease_margin, self.clock)
        return epoch

    # Forget the unit's epoch and lease (the worker stopped it).
    def release(self, unit: str) -> None:
        self.epochs.pop(unit, None)
        self.leases.pop(unit, None)

    # The unit's lease says so, and there is one.
    def may_write(self, unit: str) -> bool:
        lease = self.leases.get(unit)
        return lease is not None and lease.may_write()

    # Renews every lease; returns the units whose lease was lost — fenced or expired — for the subsystem to
    # stop.
    def renew_leases(self) -> list[str]:
        """Returns the units whose lease was lost — fenced or expired."""
        return [u for u, l in self.leases.items() if not l.renew()]

    # Sum of `conflicts` over all leases; goes into the heartbeat.
    def conflicts(self) -> int:
        return sum(l.conflicts for l in self.leases.values())

    # Writes `Heartbeat(name, wall(), status, extra)` to `<name>/<name>/heartbeat` in the object store. The
    # VMS passes `server`, `labels`, `capacity`, `headroom`, `conflicts`, `started`, `previous_hb`, etc. as
    # `extra`.
    def heartbeat(self, status: list[dict], **extra) -> None:
        self.objects.put(self.sub.heartbeat_key(self.name),
                         Heartbeat(self.name, self.wall(), status, extra).to_bytes())

    # what a subsystem implements
    # Abstract: what a subsystem implements (the VMS's is М9 Lesson 6's loop).
    def reconcile_once(self, now: float) -> list:
        raise NotImplementedError
