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

import errno
import logging
import os
import threading
import time

from w2cplatform.console import heartbeats, holder_of
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


# An archive that fails is either AWAY or WRONG, and the two are answered differently: an archive that is
# away is kept and buffered into, one that is wrong is handed back so its recordings can go somewhere that
# works. The errno says which.
#
# WRONG is only what nothing but a person will change: no permission (a key revoked, a policy changed), a
# path component that is a file, a read-only filesystem. Everything else is AWAY — a timeout, a refused or
# reset connection, a network that is down, an I/O error on a mount that went quiet — and so is whatever
# nobody listed, because the cost of guessing wrong is lopsided: an away archive handed back reshuffles
# recordings for a link that is back in a minute, while a wrong one kept is a spool that queues, visibly.
#
# Two that look wrong and are not. "No space" is an archive its OWN watermark empties (Lesson 18), and that
# pass is run by the recorder holding it: hand it back and the one process that could free it stops
# holding it. "No such file" is ambiguous at promotion — it is as likely the spool's copy that vanished as
# the archive's directory — and a wrong guess there would hand back a working volume.
PERMANENT = {errno.EACCES, errno.EPERM, errno.ENOTDIR, errno.EISDIR, errno.EROFS}


def failure_kind(e: BaseException) -> str:
    """`permanent` when only a person can fix it, else `transient`."""
    return "permanent" if getattr(e, "errno", None) in PERMANENT else "transient"


