"""Lesson 6 — the recorder: the fourth subsystem, and the only one placed on
top of the archive. A recording is a unit named by its camera; the recorder
subscribes to the fan-out of whichever worker holds the camera — from the
heartbeat, never a second connection — takes its own epoch, and writes
footage into rec/<cam>/e<epoch>/ on ITS server's archive. The worker that
holds the camera may be anywhere; the recorder must be where the disks are.
Two trees, two writers, one camera."""
import os
from datetime import datetime, timezone

from psimplatform.events import subsystems_under
from psimplatform.spec import SpecController
from psimplatform.variables import Forbidden
from vms.archive import ArchiveResource, Manifest, segment_path
from vms.config import DET_SPEC, LIVE_SPEC, REC_SPEC, SPEC, live_shm, live_url
from vms.controller import VmsController
from vms.recorder import RecWorker
from vms.worker import FakeActuator, VmsWorker
from tests.conftest import Box


def _box():
    box = Box()
    ctl = VmsController(box.vars.as_writer("vmscontroller", SPEC.acl_controller()), box.objects, wall=box.wall)
    con_vars = box.vars.as_writer("vmsconsole", SPEC.acl_console() + LIVE_SPEC.acl_console() + DET_SPEC.acl_console() + REC_SPEC.acl_console())
    con = VmsController(con_vars, box.objects, wall=box.wall)
    rec_con = SpecController(REC_SPEC, con_vars, box.objects, wall=box.wall)                      # the console's door to recordings
    rec_ctl = SpecController(REC_SPEC, box.vars.as_writer("reccontroller", REC_SPEC.acl_controller()), box.objects, wall=box.wall)
    w = VmsWorker("w-1", box.vars, box.objects, FakeActuator(), clock=box.clock, wall=box.wall, server="srv-1", archive_root=box.archive)
    w.heartbeat_once(); con.create_camera({"name": "gate", "source": "driverpack://file/gate.mp4"}); ctl.ensure_placed(); w.reconcile_once(); w.heartbeat_once()
    return box, ctl, con, rec_con, rec_ctl, w


def _recorder(box, name="r-1", server="srv-1", capacity=50):
    arch = ArchiveResource(box.spool, box.archive, wall=box.wall)
    r = RecWorker(name, box.vars.as_writer("vmsrecorder", ["rec/epoch/*", "rec/slots/*"]), box.objects, FakeActuator(), archive=arch,
                  clock=box.clock, wall=box.wall, server=server, capacity=capacity, env={})
    r.heartbeat_once()
    return r


