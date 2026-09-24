"""python3 -m vms worker|controller|recorder|reccontroller|console|resource|gateway|livecontroller|detworker|detcontroller — the box's processes.

    PLATFORM_DIR=/data/platform     the platform's stores (config/, objects/)
    SPOOL=/data/spool  ARCHIVE=/data/archive  MEDIA_DIR=/data/media
    WORKER_NAME=w-1                  the slot to claim (systemd: %i); unset: NOMAD_ALLOC_INDEX → w-<index>;
                                     neither: the first free slot, a lapsed one first
    CAPACITY=50                      cameras this worker can carry — exported as headroom for the autoscaler
    RECORDER_NAME=r-1                a recorder's slot (systemd: %i); CAPACITY here is recordings — this server's disks and NIC
    CONSOLE_PORT=8080                the console (its own process, its own token: the operator's rows, never placement)
    RESOURCE_PORT=8090  RESOURCE_URL the resource process: heartbeat, the policy pass, the event database served as /events
    EVENTDB=:memory:                 where the resource keeps its event database — a cache, rebuilt on every start
    GATEWAY_PORT=8082  GATEWAY_URL   a live gateway: WHEP on this port (`auto` — ask the OS, which is what a
                                     SECOND gateway on one box needs); the URL the console proxies to
    RTSP_PORT=8554  PLAYBACK_PORT=8083   the worker's two doors, `auto` likewise: a template that fixes a
                                     number is a door only the first instance on the box can open
    GATEWAY_NAME=g-1                 its slot (systemd: %i); CAPACITY here is viewers
    DET_NAME=d-1                     a detector worker's slot; CAPACITY here is streams; NOMAD_META_labels=gpu says where it is
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # __main__.py — `python3 -m vms worker | controller | console | resource | …`: the box's processes
#
# **Role in the module.** The entrypoint every deploy unit runs (`deploy/*.container` all say `Exec=python3
# -m vms <verb>`; the Containerfile's default `CMD` is `worker`). It reads the environment, opens the two
# file-backed stores under `$PLATFORM_DIR` with the *right token for the verb*, builds the process's object
# from `worker.py` / `controller.py` / `console.py` / `resource.py` / …, and runs it until SIGTERM/SIGINT. It is
# glue and nothing else: no logic of its own beyond wiring, and each verb's token is deliberately narrower
# than the whole `vms/*` prefix. `tests/test_deploy_units.py` imports this module (without running it) and
# checks that the verbs in the dispatch table are exactly the ones the units invoke.
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
# - `CONSOLE_HOST` (`127.0.0.1`), `CONSOLE_PORT` (`8080`) — where the console listens
#   (`console.container` sets `0.0.0.0`).
# - `RESOURCE_HOST` (`127.0.0.1`), `RESOURCE_PORT` (`8090`), `RESOURCE_URL` — the resource process's HTTP and the URL
#   its heartbeat advertises (the console asks `/events` there); `EVENTDB` (`:memory:`) — its database file.
# - `LOG_LEVEL` (`INFO`) — `logging.basicConfig` level.
#
# ## Module-level names
# - `root` — `$PLATFORM_DIR`, read once at import.
# - `stop` — a `threading.Event` set by the SIGTERM/SIGINT handler installed at import time; every verb
#   loops on it. Because the handler is installed at import, importing the module (as the deploy test does)
#   also installs the handlers in the importing process.
#
# ### `if __name__ == "__main__"`
# Dispatch table on `sys.argv[1]`: worker, controller, console, resource, gateway, livecontroller, detworker,
# detcontroller. `test_the_units_run_the_entrypoints_the_package_has` regex-extracts the names and matches
# them against the `Exec=` lines of the Quadlet units.
#
# ## Notes
# - Three tokens, three processes: `vmsworker` (epochs, slots), `vmscontroller` (placement), `console`
#   (the operator's rows). Together they partition `vms/*`; none of them can do another's job. The mounts in
#   `deploy/` repeat the same split in bytes (`test_who_may_write_where_is_in_the_mounts_too`).
# - `CAPACITY` means two different things depending on the verb: the worker's own number (what it heartbeats
#   and places by) versus the controller's fallback for a worker that has not spoken yet
#   (`test_capacity_is_the_workers_word_not_the_controllers`).
# - `resource` runs the platform's `Resource` as a process on the box exactly as М11 runs it as a job:
#   heartbeat, HTTP, the policy pass (the VMS's hook first, then bucket retention, then the mirror — off on
#   one box), and the `EventDatabase` over the tree. The old `retain` verb and its timer are gone: a pass
#   every 600 s from the process's loop is the same pass, and a oneshot could not hold a database.
# ================================================================================================
from __future__ import annotations

import logging
import os
import signal
import sys
import threading

from w2cplatform.objects import FsObjectStore
from w2cplatform.variables import open_vars

from .archive import ArchiveResource
from .controller import VmsController
from .worker import FakeActuator, VmsWorker

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
root = os.environ.get("PLATFORM_DIR", "/data/platform")
# The store seam: a process is told a URL and nothing else (`w2cplatform.variables.open_vars`). On a box
# this is `file://` — in-process, no daemon, no hop. `CONFIG_URL=nomad://…` in a cluster, `k8s://…` later;
# not one of those names appears in the loop.
CONFIG_URL = os.environ.get("CONFIG_URL") or "file://" + os.path.join(root, "config")
stop = threading.Event()
for s in (signal.SIGTERM, signal.SIGINT):
    signal.signal(s, lambda *_: stop.set())


# Builds `vmsworker`:
# - Resolves the slot name: `WORKER_NAME`, else `w-$NOMAD_ALLOC_INDEX`, else `None`. (Same rule as
#   `worker.slot_from_environment`, written out again here.)
# - Opens Variables as writer `vmsworker` with the ACL `["vms/epoch/*", "vms/slots/*"]` — literally
#   `Subsystem.acl_worker()` for `vms`: a worker takes epochs and claims its slot, and can write nothing
#   else. A bug that tried to write a camera row would be a `Forbidden` from the store.
# - Tries `gstvms.actuator.GstActuator()` — `driverpacksrc ! tee`, served as the RTSP fan-out on :8554; on
#   `ImportError` (no `gi`) logs a warning and uses `FakeActuator`, which holds nothing. The worker records
#   nothing either way: recording is the recorder's (`recorder` below).
# - Constructs `VmsWorker(name, vars_, objects, act, capacity=$CAPACITY, archive_root=archive)` — its events
#   go to this server's resource under `vms/<cam>/` — the
#   constructor claims the slot — logs the claimed name and instance, and calls `w.run(stop=stop)`. `run`
#   releases the slot on the way out, so SIGTERM is an orderly stop (scale-in), while a kill leaves the slot
#   to lapse.
def worker() -> None:
    from w2cplatform import runtime
    name = runtime.slot(os.environ, "WORKER_NAME", "w")
    vars_ = open_vars(CONFIG_URL, writer="vmsworker", acl={"vmsworker": ["vms/epoch/*", "vms/slots/*"]})
    objects = FsObjectStore(os.path.join(root, "objects"))
    archive = os.environ.get("ARCHIVE", "/data/archive")
    try:
        from gstvms.actuator import GstActuator
        from .config import port_of, RTSP_PORT
        act = GstActuator(rtsp_port=port_of(os.environ.get("RTSP_PORT"), RTSP_PORT))
    except ImportError:
        logging.warning("no GStreamer: the fake actuator holds nothing")
        act = FakeActuator()
    # Devices with an archive of their own — a camera's card, an NVR's disks (Lesson 15). The real factory
    # is a DriverPack session; without it a source has no footage but its live stream, which is the box in
    # this course.
    try:
        from gstvms.devices import open_device as device_factory              # type: ignore
    except ImportError:
        device_factory = None
    w = VmsWorker(name, vars_, objects, act, capacity=int(os.environ.get("CAPACITY", "50")), archive_root=archive,
                  device_factory=device_factory)
    # The port is the worker's own (`$PLAYBACK_PORT`, `auto` for "ask the OS"), read in its constructor and
    # written back here by whatever the socket actually got.
    srv = w.serve_playback(os.environ.get("PLAYBACK_HOST", "0.0.0.0"))
    logging.info("worker %s (instance %s) claimed its slot; playback on %s", w.name, w.instance, srv.server_address)
    try:
        w.run(stop=stop)
    finally:
        srv.shutdown()


# Builds `recworker` — the fourth subsystem's worker, the only one placed on top of the archive:
# - the slot: `RECORDER_NAME`, else `r-$NOMAD_ALLOC_INDEX`, else whichever is free; Variables as writer
#   `recworker` with `["rec/epoch/*", "rec/slots/*"]`.
# - `gstvms.actuator.GstRecActuator(spool, archive, SEGMENT_SECONDS)` — `rtspsrc ! archivesink` per
#   recording, subscribed to the worker's fan-out; without GStreamer the fake, which records nothing.
# - `RecWorker(...)`: promotes what the last instance closed but did not promote, then runs — the worker's
#   loop, plus a re-subscription when a camera's holder moves, plus promotion on every pass.
def recorder() -> None:
    from .recworker import RecWorker
    vars_ = open_vars(CONFIG_URL, writer="recworker", acl={"recworker": ["rec/epoch/*", "rec/slots/*"]})
    objects = FsObjectStore(os.path.join(root, "objects"))
    spool, archive = os.environ.get("SPOOL", "/data/spool"), os.environ.get("ARCHIVE", "/data/archive")
    try:
        from gstvms.actuator import GstRecActuator
        act = GstRecActuator(spool, archive, int(os.environ.get("SEGMENT_SECONDS", "600")))
    except ImportError:
        logging.warning("no GStreamer: the fake actuator records nothing")
        act = FakeActuator()
    # Backfill (Lesson 16): `BACKFILL_WINDOW=22-6` in LOCAL time — night where the camera is, not where the
    # server is — and `BACKFILL_BUDGET` ranges per pass. Unset window: any hour. Budget 0: only what an
    # operator asks for.
    win = os.environ.get("BACKFILL_WINDOW", "")
    window = tuple(int(x) for x in win.split("-")) if "-" in win else None
    r = RecWorker(None, vars_, objects, act, archive=ArchiveResource(spool, archive), capacity=int(os.environ.get("CAPACITY", "50")),
                  window=window, keep_days=float(os.environ.get("RETENTION_DAYS", "30")))
    r.backfill_budget = int(os.environ.get("BACKFILL_BUDGET", "1"))
    logging.info("recorder %s (instance %s) claimed its slot; promoted %d", r.name, r.instance, r.promoted)
    r.run(stop=stop)


def reccontroller() -> None:
    """The fourth subsystem's controller: the platform's class from rec.subsystem.yaml, placing recordings on
    recorders — one per server, where the archive is. No code of its own."""
    from w2cplatform.spec import SpecController
    from .config import REC_SPEC
    vars_ = open_vars(CONFIG_URL, writer="reccontroller", acl={"reccontroller": REC_SPEC.acl_controller()})
    _controller_loop(SpecController(REC_SPEC, vars_, FsObjectStore(os.path.join(root, "objects"))))


# Builds `vmscontroller` and runs the placement pass every 5 s:
# - Variables as writer `vmscontroller` with `SPEC.acl_controller()` — `vms/workers/*`, `vms/placement/*`,
#   `vms/slots/*`; never a camera's row (see `w2cplatform/spec.py`).
# - `VmsController(vars_, objects, capacity=$CAPACITY)`.
# - Each pass: `ensure_placed()` (deleted rows unplaced first, then every unplaced camera onto the workers
#   it currently sees by their heartbeats), `redistribute()` (only the cameras of a *released* slot —
#   scale-in — move; a merely silent slot is a crash and is left for the scheduler), `publish_snapshot()`
#   (one object per worker under `vms/snapshot/` in the object store). Any exception is logged and the
#   loop continues; `stop.wait(5)`
#   between passes. No port, no state: the process can be restarted at any moment, and two of them agree by
#   CAS.
def _controller_loop(ctl) -> None:
    """One controller process per subsystem, the same loop: unplace what was deleted, place what is new onto the
    workers it sees, move what a released slot left, bring one unit home if its server came back, publish the
    snapshot. Nothing else, ever."""
    while not stop.is_set():
        try:
            ctl.ensure_placed()                       # deleted rows unplaced; new units onto the workers it sees
            ctl.redistribute()                        # units of a RELEASED slot (scale-in) onto the rest
            ctl.ensure_home(1)                        # ONE unit a pass back to the server its row names, if it is back
        except Exception:                             # noqa: BLE001
            logging.exception("placement pass failed")
        # Its OWN try, and this is not tidiness. Publishing is the last call in the pass, so when it threw
        # inside the block above, placement had already succeeded — and the log said "placement pass
        # failed", naming the one thing that had not. The reverse hid the other half: a placement that
        # threw skipped the publish, the layer above went quietly stale, and the word "snapshot" appeared
        # nowhere. Two jobs, two failures, two sentences.
        try:
            ctl.publish_snapshot()
        except Exception:                             # noqa: BLE001
            # What the layer above loses by this: its copy stops ageing forward. The age itself is on
            # `/metrics` as `<sub>_snapshot_age_seconds`, read from the store rather than kept in this
            # process, so it survives a restart and any console can answer it.
            logging.exception("publishing the snapshot failed — the layer above is now reading a stale copy")
        stop.wait(5)


def controller() -> None:
    from .config import SPEC
    vars_ = open_vars(CONFIG_URL, writer="vmscontroller", acl={"vmscontroller": SPEC.acl_controller()})
    objects = FsObjectStore(os.path.join(root, "objects"))
    _controller_loop(VmsController(vars_, objects, capacity=int(os.environ.get("CAPACITY", "50"))))


def livecontroller() -> None:
    """The second subsystem's controller: the platform's class from live.subsystem.yaml, placing fan-outs on
    gateways by viewer headroom. No code of its own."""
    from w2cplatform.spec import SpecController
    from .config import LIVE_SPEC
    vars_ = open_vars(CONFIG_URL, writer="livecontroller", acl={"livecontroller": LIVE_SPEC.acl_controller()})
    _controller_loop(SpecController(LIVE_SPEC, vars_, FsObjectStore(os.path.join(root, "objects"))))


def detcontroller() -> None:
    """The third subsystem's controller: the platform's class from det.subsystem.yaml, placing models on
    GPU-labelled detector workers by stream headroom. No code of its own."""
    from w2cplatform.spec import SpecController
    from .config import DET_SPEC
    vars_ = open_vars(CONFIG_URL, writer="detcontroller", acl={"detcontroller": DET_SPEC.acl_controller()})
    _controller_loop(SpecController(DET_SPEC, vars_, FsObjectStore(os.path.join(root, "objects"))))


def detworker() -> None:
    """A detector worker: a worker of the `det` subsystem. Its token writes its slot, its epochs and its
    heartbeat; its events go into det/<unit>/e<epoch>/ on this server's resource."""
    from .detworker import DetWorker
    vars_ = open_vars(CONFIG_URL, writer="detworker", acl={"detworker": ["det/epoch/*", "det/slots/*"]})
    d = DetWorker(None, vars_, FsObjectStore(os.path.join(root, "objects")), capacity=int(os.environ.get("CAPACITY", "8")),
                  archive_root=os.environ.get("ARCHIVE", "/data/archive"))
    logging.info("detector %s (instance %s) claimed its slot; models: %s", d.name, d.instance, ",".join(d.models))
    d.run(stop=stop)


