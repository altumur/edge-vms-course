"""python3 -m vms worker|controller|console|retain — the three processes on one box, and the archive policy pass.

    PLATFORM_DIR=/data/platform     the platform's stores (config/, objects/)
    SPOOL=/data/spool  ARCHIVE=/data/archive  MEDIA_DIR=/data/media
    WORKER_NAME=w-1                  the slot to claim (systemd: %i); unset: NOMAD_ALLOC_INDEX → w-<index>;
                                     neither: the first free slot, a lapsed one first
    CAPACITY=50                      cameras this worker can carry — exported as headroom for the autoscaler
    CONSOLE_PORT=8080                the console (its own process, its own token: the operator's rows, never placement)
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # __main__.py — `python3 -m vms worker | controller | console | retain`: the three processes on one box, and
# the archive policy pass
#
# **Role in the module.** The entrypoint every deploy unit runs (`deploy/*.container` all say `Exec=python3 -m
# vms <verb>`; the Containerfile's default `CMD` is `worker`). It reads the environment, opens the two
# file-backed stores under `$PLATFORM_DIR` with the *right token for the verb*, builds the process's object
# from `worker.py` / `controller.py` / `console.py` / `archive.py`, and runs it until SIGTERM/SIGINT. It is
# glue and nothing else: no logic of its own beyond wiring, and each verb's token is deliberately narrower
# than the whole `vms/*` prefix. `tests/test_deploy_units.py` imports this module (without running it) and
# checks that the four verbs in the dispatch table are exactly the four the units invoke.
#
# Environment (from the docstring and the code):
# - `PLATFORM_DIR` (default `/data/platform`) — the platform's stores: `<dir>/config` is `FileVariables`,
#   `<dir>/objects` is `FsObjectStore`.
# - `SPOOL` (`/data/spool`), `ARCHIVE` (`/data/archive`) — the archive resource's two roots. `MEDIA_DIR` is
#   read by `gstvms/uri.py`, not here.
# - `WORKER_NAME` — the slot to claim (systemd's `%i`); unset: `NOMAD_ALLOC_INDEX` → `w-<index>`; neither:
#   `None`, which makes `VmsWorker` claim the first free slot, a lapsed one first.
# - `CAPACITY` (default `50`) — cameras this worker can carry; exported as `headroom`. Also passed to the
#   controller and console as the *fallback* for a worker whose heartbeat says nothing.
# - `SEGMENT_SECONDS` (default `600`) — segment length handed to `GstActuator`.
# - `CONSOLE_HOST` (`127.0.0.1`), `CONSOLE_PORT` (`8080`) — where the console listens (`vmsconsole.container`
#   sets `0.0.0.0`).
# - `LOG_LEVEL` (`INFO`) — `logging.basicConfig` level.
#
# ## Module-level names
# - `root` — `$PLATFORM_DIR`, read once at import.
# - `stop` — a `threading.Event` set by the SIGTERM/SIGINT handler installed at import time; every verb loops
#   on it. Because the handler is installed at import, importing the module (as the deploy test does) also
#   installs the handlers in the importing process.
#
# ## Functions
#
# ### `worker()`
# Builds `vmsworker`:
# - Resolves the slot name: `WORKER_NAME`, else `w-$NOMAD_ALLOC_INDEX`, else `None`. (Same rule as
#   `worker.slot_from_environment`, written out again here.)
# - Opens Variables as writer `vmsworker` with the ACL `["vms/epoch/*", "vms/slots/*"]` — literally
#   `Subsystem.acl_worker()` for `vms`: a worker takes epochs and claims its slot, and can write nothing else.
#   A bug that tried to write a camera row would be a `Forbidden` from the store.
# - Tries `gstvms.actuator.GstActuator(spool, archive, SEGMENT_SECONDS)`; on `ImportError` (no `gi`) logs a
#   warning and uses `FakeActuator`, which records nothing. This is what the README means by "with GStreamer:
#   records; without: the fake actuator".
# - Before the worker exists, runs the restart step of М9 Lesson 4 / М10 Lesson 3:
#   `ArchiveResource.closed_in_spool(grace_seconds=30, now)` lists segments the previous instance closed but
#   did not promote (a `kill -9` between close and promote), and each is `promote`d now. The open segment at
#   the time of the kill is the one that is lost.
# - Constructs `VmsWorker(name, vars_, objects, act, capacity=$CAPACITY, archive_root=archive)` — the
#   constructor claims the slot — logs the claimed name and instance, and calls `w.run(stop=stop)`. `run`
#   releases the slot on the way out, so SIGTERM is an orderly stop (scale-in), while a kill leaves the slot
#   to lapse.
#
# ### `controller()`
# Builds `vmscontroller` and runs the placement pass every 5 s:
# - Variables as writer `vmscontroller` with `SPEC.acl_controller()` — `vms/workers/*`, `vms/placement/*`,
#   `vms/slots/*`; never a camera's row (see `vmsplatform/spec.py`).
# - `VmsController(vars_, objects, capacity=$CAPACITY)`.
# - Each pass: `ensure_placed()` (deleted rows unplaced first, then every unplaced camera onto the workers it
#   currently sees by their heartbeats), `redistribute()` (only the cameras of a *released* slot — scale-in —
#   move; a merely silent slot is a crash and is left for the scheduler), `publish_snapshot()` (`vms/snapshot`
#   in the object store). Any exception is logged and the loop continues; `stop.wait(5)` between passes. No
#   port, no state: the process can be restarted at any moment, and two of them agree by CAS.
#
# ### `console()`
# The screen and the API, as its own process ("count as many as you like"):
# - Variables as writer `vmsconsole` with `SPEC.acl_console()` — `vms/cameras/*`, `vms/next_id`, `vms/idem/*`,
#   `vms/retention/*`; never placement. It holds a `VmsController` over that token, so a write it must not
#   make (`place`) is a `Forbidden` from the store, not a rule in the console.
# - `ArchiveResource($SPOOL, $ARCHIVE)` so the console can serve `/timeline/<id>` and `/segment/<path>` from
#   this box's archive and write operator marks into its own bucket.
# - `serve(ctl, archive, $CONSOLE_HOST, $CONSOLE_PORT)` from `vms/console.py` starts the `ThreadingHTTPServer`
#   in a daemon thread; the main thread waits on `stop`, then `srv.shutdown()`.
#
# ### `retain()`
# The archive policy pass, run by `vms-archive-retain.timer` as a oneshot (`vms-archive-retain.container`):
# "the archive resource has no controller — it has a policy, run by a timer".
# - `ArchiveResource($SPOOL, $ARCHIVE)`; Variables opened with *no* writer and no ACL — the pass only reads
#   rows (the unit mounts `/data/platform` read-only for the same reason).
# - Logs `res.repair()` (manifests made to agree with the files) and the number of `res.close_buckets(now)`
#   (event buckets whose span is over and quiet get their manifest line).
# - Media — the VMS's own policy, per camera, on its manifest: for every path under `vms/cameras/`, reads the
#   row through `config.row` and calls `res.retain(c["id"], c["retention_days"], now)`, logging what was
#   removed.
# - Events — the platform's, by the derived row `vms/retention/<cam>`: builds
#   `vmsplatform.resource.Resource(archive, hostname, "", vars_, objects)` (url `""` — nothing is served here,
#   the object is used only for its policy) and calls `platform.retain()`, which deletes every bucket file on
#   this resource older than its unit's days (`retention_days(vars, sub, unit)`: the derived row, else
#   `vms/retention`, else a year) and logs the count. Files only: the manifest lines for those buckets are
#   dropped by `res.repair()` on the *next* pass — the split `archive.py`'s docstring describes.
#
# Two halves, two owners, one pass — the division `test_events_are_buckets_on_the_resource_recording_or_not`
# exercises by hand (`res.retain(...) == 1`, then `Resource(...).retain() == 3`, then `res.repair() ==
# {dropped: 3}`). Note that `vars_.list("vms/cameras/")` includes rows marked `deleted: "true"`; `row()`
# parses them like any other, so a deleted camera's media is still retained by its last `retention_days`,
# while its buckets go at once because `delete_camera` set `vms/retention/<id>` to `{days: 0}`.
#
# ### `if __name__ == "__main__"`
# Dispatch table `{"worker", "controller", "console", "retain"}` on `sys.argv[1]`.
# `test_the_units_run_the_entrypoints_the_package_has` regex-extracts these four names and matches them
# against the `Exec=` lines of the four Quadlet units.
#
# ## Notes
# - Three tokens, three processes: `vmsworker` (epochs, slots), `vmscontroller` (placement), `vmsconsole` (the
#   operator's rows). Together they partition `vms/*`; none of them can do another's job. The mounts in
#   `deploy/` repeat the same split in bytes (`test_who_may_write_where_is_in_the_mounts_too`).
# - `CAPACITY` means two different things depending on the verb: the worker's own number (what it heartbeats
#   and places by) versus the controller's fallback for a worker that has not spoken yet
#   (`test_capacity_is_the_workers_word_not_the_controllers`).
# - No verb runs the platform's `Resource` as a *job* on one box (no heartbeat, no HTTP, no mirror); `retain`
#   instantiates one only to run its bucket-retention policy in the same timer pass as the VMS's media policy.
#   The README calls the bucket half "the platform's" and the test proves it with a `Resource` object
#   directly.
# ================================================================================================
from __future__ import annotations

import logging
import os
import signal
import sys
import threading

from vmsplatform.objects import FsObjectStore
from vmsplatform.variables import FileVariables

from .archive import ArchiveResource
from .controller import VmsController
from .worker import FakeActuator, VmsWorker

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
root = os.environ.get("PLATFORM_DIR", "/data/platform")
stop = threading.Event()
for s in (signal.SIGTERM, signal.SIGINT):
    signal.signal(s, lambda *_: stop.set())


def worker() -> None:
    name = os.environ.get("WORKER_NAME") or (f"w-{os.environ['NOMAD_ALLOC_INDEX']}" if "NOMAD_ALLOC_INDEX" in os.environ else None)
    vars_ = FileVariables(os.path.join(root, "config"), writer="vmsworker", acl={"vmsworker": ["vms/epoch/*", "vms/slots/*"]})
    objects = FsObjectStore(os.path.join(root, "objects"))
    spool, archive = os.environ.get("SPOOL", "/data/spool"), os.environ.get("ARCHIVE", "/data/archive")
    try:
        from gstvms.actuator import GstActuator
        act = GstActuator(spool, archive, int(os.environ.get("SEGMENT_SECONDS", "600")))
    except ImportError:
        logging.warning("no GStreamer: the fake actuator records nothing")
        act = FakeActuator()
    res = ArchiveResource(spool, archive)
    for p in res.closed_in_spool(grace_seconds=30, now=__import__("time").time()):     # what the last instance closed but did not promote
        res.promote(p)
    w = VmsWorker(name, vars_, objects, act, capacity=int(os.environ.get("CAPACITY", "50")), archive_root=archive)
    logging.info("worker %s (instance %s) claimed its slot", w.name, w.instance)
    w.run(stop=stop)


def controller() -> None:
    from .config import SPEC
    vars_ = FileVariables(os.path.join(root, "config"), writer="vmscontroller", acl={"vmscontroller": SPEC.acl_controller()})
    objects = FsObjectStore(os.path.join(root, "objects"))
    ctl = VmsController(vars_, objects, capacity=int(os.environ.get("CAPACITY", "50")))
    while not stop.is_set():
        try:
            ctl.ensure_placed()                       # deleted rows unplaced; new cameras onto the workers it sees
            ctl.redistribute()                        # cameras of a RELEASED slot (scale-in) onto the rest; nothing else, ever
            ctl.publish_snapshot()
        except Exception:                             # noqa: BLE001
            logging.exception("placement pass failed")
        stop.wait(5)


def console() -> None:
    """The screen and the API: its own process, count as many as you like, a
    token for the operator's rows and nothing else."""
    from .config import SPEC
    from .console import serve
    vars_ = FileVariables(os.path.join(root, "config"), writer="vmsconsole", acl={"vmsconsole": SPEC.acl_console()})
    objects = FsObjectStore(os.path.join(root, "objects"))
    ctl = VmsController(vars_, objects, capacity=int(os.environ.get("CAPACITY", "50")))
    archive = ArchiveResource(os.environ.get("SPOOL", "/data/spool"), os.environ.get("ARCHIVE", "/data/archive"))
    srv = serve(ctl, archive, os.environ.get("CONSOLE_HOST", "127.0.0.1"), int(os.environ.get("CONSOLE_PORT", "8080")))
    logging.info("console on %s", srv.server_address)
    stop.wait()
    srv.shutdown()


def retain() -> None:
    """The archive resource has no controller — it has a policy, run by a timer:
    repair, close event buckets, retain media and events by each camera's days."""
    import socket
    import time
    from vmsplatform.resource import Resource
    from .config import row
    archive = os.environ.get("ARCHIVE", "/data/archive")
    res = ArchiveResource(os.environ.get("SPOOL", "/data/spool"), archive)
    vars_ = FileVariables(os.path.join(root, "config"))
    now = time.time()
    logging.info("repair %s; closed %d buckets", res.repair(), len(res.close_buckets(now)))
    for p in vars_.list("vms/cameras/"):                                  # media: the VMS's own policy, per camera, on its manifest
        c = row(vars_.get(p)[0])
        logging.info("camera %s: media removed %s", c["id"], res.retain(c["id"], c["retention_days"], now))
    platform = Resource(archive, socket.gethostname(), "", vars_, FsObjectStore(os.path.join(root, "objects")))
    logging.info("buckets removed %s", platform.retain())                 # events: the platform's, by vms/retention/<cam> (the derived row)


if __name__ == "__main__":
    {"worker": worker, "controller": controller, "console": console, "retain": retain}[sys.argv[1]]()
