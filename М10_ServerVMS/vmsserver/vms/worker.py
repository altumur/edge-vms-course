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
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # worker.py — vmsworker: DriverPack as the worker — N pipelines against an assignment, an epoch per
# camera,
# a lease, a heartbeat with server, labels and capacity
#
# **Role in the module.** Lesson 4. One process, N pipelines, its own loop. `VmsWorker` extends
# `psimplatform.contract.Worker` (see `contract.py` for `claim_slot`, `renew_slot`, `release_slot`,
# `take_epoch`, `renew_leases`, `heartbeat`) and is the thing that knows what a camera is. It reads its
# assignment `vms/workers/<me>` and the camera rows it names, runs М9's `Reconciler` over them
# (`reconciler.py`) with an actuator that builds `driverpacksrc ! tee ! archivesink` (`gstvms/actuator.py`;
# `FakeActuator` here without GStreamer), takes an epoch per camera by CAS when it starts one, holds a lease
# per camera, writes events into the camera's bucket on this server's resource, and publishes a heartbeat
# carrying its status. It never writes configuration: its token is `vms/epoch/*` and `vms/slots/*`. Nomad or
# systemd supervises the process; the process supervises its pipelines; nothing supervises the loop, because
# the loop is the process. The docstring names what the environment hands a process: `WORKER_NAME` /
# `NOMAD_ALLOC_INDEX` (the slot preference; the CAS claim on `vms/slots/w-N` is the proof),
# `NOMAD_NODE_NAME` or the hostname (`server`: which resource it records into), `NOMAD_META_labels`
# (`labels`: what this server can reach; the controller places by them), `NOMAD_ALLOC_ID` (the instance),
# `CAPACITY` (the worker's own number, from М9 Lesson 7's probe). Run by `__main__.worker`; tested in
# `tests/test_lesson4_worker.py` and used across Lesson 5's tests.
#
# ## Module-level names
# - `log` — logger `vmsworker`.
# - `VMS` — `Subsystem("vms")`, the key layout.
#
# ### `__init__(self, name, vars_, objects, actuator=None, lease_ttl=30.0, lease_margin=5.0,
# clock=time.monotonic, wall=time.time, server=None, capacity=None, instance=None, slot_ttl=45.0,
# archive_root=None, bucket_seconds=600, env=None)` `env` defaults to `os.environ` (tests pass a dict).
# `instance` defaults to `NOMAD_ALLOC_ID`, else the base class's `hostname:pid:6hex`. Calls
# `Worker.__init__` with `name=None` and then `claim_slot(prefer=name or slot_from_environment(env))` — so
# construction *is* the claim, and `self.name` is set afterwards. Then: `archive_root` from the argument or
# `$ARCHIVE` (`/data/archive`); `capacity` from the argument or `$CAPACITY` (50) — "М9 Lesson 7's B + n·I,
# measured on ITS server"; the actuator (`FakeActuator()` if none); an empty `rows`; the `Reconciler(self,
# self._actuate)`; `recording_allowed = True`; `server` from the argument, `NOMAD_NODE_NAME`,
# `NOMAD_NODE_ID`, else the hostname; `labels`, `alloc`; the two start clocks. Finally it reads the previous
# heartbeat object of this slot name: if one exists and was written by a different instance, `previous_hb`
# is its `ts` and `previous_instance` its instance — the controller's `failover_seconds` computes `started −
# previous_hb` from these, measured from what the workers wrote.
# `test_a_replacement_without_a_name_inherits_the_lapsed_slot`: two nameless workers get `w-1`, `w-2`; after
# `w-1` lapses (46 s of wall clock) a third nameless worker gets `w-1` back and starts its two cameras with
# epoch 2.
#
# ## Notes
# - Ordering: the slot is claimed in the constructor, before any assignment is read (the name is the row
#   key); an epoch is taken in `_actuate` before the pipeline starts; `lease_pass` checks the slot before
#   the leases.
# - Recovery needs no controller: `test_restart_with_the_controller_stopped` deletes the controller, starts
#   a fresh `w-1` with an empty `actual`, and it starts all three cameras from its assignment with epoch 2
#   each — the old instance is fenced by construction.
# - Three exits from a lost lease, all in `lease_pass`: reassignment (stop that one, continue), zombie
#   (fence everything), and slot taken (fence everything, first). A lease that merely expired because the
#   loop stalled shows up as `may_write` false in `_actuate` and a fresh epoch on the next start.
# - `observe` returns the bucket path, which the tests read back with `read_bucket`; the worker keeps
#   `observed` only for tests and diagnostics.
# ================================================================================================
from __future__ import annotations