def test_a_recording_is_a_unit_placed_on_the_archive_and_fed_by_the_workers_fan_out():
    box, ctl, con, rec_con, rec_ctl, w = _box()
    assert REC_SPEC.requires == "resource" and rec_ctl.policy() == {"servers": "distinct"} and ctl.policy() == {"servers": "shared"}   # the recorder is the one that must be where the disks are, one per server; workers are not
    assert REC_SPEC.near == "vms"                                                                  # and it prefers the server whose worker holds the camera: an affinity, not a filter
    r = _recorder(box); r2 = _recorder(box, "r-2", "srv-2", capacity=1)                            # two servers with archives; the camera's worker is on srv-1
    assert r.name == "r-1" and r.SUB.name == "rec" and box.vars.list("rec/slots/") == ["rec/slots/r-1", "rec/slots/r-2"]
    # the operator records camera 1: a row under rec/, the console's token; placement is the rec controller's pass
    row = rec_con.create({"cam": "1", "retention_days": 7})
    assert row["id"] == "1" and row["retention_days"] == 7 and box.vars.list("rec/recordings/") == ["rec/recordings/1"]
    try:
        rec_con.place("1"); raise AssertionError("a console token never writes placement")
    except Forbidden:
        pass
    pl = rec_ctl.ensure_placed()[0]
    assert pl.worker == "r-1" and pl.reason.endswith("on srv-1, whose resource is unknown, beside w-1 holding it")   # the affinity, and the reason says so
    # the recorder's pass: the pipeline is built from the worker's tee — its shared-memory branch, since the worker is on THIS
    # server (no RTSP hop, no fan-out process on the recording path) — under the RECORDER's epoch
    assert r.reconcile_once() == [("start", 1)]
    started = r.actuator.started[1]
    assert started["source"] == live_shm(1) == "shm:///run/vms/1.shm" and started["via"] == "shm" and started["source_server"] == "srv-1"
    assert live_url("srv-1", 1) == "rtsp://srv-1:8554/1"                                           # what a recorder on another server would read
    assert started["epoch"] == 1 and box.vars.get("rec/epoch/1")[0] == {"epoch": "1"} and w.epochs == {"1": 1}   # two epochs, two writers, one camera
    assert started["spool"] == box.spool and started["archive"] == box.archive
    r.heartbeat_once()
    st = rec_ctl.workers_seen()["r-1"].status[0]
    assert st["phase"] == "running" and st["cam"] == "1" and st["source"] == "shm:///run/vms/1.shm" and st["via"] == "shm" and st["epoch"] == 1
    assert "rec_recordings_running 1" in r.metrics_text()
    # a segment the pipeline closed is promoted into rec/<cam>/e<epoch>/ and indexed beside it — the worker's tree stays events-only
    t = datetime.fromtimestamp(box.wall() - 1200, timezone.utc)
    p = segment_path(box.spool, 1, 1, t); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(b"x" * 100)
    os.utime(p, (box.wall() - 600, box.wall() - 600))
    r.pump_once()
    assert r.promoted == 1 and not os.path.exists(p) and Manifest(box.archive, 1).read()[0].path == "rec/1/e1/" + t.strftime("%Y%m%dT%H%M%SZ") + ".mp4"
    w.observe(1, "motion")
    assert subsystems_under(box.archive) == {"rec": ["1"], "vms": ["1"]}
    # the camera's worker fails over to srv-2: the recorder re-subscribes — to the RTSP fan-out now, the worker is on another
    # server — same recorder, same disks, same tree; a new pipeline is a new epoch (e1 before the move, e2 after, both here)
    w2 = VmsWorker("w-2", box.vars, box.objects, FakeActuator(), clock=box.clock, wall=box.wall, server="srv-2", archive_root=box.archive)
    ctl.move(1, "w-2", "test"); w2.reconcile_once(); w2.heartbeat_once(); w.reconcile_once(); w.heartbeat_once()
    assert r.resubscribe() == [1] and r.actuator.calls[-1] == ("stop", 1)
    box.clock.advance(10)
    assert r.reconcile_once() == [("start", 1)] and r.actuator.started[1]["source"] == "rtsp://srv-2:8554/1" and r.actuator.started[1]["via"] == "rtsp"
    assert r.actuator.started[1]["epoch"] == 2 and rec_ctl.where("1") == "r-1" and w2.epochs == {"1": 2}   # the recording did not move; its source did
    # two more cameras, both held on srv-2 by w-2. The affinity puts the first recording beside w-2 — r-2 reads shared memory
    # there; r-2 (capacity 1) is then full, so the second goes to r-1 and reads the fan-out — the reason says where it would rather be
    con.create_camera({"name": "yard", "source": "driverpack://file/yard.mp4"}); con.create_camera({"name": "dock", "source": "driverpack://file/dock.mp4"})
    ctl.ensure_placed(); ctl.move(2, "w-2", "test"); ctl.move(3, "w-2", "test")
    w2.reconcile_once(); w2.heartbeat_once(); w.reconcile_once(); w.heartbeat_once()
    rec_con.create({"cam": "2"}); rec_con.create({"cam": "3"})
    pl2, pl3 = rec_ctl.ensure_placed()[-2:]                                                        # (the pass returns every placement, camera 1's first)
    assert (pl2.worker, pl3.worker) == ("r-2", "r-1")
    assert pl2.reason.endswith("on srv-2, whose resource is unknown, beside w-2 holding it") and pl3.reason.endswith("on srv-1, whose resource is unknown, away from w-2 on srv-2 (no room there)")
    assert r2.reconcile_once() == [("start", 2)] and r2.actuator.started[2]["via"] == "shm" and r2.actuator.started[2]["source"] == "shm:///run/vms/2.shm"
    assert r.reconcile_once() == [("start", 3)] and r.actuator.started[3]["via"] == "rtsp" and r.actuator.started[3]["source"] == "rtsp://srv-2:8554/3"


def test_a_recording_waits_while_nobody_holds_the_camera_and_records_when_someone_does():
    box, ctl, con, rec_con, rec_ctl, w = _box()
    con.create_camera({"name": "yard", "source": "driverpack://file/yard.mp4"})                    # camera 2: created, not placed yet
    r = _recorder(box)
    rec_con.create({"cam": "2"}); rec_ctl.ensure_placed()
    assert r.reconcile_once() == [("failed", 2)]                                                   # no fan-out to subscribe to
    r.heartbeat_once()
    st = rec_ctl.workers_seen()["r-1"].status[0]
    assert st["phase"] == "waiting" and st["why"] == "camera held by nobody" and st["source"] is None
    ctl.ensure_placed(); w.reconcile_once(); w.heartbeat_once()                                   # the worker takes it
    box.clock.advance(10)
    assert r.reconcile_once() == [("start", 2)] and r.actuator.started[2]["source"] == "shm:///run/vms/2.shm"   # held here: the tee's shared memory
    # a camera with no recording is watched, not recorded: it is held (live, detection, events), and has no rec/ tree
    assert rec_ctl.units() == [{"id": "2", "cam": "2", "retention_days": 30, "enabled": True, "labels": [], "revision": 1}]
    assert [c["id"] for c in ctl.cameras()] == [1, 2] and subsystems_under(box.archive) == {}
    w.observe(1, "motion")
    assert subsystems_under(box.archive) == {"vms": ["1"]}
    # stop recording: the row goes, the placement is taken back on the next pass, the footage stays until retention
    rec_con.delete("2"); rec_ctl.unplace_deleted()
    assert rec_ctl.assignment("r-1").units == [] and r.reconcile_once() == [("stop", 2)]