def autocontroller() -> None:
    """The seventh subsystem's controller: scenarios placed on evaluators. `AutoController` and not the
    platform's class, because a scenario that says nothing runnable must be refused where it is written —
    and only this subsystem knows what runnable means."""
    from .auto import AutoController
    from .config import AUTO_SPEC
    vars_ = open_vars(CONFIG_URL, writer="autocontroller", acl={"autocontroller": AUTO_SPEC.acl_controller()})
    _controller_loop(AutoController(vars_, FsObjectStore(os.path.join(root, "objects"))))


def autoworker() -> None:
    """A scenario evaluator. Its token is the only one in the course that reaches across a subsystem's
    name — `requests_acl("vms", "rec")` — and it reaches exactly one family: bounded work, addressed to a
    unit, performed by whoever holds it. It cannot write a camera, a recording or a placement."""
    from w2cplatform.contract import requests_acl
    from .autoworker import AutoWorker
    from .config import AUTO_SPEC
    acl = AUTO_SPEC.sub.acl_worker() + requests_acl("vms", "rec")
    vars_ = open_vars(CONFIG_URL, writer="autoworker", acl={"autoworker": acl})
    a = AutoWorker(None, vars_, FsObjectStore(os.path.join(root, "objects")),
                   capacity=int(os.environ.get("CAPACITY", "50")),
                   archive_root=os.environ.get("ARCHIVE", "/data/archive"))
    logging.info("evaluator %s (instance %s) claimed its slot; may file: %s", a.name, a.instance, ",".join(acl[-2:]))
    a.run(stop=stop)


