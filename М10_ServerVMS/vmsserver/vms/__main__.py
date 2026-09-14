"""python3 -m vms worker|controller|console — the three processes, on one box.

    PLATFORM_DIR=/data/platform     the platform's stores (config/, objects/)
    SPOOL=/data/spool  ARCHIVE=/data/archive  MEDIA_DIR=/data/media
    WORKER_NAME=w-1                  the slot to claim (systemd: %i); unset: NOMAD_ALLOC_INDEX → w-<index>;
                                     neither: the first free slot, a lapsed one first
    CAPACITY=50                      cameras this worker can carry — exported as headroom for the autoscaler
    CONSOLE_PORT=8080                the console (its own process, its own token: the operator's rows, never placement)
"""
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


if __name__ == "__main__":
    {"worker": worker, "controller": controller, "console": console}[sys.argv[1]]()
