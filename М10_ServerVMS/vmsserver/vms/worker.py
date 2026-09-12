"""vmsworker — DriverPack as the worker.

One process, N pipelines, its own loop. It reads its assignment
(vms/workers/<me>) and the camera rows it names, runs М9's reconcile loop
over them with the actuator that builds `driverpacksrc ! tee ! archivesink`,
takes an epoch per camera by CAS when it starts one, holds a lease per
camera, and publishes a heartbeat carrying its status. It never writes
configuration. Nomad (or systemd, on one box) supervises the process; the
process supervises its pipelines; nothing supervises the loop, because the
loop is the process.

What the environment hands a process, on a box or in an allocation:

    WORKER_NAME / NOMAD_ALLOC_INDEX  -> the slot to claim: w-<index>. The index is the preference;
                                        the claim (CAS on vms/slots/w-N) is the proof
    NOMAD_NODE_NAME (or the hostname) -> `server` in the heartbeat: which resource it records into
    NOMAD_META_labels                 -> `labels` in the heartbeat: what this server can reach; the controller places by them
    NOMAD_ALLOC_ID                    -> the instance; CAPACITY -> the worker's own number, from М9 Lesson 7's probe
"""
from __future__ import annotations

import logging
import os
import socket
import time

from vmsplatform.contract import Subsystem, Worker
from vmsplatform.objects import ObjectStore
from vmsplatform.variables import Variables

from .archive import event_log
from .config import row
from .reconciler import CONVERGED, Reconciler

log = logging.getLogger("vmsworker")
VMS = Subsystem("vms")


class FakeActuator:
    """М9 Lesson 6's print(), with a memory. `failing` is a set of camera ids
    (or a predicate) whose start fails."""

    def __init__(self, failing=frozenset()):
        self.failing = failing
        self.calls: list[tuple[str, int]] = []
        self.running: set[int] = set()
        self.epochs: dict[int, int] = {}
        self.dead: list[int] = []
        self.posted: list[tuple[int, str, dict]] = []

    def __call__(self, verb: str, cam: dict) -> bool:
        cid = cam["id"]
        self.calls.append((verb, cid))
        if verb == "stop":
            self.running.discard(cid)
            return True
        fails = self.failing(cid) if callable(self.failing) else cid in self.failing
        if fails:
            self.running.discard(cid)
            return False
        self.running.add(cid)
        self.epochs[cid] = cam.get("epoch", 0)
        return True

    def pump(self) -> tuple[list[int], list[tuple[int, str, dict]]]:
        """(dead, posted). Tests push into `dead` and `posted` directly."""
        dead, self.dead = self.dead, []
        posted, self.posted = self.posted, []
        for cid in dead:
            self.running.discard(cid)
        return dead, posted

    def post(self, cid: int, kind: str, **fields) -> None:
        """What an element would post on the bus."""
        self.posted.append((cid, kind, fields))

    def stop_all(self) -> None:
        self.running.clear()


def slot_from_environment(env: dict) -> str | None:
    if env.get("WORKER_NAME"):
        return env["WORKER_NAME"]
    if "NOMAD_ALLOC_INDEX" in env:
        return f"w-{int(env['NOMAD_ALLOC_INDEX'])}"
    return None                                  # claim whatever is free — a lapsed slot first


def labels_from_environment(env: dict) -> list[str]:
    return [l for l in env.get("NOMAD_META_labels", "").split(",") if l]