def detjobcontroller() -> None:
    """The fifth subsystem's controller: the platform's class from detjob.subsystem.yaml, placing scans
    beside the recorder holding the footage they read, and on a GPU server when it cannot. No code of its
    own — and, until the placement predicate lands, no notion that a job ever finishes."""
    from w2cplatform.spec import SpecController
    from .config import DETJOB_SPEC
    vars_ = open_vars(CONFIG_URL, writer="detjobcontroller", acl={"detjobcontroller": DETJOB_SPEC.acl_controller()})
    _controller_loop(SpecController(DETJOB_SPEC, vars_, FsObjectStore(os.path.join(root, "objects"))))


def detjobworker() -> None:
    """A scan worker: a worker of the `detjob` subsystem. Same box, same models and same GPU as
    `detworker`, a separate process because it carries a separate budget — a retro-search must not be able
    to spend the streams live detection is running on. Its events go into detjob/<job>/e<epoch>/ on this
    server's resource; its progress goes beside them, because its token may not write the row."""
    from .detjobworker import DetJobWorker
    vars_ = open_vars(CONFIG_URL, writer="detjobworker", acl={"detjobworker": ["detjob/epoch/*", "detjob/slots/*"]})
    j = DetJobWorker(None, vars_, FsObjectStore(os.path.join(root, "objects")),
                     capacity=int(os.environ.get("SCAN_CAPACITY", "2")),
                     archive_root=os.environ.get("ARCHIVE", "/data/archive"))
    logging.info("scan worker %s (instance %s) claimed its slot; models: %s", j.name, j.instance, ",".join(j.models))
    j.run(stop=stop)


