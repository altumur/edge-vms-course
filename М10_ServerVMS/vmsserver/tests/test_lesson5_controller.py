"""Lesson 5 — vmscontroller: the only writer; refusals; placement with its
property tests; two controllers; the read model; the failure arithmetic."""
import json
import os
import threading
import urllib.request
from vms.console import serve
from vms.controller import Refused, VmsController
from vms.worker import FakeActuator, VmsWorker
from tests.conftest import Box


def test_crud_by_cas_and_what_it_refuses():
    box = Box(); ctl = VmsController(box.vars, box.objects, wall=box.wall)
    r = ctl.create_camera({"name": "gate", "source": "driverpack://file/gate.mp4"})
    assert r["id"] == 1 and r["revision"] == 1 and ctl.camera(1)["name"] == "gate"
    assert ctl.update_camera(1, {"retention_days": 14})["revision"] == 2
    for bad in ({"worker": "w-1"}, {"revision": 9}, {"phase": "running"}, {"epoch": 3}, {"placement": {}}):
        try:
            ctl.update_camera(1, bad); raise AssertionError("must refuse")
        except Refused as e:
            assert "may not set" in str(e)
    try:
        ctl.create_camera({"name": "x"}); raise AssertionError()
    except Refused as e:
        assert "needs a source" in str(e)
    ctl.delete_camera(1)
    assert ctl.camera(1) is None and ctl.cameras() == []


def test_placement_is_stored_with_a_reason_and_adding_a_worker_moves_nothing():
    box = Box(); ctl = VmsController(box.vars, box.objects, capacity=3, wall=box.wall)
    for i in range(6):
        ctl.create_camera({"source": f"driverpack://file/{i}.mp4"})
    placed = ctl.ensure_placed(workers=["w-1", "w-2"])
    assert len(placed) == 6 and all(p.reason.startswith("most free capacity") for p in placed)
    before = {c["id"]: ctl.where(c["id"]) for c in ctl.cameras()}
    assert sorted(before.values()).count("w-1") == 3 and sorted(before.values()).count("w-2") == 3
    assert ctl.create_camera({"source": "driverpack://file/7.mp4"}) and ctl.place(7, workers=["w-1", "w-2"]) is None   # the system is full
    ctl.ensure_placed(workers=["w-1", "w-2", "w-3"])                        # a worker arrives
    assert {c: ctl.where(c) for c in before} == before and ctl.where(7) == "w-3"    # nothing moved; the new one went to the new worker
    assert ctl.placement(7).rev == 1 and ctl.placement(7).at == box.wall()


def test_capacity_is_the_workers_word_not_the_controllers():
    """Two workers on different hardware say different numbers in their
    heartbeats; the controller places by what they said and its own constant
    is only the fallback for a worker that said nothing."""
    box = Box(); ctl = VmsController(box.vars, box.objects, capacity=50, wall=box.wall)
    small = VmsWorker("w-1", box.vars, box.objects, FakeActuator(), clock=box.clock, wall=box.wall, capacity=2)
    big = VmsWorker("w-2", box.vars, box.objects, FakeActuator(), clock=box.clock, wall=box.wall, capacity=6)
    small.heartbeat_once(); big.heartbeat_once()
    assert (ctl.capacity_of("w-1"), ctl.capacity_of("w-2"), ctl.capacity_of("w-9")) == (2, 6, 50)
    for i in range(9):
        ctl.create_camera({"source": f"driverpack://file/{i}.mp4"})
    placed = ctl.ensure_placed()
    assert len(placed) == 8 and ctl.load("w-1") == 2 and ctl.load("w-2") == 6    # the ninth waits: the system is full
    assert ctl.place(9) is None and ctl.headroom() == 8                            # headroom is stale until they heartbeat again
    small.reconcile_once(); big.reconcile_once(); small.heartbeat_once(); big.heartbeat_once()
    assert ctl.headroom() == 0
    assert "(6)" in ctl.placement(1).reason or "(6)" in ctl.placement(2).reason    # the reason says whose number it was


def test_two_controllers_agree_by_cas():
    box = Box()
    a = VmsController(box.vars, box.objects, capacity=100, wall=box.wall)
    for i in range(40):
        a.create_camera({"source": f"driverpack://file/{i}.mp4"})
    def race(prefer):
        c = VmsController(box.vars, box.objects, capacity=100, wall=box.wall)
        for cam in c.cameras():
            c.place(cam["id"], workers=prefer)
    ts = [threading.Thread(target=race, args=(w,)) for w in (["w-1", "w-2"], ["w-2", "w-1"])]
    [t.start() for t in ts]; [t.join() for t in ts]
    c = VmsController(box.vars, box.objects, wall=box.wall)
    where = {cam["id"]: c.where(cam["id"]) for cam in c.cameras()}
    assert len(where) == 40 and all(where.values())
    units = c.assignment("w-1").units + c.assignment("w-2").units
    assert sorted(int(u) for u in units) == list(range(1, 41))              # every camera exactly once, whoever won


