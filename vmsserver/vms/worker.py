"""vmsworker — DriverPack as the worker: the process that HOLDS a camera.

One process, N pipelines, its own loop. It reads its assignment
(vms/workers/<me>) and the camera rows it names, runs М9's reconcile loop
over them with the actuator that builds `driverpacksrc ! tee` and serves the
tee as an RTSP fan-out (`rtsp://<server>:8554/<cam>`, in the heartbeat as
`live_url`) — one connection to the camera, N subscribers: the recorder
(the fourth subsystem, on a server with an archive), live gateways,
detectors, on any server. It takes an epoch per camera by CAS when it
starts one, holds a lease per camera, writes what it observes into the
camera's bucket on ITS server's resource, and publishes a heartbeat
carrying its status. It records nothing: recording is the recorder's
(`recorder.py`), a subscriber like any other. It never writes
configuration. Nomad (or systemd, on one box) supervises the process; the
process supervises its pipelines; nothing supervises the loop, because the
loop is the process.

What the environment hands a process, on a box or in an allocation:

    WORKER_NAME / SLOT_INDEX     -> the slot to claim: w-<index>. The index is the preference;
                                    the claim (CAS on vms/slots/w-N) is the proof
    SERVER_NAME (or the hostname) -> `server` in the heartbeat: whose resource its events go to, and the host in `live_url`
    LABELS                        -> `labels` in the heartbeat: what this server can reach; the controller places by them
    INSTANCE_ID                   -> the instance; CAPACITY -> the worker's own number, from М9 Lesson 7's probe

Not one of those names an orchestrator, and that is deliberate: a Quadlet, a
Nomad jobspec and a Kubernetes manifest each map their own names into these
five (`w2cplatform/runtime.py`), and the loop never learns which did.
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # worker.py — vmsworker: DriverPack as the worker — N pipelines against an assignment, an epoch per
# camera,
# a lease, a heartbeat with server, labels and capacity
#
# **Role in the module.** Lesson 4. One process, N pipelines, its own loop. `VmsWorker` extends
# `w2cplatform.contract.Worker` (see `contract.py` for `claim_slot`, `renew_slot`, `release_slot`,
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
# `tests/test_lesson4_worker.py` and used across Lesson 6's tests.
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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from w2cplatform import runtime
from w2cplatform.console import SendMixin
from w2cplatform.contract import Subsystem, Worker
from w2cplatform.objects import ObjectStore
from w2cplatform.variables import Variables

from w2cplatform.events import ALARM, OBSERVATION, EventLog, Suppressor

from .config import (PLAYBACK_PORT, RTSP_PORT, SHM_DIR, SPEC, channel_of, device_of, live_shm, live_url,
                     playback_url, port_of, row)
from .reconciler import CONVERGED, Reconciler

log = logging.getLogger("vmsworker")
VMS = Subsystem("vms")


# М9 Lesson 6's `print()` with a memory — the actuator without GStreamer, and the one `__main__` falls back
# to. It mirrors `GstActuator`'s surface: callable, `pump()`, `stop_all()`, plus test hooks. State:
# `failing` (a set of camera ids, or a predicate, whose start fails), `calls` (every `(verb, id)`),
# `running` (ids started and not stopped), `epochs` (`{id: epoch}` as passed in the row at start), `dead`
# and `posted` (what tests push in to simulate a bus).
# What DriverPack connects to, without DriverPack: N channels, an archive of its own, a session budget.
# A camera with an SD card is a device with one channel; an NVR is a device with thirty-two. The real one
# is a DriverPack session; this one is what the tests hold.
class FakeDevice:
    """A held device: its channels, its own footage, and how many playbacks it allows at once."""

    def __init__(self, key: str, channels=(), coverage=None, max_playbacks: int = 2, bps: int = 1, index=None,
                 rays: int = 0, relays: int = 0, ptz: bool = False):
        self.key, self._channels = key, [str(c) for c in channels]
        self._coverage = {str(k): v for k, v in (coverage or {}).items()}   # camera -> (from, to[, fragments])
        self._index = {str(k): [(float(a), float(b)) for a, b in v] for k, v in (index or {}).items()}
        self.max_playbacks, self.bps = max_playbacks, bps
        self.open: dict[str, tuple] = {}
        self.fetched: list[tuple] = []
        self.rays, self.relays, self.ptz = rays, relays, ptz     # what the `.rep` file would have said
        self.did: list[tuple] = []                               # every command performed, in order

    def channels(self) -> list[str]:
        return list(self._channels)

    # What the device can DO, beside what it can show. Two of the eleven part-kinds DriverPack describes
    # (`ioDevice` with its `rayCount`/`relayCount`, and `telemetry`), because those are the two an
    # operator points at: open the door, look at the gate. Both are momentary — there is nothing to hold
    # open, nothing to fence over time — which is why they are requests and not units.
    def capabilities(self) -> dict:
        return {"rays": self.rays, "relays": self.relays, "ptz": self.ptz}

    def output(self, port: int, state: str, ms: int = 0) -> None:
        if not 1 <= int(port) <= self.relays:
            raise ValueError(f"{self.key} has {self.relays} relay(s), not {port}")
        self.did.append(("output", int(port), state, int(ms)))

    def preset(self, n: int) -> None:
        if not self.ptz:
            raise ValueError(f"{self.key} has no telemetry")
        self.did.append(("preset", int(n), "", 0))

    # The summary the holder puts in its heartbeat — not the index. Drawing a timeline must not
    # cost a session, and on a device that allows two of them, it must not cost a request either.
    def coverage(self, cam) -> dict | None:
        c = self._coverage.get(str(cam))
        return None if c is None else {"from": c[0], "to": c[1], "fragments": c[2] if len(c) > 2 else 0}

    # The INDEX: what the device actually holds, span by span. A card recording continuously has one span
    # and its summary says everything; an NVR recording on motion has hundreds, and between its `from` and
    # its `to` there is mostly nothing. Without this a scan is promised minutes that do not exist.
    #
    # Absent here means "this driver cannot list" — not "the device holds nothing". The two are different
    # answers and the caller has to be able to tell them apart, so it is `None` rather than `[]`.
    def recordings(self, cam, t0: float, t1: float) -> list[tuple[float, float]] | None:
        spans = self._index.get(str(cam))
        if spans is None:
            return None
        return [(max(a, t0), min(b, t1)) for a, b in spans if b > t0 and a < t1]

    def in_use(self) -> int:
        return len(self.open)

    def open_playback(self, cam, t0: float, t1: float) -> str:
        if len(self.open) >= self.max_playbacks:
            raise OverflowError(f"device {self.key}: {self.max_playbacks} playback sessions, all in use")
        sid = f"{cam}:{t0}:{len(self.fetched)}:{len(self.open)}"
        self.open[sid] = (str(cam), t0, t1)
        return sid

    def read(self, sid: str) -> bytes:
        cam, t0, t1 = self.open[sid]
        self.fetched.append((cam, t0, t1))
        return b"\x00" * max(1, int((t1 - t0) * self.bps))

    def close_playback(self, sid: str) -> None:
        self.open.pop(sid, None)

    def close(self) -> None:
        self.open.clear()


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
        self.fetched: list[tuple] = []                   # what `record_range` was asked for
        self.started: dict[int, dict] = {}

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
        self.started[cid] = cam                     # what the pipeline was built from: the row plus what `enrich` added
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
    # Fetch a range out of a device's own archive and write it as segments in the spool, the way a live
    # recording is written — the only difference is where the bytes came from. The real one is a pipeline on
    # the holder's playback door; this one writes the files so the ordering and the manifest can be tested.
    def record_range(self, cam, source: str, epoch: int, t0: float, t1: float, spool: str, seg: int = 600) -> list[str]:
        import os
        from datetime import datetime, timezone
        from .archive import segment_path
        out = []
        t = t0
        while t < t1:
            end = min(t + seg, t1)
            p = segment_path(spool, str(cam), epoch, datetime.fromtimestamp(t, timezone.utc).replace(microsecond=0))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(b"\x00" * 16)
            os.utime(p, (end, end))                      # the segment ends where it ends: `promote` reads mtime
            out.append(p); self.fetched.append((str(cam), t, end, source))
            t = end
        return out

    def stop_all(self) -> None:
        self.running.clear()


# The live branch's RTP port for a camera on its worker's server: deterministic, so a gateway needs only the
# heartbeat (server + this) to subscribe, and nobody keeps a port table.
def live_port(cid) -> int:
    from .config import LIVE_PORT_BASE
    try:                                         # a port is a number, and this is the only place an id must be one:
        return LIVE_PORT_BASE + int(cid)         # a named unit gets the base port and the fan-out has none to publish
    except ValueError:
        return LIVE_PORT_BASE


# `WORKER_NAME` if set; else `w-<NOMAD_ALLOC_INDEX>`; else `None` — claim whatever is free, a lapsed slot
# first. The recorder uses the same rule with `RECORDER_NAME` and `r-`.
def slot_from_environment(env: dict, name_env: str = "WORKER_NAME", prefix: str = "w") -> str | None:
    return runtime.slot(env, name_env, prefix)   # None: claim whatever is free — a lapsed slot first


# `LABELS` split on commas, empties dropped — what this server can reach, as the runtime said it.
def labels_from_environment(env: dict) -> list[str]:
    return runtime.labels(env)


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
    whose stores happen to be raft: same class, same heartbeat. The
    recorder (`recorder.py`) is this class over another subsystem's rows."""

    SUB = VMS                       # the subsystem whose assignment and rows this worker runs
    ROWS = "cameras"                # <sub>/<ROWS>/<id>
    SLOT_PREFIX, NAME_ENV = "w", "WORKER_NAME"
    parse_row = staticmethod(row)

    def __init__(self, name: str | None, vars_: Variables, objects: ObjectStore, actuator=None,
                 lease_ttl: float = 30.0, lease_margin: float = 5.0, clock=time.monotonic, wall=time.time,
                 server: str | None = None, capacity: int | None = None, instance: str | None = None, slot_ttl: float = 45.0,
                 archive_root: str | None = None, bucket_seconds: int = 600, env: dict | None = None,
                 device_factory=None):
        env = dict(os.environ if env is None else env)
        instance = instance or runtime.instance(env)
        super().__init__(self.SUB, None, vars_, objects, lease_ttl, lease_margin, clock, wall, instance, slot_ttl)
        self.claim_slot(prefer=name if name is not None else slot_from_environment(env, self.NAME_ENV, self.SLOT_PREFIX))
        self.archive_root = archive_root or env.get("ARCHIVE", "/data/archive")   # this server's resource: where its events go
        self.shm_dir = env.get("SHM_DIR", SHM_DIR)                                 # the tee's shared-memory branch, for subscribers on this server
        # THIS instance's two doors. Defaults are what they always were, so a box with one worker is
        # unchanged; `auto` asks the OS, which is what makes a SECOND worker on the same box possible at
        # all. Both numbers are published, never assumed: a subscriber reads the address out of the
        # heartbeat (`live_url`, `playback_url`), and it has done since Lesson 4.
        self.rtsp_port = port_of(env.get("RTSP_PORT"), RTSP_PORT)
        self.playback_port = port_of(env.get("PLAYBACK_PORT"), PLAYBACK_PORT)
        self.bucket_seconds = bucket_seconds
        # What this subsystem declared about repeats (`events.suppress`), held for as long as this worker
        # holds its units. The counters live HERE and nowhere else: this process is the only one that sees
        # the stream before it is a file, and the only one holding the epoch that makes the file writable.
        self.suppressor = Suppressor(SPEC.suppress)
        self.observed: list[tuple[int, float, str]] = []
        # Request ids this worker has served — performed, refused or expired, all three being answers.
        # The heartbeat carries them and the controller's `clear_requests` removes the rows: a worker
        # writes no configuration, so it cannot delete what it has done, only say that it did it.
        self.fetched: list[str] = []
        self.capacity = capacity if capacity is not None else int(env.get("CAPACITY", "50"))   # М9 Lesson 7's B + n·I, measured on ITS server
        self.actuator = actuator or FakeActuator()
        self.rows: list[dict] = []
        # One session per DEVICE, not per channel. `device_factory(key)` opens it — a DriverPack session on
        # a box, a `FakeDevice` in the tests, `None` for a source with no archive of its own (a file).
        self.device_factory = device_factory or (lambda key: None)
        self.devices: dict[str, object] = {}
        self.assignment_rev = 0
        self.reconciler = Reconciler(self, self._actuate)
        self.recording_allowed = True
        self.fenced_reason: str | None = None
        self.server = runtime.server(env, server)
        self.labels = labels_from_environment(env)
        self.alloc = runtime.instance(env) or ""          # published as `alloc` for the readers that already know that name
        self.started_at = clock()
        self._started_wall = self.wall()
        self.passes = 0
        # the previous instance of this slot, if it left a heartbeat: what failover is measured from
        self.previous_hb, self.previous_instance = 0.0, ""
        raw = objects.get(self.sub.heartbeat_key(self.name))
        if raw:
            from w2cplatform.contract import Heartbeat
            old = Heartbeat.from_bytes(raw)
            if old.extra.get("instance") != self.instance:
                self.previous_hb, self.previous_instance = old.ts, old.extra.get("instance", "")

    # -- the store, as the reconciler sees it ------------------------------------
    # The reconciler's store: `self.rows`.
    def desired(self) -> list[dict]:
        """What the reconciler runs. A row with `live: on-demand` is NOT here: its device is
        still held (see `_refresh_devices`) and its archive still served, but no pipeline is
        built for it. Holding the device is what the row buys; the live fan-out is what `live`
        asks for."""
        return [r for r in self.rows if r.get("live", "always") != "on-demand"]

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
            items, _ = self.vars.get(self.SUB.config(self.ROWS, unit))
            if items and items.get("deleted") != "true":
                rows.append(self.parse_row(items))
        self.rows = rows
        self._refresh_devices()

    # One connection per device, however many of its channels are assigned: an NVR with thirty-two cameras
    # is one session, not thirty-two — the same argument as "one connection to the camera" (Lesson 4), a
    # level up. A device no row names any more is closed.
    def _refresh_devices(self) -> None:
        want = {device_of(r["source"]) for r in self.rows if r.get("source")}   # a recorder's rows name none
        for key in want - set(self.devices):
            dev = self.device_factory(key)
            if dev is not None:
                self.devices[key] = dev
        for key in set(self.devices) - want:
            dev = self.devices.pop(key)
            if hasattr(dev, "close"):
                dev.close()

    # The device a camera is a channel of, and the device object if it is held.
    def device_of_row(self, cam: dict):
        return self.devices.get(device_of(cam["source"])) if cam.get("source") else None

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
            cam = self.enrich(cam)                              # what the pipeline needs beyond the row: the fan-out here, the source for a recorder
            if cam is None:
                return False                                    # not startable now (a recorder whose camera nobody holds): the reconciler retries
            return self.actuator(verb, cam)
        ok = self.actuator("stop", cam)
        self.release(unit)
        return ok

    # What the pipeline needs beyond the row. The worker's tee: its RTSP fan-out (`live_url`) and the loopback
    # port the fan-out server listens on. `None` means "cannot start now".
    #
    # A device with no picture (`kind: io` — a door controller, a relay board) gets none of it, and that
    # absence is the whole of its special treatment. There is no fan-out to publish, so nothing is
    # published; the recorder looks for a holder with a `live_url` and does not find one, the gateway the
    # same, the page the same. One field decided in one place, and no second branch anywhere else.
    def enrich(self, cam: dict) -> dict | None:
        if cam.get("kind") == "io":
            return dict(cam)
        return dict(cam, live_url=live_url(self.server, cam["id"], self.fanout_port()), live_port=live_port(cam["id"]),
                    live_shm=live_shm(cam["id"], self.shm_dir))

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
                # `lost` names units the way the lease does — as text. The reconciler keys by the row's id,
                # which the spec parsed (a number for cameras, a name for recordings): match it, never cast.
                uid = next((k for k in self.reconciler.actual if str(k) == unit), unit)
                self.actuator("stop", {"id": uid})
                self.reconciler.actual.pop(uid, None)
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
        # Suppression stands between the observation and the file, and it is the LAST thing before the
        # write for a reason: everything above this line — the epoch, the fence, `observed` — is about
        # whether this worker may speak about this unit at all, and that answer does not change because
        # the same thing happened twice. What comes back is what belongs in the log: usually this line,
        # sometimes nothing, sometimes the summary of a window that just closed and then this line.
        return self._write(cid, epoch, self.suppressor.lines(t, str(cid), kind, fields), self.class_of(cid, kind))

    # The traffic class of one line: `alarm` when this DEVICE lists this kind among its alarms, else
    # `observation`. The platform fixes the two words and refuses anything else (`events.py`); which of a
    # device's kinds belong to which is the operator's, on the row, because it is a fact about the wiring —
    # the same `io.input` is a door forced on one camera and a technician's cabinet on the next.
    #
    # A camera this worker holds no row for reads as observation rather than refusing: the epoch says the
    # unit is mine, the row may be a pass behind, and downgrading a line beats dropping it.
    def class_of(self, cid: int, kind: str) -> str:
        row = next((r for r in self.rows if str(r.get("id")) == str(cid)), None)
        alarms = (row or {}).get("alarms") or ""
        names = alarms if isinstance(alarms, (list, tuple)) else str(alarms).split(",")
        return ALARM if kind in [str(n).strip() for n in names if str(n).strip()] else OBSERVATION

    # Writes the lines a suppressor handed back, and answers with the path of the LAST one — the caller
    # asked "where did my observation go", and the summary that may precede it is not its answer. `None`
    # when nothing was written, which is what a suppressed repeat is.
    def _write(self, cid: int, epoch: int, lines, cls: str = OBSERVATION) -> str | None:
        log_ = EventLog(self.archive_root, self.SUB.name, str(cid), epoch, self.bucket_seconds)
        path = None
        for t, kind, fields in lines:
            path = log_.append(t, kind, cls, **fields)
        return path

    # Windows that closed with nobody left to close them — the storm stopped, so no observation came to
    # carry the summary out. Called once a pass: without it a burst that ENDS is a burst nobody ever
    # counted, and the log says the quiet minute and the swallowed thousand with the same silence.
    #
    # A summary needs the epoch its window was opened under, and this worker may have lost the unit since.
    # Then the line is dropped rather than written under a fresh epoch: the events it counted belong to
    # the run that observed them, and moving them forward would put a predecessor's storm in a successor's
    # bucket. Being fenced drops them for the same reason, one that this whole file already obeys.
    def flush_suppressed(self) -> int:
        if not self.recording_allowed:
            return 0
        written = 0
        for unit, t, kind, fields in self.suppressor.flush(self.wall()):
            epoch = self.epochs.get(str(unit))
            if epoch is None:
                continue
            self._write(int(unit), epoch, [(t, kind, fields)], self.class_of(int(unit), kind))
            written += 1
        return written

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
        self.flush_suppressed()                         # …storms that ENDED, which no observation will close
        self.requests()                                 # …and what somebody asked this device to DO

    # -- commands: `<sub>/requests/<id>`, done by whoever holds the device ------------------------------
    # The other half of what a device is. Until now a holder only OBSERVED: one connection, a fan-out,
    # events. A device also acts — a relay to pulse, a preset to go to — and the command has to travel the
    # same connection, because there is only one: a second process opening the device to click a relay is
    # the thing this subsystem is built not to do.
    #
    # So a command is a ROW, in the family the platform already has for "bounded work somebody asked for",
    # and the worker holding the unit performs it. Not a unit of its own: a pulse has no duration to hold,
    # no epoch to fence, nothing to reconcile — it happens and it is over. `RecWorker.requests` is the
    # same method for the same reason, one subsystem over.
    #
    # `valid_until` is the one field a recording's request does not need. Footage fetched an hour late is
    # still the footage; a door opened an hour late is an incident. A request that arrives after its
    # moment is EXPIRED, reported as such and cleared — never performed, never silently dropped.
    def requests(self, budget: int = 4, now: float | None = None) -> list[dict]:
        now = self.wall() if now is None else now
        mine = {str(r["id"]): r for r in self.rows}
        done: list[dict] = []
        for key in sorted(self.vars.list(self.SUB.requests_prefix())):
            if len(done) >= budget:
                break
            it, _ = self.vars.get(key)
            rid = key.rsplit("/", 1)[1]
            row = mine.get(str(it.get("unit", ""))) if it else None
            if row is None or rid in self.fetched:
                continue                                 # another worker's device, or one we have done
            until = float(it.get("valid_until", 0) or 0)
            if until and now > until:
                self.fetched.append(rid)                 # say so, so it is cleared rather than asked again
                done.append({"request": rid, "unit": row["id"], "expired": True})
                log.warning("%s: request %s expired unperformed (%.0fs late)", self.name, rid, now - until)
                continue
            dev = self.device_of_row(row)
            if dev is None:
                continue                                 # the device is not open yet: ask again next pass
            try:
                out = self.perform(dev, row, it)
            except Exception as e:                       # noqa: BLE001 — the device's word, whatever it is
                self.fetched.append(rid)                 # a refusal is an answer: do not ask for ever
                done.append({"request": rid, "unit": row["id"], "error": str(e)})
                self.observe(row["id"], "command.failed", action=str(it.get("action", "")), error=str(e))
                continue
            self.fetched.append(rid)
            done.append({"request": rid, "unit": row["id"], **out})
            self.observe(row["id"], "command", **out)    # what was done to a device is an event about it
        return done

    # One command against an open device. Two verbs, because two are what an operator points at; a third
    # belongs here and not in a new place. Unknown verbs raise, which the caller turns into a refusal
    # recorded on the unit — an operator who asked for something this device cannot do gets an answer.
    def perform(self, dev, row: dict, it: dict) -> dict:
        action = str(it.get("action", ""))
        if action == "output":
            port, state, ms = int(it.get("port", 0)), str(it.get("state", "pulse")), int(it.get("pulse_ms", 0) or 0)
            if not hasattr(dev, "output"):
                raise ValueError("this device has no outputs")
            dev.output(port, state, ms)
            return {"action": action, "port": port, "state": state}
        if action == "preset":
            n = int(it.get("n", 0))
            if not hasattr(dev, "preset"):
                raise ValueError("this device has no telemetry")
            dev.preset(n)
            return {"action": action, "n": n}
        raise ValueError(f"unknown action {action!r}")

    # The read model, per assigned row: `id`, `ref`, `name`, `enabled`, `phase` (`running` if in
    # `reconciler.actual`; `pending` if disabled; `failed` if in `reconciler.failures`; else `pending`),
    # `position` (`converged | lagging | stalled`), `revision`, `observed_revision` (what is actually
    # running), `epoch` (held, or 0). This list is the heartbeat's `status`; the controller's `read_model`
    # and the console's `/cameras` show it, and `/metrics` counts `phase == running` into
    # `vms_cameras_running`.
    def status(self) -> list[dict]:
        st = self.reconciler.status()
        out = []
        for cam in self.rows:
            cid = cam["id"]
            pos, lag = st.get(cid, (CONVERGED, 0))
            phase = "running" if cid in self.reconciler.actual else ("pending" if not cam["enabled"] else
                                                                     ("failed" if cid in self.reconciler.failures else "pending"))
            out.append({"id": cid, "ref": cam.get("ref", ""), "name": cam.get("name", str(cid)), "enabled": cam["enabled"], "phase": phase, "position": pos,
                        "revision": cam["revision"], "observed_revision": self.reconciler.actual.get(cid, {}).get("revision", 0),
                        "epoch": self.epochs.get(str(cid), 0), **self.status_extra(cam)})
        for cam, st in zip(self.rows, out):                # `held`: the device is on the line, no stream is built
            if cam.get("live", "always") == "on-demand" and st["phase"] != "running":
                st["phase"] = "held" if self.device_of_row(cam) is not None else "pending"
        return out

    # What each held device is, and what it has that we have not imported. Discovery is an OBSERVATION and
    # goes where observations go: this worker's token writes `vms/epoch/*` and `vms/slots/*`, never
    # `vms/cameras/*`. The operator imports channels from the page, with their own token.
    def device_status(self) -> list[dict]:
        known: dict[str, set] = {}
        for r in self.rows:
            if r.get("source"):
                known.setdefault(device_of(r["source"]), set()).add(str(channel_of(r["source"]) or r["id"]))
        out = []
        for key, dev in sorted(self.devices.items()):
            chans = [str(c) for c in (dev.channels() if hasattr(dev, "channels") else [])]
            have = known.get(key, set())
            out.append({"device": key, "channels": len(chans), "known": sorted(have),
                        "unimported": [c for c in chans if c not in have],
                        "playbacks": dev.in_use(), "max_playbacks": dev.max_playbacks})
        return out

    # A range out of the device's own archive. The ceiling belongs to the hardware, not to this worker:
    # capacity here is still cameras, and an exhausted device is a 503 — the same admission control the
    # gateway does for viewers (Lesson 13), one floor down. On a camera, this competes with live for the
    # one uplink; on an NVR it usually does not.
    # Where the device's footage is, span by span, clipped to `[t0, t1)`. `None` means this driver cannot
    # list — which is not the same answer as "the device holds nothing here", and the caller must be able
    # to tell the two apart. Costs no playback session: listing is not reading.
    def recordings(self, cam, t0: float, t1: float) -> list[tuple[float, float]] | None:
        row = next((r for r in self.rows if str(r["id"]) == str(cam)), None)
        if row is None:
            raise KeyError(cam)
        dev = self.device_of_row(row)
        if dev is None or dev.coverage(cam) is None:
            raise KeyError(cam)
        lister = getattr(dev, "recordings", None)
        return None if lister is None else lister(cam, t0, t1)

    def playback(self, cam, t0: float, t1: float) -> bytes:
        row = next((r for r in self.rows if str(r["id"]) == str(cam)), None)
        if row is None:
            raise KeyError(cam)
        dev = self.device_of_row(row)
        if dev is None or dev.coverage(cam) is None:
            raise KeyError(cam)
        sid = dev.open_playback(cam, t0, t1)            # OverflowError when the device is full
        try:
            return dev.read(sid)
        finally:
            dev.close_playback(sid)

    # What the heartbeat says per unit beyond the platform's fields: the worker publishes `live_url` — where a
    # recorder, a gateway or a detector subscribes; never a viewer.
    # Which port the fan-out is really on. The actuator owns that server, so the actuator is asked; a fake
    # one has no door and the configured number stands. Asking rather than assuming is the same rule as
    # everywhere here: the process that opened the thing is the one that knows.
    def fanout_port(self) -> int:
        return int(getattr(self.actuator, "rtsp_port", 0) or self.rtsp_port)

    def status_extra(self, cam: dict) -> dict:
        """What the heartbeat says per camera beyond the platform's fields. Two kinds of output:
        `live_url`/`live_shm` — the stream now; `playback_url` + `coverage` — the archive the DEVICE
        wrote, which we did not. A subscriber needs nothing but this object, for either."""
        if cam.get("kind") == "io":
            # Nothing to subscribe to, and saying so is the point: a subscriber reads this object and
            # nothing else, so an address published here would be an address somebody dials.
            return {"kind": "io"}
        out = {"live_url": live_url(self.server, cam["id"], self.fanout_port()),
               "live_shm": live_shm(cam["id"], self.shm_dir)}
        dev = self.device_of_row(cam)
        cov = dev.coverage(cam["id"]) if dev is not None else None
        if cov is not None:
            out["playback_url"] = playback_url(self.server, cam["id"], self.playback_port)
            out["coverage"] = cov                         # the SUMMARY: from, to, fragments — never the index
            # …and WHERE to ask for the index, which is not the same thing as carrying it. The heartbeat is
            # one object under a ceiling; thirty days of motion recording is thousands of spans. A door,
            # not a field (М10A Lesson 26 made the same choice for a mask).
            out["index_url"] = playback_url(self.server, cam["id"], self.playback_port).replace("/playback/", "/recordings/")
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
    # `conflicts`, `passes`, `capacity`, `headroom`, `started`, `previous_hb`, `previous_instance`, `archive`
    # (the resource root it records into — on a cluster the value of Nomad's `meta.archive`, via `$ARCHIVE`). The
    # controller's `capacity_of`, `labels_of`, `server_of`, `headroom`, `failover_seconds` and the console's
    # metrics all read from here.
    def heartbeat_once(self) -> None:
        self.heartbeat(self.status(), server=self.server, instance=self.instance, alloc=self.alloc,
                       labels=",".join(self.labels), assignment_rev=self.assignment_rev,
                       fenced=not self.recording_allowed, conflicts=self.conflicts(), passes=self.passes,
                       capacity=self.capacity, headroom=self.headroom(), started=self._started_wall,
                       previous_hb=self.previous_hb, previous_instance=self.previous_instance,
                       archive=self.archive_root, devices=self.device_status(),                                    # the resource its events (a recorder: its footage) go to — Nomad's meta.archive, through $ARCHIVE
                       **self.heartbeat_extra())

    # What a subclass adds to the heartbeat. `fetched` for everyone — the requests this worker has
    # answered, which is how the rows get cleared — and a subsystem with one more fact about itself says
    # it by extending this, not by rewriting the heartbeat.
    def heartbeat_extra(self) -> dict:
        return {"fetched": ",".join(self.fetched[-32:])}

    # -- the playback door ---------------------------------------------------------------------------
    # The holder's second surface, and the reason it is HTTP and not the RTSP fan-out: a browser has to
    # seek inside what it gets, and the recorder fetches ranges through the same door (Lesson 16). The
    # console proxies to it; nothing about a device leaves this process except bytes and the summary.
    #
    #   GET /playback/<cam>?from&to      the device's own footage for that range
    #   GET /recordings/<cam>?from&to    WHERE that footage is: the device's own index, span by span
    #   GET /devices                     what is held, and what channels are not imported yet
    #
    # The index is fetched and not heartbeated, and that is a decision rather than a detail. Thirty days of
    # motion recording on thirty-two channels is thousands of spans; the heartbeat is ONE object under a
    # ceiling (М10A Lesson 25 and 26), and a field that grows with the device does not belong in it. The
    # heartbeat keeps the summary — two numbers, enough to draw a timeline and to know there is something
    # to ask about — and whoever needs the spans pays a request for them.
    def playback_handler(self):
        gw = self

        class H(SendMixin, BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def do_GET(self):
                u = urlsplit(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if u.path == "/devices":
                    return self._send(200, gw.device_status())
                if u.path.startswith("/recordings/"):
                    spans = gw.recordings(u.path.rsplit("/", 1)[1], float(q.get("from", 0)), float(q.get("to", 1e12)))
                    if spans is None:
                        return self._send(501, {"detail": "this driver cannot list what the device holds",
                                                "error": "no index"})
                    return self._send(200, {"spans": [{"from": a, "to": b} for a, b in spans]})
                if not u.path.startswith("/playback/"):
                    return self._send(404, {"detail": "no such route", "error": "no such path"})
                try:
                    data = gw.playback(u.path.rsplit("/", 1)[1], float(q.get("from", 0)), float(q.get("to", 1e12)))
                except KeyError:
                    return self._send(404, {"detail": "this camera has no archive of its own here",
                                            "error": "no device archive"})
                except OverflowError as e:                       # the device's ceiling, not ours
                    return self._send(503, {"detail": str(e), "error": str(e)})
                self.send_response(200); self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

        return H

    # …and the door is opened here, which is the only place that knows what the OS actually gave. With
    # `port=0` the number is invented by the kernel, so it is read back and kept: from this moment
    # `playback_url` says the truth, and a second worker on the same box is an ordinary thing rather than
    # a crash loop every two seconds.
    def serve_playback(self, host: str = "127.0.0.1", port: int | None = None) -> ThreadingHTTPServer:
        srv = ThreadingHTTPServer((host, self.playback_port if port is None else port), self.playback_handler())
        self.playback_port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log.info("%s: playback door on %s:%d", self.name, host, self.playback_port)
        return srv

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
        # Said out loud rather than left to the clock: the first heartbeat goes NOW. `time.monotonic()`
        # counts from boot, so `clock() - 0 >= 10` happens to be true here on the first pass — and Go's
        # monotonic counts from process start, where it is false, which left a Go worker invisible to the
        # controller for ten seconds. The two loops now do the same thing for a reason instead of by luck.
        self.heartbeat_once()
        last_lease, last_hb = 0.0, self.clock()
        while not stop.is_set():
            # The WORK, and whatever it raises stays in here.
            try:
                self.reconcile_once()
                self.pump_once()
            except Exception:                              # noqa: BLE001
                log.exception("%s: pass failed; will retry", self.name)
            # STAYING ALIVE, in a try of its own and never inside the one above. These two used to share
            # it, so anything the work raised skipped them — every pass, for as long as it kept raising.
            # A recorder whose archive went away stopped renewing its leases (fenced at 30 s) and stopped
            # heartbeating (called dead at 45 s), and the outage the spool was there to absorb ended the
            # recording instead. A pass that failed is a pass to retry; the process that ran it still holds
            # its units, and saying so is not something a failure elsewhere gets to switch off.
            try:
                if self.clock() - last_lease >= lease_every:
                    self.lease_pass(); last_lease = self.clock()
                if self.clock() - last_hb >= 10.0:
                    self.heartbeat_once(); last_hb = self.clock()
            except Exception:                              # noqa: BLE001
                log.exception("%s: lease or heartbeat failed; will retry", self.name)
            stop.wait(poll)
        self.actuator.stop_all()
        self.heartbeat_once()
        self.release_slot()                           # an orderly stop says so; a crash says nothing