def surveycontroller() -> None:
    """The sixth subsystem's controller: the platform's class from survey.subsystem.yaml, placing watches
    beside the worker holding the camera. No code of its own."""
    from w2cplatform.spec import SpecController
    from .config import SURVEY_SPEC
    vars_ = open_vars(CONFIG_URL, writer="surveycontroller", acl={"surveycontroller": SURVEY_SPEC.acl_controller()})
    _controller_loop(SpecController(SURVEY_SPEC, vars_, FsObjectStore(os.path.join(root, "objects"))))


def surveyworker() -> None:
    """A survey worker: a worker of the `survey` subsystem. It watches a camera's DEVICE archive — a card,
    an NVR — and copies none of it: what comes out is events. Its budget is its own (`SURVEY_CAPACITY`),
    because a survey must never be able to spend what live detection is running on, and its real limit is
    usually the two playback sessions the device allows rather than the GPU."""
    from .surveyworker import SurveyWorker
    vars_ = open_vars(CONFIG_URL, writer="surveyworker", acl={"surveyworker": ["survey/epoch/*", "survey/slots/*"]})
    s = SurveyWorker(None, vars_, FsObjectStore(os.path.join(root, "objects")),
                     capacity=int(os.environ.get("SURVEY_CAPACITY", "2")),
                     archive_root=os.environ.get("ARCHIVE", "/data/archive"))
    logging.info("survey %s (instance %s) claimed its slot; models: %s", s.name, s.instance, ",".join(s.models))
    s.run(stop=stop)