def test_rebalance_is_explicit_budgeted_and_stops_in_the_dead_band():
    box = Box(); ctl = VmsController(box.vars, box.objects, capacity=10, wall=box.wall)
    for i in range(8):
        ctl.create_camera({"source": f"driverpack://file/{i}.mp4"})
    ctl.ensure_placed(workers=["w-1"])                                       # all eight on w-1
    assert ctl.rebalance(budget=0, workers=["w-1", "w-2"]) == []             # no budget, no moves
    moves = ctl.rebalance(budget=3, workers=["w-1", "w-2"])
    assert len(moves) == 3 and all(m[1] == "w-1" and m[2] == "w-2" for m in moves)
    assert "rebalance" in ctl.placement(moves[0][0]).reason
    assert ctl.load("w-1") == 5 and ctl.load("w-2") == 3
    assert ctl.rebalance(budget=5, workers=["w-1", "w-2"]) == [(m, "w-1", "w-2") for m in [4]]   # one more, then inside the dead band


def test_the_failure_arithmetic():
    """Stop each process in turn and say what stopped."""
    box = Box(); ctl = VmsController(box.vars, box.objects, wall=box.wall)
    for i in range(1, 3):
        ctl.create_camera({"source": f"driverpack://file/{i}.mp4"})
    act = FakeActuator()
    w = VmsWorker("w-1", box.vars, box.objects, act, clock=box.clock, wall=box.wall)
    w.heartbeat_once(); ctl.ensure_placed()
    w.reconcile_once(); w.heartbeat_once()
    assert act.running == {1, 2}
    # controller down: the read model still answers (heartbeats), recording continues, edits stop
    rows = VmsController(box.vars, box.objects, wall=box.wall).read_model()
    assert [r["phase"] for r in rows] == ["running", "running"]
    # worker down: the console shows the last snapshot with its age; edits still land in the store
    box.wall.advance(100)
    ctl.update_camera(1, {"name": "edited while w-1 was down"})
    rows = ctl.read_model(lost_after=45)
    assert rows[0]["worker_state"] == "stale" and rows[0]["age"] == 100.0
    # ...and are applied the moment the worker is back — from the store, not from the controller
    w2 = VmsWorker("w-1", box.vars, box.objects, FakeActuator(), clock=box.clock, wall=box.wall)
    assert w2.reconcile_once() == [("start", 1), ("start", 2)] and w2.rows[0]["name"] == "edited while w-1 was down"


def test_scale_in_releases_a_slot_and_the_controller_redistributes():
    """Nomad decided `count` 3 → 2. The worker it stops releases its slot;
    the controller's placement pass moves that slot's cameras — its one
    unasked move — and nothing else. A crash releases nothing and moves nothing."""
    box = Box(); ctl = VmsController(box.vars, box.objects, capacity=4, wall=box.wall)
    ws = [VmsWorker(None, box.vars, box.objects, FakeActuator(), clock=box.clock, wall=box.wall, capacity=4) for _ in range(3)]
    for w in ws:
        w.heartbeat_once()
    for i in range(1, 7):
        ctl.create_camera({"source": f"driverpack://file/{i}.mp4"})
    ctl.ensure_placed()
    assert {w: len(a.units) for w, a in ctl.assignments().items()} == {"w-1": 2, "w-2": 2, "w-3": 2}
    assert ctl.headroom() == 12 and ctl.redistribute() == []           # nothing released: nothing moves
    box.wall.advance(46)                                               # w-3 crashed: silent, not released
    ws[0].heartbeat_once(); ws[1].heartbeat_once()
    assert ctl.redistribute() == [] and ctl.where(3) == "w-3"          # a crash is Nomad's to fix; the cameras wait for w-3
    ws[2].release_slot()                                               # scale-in: SIGTERM, an orderly stop
    moves = ctl.redistribute()
    assert [(cid, frm) for cid, frm, _ in moves] == [(3, "w-3"), (6, "w-3")]
    assert ctl.assignment("w-3").units == [] and {ctl.where(3), ctl.where(6)} <= {"w-1", "w-2"}
    assert "slot w-3 released" in ctl.placement(3).reason
    ws[0].reconcile_once(); ws[1].reconcile_once()
    for w in ws[:2]:
        w.heartbeat_once()
    assert ctl.headroom() == 8 - 6                                     # 2 workers × 4, six cameras: what the autoscaler reads
    ctl.retire("w-1")                                                  # the operator's word that a slot is gone for good
    assert ctl.released_slots() == ["w-1"]


