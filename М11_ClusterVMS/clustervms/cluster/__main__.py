"""python3 -m cluster worker | controller | recorder | reccontroller | console | resource — the jobs
(each resource keeps the event database over its own tree; the recorder is the only writer of footage).

    CONFIG_URL                         the store, as a URL: nomad://host:port here, k8s://ns/prefix at a k8s site,
                                       file:///path on a bench. Defaults from NOMAD_ADDR; NOMAD_TOKEN is the task's
                                       own workload identity and is read by the nomad backend alone
    OBJECTS=variables://objects        the object store — heartbeats and the snapshot — as Variables (the default);
                                       s3+http://… when a cluster is large enough to want MinIO; file:///path on a bench
    SLOT_INDEX, SERVER_NAME, LABELS    worker, recorder: the slot to claim (w-<i>, r-<i>), the server, what it can reach —
                                       neutral names (`psimplatform/runtime.py`); the jobspec maps NOMAD_ALLOC_INDEX,
                                       node.unique.name and meta.labels into them, a k8s manifest the ordinal and a fieldRef
    ARCHIVE                            worker: where its events go (the resource on its server); recorder and resource: the same disks
    SPOOL                              recorder: where its pipelines write before promotion
    RESOURCE_URL                       resource: how the console reaches this server's manifests and events
    EVENTDB                            resource: where its event database lives (default :memory: — a cache, rebuilt on start)
    CAPACITY                           worker: cameras it can carry on this server
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time

import cluster  # noqa: F401  — puts М10's vmsserver on sys.path
from psimplatform import runtime
from psimplatform.variables import open_vars

# The store seam. `nomad://` is registered by `cluster/variables.py`; the default keeps the
# cluster working with no new environment, and a k8s site changes this one variable.
CONFIG_URL = os.environ.get("CONFIG_URL") or "nomad://" + os.environ.get("NOMAD_ADDR", "127.0.0.1:4646").replace("http://", "")

from cluster.objectstore import open_store
from cluster.variables import NomadVariables

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
stop = threading.Event()
for s in (signal.SIGTERM, signal.SIGINT):
    signal.signal(s, lambda *_: stop.set())
objects = open_store(os.environ.get("OBJECTS", "variables://objects"))
spool, archive = os.environ.get("SPOOL", "/data/spool"), os.environ.get("ARCHIVE", "/data/archive")


def worker() -> None:
    """holds the camera: one connection, one epoch, one fan-out (rtsp://<server>:8554/<cam>), its events into
    the resource on its server. No spool, no footage: recording is the recorder's."""
    from cluster.worker import ClusterWorker
    try:
        from gstvms.actuator import GstActuator
        act = GstActuator()
    except ImportError:
        logging.warning("no GStreamer: the fake actuator holds nothing"); act = None
    w = ClusterWorker(open_vars(CONFIG_URL), objects, act)
    logging.info("worker %s on %s (alloc %s) claimed its slot; labels %s", w.name, w.server, w.alloc, w.labels)
    w.run(stop=stop)                              # SIGTERM from Nomad → release_slot(): scale-in, not a crash


def recorder() -> None:
    """the only writer of footage: subscribes to the worker's fan-out, writes rec/<cam>/e<epoch>/ on THIS server's
    archive, promotes closed segments from the spool on every pass."""
    from vms.archive import ArchiveResource
    from cluster.recorder import ClusterRecorder
    try:
        from gstvms.actuator import GstRecActuator
        act = GstRecActuator(spool, archive, int(os.environ.get("SEGMENT_SECONDS", "600")))
    except ImportError:
        logging.warning("no GStreamer: the fake actuator records nothing"); act = None
    r = ClusterRecorder(open_vars(CONFIG_URL), objects, act, archive=ArchiveResource(spool, archive))
    logging.info("recorder %s on %s (alloc %s) claimed its slot; labels %s", r.name, r.server, r.alloc, r.labels)
    r.run(stop=stop)


def reccontroller() -> None:
    """count = 1, the only writer of rec placement: which recorder writes which camera's footage, where the
    resource answers, one recorder per server by default (rec/policy)."""
    from psimplatform.spec import SpecController
    from vms.config import REC_SPEC
    ctl = SpecController(REC_SPEC, open_vars(CONFIG_URL), objects, capacity=int(os.environ.get("CAPACITY", "50")))
    while not stop.is_set():
        try:
            ctl.ensure_placed(); ctl.redistribute(); ctl.unplace_deleted()
        except Exception:                         # noqa: BLE001
            logging.exception("rec placement pass failed")
        stop.wait(5)


def controller() -> None:
    """count = 1, the only writer of placement. No HTTP: nothing asks it anything."""
    from cluster.controller import ClusterController
    ctl = ClusterController(open_vars(CONFIG_URL), objects, capacity=int(os.environ.get("CAPACITY", "50")),
                            cluster=os.environ.get("CLUSTER", "cluster-a"))
    while not stop.is_set():
        try:
            ctl.ensure_placed(); ctl.redistribute(); ctl.publish_snapshot()
        except Exception:                         # noqa: BLE001
            logging.exception("placement pass failed")
        stop.wait(5)


def console() -> None:
    """one per server, a system job: the page and the API. Its token writes the
    operator's rows and nothing else; a create is placed by the controller's next
    pass. No event database of its own: /events asks the live resources and merges."""
    from cluster.console import serve
    from cluster.controller import ClusterController
    from psimplatform.spec import SpecController
    from vms.config import REC_SPEC
    vars_ = open_vars(CONFIG_URL)
    ctl = ClusterController(vars_, objects, capacity=int(os.environ.get("CAPACITY", "50")),
                            cluster=os.environ.get("CLUSTER", "cluster-a"))
    srv = serve(ctl, os.environ.get("CONSOLE_HOST", "0.0.0.0"), int(os.environ.get("CONSOLE_PORT", "8080")),
                archive_root=archive if os.path.isdir(archive) else None,     # marks go into this server's resource, if it has one
                rec_ctl=SpecController(REC_SPEC, vars_, objects))              # the recorder at /rec/…: the page's Record toggle
    stop.wait()
    srv.shutdown()


def resource() -> None:
    """М10's resource process as a job: the platform's Resource with the VMS registered on it, and the event database over its own tree."""
    from vms.archive import ArchiveResource
    from cluster.resource import cluster_resource, vms_routes
    from psimplatform.resource import serve
    arch = ArchiveResource(spool, archive)
    server = runtime.server(os.environ)
    url = os.environ.get("RESOURCE_URL", f"http://{server}:8090")
    res = cluster_resource(arch, server, url, open_vars(CONFIG_URL), objects, database=os.environ.get("EVENTDB", ":memory:"))
    srv = serve(res, "0.0.0.0", int(os.environ.get("RESOURCE_PORT", "8090")), extra=vms_routes(arch))
    res.heartbeat(); logging.info("restore: %s", res.restore())    # back with an empty disk? pull my buckets from my peers first
    res.database.start()                                              # a cache over MY tree: rebuilt after restore, tailed every 3 s
    last_policy = 0.0
    while not stop.is_set():
        try:
            res.heartbeat()
            if time.time() - last_policy >= 600:
                logging.info("policy: %s", res.pass_()); last_policy = time.time()
        except Exception:                         # noqa: BLE001
            logging.exception("resource pass failed")
        stop.wait(10)
    res.database.stop(); srv.shutdown()


if __name__ == "__main__":
    {"worker": worker, "controller": controller, "recorder": recorder, "reccontroller": reccontroller,
     "console": console, "resource": resource}[sys.argv[1]]()