def gateway() -> None:
    """A live gateway: a worker of the `live` subsystem. Its token writes its slot and epochs, its heartbeat,
    and `live/streams/*` — so it can delete the fan-out it holds once nobody has watched it for `grace`."""
    from w2cplatform.spec import SpecController
    from .config import LIVE_SPEC
    from .liveworker import LiveWorker
    vars_ = open_vars(CONFIG_URL, writer="liveworker",
                          acl={"liveworker": ["live/epoch/*", "live/slots/*", "live/streams/*"]})
    objects = FsObjectStore(os.path.join(root, "objects"))
    from .config import port_of
    # `auto` is what lets a second gateway run on this box: it publishes the address it bound (`serve`
    # reads it back into `self.url`), and every viewer reaches it through that, never through a number.
    host, port = os.environ.get("GATEWAY_HOST", "127.0.0.1"), port_of(os.environ.get("GATEWAY_PORT"), 8082)
    peer = None
    try:
        from gstvms.webrtc import GstPeer
        peer = GstPeer
    except ImportError:
        logging.warning("no GStreamer webrtcbin: the fake peer answers SDP and carries no media")
    gw = LiveWorker(None, vars_, objects, ctl=SpecController(LIVE_SPEC, vars_, objects), url=os.environ.get("GATEWAY_URL", f"http://{host}:{port}"),
                     capacity=int(os.environ.get("CAPACITY", "100")), peer_factory=peer)
    srv = gw.serve(host, port)
    logging.info("gateway %s (instance %s) on %s", gw.name, gw.instance, srv.server_address)
    gw.run(stop=stop)
    srv.shutdown()


