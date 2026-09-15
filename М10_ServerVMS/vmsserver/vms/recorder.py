"""vmsrecorder — the fourth subsystem's worker: the only one placed on top of the archive.

A recorder is a worker in the platform's sense (a slot claimed by CAS —
`r-1` — an assignment read from the store, an epoch per unit, a heartbeat
with capacity and headroom) whose unit is one camera's RECORDING,
`rec/recordings/<cam>`. It does not hold the camera: it subscribes to the
fan-out of whichever VMS worker does (`live_url` in the VMS heartbeat, found
the way a gateway finds it — never by calling a worker, never a second
connection to the camera) and writes footage into `rec/<cam>/e<epoch>/` on
ITS server's archive, with the manifest beside it. The worker that holds
the camera may be anywhere, `servers: shared`; the recorder must be where
the disks are — `requires: resource`, `servers: distinct` by default — and
when its server dies the controller moves its recordings to a server whose
resource answers (Lesson 4's two silences). Footage from before the move
stays where it was written, under the old epoch; the console's timeline
merges the two.

    RECORDER_NAME / NOMAD_ALLOC_INDEX  -> the slot to claim: r-<index>
    NOMAD_NODE_NAME (or the hostname)  -> `server` in the heartbeat: whose archive it writes into
    SPOOL, ARCHIVE                     -> the archive resource's two roots on this server
    CAPACITY                           -> recordings this server's disks and NIC can take — its own number

Same class as the worker (`VmsWorker` over the `rec` rows): the same
reconciler, the same gate (an epoch per unit, a lease, the fence at the
slot), the same heartbeat. What differs is what the pipeline needs — a
source, not a camera — and that closed segments are promoted from the
spool into the archive on every pass.
"""
from __future__ import annotations

import logging
import os
import time

from psimplatform.console import heartbeats
from psimplatform.contract import Subsystem
from psimplatform.objects import ObjectStore
from psimplatform.variables import Variables

from .archive import ArchiveResource
from .config import rec_row
from .worker import FakeActuator, VmsWorker

log = logging.getLogger("vmsrecorder")
REC = Subsystem("rec")


class RecWorker(VmsWorker):
    """A recorder: `VmsWorker` over `rec/recordings/*`, its pipelines fed by the
    worker's fan-out, its segments promoted into this server's archive."""

    SUB = REC
    ROWS = "recordings"
    SLOT_PREFIX, NAME_ENV = "r", "RECORDER_NAME"
    parse_row = staticmethod(rec_row)

    def __init__(self, name: str | None, vars_: Variables, objects: ObjectStore, actuator=None, archive: ArchiveResource | None = None,
                 lease_ttl: float = 30.0, lease_margin: float = 5.0, clock=time.monotonic, wall=time.time,
                 server: str | None = None, capacity: int | None = None, instance: str | None = None, slot_ttl: float = 45.0,
                 env: dict | None = None, grace_seconds: float = 30.0):
        env = dict(os.environ if env is None else env)
        self.archive = archive or ArchiveResource(env.get("SPOOL", "/data/spool"), env.get("ARCHIVE", "/data/archive"), wall=wall)
        super().__init__(name, vars_, objects, actuator or FakeActuator(), lease_ttl, lease_margin, clock, wall, server, capacity, instance,
                         slot_ttl, archive_root=self.archive.root, env=env)
        self.grace_seconds = grace_seconds
        self.promoted = 0
        self.waiting: set[int] = set()
        self.sources: dict[int, str] = {}                                     # what each running pipeline subscribed to
        for p in self.archive.closed_in_spool(grace_seconds, self.wall()):     # what the last instance closed but did not promote
            self.archive.promote(p); self.promoted += 1

    # -- where a camera's stream is: the VMS heartbeat, never a call to the worker ------------------
    def source(self, cam) -> tuple[str, str] | None:
        """`(server, source)` of the worker holding the camera, from its heartbeat; None if nobody does.
        The source is the worker's shared-memory branch (`live_shm`, shm://…) when that worker is on
        THIS server — the same bytes with no RTSP hop, no fan-out process on the recording path — and
        its RTSP fan-out (`live_url`) otherwise."""
        for hb in heartbeats(self.objects, "vms/").values():
            for st in hb.status:
                if str(st.get("id")) == str(cam) and st.get("phase") == "running" and st.get("live_url"):
                    server = hb.extra.get("server", "?")
                    if server == self.server and st.get("live_shm"):
                        return server, st["live_shm"]
                    return server, st["live_url"]
        return None

    # A recording's pipeline needs a source: `rtspsrc location=<live_url> ! archivesink` under this
    # recorder's epoch. No source (the camera is held by nobody yet) means "cannot start now": the
    # reconciler backs off and retries, and the status says `waiting`.
    def enrich(self, cam: dict) -> dict | None:
        src = self.source(cam["id"])
        if src is None:
            self.waiting.add(cam["id"])
            return None
        self.waiting.discard(cam["id"])
        self.sources[cam["id"]] = src[1]
        return dict(cam, source=src[1], source_server=src[0], via="shm" if src[1].startswith("shm://") else "rtsp",
                    spool=self.archive.spool, archive=self.archive.root)

    def status_extra(self, cam: dict) -> dict:
        src = self.source(cam["id"])
        out = {"cam": str(cam["id"]), "source": src[1] if src else None, "via": (None if src is None else "shm" if src[1].startswith("shm://") else "rtsp")}
        if cam["id"] in self.waiting and cam["id"] not in self.reconciler.actual:
            out["why"] = "camera held by nobody"
        return out

    def status(self) -> list[dict]:
        out = super().status()
        for st in out:
            if st["phase"] != "running" and st["id"] in self.waiting and st["enabled"]:
                st["phase"] = "waiting"
        return out

    # -- the passes: the worker's, plus a re-subscription when the camera's holder moved, plus the
    # promotion of closed segments ---------------------------------------------------------------------
    # The camera's worker moved: the source is another server's fan-out now — or, if it moved HERE, the
    # shared-memory branch. The pipeline reading the old source is stopped and counted lost, so the
    # reconciler starts it again on the new one, under a new rec epoch (a start is a new writer).
    def resubscribe(self, now: float | None = None) -> list[int]:
        now = self.now() if now is None else now
        moved = []
        for cid in list(self.reconciler.actual):
            src = self.source(cid)
            if src is not None and self.sources.get(cid) not in (None, src[1]):
                self.actuator("stop", {"id": cid})
                self.reconciler.lost(cid, now)
                self.sources.pop(cid, None)
                moved.append(cid)
                log.info("%s: camera %s is held elsewhere now (%s): re-subscribing", self.name, cid, src[1])
        return moved

    def reconcile_once(self, now: float | None = None) -> list[tuple[str, int]]:
        self.resubscribe(now)
        return super().reconcile_once(now)

    def promote_closed(self) -> int:
        n = 0
        for p in self.archive.closed_in_spool(self.grace_seconds, self.wall()):
            self.archive.promote(p); n += 1
        self.promoted += n
        return n

    def pump_once(self) -> None:
        super().pump_once()
        self.promote_closed()

    def metrics_text(self) -> str:
        return (f"# TYPE rec_recordings_running gauge\nrec_recordings_running {len(self.reconciler.actual)}\n"
                f"# TYPE rec_segments_promoted counter\nrec_segments_promoted {self.promoted}\n")