class RecWorker(VmsWorker):
    """A recorder: `VmsWorker` over `rec/recordings/*`, its pipelines fed by the
    worker's fan-out, its segments promoted into this server's archive."""

    SUB = REC
    ROWS = "recordings"
    # How long one pass waits on a promotion before moving on, and how long a promotion may run before it
    # is reported stuck. The first keeps a healthy archive synchronous — a local disk or a live bucket
    # finishes well inside it, so a pass that promotes still ends with the segment in the archive. The
    # second is the only way a hang gets said at all: nothing raises, so the recorder has to notice the
    # silence itself. Both in seconds.
    PROMOTE_WAIT = 5.0
    PROMOTE_STUCK = 60.0
    # How many of its own segments one promotion moves. In ordinary running a pass finds nought or one, and
    # this is never reached; it is for the queue an outage leaves behind. It keeps each promotion SHORT —
    # so what the heartbeat says between passes is true — and it paces the drain rather than emptying an
    # hour of footage in one go. It is a pace and not a bandwidth cap: shaping the uplink is bytes per
    # second, and that is not this.
    PROMOTE_BUDGET = 8
    # How long a volume that refused writes is left alone before this recorder tries it again. Opening it
    # may well succeed — the directories are there — and the first write fail again, so without a pause a
    # recorder flaps between taking and dropping the same broken archive. Long enough not to flap, short
    # enough that a key somebody fixed is picked up without a restart.
    REFUSED_FOR = 600.0
    # How long `volume_pass` waits on OPENING an archive before calling it away. Opening is a `makedirs` on the
    # root — near instant when the archive is there — and it happens inside `lease_pass`, so this wait is
    # the most a hung mount may take from the renewals, once per volume taken.
    OPEN_WAIT = 5.0
    # How many closed ranges the heartbeat carries. A window and not a queue: the console acts on what it
    # sees, and a range that scrolled out was either acted on or is gone — which is why the console's
    # decision has to be idempotent on its own (it is: the job's id is the range).
    CLOSED_REPORTED = 32
    # How long a primary recording may be not written before a `when: offline` backup stands in for it
    # (Lesson 26). Every event-driven recording starts this way — a row appears, a pipeline takes a few
    # seconds — and waking the backup for each would make it record every event twice. The product's
    # number, from what a start takes on a real box.
    START_GRACE = 20.0
    # How long a released backup keeps writing after its primary is written again. The primary's first
    # segment is not visible until it closes, and "running" in a heartbeat comes before the first frame on
    # disk; stopping the backup at that word would leave the seam between them to nobody. With a minute of
    # overlap the two archives overlap, and backfill finds the seam from both sides.
    HOLD_AFTER = 60.0
    # How much of the past a held backup keeps in memory (Lesson 26). When the primary is found missing —
    # `START_GRACE` after it went quiet, plus a pass — the ring is written first, so the backup's footage
    # starts BEFORE the moment anybody noticed. Thirty seconds covers the grace, a pass and a keyframe.
    PREBUFFER = 30.0
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
        # Why the archive it DID open has stopped taking segments, and since when. Not `volume_error`: that
        # one means "will not open" and costs the volume its capacity; this one means "opened, then went
        # away", and the answer to it is a queue in the spool, not a different archive.
        self.archive_error, self.archive_away_since = "", 0.0
        self.archive_failure = ""                    # "transient" (away) or "permanent" (wrong) while archive_error is set
        self.refused: dict[str, tuple[float, str]] = {}   # volume -> (tried again after, why) — see REFUSED_FOR
        # The one promotion in flight, and since when (0: none). ONE: a mount that stopped answering keeps
        # the call it swallowed, and starting another behind it every pass would pile up threads that are
        # all waiting on the same dead mount.
        self._promoter: threading.Thread | None = None
        self._opener: threading.Thread | None = None     # the one open in flight — a hung one included
        self.promoting_since = 0.0
        self.last_progress = 0.0                     # when a promotion last moved a segment — what tells moving from stuck
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
        # What a clean fetch was asked for and did not get, per (recording, source) — the source does not
        # have it either (Lesson 16, the feedback's P). A summary in a heartbeat says where a card starts and
        # ends, never where its holes are; without this the same empty range is planned every pass, and each
        # plan opens one of the device's one or two sessions.
        self.nowhere: dict[tuple[str, str], list[tuple[float, float]]] = {}
        self.archive_url = ""                       # this recorder's archive door, once served (Lesson 26)
        self._not_written_since: dict[str, float] = {}   # primary recording -> since when nobody writes it
        self.holding: dict[str, bool] = {}          # `when: offline` recording -> is its pipeline on hold now
        self._primary_back_since: dict[str, float] = {}  # released backup -> since when its primary is written again
        self.promoted = 0
        self.waiting: set[str] = set()                                        # units with nobody holding their camera
        self.sources: dict[str, str] = {}                                     # what each running pipeline subscribed to
        # What the last instance closed and did not promote — but ONLY into the archive it was recorded for.
        # This used to be a plain loop into `self.archive`, which in a constructor is the local default: no
        # volume has been looked at yet. So a recorder writing to a network archive filed its last segment
        # in the local one on every restart, and during an outage its whole queue. `promote_closed` asks each
        # segment whose footage it is, and one marked for another volume waits for whoever holds that volume.
        self.promote_closed()

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
        # The pipeline is about to record this unit under this epoch, for the volume held now: say so in the
        # epoch's own directory before the first segment lands there.
        self.mark_epoch(cam["id"], cam.get("epoch", 0))
        out = dict(cam, source=src[1], source_server=src[0], via="shm" if src[1].startswith("shm://") else "rtsp",
                   spool=self.archive.spool, archive=self.archive.root)
        # A `when: offline` backup runs ON HOLD while its primary is written: the pipeline is up, subscribed,
        # and recording into a ring in memory, writing nothing (Lesson 26).
        if self._offline_backup(cam):
            hold = not self.primary_needs_cover(cam)
            self.holding[str(cam["id"])] = hold
            out.update(hold=hold, ring_seconds=self.PREBUFFER, now=self.wall())
        return out

    def status_extra(self, cam: dict) -> dict:
        src = self.source(cam["cam"])
        out = {"cam": str(cam["cam"]), "source": src[1] if src else None, "via": (None if src is None else "shm" if src[1].startswith("shm://") else "rtsp")}
        if cam["id"] in self.waiting and cam["id"] not in self.reconciler.actual:
            out["why"] = "camera held by nobody"
        # A BACKUP recording says what it holds, the way Lesson 15's holder says what a card holds: a
        # summary, cheap to carry in every heartbeat. The primary plans from it and asks the manifest before
        # it copies anything (Lesson 26).
        if volumes.is_backup(cam, self.vars):
            ours = self.our_coverage(cam["id"])
            if ours:
                out["coverage"] = {"from": ours[0][0], "to": ours[-1][1], "fragments": len(ours)}
        return out

    def status(self) -> list[dict]:
        out = super().status()
        for st in out:
            if st["phase"] != "running" and st["id"] in self.waiting and st["enabled"]:
                st["phase"] = "waiting"
            if st["phase"] == "running" and self.holding.get(str(st["id"])):
                st["phase"], st["why"] = "standby", f"the primary recording is being written; the last {self.PREBUFFER:.0f} s are held in memory"
        return out

    # -- a backup that records only for a primary that is down (Lesson 26) -----------------------------
    # `when: offline` on a backup recording: record only while the camera's PRIMARY recording should be
    # written and is not. "Should be" is the whole rule, and the feedback's point S is why: a primary that is
    # switched off, or an event recording whose event has ended, is nobody's failure — standing in for it
    # would turn a recording on events into a recording always, on the backup's disk and, for a card, over
    # the camera's uplink. What is covered is a failure, never a decision.
    #
    # The backup's pipeline does not wait to be started: it runs on hold, a ring of the last `PREBUFFER`
    # seconds in memory, and the gate below opens and closes it. Starting it only when the primary is found
    # missing would lose exactly the seconds before that — the grace, the pass, the pipeline's own start —
    # which are the seconds the failure happened in.
    def _offline_backup(self, row: dict) -> bool:
        return str(row.get("when") or "") == "offline" and volumes.is_backup(row, self.vars)

    # One pass of the gate, after the reconciler's. A held backup whose primary now needs cover is RELEASED:
    # the ring is written first, then live. A released one whose primary has been back for `HOLD_AFTER` is
    # put on hold again — a restart under the same epoch, so the open segment is finalized and promoted, and
    # the ring starts filling afresh.
    def gate_pass(self, now: float | None = None) -> list[tuple[str, str]]:
        now = self.wall() if now is None else now
        done = []
        for row in self.rows:
            uid = str(row["id"])
            if not self._offline_backup(row) or uid not in {str(u) for u in self.reconciler.actual}:
                continue
            need = self.primary_needs_cover(row, now)
            if need:
                self._primary_back_since.pop(uid, None)
            if need and self.holding.get(uid):
                if self.actuator("release", {"id": row["id"], "now": now}):
                    self.holding[uid] = False
                    done.append((uid, "released"))
                    log.warning("%s: the primary of %s is not being written — recording from %.0f s ago",
                                self.name, uid, self.PREBUFFER)
            elif not need and self.holding.get(uid) is False:
                back = self._primary_back_since.setdefault(uid, now)
                if now - back >= self.HOLD_AFTER and self._actuate("restart", row):
                    self._primary_back_since.pop(uid, None)
                    done.append((uid, "held"))
        return done

    def primary_needs_cover(self, row: dict, now: float | None = None) -> bool:
        now = self.wall() if now is None else now
        names = volumes.backups(self.vars)
        running = {str(st.get("id")) for hb in heartbeats(self.objects, self.SUB.name + "/").values()
                   if now - hb.ts <= 45.0 for st in hb.status if st.get("phase") == "running"}
        need = False
        for key in self.vars.list(self.SUB.config(self.ROWS, "")):
            items, _ = self.vars.get(key)
            if not items or items.get("deleted") == "true":
                continue
            other = self.parse_row(items)
            if str(other["id"]) == str(row["id"]) or str(other["cam"]) != str(row["cam"]) or volumes.is_backup(other, names=names):
                continue
            until = float(other.get("until") or 0)
            should = bool(other.get("enabled")) and (until == 0 or until > now)
            if not should or str(other["id"]) in running:
                self._not_written_since.pop(str(other["id"]), None)
                continue
            since = self._not_written_since.setdefault(str(other["id"]), now)
            need = need or now - since >= self.START_GRACE
        return need

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
        out = super().reconcile_once(now)
        self.gate_pass()
        return out

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
                # Empty while the archive takes segments. Otherwise the reason it stopped, and when — the
                # spool count beside it is the queue that is waiting. Different from `volume_error` on
                # purpose: an archive that went away is still this recorder's place, and it is not
                # handed back for being away.
                "archive_error": self.archive_error,
                "archive_away_since": self.archive_away_since,
                # AWAY ("transient") is kept and buffered into; WRONG ("permanent") is handed back.
                "archive_failure": self.archive_failure,
                # Volumes this recorder handed back for refusing writes, and why — left alone until the time
                # given, so that a key somebody fixes is picked up without a restart.
                "refused": {n: why for n, (_, why) in self.refused.items()},
                "spool": len(self.archive.closed_in_spool(0.0, self.wall())),
                # …and whose footage those are, by volume. A volume here that no recorder holds is footage
                # that is waiting — not lost and not misfiled — until somebody takes it.
                "spool_for": self.spool_by_volume(),
                "closed": ",".join(self.closed),
                # Lesson 26: the door this recorder serves its archive at, for a primary backfilling from it.
                **({"archive_url": self.archive_url} if self.archive_url else {}),
                # Lesson 16: what a clean fetch found nowhere — ours missing it, the source missing it too.
                # A number the operator wants on its own: "of what we lost, 519 s were not on the card either".
                "nowhere_seconds": int(sum(b - a for spans in self.nowhere.values() for a, b in spans))}

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
        # An archive that refuses writes — WRONG, not away — is handed back: buffering into it waits for
        # nothing while the spool grows, and its recordings should go somewhere that works. What it already
        # holds stays in the spool, marked for it. And it is left alone for REFUSED_FOR, or the next pass
        # would take it straight back: opening may succeed and the first write fail again.
        now = self.wall()
        # …and an archive that is only AWAY becomes a wrong one when waiting stops being free: the local spool
        # running out. Until then every minute of outage is footage kept; after it, every minute is the
        # recordings that COULD be delivered losing their room to a queue for a place that is not answering.
        if self.hold is not None and self.archive_failure == "transient" and self.spool_full():
            self.archive_error = f"the spool is full: stopped waiting for {self.hold} ({self.archive_error})"
            self.archive_failure = "permanent"
        if self.hold is not None and self.archive_failure == "permanent":
            why = self.archive_error
            self.refused[self.hold] = (now + self.REFUSED_FOR, why)
            logging.error("%s: %s refuses writes (%s) — handing it back", self.name, self.hold, why)
            self.leave_volume(f"volume {self.hold} refuses writes")
        self.refused = {n: v for n, v in self.refused.items() if v[0] > now}
        free = [n for n in free if n not in self.refused]
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
            broken.append((self.hold, str(err)))
            self.volume_error = str(err)
            self.release_hold()                                    # so somebody who CAN write there may take it

    # Taking a volume means writing into ITS tree, so the archive this process promotes into follows the
    # hold. The spool does not: it is local scratch, one per process, and what is in it belongs to the
    # volume we were holding when it was recorded — which is why `leave_volume` promotes before letting
    # go, while we may still write there.
    # Returns None when the archive is open and writable, or the reason it is not. Opening is the only
    # honest test: a declaration can name a path that does not exist, a mount that is gone or a bucket
    # nobody can reach, and none of that is visible in the row.
    #
    # A failure to open is answered by its KIND (`failure_kind`). WRONG — a key, a path, a read-only mount —
    # is returned, and `volume_pass` hands the volume back: nobody can write there until a person fixes it.
    # AWAY — a timeout, a refused connection — is not a reason to give the volume up: the recorder keeps it,
    # at full capacity, and points at it anyway with a handle that does not touch the root. Recording goes
    # into the local spool as it always does, marked for this volume, and promotion keeps trying; the
    # heartbeat says the archive is away. Handing it back instead would reshuffle every recording on it for
    # a link that is back in a minute — the restart-during-an-outage case, which is exactly when opening
    # fails this way.
    def _write_into(self, vol) -> OSError | None:
        if not vol.url or vol.url == self.archive.root:
            return None
        e = self._open(vol.url)
        if e is not None and failure_kind(e) == "permanent":
            return e
        # Opened — or away: either way a handle that does not touch the root. When it opened, the thread
        # already made it; when it is away, promotion makes its directories segment by segment once it answers.
        archive = ArchiveResource(self.archive.spool, vol.url, wall=self.wall, create=False)
        if e is not None:
            if not self.archive_error:
                self.archive_away_since = self.wall()
                logging.warning("%s: %s is away at open (%s) — keeping it and recording into the spool",
                                self.name, vol.name, e)
            self.archive_error, self.archive_failure = str(e), "transient"
        self.archive, self.archive_root = archive, vol.url
        logging.info("%s: writing into %s (%s)", self.name, vol.name, vol.url)
        return None

    # Open an archive — make its root — on a thread of its own, and wait `OPEN_WAIT` for it. Opening is where a
    # mount that went quiet hangs, and it used to hang right here, inside `lease_pass`, stopping the very
    # renewals that keep this process's epochs. So: the error it raised, None if it opened, or — when it has
    # not come back — a `TimeoutError` with `ETIMEDOUT`, which `failure_kind` reads as AWAY like any other
    # timeout. A hung call is not cancelled, only left; and while one is still in flight, no second is
    # started behind it, the way promotion keeps one.
    def _open(self, url: str) -> OSError | None:
        if self._opener is not None and self._opener.is_alive():
            return TimeoutError(errno.ETIMEDOUT, f"an earlier open has not returned; not starting another for {url}")
        result: dict = {}

        def attempt():
            try:
                ArchiveResource(self.archive.spool, url, wall=self.wall)
            except OSError as e:
                result["error"] = e

        self._opener = threading.Thread(target=attempt, name=f"{self.name}-open", daemon=True)
        self._opener.start()
        self._opener.join(timeout=self.OPEN_WAIT)
        if self._opener.is_alive():
            return TimeoutError(errno.ETIMEDOUT, f"opening {url} has not returned in {self.OPEN_WAIT:.0f} s")
        return result.get("error")

    # Stop writing into a volume that is no longer ours — the administrator withdrew it, or the hold
    # lapsed and somebody else took it. Every recording of that archive is stopped and released, which is
    # the reassignment path of `lease_pass` and not the zombie one: the process keeps its slot, keeps
    # running, and may take another volume on the next pass.
    def leave_volume(self, why: str) -> None:
        logging.warning("%s: %s — stopping its recordings", self.name, why)
        if self._promoter is not None and self._promoter.is_alive():
            # A promotion into this archive has not come back. Promoting again here, on the loop's thread,
            # would hang the loop on the same dead mount — inside `lease_pass`, of all places. The spool
            # keeps what it has.
            logging.warning("%s: a promotion into %s is still in flight — the spool keeps its segments",
                            self.name, self.archive.root)
        else:
            # What we can move while we still hold it, bounded by the budget: this runs on the loop's thread,
            # inside `lease_pass`, and it no longer has to drain everything — a segment left behind is marked
            # for this volume and waits for whoever holds it next.
            try:
                self.promote_closed(limit=self.PROMOTE_BUDGET)
            except OSError as e:                     # a volume that went away under us: the footage is where it is
                logging.warning("%s: could not promote the spool into %s: %s", self.name, self.archive.root, e)
        for uid in list(self.reconciler.actual):
            self.actuator("stop", {"id": uid})
            self.reconciler.actual.pop(uid, None)
            self.release(str(uid))
        self.release_hold()
        self.volume, self.capacity = "", 0
        # What the archive's state said was about the archive we just left. The next one starts clean — and
        # a promotion still in flight into the old one will not write over it (`promote_closed`).
        self.archive_error, self.archive_failure, self.archive_away_since = "", "", 0.0

    def lease_pass(self) -> list[str]:
        lost = super().lease_pass()
        if self.recording_allowed:                   # a fenced instance decides nothing about volumes
            self.volume_pass()
        return lost

    # Move what has closed from the spool into the archive — and survive the archive being away.
    #
    # The spool is local and the pipeline never touches the network, so an archive that stops answering
    # costs nothing but a queue: the segment stays where it is and goes across when the link returns. What
    # it must not cost is the rest of the pass. A promote that raised used to take the lease renewal and
    # the heartbeat down with it — every pass, for as long as the archive was away — so in thirty seconds
    # the recorder lost its epochs and in forty-five its controller called it dead: the recording the
    # spool could have carried through the outage was stopped by the outage.
    #
    # In ORDER, stopping at the first failure: segments are promoted oldest first, and one that could not
    # go makes the next wait behind it rather than jump the queue. Only `OSError` is an outage — a path
    # that does not parse is a bug, and swallowing it here would hide it for as long as the box ran.
    def promote_closed(self, archive: ArchiveResource | None = None, volume: str | None = None,
                       limit: int | None = None) -> int:
        archive = archive or self.archive
        volume = self.volume if volume is None else volume
        n, failed = 0, False
        for p in archive.closed_in_spool(self.grace_seconds, self.wall()):
            if limit is not None and n >= limit:
                break                                    # the budget: the rest waits for the next pass, in order
            owner = self.volume_of(p)
            if owner and owner != volume:
                continue                                 # another volume's footage — skipped, and NOT counted
            try:
                archive.promote(p)
            except OSError as e:
                failed = True
                if volume == self.volume:                   # still ours: a late answer about a volume we left says nothing now
                    if not self.archive_error:              # said once, when it starts — not every pass
                        self.archive_away_since = self.wall()
                        logging.warning("%s: the archive %s does not answer (%s) — keeping segments in the "
                                        "spool until it does", self.name, archive.root, e)
                    self.archive_error, self.archive_failure = str(e), failure_kind(e)
                break
            n += 1
            if volume == self.volume:
                self.last_progress = self.wall()         # a segment went across: whatever this is, it is not a hang
        # Back only when a segment actually went across. An empty spool proves nothing about an archive
        # that was away: nothing was asked of it.
        if n and not failed and self.archive_error and volume == self.volume:
            logging.info("%s: the archive %s answers again after %.0f s", self.name, archive.root,
                         self.wall() - self.archive_away_since)
            self.archive_error, self.archive_away_since, self.archive_failure = "", 0.0, ""
        self.promoted += n
        return n

    # -- whose footage a segment is ----------------------------------------------------------------------
    #
    # A segment's path is `rec/<unit>/e<epoch>/<start>`: it does not name the volume it was recorded for.
    # That lived in the process (`self.hold`), and a process that dies takes it along. So the directory says
    # it, in one line beside the segments: `rec/<unit>/e<epoch>/.volume`.
    #
    # The EPOCH directory, and not the spool, because the spool is not one process's. The unit file is a
    # template and every recorder on a box mounts the same `/data/spool` — three disks, three recorders, one
    # spool. What IS one writer's is a unit's epoch: exactly one recorder holds it, for exactly one volume,
    # and the fence already guarantees nobody else writes there. Ownership follows the thing that is
    # already exclusive.
    #
    # With it, promotion asks each segment where it belongs, and nobody promotes footage into an archive it
    # was not recorded for — not a process that restarted and has not looked at a volume yet, not one that
    # moved to another volume, not a neighbour on the same box. A recorder stays free to take any volume;
    # what it can no longer do is carry someone else's footage there.
    EPOCH_MARK = ".volume"

    def mark_epoch(self, unit, epoch) -> None:
        if not self.volume:
            return
        d = os.path.join(self.archive.spool, "rec", str(unit), f"e{epoch}")
        try:
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, self.EPOCH_MARK), "w") as f:
                f.write(self.volume + "\n")
        except OSError as e:                             # a spool we cannot write to has bigger news than this
            logging.warning("%s: could not mark %s for %s: %s", self.name, d, self.volume, e)

    # The volume a segment was recorded for, or "" for one from before the mark existed — whose that is, is
    # unknown, as it always was, and it is promoted the way it always was.
    def volume_of(self, segment_path: str) -> str:
        try:
            with open(os.path.join(os.path.dirname(segment_path), self.EPOCH_MARK)) as f:
                return f.read().strip()
        except OSError:
            return ""

    # What is waiting in the spool, by the volume it belongs to: `{volume: segments}`. The spool is the
    # box's, so this is the box's picture as this recorder sees it — a neighbour's segment shows here for
    # the pass it takes the neighbour to promote it, and footage for a volume nobody holds shows here until
    # somebody does. That last is the one to act on: it is not lost and not misfiled, it is waiting.
    def spool_by_volume(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in self.archive.closed_in_spool(0.0, self.wall()):
            v = self.volume_of(p) or "?"
            out[v] = out.get(v, 0) + 1
        return out

    # The same promotion, off the loop's thread — because an archive can fail by NOT RETURNING.
    #
    # A network mount that goes quiet does not raise: `rename` and `write` into it wait in the kernel with
    # no timeout to give them, for as long as the mount is gone. The two `try`s in `run` cannot help, there
    # is no exception to catch; there is only a call that has not come back, and while it has not, the
    # thread it was made on renews nothing. So the call is made on a thread of its own.
    #
    # The pass waits on it for `PROMOTE_WAIT` and no longer. That is what keeps a HEALTHY archive
    # synchronous: a local disk or a live bucket finishes well inside it, and a pass that promotes still
    # ends with the segment in the archive — everything that reads the archive after a pass sees the same
    # thing it always did. Only a hung call outlives the wait, and then the pass moves on without it.
    #
    # One in flight. A hung call is not cancelled — a thread waiting in the kernel on a dead mount cannot
    # be — so the next pass does not start another behind it; it finds the first still running and leaves
    # it. The archive the promotion writes into is the one held when it STARTED: a volume switch meanwhile
    # does not redirect segments recorded for the old one.
    def promote_in_background(self) -> None:
        if self._promoter is not None and self._promoter.is_alive():
            return
        archive, volume = self.archive, self.volume

        def promote():
            try:
                self.promote_closed(archive, volume, limit=self.PROMOTE_BUDGET)
            except Exception:                                  # noqa: BLE001 — a thread has nobody to raise to
                logging.exception("%s: promotion failed", self.name)
            finally:
                self.promoting_since = 0.0

        self.promoting_since = self.last_progress = self.wall()
        self._promoter = threading.Thread(target=promote, name=f"{self.name}-promote", daemon=True)
        self._promoter.start()
        self._promoter.join(timeout=self.PROMOTE_WAIT)

    # A hang has no exception to report it, so the recorder notices the silence itself — and the silence it
    # listens for is PROGRESS, not age. The first version asked how long a promotion had been running, and
    # a promotion was the whole queue: an hour of footage after an outage drains for minutes on a healthy
    # link, so a minute in it said "has not returned" about a drain that was moving the whole time — the
    # same words it uses for a dead mount. A hang and a slow drain differ in one thing: a dead mount moves
    # nothing, a drain finishes a segment every so often. So a promotion is stuck when nothing has gone
    # across for `PROMOTE_STUCK`, which therefore has to be longer than one segment takes on the slowest
    # link this box will see.
    #
    # Written into the same field an outage that raises uses: whatever reads the heartbeat learns the same
    # thing either way — the archive is not taking segments, since when, and the spool count behind it.
    def watch_promotion(self) -> None:
        if not self.promoting_since or self.wall() - self.last_progress <= self.PROMOTE_STUCK:
            return
        stuck = self.wall() - self.last_progress
        if not self.archive_error.startswith("promotion into"):
            logging.warning("%s: a promotion into %s has moved nothing for %.0f s", self.name, self.archive.root, stuck)
        self.archive_away_since = self.archive_away_since or self.last_progress
        self.archive_error = f"promotion into {self.archive.root} has not returned for {stuck:.0f} s"
        self.archive_failure = "transient"               # a mount gone quiet is away, not wrong

    # The archive is taking nothing: it said so (`archive_error`), or a promotion into it is still in
    # flight. Either way it cannot be given more.
    def archive_busy(self) -> bool:
        return bool(self.archive_error) or bool(self.promoting_since)

    def pump_once(self) -> None:
        super().pump_once()                         # …which now includes `requests()`: the base serves the
                                                    # family for every subsystem, and this one overrides
                                                    # the method, not the call — asking twice a pass would
                                                    # spend the budget twice
        self.promote_in_background()                # on its own thread: an archive can hang as well as fail
        self.watch_promotion()
        # Bounded, inside the window — it shares the device's uplink — and never while the archive is taking
        # nothing: a fetched range would only pile into the spool behind the queue, and its own promote
        # would be one more call waiting on the dead mount, made on this thread.
        if self.backfill_budget and not self.archive_busy():
            self.backfill(self.backfill_budget)

    # -- backfill: closing our gaps from the device's own archive (Lesson 16) ---------------------------
    # The card exists because the camera kept recording while we could not, so replication is not "copy
    # everything" — it is the difference between two coverages. Desired: continuous. Actual: the manifest.
    # The difference is the work. Lesson 2's loop, over time instead of pipelines.
    def our_coverage(self, unit) -> list[tuple[float, float]]:
        return self.archive.coverage(str(unit), self.stitch)

    # What a source has and we do not, bounded at both ends. Not older than our own retention — otherwise
    # backfill and retention chase each other round the clock, for ever.
    #
    # Not newer than what we can SEE (the feedback's Q). Everything after the end of our visible coverage is
    # either being written this minute or written and not yet in the manifest, and there is no need to tell
    # the two apart: neither is a gap. `settle` alone used to stand for that, and it held only while
    # `settle > segment + grace` — true for the defaults, written nowhere, and false the day somebody sets
    # twenty-minute segments: the recorder then fetched from the card what it was recording that minute,
    # and the overlap check before `promote` could not catch it, because the live segment was not in the
    # manifest yet. The visible end is the lag MEASURED; `settle` stays as the floor, and is all there is
    # for a recording with nothing visible.
    #
    # PLANNED backfill starts at our first visible second (the feedback's S). Before it the recording was not
    # running by design — created yesterday, or a recording on events — and filling it from the card would
    # turn a recording on events into a recording always, a night late. An operator's request is not
    # planned: a person asked for that hour, and may ask for any hour.
    #
    # And never what a clean fetch from THIS source already found nowhere.
    def gaps(self, unit, coverage: dict, now: float, planned: bool = True, source: str = "device") -> list[tuple[float, float]]:
        ours = self.our_coverage(unit)
        lo = max(float(coverage["from"]), now - self.keep_days * 86400)
        hi = min(float(coverage["to"]), now - self.settle, ours[-1][1] if ours else now)
        if planned:
            if not ours:
                return []
            lo = max(lo, ours[0][0])
        if hi <= lo:
            return []
        holes = subtract((lo, hi), ours)
        for gone in self.nowhere.get((str(unit), source), []):
            holes = [h for hole in holes for h in subtract(hole, [gone])]
        return holes

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

    # The spool's disk is past the high mark — the watermark's own number (Lesson 18), read whether or not the
    # watermark is switched on. Switching it on means DELETING footage to make room; this only decides that
    # waiting for an away archive has stopped being free, and giving up a wait deletes nothing.
    def spool_full(self) -> bool:
        total, free = self.space_probe(self.archive.spool)
        return bool(total) and (total - free) > total * space_settings(self.vars)["high"]

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

    # -- the backup archive: a recording of the same camera on a backup volume (Lesson 26) ----------------
    # Found the way everything here is found — in heartbeats: a recording of this camera, homed on a backup
    # volume, run by a recorder that is alive, says what it holds and serves its archive. `self` is never its
    # own source, and a backup fetches from nobody.
    def backup_sources(self, row: dict, now: float | None = None) -> list[dict]:
        now = self.wall() if now is None else now
        names = volumes.backups(self.vars)
        if not names or volumes.is_backup(row, names=names):
            return []
        recs = set()
        for key in self.vars.list(self.SUB.config(self.ROWS, "")):
            items, _ = self.vars.get(key)
            if items and items.get("deleted") != "true":
                other = self.parse_row(items)
                if str(other["id"]) != str(row["id"]) and str(other["cam"]) == str(row["cam"]) and volumes.is_backup(other, names=names):
                    recs.add(str(other["id"]))
        out = []
        for name, hb in sorted(heartbeats(self.objects, self.SUB.name + "/").items()):
            url = hb.extra.get("archive_url", "")
            if not url or now - hb.ts > 45.0:
                continue
            for st in hb.status:
                if str(st.get("id")) in recs and st.get("coverage"):
                    out.append({"key": f"backup:{st['id']}", "kind": "backup", "recording": str(st["id"]),
                                "recorder": name, "url": url.rstrip("/"), "coverage": st["coverage"]})
        return out

    # Where one recording's gaps can come from, in order: the device's own archive (Lesson 16), then every
    # backup recording of the same camera.
    def sources_of(self, row: dict) -> list[dict]:
        out = []
        dev = self.device_source(row["cam"])
        if dev is not None:
            out.append({"key": "device", "kind": "device", "url": dev[0], "coverage": dev[1]})
        return out + self.backup_sources(row)

    # The backup's MANIFEST, through its recorder's door: the exact list of what it holds, where the
    # heartbeat had only a summary. The plan comes from the summary; what is copied comes from this.
    def read_manifest(self, url: str, unit: str):
        import urllib.parse
        import urllib.request
        from .archive import Segment
        with urllib.request.urlopen(f"{url}/manifest/{urllib.parse.quote(str(unit))}", timeout=10) as r:
            return [Segment.from_line(l) for l in r.read().decode().splitlines() if l.strip()]

    # This recorder's archive, served — `/manifest/<unit>` and `/segment/<path>`, the resource's two reads
    # (`vms/resource.py`), over the archive THIS process writes. A backup recorder serves it so a primary
    # can copy from it; any recorder may.
    def serve_archive(self, host: str = "127.0.0.1", port: int = 0):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from .resource import vms_routes
        routes = vms_routes(self.archive)

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                got = routes(self.path, self.headers)
                if got is None:
                    self.send_response(404); self.end_headers(); return
                status, body, *extra = got
                self.send_response(status)
                for k, v in (extra[0] if extra else ()):
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        srv = ThreadingHTTPServer((host, port), H)
        threading.Thread(target=srv.serve_forever, daemon=True, name="archive-door").start()
        self.archive_url = f"http://{host}:{srv.server_address[1]}"
        return srv

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
            # One family, two kinds of asking. A backfill names a RANGE and this worker fetches it; a
            # `record` names a DURATION and is not a worker's to serve at all — it becomes a row, and rows
            # are the console's. Skipping it here is what keeps the recorder from tripping over a request
            # that was never addressed to it (`it["from"]` would not be there).
            if str(it.get("action", "backfill")) != "backfill":
                continue
            unit, cam = str(it["unit"]), str(it.get("cam", it["unit"]))
            srcs = self.sources_of({"id": unit, "cam": cam, "home": next((r.get("home") for r in self.rows if str(r["id"]) == unit), "")})
            rid = key.rsplit("/", 1)[1]
            if not srcs:
                continue                                     # nobody holds the device and no backup answers; ask again next pass
            r = self.fetch_from(unit, cam, srcs[0], float(it["from"]), float(it["to"]))
            if r.get("skipped"):
                continue                                     # not fetched: reporting it would have the console
                                                             # delete a request nobody served
            self.fetched.append(rid)                         # the heartbeat says so; the console removes the row
            done.append({**r, "request": rid})
        return done

    # Bounded work, on request — never in the ordinary pass, the way `rebalance(budget)` is bounded
    # (Lesson 13): backfill competes with live for the device's uplink, so it gets a ceiling and an hour.
    # Each of this recorder's recordings, from each of its sources in order — the device, then any backup —
    # and a backup recording from none: a backup fetches from nobody.
    def backfill(self, budget: int = 1, now: float | None = None, force: bool = False) -> list[dict]:
        now = self.wall() if now is None else now
        if not (force or self.in_window(now)):
            return []
        if self.under_pressure():
            return []
        done: list[dict] = []
        names = volumes.backups(self.vars)
        for row in self.rows:
            if len(done) >= budget:
                break
            if volumes.is_backup(row, names=names):
                continue
            for src in self.sources_of(row):
                for (t0, t1) in self.gaps(row["id"], src["coverage"], now, source=src["key"])[:budget - len(done)]:
                    done.append(self.fetch_from(row["id"], row["cam"], src, t0, t1))
                if len(done) >= budget:
                    break
        return done

    # One range from one source. From the device: a pipeline on its playback door, as Lesson 16 wrote it.
    # From a backup recording: its manifest first — the exact spans, where the heartbeat had a summary — and
    # then each of its segments that falls in the range COPIED, cut to the range, with the times it was
    # recorded at. Either way what lands is promoted as ours.
    def fetch_from(self, unit, cam, src: dict, t0: float, t1: float) -> dict:
        if src["kind"] == "device":
            return self.fetch(unit, cam, src["url"], t0, t1)
        unit = str(unit)
        if not self.may_write(unit):
            return {"unit": unit, "cam": str(cam), "from": t0, "to": t1, "skipped": "no lease"}
        try:
            segs = self.read_manifest(src["url"], src["recording"])
        except OSError as e:
            return {"unit": unit, "cam": str(cam), "from": t0, "to": t1, "error": f"{src['recording']}: {e}"}
        self.actuator.range_error = ""
        paths = []
        for seg in sorted(segs, key=lambda x: x.start):
            lo, hi = max(t0, seg.start), min(t1, seg.end)
            if hi > lo:
                paths += self.actuator.copy_range(unit, f"{src['url']}/segment/{seg.path}#{seg.start}",
                                                  self.epochs.get(unit, 0), lo, hi, self.archive.spool)
        return self._land(unit, cam, paths, t0, t1, "backup", src["key"])

    # One range from the device: fetch it, and promote what came back as OURS — `source: edge`, our epoch,
    # our manifest, our retention.
    def fetch(self, unit, cam, url: str, t0: float, t1: float) -> dict:
        unit = str(unit)
        if not self.may_write(unit):
            return {"unit": unit, "cam": str(cam), "from": t0, "to": t1, "skipped": "no lease"}
        self.actuator.range_error = ""
        paths = self.actuator.record_range(unit, f"{url}?from={t0}&to={t1}", self.epochs.get(unit, 0),
                                           t0, t1, self.archive.spool)
        return self._land(unit, cam, paths, t0, t1, "edge", "device")

    # What a fetch brought, landed. The overlap is checked a second time here because live recording may
    # have reached the same minutes while we were fetching; a segment that would land on top of one we
    # already have is dropped rather than written. Then, if the fetch was CLEAN, whatever of the range is
    # still not ours is remembered as nowhere for this source — never after a fetch that failed half way,
    # which says nothing about what the source holds.
    def _land(self, unit: str, cam, paths: list[str], t0: float, t1: float, origin: str, source: str) -> dict:
        have, kept = self.our_coverage(unit), 0
        for p in paths:
            parsed = parse(p, self.archive.spool)
            span = (parsed[2].timestamp(), os.path.getmtime(p)) if parsed else (t0, t1)
            if overlaps(have, span):
                os.remove(p); continue                   # live recording got there while we were fetching
            self.archive.promote(p, source=origin); kept += 1
        self.backfilled += kept
        failed = getattr(self.actuator, "range_error", "")
        if not failed:
            missing = subtract((t0, t1), self.our_coverage(unit))
            if missing:
                self.nowhere[(unit, source)] = sorted(self.nowhere.get((unit, source), []) + missing)
        if kept:
            # What arrived is now ordinary footage — and a hole in the DETECTIONS, because nothing was
            # watching this camera while nothing was recording it. The console turns each of these into a
            # scan (М10B Lesson 22), so the two holes close together. Reported here and not written
            # anywhere: a worker's token writes no configuration.
            self.closed = (self.closed + [f"{unit}|{t0:.0f}|{t1:.0f}"])[-self.CLOSED_REPORTED:]
        out = {"unit": unit, "cam": str(cam), "from": t0, "to": t1, "segments": kept, "source": source}
        if failed:
            out["error"] = failed
        return out

    def metrics_text(self) -> str:
        return (f"# TYPE rec_recordings_running gauge\nrec_recordings_running {len(self.reconciler.actual)}\n"
                f"# TYPE rec_segments_promoted counter\nrec_segments_promoted {self.promoted}\n"
                f"# TYPE rec_segments_backfilled counter\nrec_segments_backfilled {self.backfilled}\n")