# The screen and the API, as its own process ("count as many as you like"):
# - Variables as writer `console` with `SPEC.acl_console()` — `vms/cameras/*`, `vms/next_id`,
#   `vms/idem/*`, `vms/retention/*`; never placement. It holds a `VmsController` over that token, so a write
#   it must not make (`place`) is a `Forbidden` from the store, not a rule in the console.
# - `ArchiveResource($SPOOL, $ARCHIVE)` so the console can serve `/timeline/<id>` and `/segment/<path>` from
#   this box's archive and write operator marks into its own bucket.
# - `serve(ctl, archive, $CONSOLE_HOST, $CONSOLE_PORT)` from `vms/console.py` starts the
#   `ThreadingHTTPServer` in a daemon thread; the main thread waits on `stop`, then `srv.shutdown()`.
# Housekeeping the console owns BECAUSE THE ACL SAYS SO. `<sub>/blobs/*` is the console's to write
# (Lesson 27), so it is the console's to collect; the controller could not delete a blob if it wanted to,
# and that is the right way round — the process that creates a thing is the one that can be trusted to
# know when nothing names it.
#
# Its own loop and its own log line. That is Lesson 28 applied before the same mistake is made twice: a
# sweep that fails inside somebody else's `try` would be reported as somebody else's failure, and the
# consequence — blobs accumulating with nothing reclaiming them — is exactly the kind that shows up as a
# disk full a year later.
def _sweep_loop(controllers, every: float = 60.0) -> None:
    while not stop.is_set():
        for c in controllers:
            try:
                r = c.sweep_blobs()
                if r["deleted"]:
                    logging.info("swept %d blob(s) nothing names in %s", r["deleted"], c.spec.name)
            except Exception:                         # noqa: BLE001
                logging.exception("the blob sweep failed in %s — nothing is reclaiming its blobs", c.spec.name)
        stop.wait(every)