import logging
import os
import socket
import time

from psimplatform.contract import Subsystem, Worker
from psimplatform.objects import ObjectStore
from psimplatform.variables import Variables

from .archive import event_log
from .config import row
from .reconciler import CONVERGED, Reconciler

log = logging.getLogger("vmsworker")
VMS = Subsystem("vms")


# М9 Lesson 6's `print()` with a memory — the actuator without GStreamer, and the one `__main__` falls back
# to. It mirrors `GstActuator`'s surface: callable, `pump()`, `stop_all()`, plus test hooks. State:
# `failing` (a set of camera ids, or a predicate, whose start fails), `calls` (every `(verb, id)`),
# `running` (ids started and not stopped), `epochs` (`{id: epoch}` as passed in the row at start), `dead`
# and `posted` (what tests push in to simulate a bus).
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

    # Records the call. `stop` always succeeds and removes the id. A start/restart on a failing id fails
    # (and clears `running`); otherwise the id is running and `cam["epoch"]` is remembered — the tests read
    # `act.epochs` to see which epoch the worker handed the pipeline.
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

    # Returns and clears `dead` and `posted`; dead ids leave `running`. Same contract as `GstActuator.pump`.
    def pump(self) -> tuple[list[int], list[tuple[int, str, dict]]]:
        """(dead, posted). Tests push into `dead` and `posted` directly."""
        dead, self.dead = self.dead, []
        posted, self.posted = self.posted, []
        for cid in dead:
            self.running.discard(cid)
        return dead, posted

    # What an element would post on the bus: appends to `posted`.
    def post(self, cid: int, kind: str, **fields) -> None:
        """What an element would post on the bus."""
        self.posted.append((cid, kind, fields))

    # Clears `running`.
    def stop_all(self) -> None:
        self.running.clear()


# The live branch's RTP port for a camera on its worker's server: deterministic, so a gateway needs only the
# heartbeat (server + this) to subscribe, and nobody keeps a port table.
def live_port(cid: int) -> int:
    from .config import LIVE_PORT_BASE
    return LIVE_PORT_BASE + int(cid)


# `WORKER_NAME` if set; else `w-<NOMAD_ALLOC_INDEX>`; else `None` — claim whatever is free, a lapsed slot
# first.
def slot_from_environment(env: dict) -> str | None:
    if env.get("WORKER_NAME"):
        return env["WORKER_NAME"]
    if "NOMAD_ALLOC_INDEX" in env:
        return f"w-{int(env['NOMAD_ALLOC_INDEX'])}"
    return None                                  # claim whatever is free — a lapsed slot first


# `NOMAD_META_labels` split on commas, empties dropped.
def labels_from_environment(env: dict) -> list[str]:
    return [l for l in env.get("NOMAD_META_labels", "").split(",") if l]