def test_the_console_over_http():
    box = Box(); ctl = VmsController(box.vars, box.objects, wall=box.wall)
    w = VmsWorker("w-1", box.vars, box.objects, FakeActuator(), clock=box.clock, wall=box.wall, server="srv-1")
    w.heartbeat_once()
    from vms.archive import ArchiveResource
    from vmsplatform.events import read_bucket, subsystems_under
    srv = serve(ctl, ArchiveResource(box.spool, box.archive), port=0, wall=box.wall); port = srv.server_address[1]
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/cameras", data=json.dumps({"name": "gate", "source": "driverpack://file/gate.mp4"}).encode(),
                                     method="POST", headers={"Idempotency-Key": "k1"})
        r = json.load(urllib.request.urlopen(req)); assert r["id"] == 1 and r["worker"] == "w-1"
        r2 = json.load(urllib.request.urlopen(req)); assert r2 == r                    # the same POST, not a second camera
        assert len(ctl.cameras()) == 1
        req = urllib.request.Request(f"http://127.0.0.1:{port}/cameras/1", data=b'{"worker":"w-9"}', method="PUT", headers={"Idempotency-Key": "k2"})
        try:
            urllib.request.urlopen(req); raise AssertionError()
        except urllib.error.HTTPError as e:
            assert e.code == 400
        w.reconcile_once(); w.heartbeat_once()
        body = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/cameras"))
        assert body["rows"][0]["phase"] == "running" and body["rows"][0]["server"] == "srv-1"
        assert json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/where/1")) == {"worker": "w-1"}
        assert b"vms_cameras_recording 1" in urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics").read()
        # an operator's mark is the CONSOLE's event: its own bucket, never a worker's
        req = urllib.request.Request(f"http://127.0.0.1:{port}/marks", data=json.dumps({"cam": 1, "note": "left the bag"}).encode(),
                                     method="POST", headers={"Idempotency-Key": "k3", "X-User": "murat"})
        m = json.load(urllib.request.urlopen(req))
        assert m["subsystem"] == "console" and m["bucket"].startswith(f"console/{m['unit']}/e1/")
        assert json.load(urllib.request.urlopen(req)) == m                                      # idempotent: one mark
        ev = read_bucket(os.path.join(box.archive, m["bucket"]))
        assert ev == [{"t": box.wall(), "kind": "mark", "cam": 1, "user": "murat", "note": "left the bag"}]
        assert subsystems_under(box.archive) == {"console": [m["unit"]]}                          # not in vms/1/: that bucket has one writer
        # the page, and the bytes it plays: three fetches and a Range
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
        assert "/cameras" in page and "/timeline/" in page and "/segment/" in page and "<video" in page
        from datetime import datetime, timezone
        from vms.archive import segment_path
        seg = segment_path(box.spool, 1, 1, datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)); os.makedirs(os.path.dirname(seg), exist_ok=True)
        open(seg, "wb").write(bytes(range(256))); ArchiveResource(box.spool, box.archive).promote(seg)
        tl = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/timeline/1"))
        assert len(tl) == 1 and tl[0]["media"] == "vms/1/e1/20260912T100000Z.mp4"
        req = urllib.request.Request(f"http://127.0.0.1:{port}/segment/{tl[0]['media']}", headers={"Range": "bytes=10-19"})
        with urllib.request.urlopen(req) as r:
            assert r.status == 206 and r.read() == bytes(range(10, 20)) and r.headers["Content-Range"] == "bytes 10-19/256"
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/segment/vms/1/e1/nope.mp4"); raise AssertionError()
        except urllib.error.HTTPError as e:
            assert e.code == 404
        # the page's writes: disable, then delete — through the controller, refused where the controller refuses
        req = urllib.request.Request(f"http://127.0.0.1:{port}/cameras/1", data=b'{"enabled": false}', method="PUT", headers={"Idempotency-Key": "k4"})
        assert json.load(urllib.request.urlopen(req))["enabled"] is False and ctl.camera(1)["revision"] == 2
        req = urllib.request.Request(f"http://127.0.0.1:{port}/cameras/1", method="DELETE")
        assert json.load(urllib.request.urlopen(req)) == {"deleted": 1} and ctl.cameras() == [] and ctl.where(1) is None
        try:
            urllib.request.urlopen(req); raise AssertionError()
        except urllib.error.HTTPError as e:
            assert e.code == 404                                                                   # gone is gone
    finally:
        srv.shutdown(); srv.server_close()