# The console's second pass, beside the sweep: a job's row follows the worker that finished it. The worker
# cannot write the row (its ACL forbids configuration) and the controller must not (one row, one writer),
# so the console — which already reads these heartbeats — is where the fact lands. See `vms/jobs.py`.
def _reap_loop(controllers, requests=(), rec_ctl=None, det_ctl=None, survey_ctl=None, every: float = 30.0) -> None:
    import time
    from .jobs import (ask_for_footage, clear_requests, expire_recordings, keep_what_fired, reap,
                       record_on_request, scan_what_arrived)
    while not stop.is_set():
        for c in controllers:
            try:
                moved = reap(c)
                if moved["done"] or moved["failed"]:
                    logging.info("%s: %d done, %d failed", c.spec.name, moved["done"], moved["failed"])
            except Exception:                         # noqa: BLE001
                logging.exception("the job reaper failed in %s — finished jobs will stay open", c.spec.name)
        for c in controllers if rec_ctl is not None else ():
            try:
                asked = ask_for_footage(c, rec_ctl)       # a job stuck on footage the DEVICE has: ask the recorder
                if asked:
                    logging.info("%s: asked the recorder for %d range(s)", c.spec.name, asked)
            except Exception:                             # noqa: BLE001
                logging.exception("asking for footage failed in %s — those jobs will wait", c.spec.name)
        for c in controllers if rec_ctl is not None and det_ctl is not None else ():
            try:
                made = scan_what_arrived(rec_ctl, det_ctl, c)   # footage that arrived from a device is a hole in the detections
                if made:
                    logging.info("%s: %d scan(s) queued over footage that just arrived", c.spec.name, made)
            except Exception:                             # noqa: BLE001
                logging.exception("queueing scans over new footage failed in %s — the detections keep the hole", c.spec.name)
        if survey_ctl is not None and rec_ctl is not None:
            try:
                kept = keep_what_fired(survey_ctl, rec_ctl)   # watch everything, copy what a model liked
                if kept:
                    logging.info("%s: asked the recorder to keep %d stretch(es)", survey_ctl.spec.name, kept)
            except Exception:                         # noqa: BLE001
                logging.exception("keeping what fired failed in %s — those minutes stay on the device", survey_ctl.spec.name)
        for c in requests:                            # the same division, one row simpler: fetched, so gone
            try:
                gone = clear_requests(c)
                if gone:
                    logging.info("%s: %d request(s) fetched and cleared", c.spec.name, gone)
            except Exception:                         # noqa: BLE001
                logging.exception("clearing requests failed in %s — they will be asked for again", c.spec.name)
        if rec_ctl is not None:
            # A scenario asked for ten minutes of a camera. Turning that into a recording is a write to
            # CONFIGURATION, and of the three processes only this one holds the token for it — a worker
            # writes none, and the controller writes placement. Both ends here: the row that starts, and
            # the row whose `until` has passed.
            try:
                started = record_on_request(rec_ctl, time.time())
                ended = expire_recordings(rec_ctl, time.time())
                if started or ended:
                    logging.info("%s: %d recording(s) started on request, %d ended", rec_ctl.spec.name, started, ended)
            except Exception:                         # noqa: BLE001
                logging.exception("timed recordings failed — a scenario's minutes may not have started, "
                                  "or a finished one is still recording")
        stop.wait(every)


def console() -> None:
    """The screen and the API: its own process, count as many as you like, a
    token for the operator's rows and nothing else."""
    from .config import SPEC
    from .console import serve
    from w2cplatform.spec import SpecController
    from .auto import AutoController
    from .config import AUTO_SPEC, DET_SPEC, DETJOB_SPEC, LIVE_SPEC, REC_SPEC, SURVEY_SPEC
    vars_ = open_vars(CONFIG_URL, writer="console",
                          acl={"console": SPEC.acl_console() + LIVE_SPEC.acl_console() + DET_SPEC.acl_console()
                               + REC_SPEC.acl_console() + DETJOB_SPEC.acl_console()
                               + SURVEY_SPEC.acl_console() + AUTO_SPEC.acl_console()})   # the operator's rows of EVERY subsystem it fronts
    objects = FsObjectStore(os.path.join(root, "objects"))
    ctl = VmsController(vars_, objects, capacity=int(os.environ.get("CAPACITY", "50")))
    archive = ArchiveResource(os.environ.get("SPOOL", "/data/spool"), os.environ.get("ARCHIVE", "/data/archive"))
    srv = serve(ctl, archive, os.environ.get("CONSOLE_HOST", "127.0.0.1"), int(os.environ.get("CONSOLE_PORT", "8080")),
                live_ctl=SpecController(LIVE_SPEC, vars_, objects),
                mounts={"det": SpecController(DET_SPEC, vars_, objects), "rec": SpecController(REC_SPEC, vars_, objects),
                        "detjob": SpecController(DETJOB_SPEC, vars_, objects),
                        "survey": SpecController(SURVEY_SPEC, vars_, objects),
                        # `AutoController` and not the platform's class: a scenario is refused where it is
                        # written, which is here, and the refusal has to be the subsystem's own words.
                        "auto": AutoController(vars_, objects)})
    logging.info("console on %s", srv.server_address)                     # no event database here: /events asks the resource process
    det_ctl, rec_ctl = SpecController(DET_SPEC, vars_, objects), SpecController(REC_SPEC, vars_, objects)
    job_ctl = SpecController(DETJOB_SPEC, vars_, objects)
    survey_ctl = SpecController(SURVEY_SPEC, vars_, objects)
    threading.Thread(target=_sweep_loop, args=([ctl, det_ctl, rec_ctl, job_ctl, survey_ctl],), daemon=True).start()
    # `[rec_ctl, ctl]`: two families of requests to clear now — footage a person asked for, and commands
    # somebody sent a device (a relay, a preset). Same division as everywhere: the worker performs and
    # says so in its heartbeat, the controller removes the row, because a worker writes no configuration.
    threading.Thread(target=_reap_loop, args=([job_ctl], [rec_ctl, ctl], rec_ctl, det_ctl, survey_ctl), daemon=True).start()
    stop.wait()
    srv.shutdown()