# `name` is a slot. Given (systemd's `%i`, Nomad's alloc index) it is claimed by that name — taken outright,
# even from a holder that has not lapsed, because the scheduler is the authority on which process is
# current; `None` means the environment's, and failing that "whichever slot is free" — a lapsed one first,
# so a replacement inherits its assignment. "A worker on a cluster is a worker on a box whose stores happen
# to be raft: same class, same heartbeat." It is also the `Store` of its own `Reconciler` (`desired()`).
#
# State beyond the base class: `archive_root` (this server's resource), `bucket_seconds`, `observed` (every
# `(cid, t, kind)` this instance wrote), `capacity`, `actuator`, `rows` (the assignment's camera rows,
# refreshed each pass), `assignment_rev`, `reconciler`, `recording_allowed` / `fenced_reason` (the
# instance-wide fence), `server`, `labels`, `alloc`, `started_at` (monotonic) and `_started_wall`, `passes`,
# `previous_hb` / `previous_instance` (what failover is measured from).
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
            from psimplatform.contract import Heartbeat
            old = Heartbeat.from_bytes(raw)
            if old.extra.get("instance") != self.instance:
                self.previous_hb, self.previous_instance = old.ts, old.extra.get("instance", "")

    # -- the store, as the reconciler sees it ------------------------------------
    # The reconciler's store: `self.rows`.
    def desired(self) -> list[dict]:
        return self.rows

    # Read the assignment (`assignment_rev` kept for the heartbeat) and, for each unit it names, the row
    # `vms/cameras/<id>`; rows that are missing or marked `deleted: "true"` are skipped. "A fresh worker
    # knows nothing and reads everything; nothing about what is running is stored." An unassigned worker has
    # no rows and invents nothing (`test_worker_runs_its_assignment…`: the first `reconcile_once` is `[]`).
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
    # The gate between the reconciler and the real actuator. For `start`/`restart`: refuse if the instance
    # is fenced (`recording_allowed` false); on `start`, or if no epoch is held for the unit,
    # `take_epoch(unit)` — a new epoch for a new writer — else reuse the held epoch (an edit's restart keeps
    # epoch 1); refuse if `may_write(unit)` is false (no lease, or a lost one); then call the actuator with
    # `epoch` added to the row — the number archivesink puts in every path. For `stop`: call the actuator
    # and `release(unit)` (forget epoch and lease). `test_lease_expiry_without_renewal_stops_starts`: after
    # 26 s without renewal `may_write` is false; a later start takes epoch 2.
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
            cam = dict(cam, live_port=live_port(cam["id"]))     # the tee's live branch: RTP to the loopback, fire-and-forget
            return self.actuator(verb, cam)
        ok = self.actuator("stop", cam)
        self.release(unit)
        return ok

    # Seconds since start on the monotonic clock — the reconciler's `now` for backoff.
    def now(self) -> float:
        return self.clock() - self.started_at

    # -- the passes ---------------------------------------------------------------
    # One pass: `refresh`, `reconciler.reconcile(now)`, count the pass, log each action. The base class's
    # abstract method; called by `run` and directly by every test.
    def reconcile_once(self, now: float | None = None) -> list[tuple[str, int]]:
        self.refresh()
        actions = self.reconciler.reconcile(self.now() if now is None else now)
        self.passes += 1
        for verb, cid in actions:
            log.info("%s: %s camera %s", self.name, verb, cid)
        return actions

    # Renew the slot and every lease, and decide what a lost lease means. First `renew_slot()`: if the slot
    # is held by another instance now, `fence("slot w-N is held by another instance now")` and return every
    # held unit — the zombie is fenced at the slot *before* any epoch is looked at
    # (`test_the_zombie_is_fenced_at_the_slot_first`: the replacement's `slot.gen == 2`). Then
    # `renew_leases()`; for each lost unit: if it is no longer in my assignment this is a reassignment —
    # stop the pipeline, drop it from `reconciler.actual`, `release` it, and carry on recording the rest
    # (`test_a_reassignment_is_not_a_zombie`: released, not fenced, `recording_allowed` still true); if it
    # *is* still mine, another instance of me took the epoch — I am the zombie — `fence` and stop looking.
    # Returns the lost units. `test_the_zombie_on_one_box`: A's `lease_pass` returns `["1"]`, A is fenced
    # with "slot w-1" in the reason, its epoch lease also reports a conflict, and it may start nothing
    # (`("failed", 1)`); B is fine.
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

    # Once: log at error, set `recording_allowed = False` and `fenced_reason`, `actuator.stop_all()`,
    # `reconciler.clear()` — the pipelines were stopped underneath the loop. Idempotent (a second call
    # returns immediately). After this the heartbeat says `fenced: true`, `_actuate` refuses every start,
    # and `observe` writes nothing.
    def fence(self, why: str) -> None:
        if not self.recording_allowed:
            return
        log.error("%s: FENCED (%s). Stopping every pipeline.", self.name, why)
        self.recording_allowed, self.fenced_reason = False, why
        self.actuator.stop_all()
        self.reconciler.clear()

    # An event: written by this worker, now (`wall()`), into the camera's bucket on this server's resource
    # under the epoch this worker holds for it — recording or not. `None` if no epoch is held for the camera
    # (not mine to observe) or the instance is fenced. Records `(cid, t, kind)` in `observed` and returns
    # the bucket path from `event_log(archive_root, cid, epoch, bucket_seconds).append(...)`. Nothing else
    # is told — no store write, no controller. `test_the_worker_observes_what_it_holds_recording_or_not`:
    # before the first reconcile `observe(1, ...)` is `None`; after it the line lands in
    # `<archive>/vms/1/e1/…`; camera 2 (not assigned) is `None`; after `fence` a post is dropped;
    # `vms/events` in the store stays empty.
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

    # The bus, drained: `actuator.pump()` gives `(dead, posted)`; every posted `(cid, kind, fields)` becomes
    # `observe(...)` — a line only if I still hold the epoch; every dead camera becomes
    # `reconciler.lost(cid, now)` (restart after backoff) plus `observe(cid, "silent")` — "the event with no
    # segment open, by definition".
    def pump_once(self) -> None:
        """The bus, drained: what elements posted becomes events — if I still
        hold the epoch — and what died becomes `lost` and a `silent` event."""
        dead, posted = self.actuator.pump()
        for cid, kind, fields in posted:
            self.observe(cid, kind, **fields)
        for cid in dead:
            self.reconciler.lost(cid, self.now())
            self.observe(cid, "silent")                 # the event with no segment open, by definition

    # The read model, per assigned row: `id`, `ref`, `name`, `enabled`, `phase` (`running` if in
    # `reconciler.actual`; `pending` if disabled; `failed` if in `reconciler.failures`; else `pending`),
    # `position` (`converged | lagging | stalled`), `revision`, `observed_revision` (what is actually
    # running), `epoch` (held, or 0). This list is the heartbeat's `status`; the controller's `read_model`
    # and the console's `/cameras` show it, and `/metrics` counts `phase == running` into
    # `vms_cameras_recording`.
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
                        "epoch": self.epochs.get(str(cid), 0), "live_port": live_port(cid)})   # where a gateway subscribes — never a viewer
        return out

    # `max(0, capacity − len(rows))`: cameras this worker could still take. "Not CPU — a worker at 40 % CPU
    # with no assignment left is full." What the autoscaler reads via the controller's `headroom()` and
    # `/metrics`.
    def headroom(self) -> int:
        """What the autoscaler reads: cameras this worker could still take.
        Not CPU — a worker at 40 % CPU with no assignment left is full."""
        return max(0, self.capacity - len(self.rows))

    # `Worker.heartbeat(status, …)` to the object `vms/<name>/heartbeat` with the extras the platform reads
    # by name: `server`, `instance`, `alloc`, `labels` (comma-joined), `assignment_rev`, `fenced`,
    # `conflicts`, `passes`, `capacity`, `headroom`, `started`, `previous_hb`, `previous_instance`. The
    # controller's `capacity_of`, `labels_of`, `server_of`, `headroom`, `failover_seconds` and the console's
    # metrics all read from here.
    def heartbeat_once(self) -> None:
        self.heartbeat(self.status(), server=self.server, instance=self.instance, alloc=self.alloc,
                       labels=",".join(self.labels), assignment_rev=self.assignment_rev,
                       fenced=not self.recording_allowed, conflicts=self.conflicts(), passes=self.passes,
                       capacity=self.capacity, headroom=self.headroom(), started=self._started_wall,
                       previous_hb=self.previous_hb, previous_instance=self.previous_instance)

    # The loop as a process. Every `poll` seconds: `reconcile_once`, `pump_once`, `lease_pass` every `max(1,
    # (lease_ttl − lease_margin)/3)` s (≈8.3 s by default, well inside the 25 s the lease allows),
    # `heartbeat_once` every 10 s; any exception is logged and the loop continues. On `stop`:
    # `actuator.stop_all()` (with GStreamer, EOS lets each splitmuxsink finalize its open segment —
    # `vmsworker@.container` gives it `StopTimeout=20`), a last heartbeat, then `release_slot()` — "an
    # orderly stop says so; a crash says nothing", which is what lets the controller tell scale-in
    # (redistribute) from a crash (leave it to the scheduler).
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
