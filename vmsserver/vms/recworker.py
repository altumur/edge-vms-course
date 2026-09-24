"""recworker — the fourth subsystem's worker: the only one placed on top of the archive.

A recorder is a worker in the platform's sense (a slot claimed by CAS —
`r-1` — an assignment read from the store, an epoch per unit, a heartbeat
with capacity and headroom) whose unit is ONE RECORDING of a camera,
`rec/recordings/<name>` — a camera written to two archives has two of them,
and the row's `cam` field says whose footage this one holds. It does not
hold the camera: it subscribes to the fan-out of whichever VMS worker does
(`live_url` in the VMS heartbeat, found the way a gateway finds it — never
by calling a worker, never a second connection to the camera) and writes
footage into `rec/<name>/e<epoch>/` on the archive it is homed to, with the
manifest beside it. The worker that holds
the camera may be anywhere, `servers: shared`; the recorder must be where
the disks are — `requires: resource`, `servers: distinct` by default — and
when its server dies the controller moves its recordings to a server whose
resource answers (Lesson 4's two silences). Footage from before the move
stays where it was written, under the old epoch; the console's timeline
merges the two.

    RECORDER_NAME / SLOT_INDEX     -> the slot to claim: r-<index>
    SERVER_NAME (or the hostname)  -> `server` in the heartbeat: whose archive it writes into
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

from w2cplatform.console import holder_of
from w2cplatform.contract import Subsystem
from w2cplatform.objects import ObjectStore
from w2cplatform.resource import disk_space, space_settings
from w2cplatform.variables import Variables

from . import volumes
from .archive import ArchiveResource, overlaps, parse, subtract
from .config import rec_row
from .worker import FakeActuator, VmsWorker

log = logging.getLogger("recworker")
REC = Subsystem("rec")


class RecWorker(VmsWorker):
    """A recorder: `VmsWorker` over `rec/recordings/*`, its pipelines fed by the
    worker's fan-out, its segments promoted into this server's archive."""

    SUB = REC
    ROWS = "recordings"
    # How many closed ranges the heartbeat carries. A window and not a queue: the console acts on what it
    # sees, and a range that scrolled out was either acted on or is gone — which is why the console's
    # decision has to be idempotent on its own (it is: the job's id is the range).
    CLOSED_REPORTED = 32
    SLOT_PREFIX, NAME_ENV = "r", "RECORDER_NAME"
    parse_row = staticmethod(rec_row)

    def __init__(self, name: str | None, vars_: Variables, objects: ObjectStore, actuator=None, archive: ArchiveResource | None = None,
                 lease_ttl: float = 30.0, lease_margin: float = 5.0, clock=time.monotonic, wall=time.time,
                 server: str | None = None, capacity: int | None = None, instance: str | None = None, slot_ttl: float = 45.0,
                 env: dict | None = None, grace_seconds: float = 30.0,
                 window: tuple[int, int] | None = None, keep_days: float = 30.0, settle: float = 900.0,
                 stitch: float = 2.0):
        env = dict(os.environ if env is None else env)
        self.archive = archive or ArchiveResource(env.get("SPOOL", "/data/spool"), env.get("ARCHIVE", "/data/archive"), wall=wall)
        super().__init__(name, vars_, objects, actuator or FakeActuator(), lease_ttl, lease_margin, clock, wall, server, capacity, instance,
                         slot_ttl, archive_root=self.archive.root, env=env)
        # WHICH ARCHIVE THIS RECORDER WRITES INTO — its place, in the sense `place_by: volume` means. Three
        # ways to be told, in this order:
        #
        # `$VOLUME` — PINNED. A disk is bolted to one machine, so the unit file that knows which disk this
        # instance mounts is the right place to say so, the way `$RECORDER_NAME` says which slot it is.
        # A pinned recorder takes no hold and gives none up.
        #
        # Nothing pinned and volumes DECLARED (`rec/volumes/*`) — the recorder takes one, by CAS, and is
        # that volume's recorder until it stops or lapses (`volume_pass`). This is what makes a network
        # archive created on the console get served without anybody starting a process for it.
        #
        # Nothing pinned and nothing declared — the SERVER's own name, which is what every single-disk box
        # meant before any of this existed: one place, named after the machine, `home: srv-a` still true,
        # `place_by: volume` behaving exactly like `place_by: server`.
        self.pinned = bool(env.get("VOLUME"))
        self.default_volume = str(self.server or os.path.basename(self.archive.root.rstrip("/")) or "default")
        self.volume = str(env.get("VOLUME") or self.default_volume)
        self.full_capacity = self.capacity           # what it reports while it has a place to record in
        self.volume_error = ""                       # why the archive it holds will not open, if it will not
        self.grace_seconds = grace_seconds
        # Backfill (Lesson 16): the hours in local time it may run in (None: any), how far back it may
        # reach, how fresh it must NOT touch, and the seam tolerance that stops 144 seams a day from
        # looking like 144 gaps.
        self.window, self.keep_days, self.settle, self.stitch = window, keep_days, settle, stitch
        self.space_probe = disk_space                # the disk under the archive; a test cannot fill one
        self.backfill_budget = 0                    # ranges per pass; 0 = only what an operator asks for
        self.backfilled = 0
        self.fetched: list[str] = []                # request ids this worker has fetched — the heartbeat carries them
        self.closed: list[str] = []                 # ranges promoted from a device: `<unit>|<from>|<to>`, for the console
        self.promoted = 0
        self.waiting: set[str] = set()                                        # units with nobody holding their camera
        self.sources: dict[str, str] = {}                                     # what each running pipeline subscribed to
        for p in self.archive.closed_in_spool(grace_seconds, self.wall()):     # what the last instance closed but did not promote
            self.archive.promote(p); self.promoted += 1

    # -- where a camera's stream is: the VMS heartbeat, never a call to the worker ------------------
    def source(self, cam) -> tuple[str, str] | None:
        """`(server, source)` of the worker holding the camera, from its heartbeat; None if nobody does.
        The source is the worker's shared-memory branch (`live_shm`, shm://…) when that worker is on
        THIS server — the same bytes with no RTSP hop, no fan-out process on the recording path — and
        its RTSP fan-out (`live_url`) otherwise."""
        found = holder_of(self.objects, "vms/", cam, self.wall(), phase="running", field="live_url")
        if found is None:
            return None
        _, hb, st = found
        server = hb.extra.get("server", "?")
        if server == self.server and st.get("live_shm"):
            return server, st["live_shm"]
        return server, st["live_url"]

    # A recording's pipeline needs a source: `rtspsrc location=<live_url> ! archivesink` under this
    # recorder's epoch. No source (the camera is held by nobody yet) means "cannot start now": the
    # reconciler backs off and retries, and the status says `waiting`.
    def enrich(self, cam: dict) -> dict | None:
        # Two identities, and this is the one method where both are used in three lines: `cam["cam"]` is
        # WHOSE fan-out to subscribe to, `cam["id"]` is WHICH recording is subscribing. They were the same
        # string while the spec said `id: cam`; they stopped being it, and nothing here changed.
        src = self.source(cam["cam"])
        if src is None:
            self.waiting.add(cam["id"])
            return None
        self.waiting.discard(cam["id"])
        self.sources[cam["id"]] = src[1]
        return dict(cam, source=src[1], source_server=src[0], via="shm" if src[1].startswith("shm://") else "rtsp",
                    spool=self.archive.spool, archive=self.archive.root)

    def status_extra(self, cam: dict) -> dict:
        src = self.source(cam["cam"])
        out = {"cam": str(cam["cam"]), "source": src[1] if src else None, "via": (None if src is None else "shm" if src[1].startswith("shm://") else "rtsp")}
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

    # What this recorder adds to the heartbeat: how many segments are in the spool and not yet in the
    # archive. Normally nought or one — the fragment being written — and it is the answer to the only
    # question a rolling upgrade really asks: is it safe to stop this machine now. A recorder whose units
    # have left promotes what they closed on its next pump, and then this is zero.
    def heartbeat_extra(self) -> dict:
        # `volume`: the archive this recorder writes into, and the place the policy counts in. A box with
        # three disks runs three recorders, and `servers: distinct` over `place_by: volume` puts one
        # recording's worth of work on each — which is what the operator meant by three disks.
        #
        # EMPTY means something different from absent, and both are said on purpose. Absent: a recorder
        # that was never told about volumes, read as one place named after its server. Empty: this process
        # is a SPARE — it is running, it has taken no volume, and it is not a place to record. It reports
        # zero capacity with it, so the two halves of the same statement cannot drift apart.
        #
        # `fetched`: the requests this recorder has closed. It cannot delete the rows — a worker writes no
        # configuration — so it says which ones are done and the console removes them.
        return {**super().heartbeat_extra(),         # `fetched`: the same answer every worker gives
                "volume": self.volume,
                # Empty unless the archive this process holds will not open. Published because the
                # alternative is the failure that looks like health: a fresh hold, a green console and
                # nothing being written. Whatever reads it must not count that volume as served.
                "volume_error": self.volume_error,
                "spool": len(self.archive.closed_in_spool(0.0, self.wall())),
                "closed": ",".join(self.closed)}

    # -- which archive this recorder writes into ------------------------------------------------------
    # Run once a pass, after the slot and the leases. Four outcomes, and the one that matters is the last.
    #
    # Pinned: nothing to decide. Nothing declared: the server's own name, as before volumes were rows.
    # Holding a volume that is still declared and still enabled: renew, keep recording.
    # Otherwise: let go of what is no longer ours and try to take a free one — and if every volume is
    # taken, become a SPARE. A spare is a normal, visible state: a process that is running, carrying
    # nothing, and waiting for a place. It is what makes the next network archive somebody creates on the
    # console get served in one pass instead of one deploy.
    #
    # Losing a hold is not the same as losing a slot. The slot says which process of the deployment this
    # is; the hold says which archive it writes into. A process can lose the second and keep the first,
    # and it then stops recording — the footage of those recordings belongs to whoever holds the volume
    # now — without fencing the instance, which would mean it could never take another.
    def volume_pass(self) -> str:
        if self.pinned:
            return self.volume
        rows = {v.name: v for v in volumes.declared(self.vars)}
        free = volumes.servable(list(rows.values()), self.server)
        held = self.hold
        if held is not None and (held not in free or not self.renew_hold()):
            self.leave_volume(f"volume {held} is not this recorder's any more")   # withdrawn, disabled, or taken from us
        if self.hold is None and not free:
            # Nothing declared anywhere: the box as it was before volumes were rows — one place, named
            # after the server. Note this is reached after letting go above, so withdrawing the last
            # volume does not leave a process quietly writing into it.
            self.volume, self.capacity, self.volume_error = self.default_volume, self.full_capacity, ""
            return self.volume
        # Take one, and then OPEN it — the step that was missing. Holding a volume and being unable to
        # write into it is the worst failure this subsystem has, because every number says it is fine:
        # the hold is fresh, the console counts it served, and no footage is being written. So the open
        # decides, and a volume that will not open is handed back — but only if there is somewhere else
        # to go. Letting go of the only archive this box can reach would stop it recording altogether,
        # which is a worse answer than recording into a broken one and saying so.
        skipped: set[str] = set()
        broken: list[tuple[str, str]] = []                         # what we handed back on the way, and why
        while True:
            if self.hold is None:
                candidates = [n for n in free if n not in skipped]
                taken = self.claim_hold(candidates) if candidates else None
                if taken is None:
                    # Nothing else to be had — every other declared archive has a live recorder, or there
                    # are none. If we handed a broken one back getting here, take it BACK rather than
                    # leave this box holding nothing: writing into an archive that will not open and
                    # saying so is bad, and not recording at all because of diagnostics is worse.
                    if broken and self.claim_hold([broken[0][0]]) is not None:
                        self.volume, self.capacity, self.volume_error = self.hold, 0, broken[0][1]
                        logging.error("%s: %s is the only archive it can reach and it will not open: %s",
                                      self.name, self.hold, self.volume_error)
                        return self.volume
                    self.volume, self.capacity = "", 0             # a spare is not a place to put a recording
                    return self.volume
            err = self._write_into(rows[self.hold])
            if err is None:
                self.volume, self.capacity, self.volume_error = self.hold, self.full_capacity, ""
                return self.volume
            logging.warning("%s: %s will not open (%s) — looking for another", self.name, self.hold, err)
            skipped.add(self.hold)
            broken.append((self.hold, err))
            self.volume_error = err
            self.release_hold()                                    # so somebody who CAN write there may take it

    # Taking a volume means writing into ITS tree, so the archive this process promotes into follows the
    # hold. The spool does not: it is local scratch, one per process, and what is in it belongs to the
    # volume we were holding when it was recorded — which is why `leave_volume` promotes before letting
    # go, while we may still write there.
    # Returns None when the archive is open and writable, or the reason it is not. Opening is the only
    # honest test: a declaration can name a path that does not exist, a mount that is gone or a bucket
    # nobody can reach, and none of that is visible in the row.
    def _write_into(self, vol) -> str | None:
        if not vol.url or vol.url == self.archive.root:
            return None
        try:
            archive = ArchiveResource(self.archive.spool, vol.url, wall=self.wall)
        except OSError as e:
            return str(e)
        self.archive, self.archive_root = archive, vol.url
        logging.info("%s: writing into %s (%s)", self.name, vol.name, vol.url)
        return None

    # Stop writing into a volume that is no longer ours — the administrator withdrew it, or the hold
    # lapsed and somebody else took it. Every recording of that archive is stopped and released, which is
    # the reassignment path of `lease_pass` and not the zombie one: the process keeps its slot, keeps
    # running, and may take another volume on the next pass.
    def leave_volume(self, why: str) -> None:
        logging.warning("%s: %s — stopping its recordings", self.name, why)
        try:
            self.promote_closed()                    # what is in the spool belongs to THAT archive, and we still hold it
        except OSError as e:                         # a volume that went away under us: the footage is where it is
            logging.warning("%s: could not promote the spool into %s: %s", self.name, self.archive.root, e)
        for uid in list(self.reconciler.actual):
            self.actuator("stop", {"id": uid})
            self.reconciler.actual.pop(uid, None)
            self.release(str(uid))
        self.release_hold()
        self.volume, self.capacity = "", 0

    def lease_pass(self) -> list[str]:
        lost = super().lease_pass()
        if self.recording_allowed:                   # a fenced instance decides nothing about volumes
            self.volume_pass()
        return lost

    def promote_closed(self) -> int:
        n = 0
        for p in self.archive.closed_in_spool(self.grace_seconds, self.wall()):
            self.archive.promote(p); n += 1
        self.promoted += n
        return n

    def pump_once(self) -> None:
        super().pump_once()                         # …which now includes `requests()`: the base serves the
                                                    # family for every subsystem, and this one overrides
                                                    # the method, not the call — asking twice a pass would
                                                    # spend the budget twice
        self.promote_closed()
        if self.backfill_budget:                    # bounded, and inside the window: it shares the device's uplink
            self.backfill(self.backfill_budget)

    # -- backfill: closing our gaps from the device's own archive (Lesson 16) ---------------------------
    # The card exists because the camera kept recording while we could not, so replication is not "copy
    # everything" — it is the difference between two coverages. Desired: continuous. Actual: the manifest.
    # The difference is the work. Lesson 2's loop, over time instead of pipelines.
    def our_coverage(self, unit) -> list[tuple[float, float]]:
        return self.archive.coverage(str(unit), self.stitch)

    # What the device has and we do not, bounded at both ends. Not older than our own retention — otherwise
    # backfill and retention chase each other round the clock, for ever. Not fresher than `settle` — the
    # last minutes are being written right now, are in no manifest yet, and we would be fetching what we
    # are recording.
    def gaps(self, unit, coverage: dict, now: float) -> list[tuple[float, float]]:
        want = (max(float(coverage["from"]), now - self.keep_days * 86400),
                min(float(coverage["to"]), now - self.settle))
        return [] if want[1] <= want[0] else subtract(want, self.our_coverage(unit))

    # The disk is over its high mark: the resource is freeing space this minute, and backfill exists to
    # bring more in. Without this line they chase each other for ever on a full disk — the same trap
    # `keep_days` closes in time, closed here in space. Not a `force` override either: an operator asking
    # for a range cannot be given one the resource is about to delete.
    def under_pressure(self) -> bool:
        knob = space_settings(self.vars)
        if not knob["enabled"]:
            return False
        total, free = self.space_probe(self.archive.root)
        return bool(total) and (total - free) > total * knob["high"]

    # Local time, and the one place in the course where that is right: "at night" is night where the camera
    # is, not where the server is. `(22, 6)` wraps midnight — without that branch it would never arrive.
    def in_window(self, now: float) -> bool:
        if not self.window:
            return True
        h = time.localtime(now).tm_hour
        a, b = self.window
        return a <= h < b if a < b else (h >= a or h < b)

    # `(cam, playback_url, coverage)` for a recording whose camera is held by a worker that serves the
    # device's own archive — found the way everything is found here: in the holder's heartbeat.
    def device_source(self, cam) -> tuple[str, dict] | None:
        found = holder_of(self.objects, "vms/", cam, self.wall(), field="playback_url")
        if found is None or not found[2].get("coverage"):
            return None                       # no phase: a channel held only for its archive answers too
        return found[2]["playback_url"], found[2]["coverage"]

    # -- what an operator asked for: `rec/requests/<id>`, written by the console ------------------------
    #
    # The ordinary pass is bounded by a budget and an hour because backfill competes with live for the
    # device's uplink. A range a PERSON asked for is different work: they are looking at that gap now, and
    # the night is not a useful answer. So these are fetched outside both — but not outside
    # `under_pressure`, because a disk that is being emptied this minute cannot be given more.
    #
    # The request is not cleared here. A worker's token writes its slot and its epochs, never configuration
    # (М10A Lesson 10), so the recorder REPORTS what it fetched in its heartbeat and the console's reaper
    # removes the row — the same division as a scan that finishes (М10B Lesson 21).
    def requests(self, budget: int = 2, now: float | None = None) -> list[dict]:
        now = self.wall() if now is None else now
        if self.under_pressure():
            return []
        mine = {str(r["id"]) for r in self.rows}
        done: list[dict] = []
        for key in self.vars.list(REC.requests_prefix()):
            if len(done) >= budget:
                break
            it, _ = self.vars.get(key)
            if not it or str(it.get("unit", "")) not in mine:
                continue                                     # another recorder's recording: not ours to fetch
            src = self.device_source(it.get("cam", it["unit"]))
            rid = key.rsplit("/", 1)[1]
            if src is None:
                continue                                     # nobody holds the device right now; ask again next pass
            r = self.fetch(str(it["unit"]), str(it.get("cam", it["unit"])), src[0], float(it["from"]), float(it["to"]))
            if r.get("skipped"):
                continue                                     # not fetched: reporting it would have the console
                                                             # delete a request nobody served
            self.fetched.append(rid)                         # the heartbeat says so; the console removes the row
            done.append({**r, "request": rid})
        return done

    # Bounded work, on request — never in the ordinary pass, the way `rebalance(budget)` is bounded
    # (Lesson 13): backfill competes with live for the device's uplink, so it gets a ceiling and an hour.
    def backfill(self, budget: int = 1, now: float | None = None, force: bool = False) -> list[dict]:
        now = self.wall() if now is None else now
        if not (force or self.in_window(now)):
            return []
        if self.under_pressure():
            return []
        done: list[dict] = []
        for row in self.rows:
            if len(done) >= budget:
                break
            src = self.device_source(row["cam"])            # the DEVICE is the camera's
            if src is None:
                continue
            url, cov = src
            for (t0, t1) in self.gaps(row["id"], cov, now)[:budget - len(done)]:   # the GAPS are this recording's
                done.append(self.fetch(row["id"], row["cam"], url, t0, t1))
        return done

    # One range: fetch it, and promote what came back as OURS — `source: edge`, our epoch, our manifest,
    # our retention. The overlap is checked a second time here because live recording may have reached the
    # same minutes while we were fetching; a segment that would land on top of one we already have is
    # dropped rather than written.
    def fetch(self, unit, cam, url: str, t0: float, t1: float) -> dict:
        unit = str(unit)
        if not self.may_write(unit):
            return {"unit": unit, "cam": str(cam), "from": t0, "to": t1, "skipped": "no lease"}
        paths = self.actuator.record_range(unit, f"{url}?from={t0}&to={t1}", self.epochs.get(unit, 0),
                                           t0, t1, self.archive.spool)
        have, kept = self.our_coverage(unit), 0
        for p in paths:
            parsed = parse(p, self.archive.spool)
            span = (parsed[2].timestamp(), os.path.getmtime(p)) if parsed else (t0, t1)
            if overlaps(have, span):
                os.remove(p); continue                   # live recording got there while we were fetching
            self.archive.promote(p, source="edge"); kept += 1
        self.backfilled += kept
        if kept:
            # What arrived is now ordinary footage — and a hole in the DETECTIONS, because nothing was
            # watching this camera while nothing was recording it. The console turns each of these into a
            # scan (М10B Lesson 22), so the two holes close together. Reported here and not written
            # anywhere: a worker's token writes no configuration.
            self.closed = (self.closed + [f"{unit}|{t0:.0f}|{t1:.0f}"])[-self.CLOSED_REPORTED:]
        return {"unit": unit, "cam": str(cam), "from": t0, "to": t1, "segments": kept}

    def metrics_text(self) -> str:
        return (f"# TYPE rec_recordings_running gauge\nrec_recordings_running {len(self.reconciler.actual)}\n"
                f"# TYPE rec_segments_promoted counter\nrec_segments_promoted {self.promoted}\n"
                f"# TYPE rec_segments_backfilled counter\nrec_segments_backfilled {self.backfilled}\n")