# The resource process — the platform's resource job on one box, the same as М11's `resource` job:
# - `ArchiveResource($SPOOL, $ARCHIVE)`; Variables opened with *no* writer and no ACL — the resource only
#   reads rows (the camera rows for media retention, `<sub>/retention/*` for buckets, `platform/mirror`).
# - `vms_resource(archive, hostname, $RESOURCE_URL, vars_, objects)` — the platform's `Resource` with
#   `ArchivePolicy` registered as the `vms` hook and an `EventDatabase($EVENTDB)` over the tree.
# - `serve(res, $RESOURCE_HOST, $RESOURCE_PORT, extra=vms_routes(archive), extra_put=vms_writes(archive))` — `/buckets`, `/events`,
#   `/mirrored`, `PUT /mirror`, plus the VMS's `/manifest/<cam>` and `/segment/<path>`.
# - one heartbeat (`platform/resources/<server>/heartbeat` — how the console finds this process), then
#   `restore()` (nothing to pull on one box: no peers), then `database.start()` — rebuilt from the tree,
#   tailed every 3 s — and the loop: a heartbeat every 10 s, the policy pass every 600 s (`pass_`: repair,
#   close buckets, media retention per camera row; then bucket retention by `vms/retention/<cam>`, which
#   tells the database what it removed; then the mirror, off).
def resource() -> None:
    """The resource process: the archive has no controller — it has a policy pass, a
    heartbeat, its HTTP, and the event database over its own tree."""
    import socket
    import time
    from w2cplatform.resource import serve
    from .config import REC_SPEC
    from .resource import refresh_volumes, vms_resource, vms_routes, vms_writes
    archive = ArchiveResource(os.environ.get("SPOOL", "/data/spool"), os.environ.get("ARCHIVE", "/data/archive"))
    vars_ = open_vars(CONFIG_URL)
    objects = FsObjectStore(os.path.join(root, "objects"))
    host, port = os.environ.get("RESOURCE_HOST", "127.0.0.1"), int(os.environ.get("RESOURCE_PORT", "8090"))
    res = vms_resource(archive, socket.gethostname(), os.environ.get("RESOURCE_URL", f"http://{host}:{port}"), vars_, objects,
                       database=os.environ.get("EVENTDB", ":memory:"))
    srv = serve(res, host, port, extra=vms_routes(archive, objects, res.server), extra_put=vms_writes(archive))
    res.heartbeat(); logging.info("restore: %s", res.restore())
    res.database.start()                                                  # a cache over THIS tree: rebuilt after restore, tailed every 3 s
    logging.info("resource %s on %s", res.server, srv.server_address)
    last_policy = 0.0
    while not stop.is_set():
        try:
            # The declared volumes are configuration and they change under a running process: a network
            # archive created on the console, a disk split in two. Read before the heartbeat, so the
            # spaces this box publishes are the spaces it is actually responsible for.
            refresh_volumes(res, vars_, objects, REC_SPEC.sub, time.time())
            res.heartbeat()
            if time.time() - last_policy >= 600:
                logging.info("policy: %s", res.pass_()); last_policy = time.time()
        except Exception:                                                 # noqa: BLE001
            logging.exception("resource pass failed")
        stop.wait(10)
    res.database.stop(); srv.shutdown()


if __name__ == "__main__":
    {"worker": worker, "controller": controller, "recorder": recorder, "reccontroller": reccontroller, "console": console, "resource": resource,
     "gateway": gateway, "livecontroller": livecontroller, "detworker": detworker, "detcontroller": detcontroller,
     "detjobworker": detjobworker, "detjobcontroller": detjobcontroller,
     "surveyworker": surveyworker, "surveycontroller": surveycontroller,
     "autoworker": autoworker, "autocontroller": autocontroller}[sys.argv[1]]()