class VmsWorker(Worker):
    """`name` is a slot. Given (systemd's %i, Nomad's alloc index) it is
    claimed by that name; None means the environment's, and failing that
    "whichever slot is free" — a lapsed one first, so a replacement
    inherits its assignment. A worker on a cluster is a worker on a box
    whose stores happen to be raft: same class, same heartbeat."""

    def __init__(self, name: str | None, vars_: Variables, objects: ObjectStore, actuator=None,
                 lease_ttl: float = 30.0, lease_margin: float = 5.0, clock=time.monotonic, wall=time.time,
                 server: str | None = None, capacity: int | None = None, instance: str | None = None, slot_ttl: float = 45.0,
                 archive_root: str | None = None, bucket_seconds: int = 600, env: dict | None = None):
        env = dict(os.environ if env is None else env)
        instance = instance or env.get("NOMAD_ALLOC_ID") or None
        super().__init__(VMS, None, vars_, objects, lease_ttl, lease_margin, clock, wall, instance, slot_ttl)
        self.claim_slot(prefer=name if name is not None else slot_from_environment(env))
        self.archive_root = archive_root or env.get("ARCHIVE", "/data/archive")   # this server's resource
        self.bucket_seconds = bucket_seconds
        self.observed: list[tuple[int, float, str]] = []
        self.capacity = capacity if capacity is not None else int(env.get("CAPACITY", "50"))   # М9 Lesson 7's B + n·I, measured on ITS server
        self.actuator = actuator or FakeActuator()
        self.rows: list[dict] = []
        self.assignment_rev = 0
        self.reconciler = Reconciler(self, self._actuate)
        self.recording_allowed = True
        self.fenced_reason: str | None = None
        self.server = server or env.get("NOMAD_NODE_NAME") or env.get("NOMAD_NODE_ID") or socket.gethostname()
        self.labels = labels_from_environment(env)
        self.alloc = env.get("NOMAD_ALLOC_ID", "")
        self.started_at = clock()
        self._started_wall = self.wall()
        self.passes = 0
        # the previous instance of this slot, if it left a heartbeat: what failover is measured from
        self.previous_hb, self.previous_instance = 0.0, ""
        raw = objects.get(self.sub.heartbeat_key(self.name))
        if raw:
            from vmsplatform.contract import Heartbeat
            old = Heartbeat.from_bytes(raw)
            if old.extra.get("instance") != self.instance:
                self.previous_hb, self.previous_instance = old.ts, old.extra.get("instance", "")

    # -- the store, as the reconciler sees it ------------------------------------
    def desired(self) -> list[dict]:
        return self.rows

    def refresh(self) -> None:
        """Read the assignment and the rows it names. A fresh worker knows
        nothing and reads everything; nothing about what is running is stored."""
        a = self.assignment()
        self.assignment_rev = a.rev
        rows = []
        for unit in a.units:
            items, _ = self.vars.get(VMS.config("cameras", unit))
            if items and items.get("deleted") != "true":
                rows.append(row(items))
        self.rows = rows

    # -- the gate ---------------------------------------------------------------
    def _actuate(self, verb: str, cam: dict) -> bool:
        unit = str(cam["id"])
        if verb in ("start", "restart"):
            if not self.recording_allowed:
                return False
            if verb == "start" or unit not in self.epochs:
                cam = dict(cam, epoch=self.take_epoch(unit))       # a new epoch for a new writer
            else:
                cam = dict(cam, epoch=self.epochs[unit])
            if not self.may_write(unit):
                return False
            return self.actuator(verb, cam)
        ok = self.actuator("stop", cam)
        self.release(unit)
        return ok

    def now(self) -> float:
        return self.clock() - self.started_at

    # -- the passes ---------------------------------------------------------------
    def reconcile_once(self, now: float | None = None) -> list[tuple[str, int]]:
        self.refresh()
        actions = self.reconciler.reconcile(self.now() if now is None else now)
        self.passes += 1
        for verb, cid in actions:
            log.info("%s: %s camera %s", self.name, verb, cid)
        return actions

    def lease_pass(self) -> list[str]:
        """Renew every lease. A lost lease on a camera that is no longer
        assigned to me is a reassignment: let it go. A lost lease on a camera
        that IS still mine means another instance of ME took it: I am the
        zombie, and the whole instance fences."""
        if not self.renew_slot():
            self.fence(f"slot {self.name} is held by another instance now")
            return list(self.epochs)
        lost = self.renew_leases()
        if not lost:
            return []
        assigned = set(self.assignment().units)
        for unit in lost:
            if unit not in assigned:
                self.actuator("stop", {"id": int(unit)})
                self.reconciler.actual.pop(int(unit), None)
                self.release(unit)
            else:
                self.fence(f"camera {unit}: a newer epoch was issued to another instance of {self.name}")
                break
        return lost

    def fence(self, why: str) -> None:
        if not self.recording_allowed:
            return
        log.error("%s: FENCED (%s). Stopping every pipeline.", self.name, why)
        self.recording_allowed, self.fenced_reason = False, why
        self.actuator.stop_all()
        self.reconciler.clear()

    def observe(self, cid: int, kind: str, **fields) -> str | None:
        """An event: written by this worker, now, into the camera's bucket on
        this server's resource, under the epoch this worker holds for it —
        recording or not. A camera it holds no epoch for is not its to
        observe. Nothing else is told."""
        epoch = self.epochs.get(str(cid))
        if epoch is None or not self.recording_allowed:
            return None
        t = self.wall()
        self.observed.append((cid, t, kind))
        return event_log(self.archive_root, cid, epoch, self.bucket_seconds).append(t, kind, **fields)

    def pump_once(self) -> None:
        """The bus, drained: what elements posted becomes events — if I still
        hold the epoch — and what died becomes `lost` and a `silent` event."""
        dead, posted = self.actuator.pump()
        for cid, kind, fields in posted:
            self.observe(cid, kind, **fields)
        for cid in dead:
            self.reconciler.lost(cid, self.now())
            self.observe(cid, "silent")                 # the event with no segment open, by definition

    def status(self) -> list[dict]:
        st = self.reconciler.status()
        out = []
        for cam in self.rows:
            cid = cam["id"]
            pos, lag = st.get(cid, (CONVERGED, 0))
            phase = "running" if cid in self.reconciler.actual else ("pending" if not cam["enabled"] else
                                                                     ("failed" if cid in self.reconciler.failures else "pending"))
            out.append({"id": cid, "ref": cam.get("ref", ""), "name": cam["name"], "enabled": cam["enabled"], "phase": phase, "position": pos,
                        "revision": cam["revision"], "observed_revision": self.reconciler.actual.get(cid, {}).get("revision", 0),
                        "epoch": self.epochs.get(str(cid), 0)})
        return out

    def headroom(self) -> int:
        """What the autoscaler reads: cameras this worker could still take.
        Not CPU — a worker at 40 % CPU with no assignment left is full."""
        return max(0, self.capacity - len(self.rows))

    def heartbeat_once(self) -> None:
        self.heartbeat(self.status(), server=self.server, instance=self.instance, alloc=self.alloc,
                       labels=",".join(self.labels), assignment_rev=self.assignment_rev,
                       fenced=not self.recording_allowed, conflicts=self.conflicts(), passes=self.passes,
                       capacity=self.capacity, headroom=self.headroom(), started=self._started_wall,
                       previous_hb=self.previous_hb, previous_instance=self.previous_instance)

    def run(self, poll: float = 2.0, stop=None) -> None:
        """One box: the loop as a process. Nomad or systemd restarts it."""
        import threading
        stop = stop or threading.Event()
        lease_every = max(1.0, (self.lease_ttl - self.lease_margin) / 3)
        last_lease = last_hb = 0.0
        while not stop.is_set():
            try:
                self.reconcile_once()
                self.pump_once()
                if self.clock() - last_lease >= lease_every:
                    self.lease_pass(); last_lease = self.clock()
                if self.clock() - last_hb >= 10.0:
                    self.heartbeat_once(); last_hb = self.clock()
            except Exception:                              # noqa: BLE001
                log.exception("%s: pass failed; will retry", self.name)
            stop.wait(poll)
        self.actuator.stop_all()
        self.heartbeat_once()
        self.release_slot()                           # an orderly stop says so; a crash says nothing
