"""Lesson 2 — workers, resources and the controller as jobs. Identity by
claim from NOMAD_ALLOC_INDEX; scale out and in; the duplicate index; the
ACL from inside an allocation."""
from cluster.controller import ClusterController
from cluster.variables import Forbidden
from tests.conftest import Cluster


def _cluster(n_cams=6):
    c = Cluster(); ctl = ClusterController(c.vars.as_writer("vmscontroller", ["vms/*"]), c.objects, capacity=4, wall=c.wall)
    for i in range(n_cams):
        ctl.create_camera({"source": f"driverpack://file/{i}.mp4"})
    return c, ctl


def test_the_slot_comes_from_the_allocation_index_and_the_labels_from_the_server():
    c, ctl = _cluster()
    w0, w1 = c.worker(0, "srv-a", capacity=4), c.worker(1, "srv-b", capacity=4)
    assert (w0.name, w1.name) == ("w-0", "w-1")
    w0.heartbeat_once(); w1.heartbeat_once()
    hb = ctl.workers_seen()
    assert hb["w-0"].extra["server"] == "srv-a" and hb["w-0"].extra["labels"] == "vlan:cctv-a"
    assert hb["w-1"].extra["labels"] == "vlan:cctv-a,vlan:cctv-b" and hb["w-1"].extra["alloc"] == "alloc-0002"
    assert ctl.slots()["w-1"].holder == "alloc-0002"                     # the claim names the allocation


def test_nomad_job_scale_out_then_in():
    """`nomad job scale vmsworker 3`: a new allocation with index 2 claims w-2 and
    the next camera lands on it. `… 2`: index 2 gets SIGTERM, releases, and its
    cameras are redistributed in one placement pass. The controller asked for none of it."""
    c, ctl = _cluster(n_cams=8)
    ws = [c.worker(0, "srv-a", capacity=4), c.worker(1, "srv-b", capacity=4)]
    for w in ws: w.heartbeat_once()
    ctl.ensure_placed()
    for w in ws: w.reconcile_once(); w.heartbeat_once()
    assert ctl.headroom() == 0 and sum(ctl.load(w) for w in ("w-0", "w-1")) == 8   # what /metrics shows the autoscaler
    # scale out: the autoscaler saw avg(vms_worker_load) = 1.0
    ctl.create_camera({"source": "driverpack://file/9.mp4"})
    assert ctl.ensure_placed() and ctl.where(9) is None                  # full: the ninth waits
    w2 = c.worker(2, "srv-c", capacity=4); w2.heartbeat_once()
    ctl.ensure_placed()
    assert w2.name == "w-2" and ctl.where(9) == "w-2" and "on srv-c" in ctl.placement(9).reason
    # scale in: index 2 is stopped in order
    w2.reconcile_once(); w2.release_slot()
    ctl.delete_camera(1); ctl.delete_camera(2)                           # room to move into
    moves = ctl.redistribute()
    assert [m[0] for m in moves] == [9] and moves[0][1] == "w-2" and ctl.assignment("w-2").units == []


def test_two_allocations_with_one_index_resolve_at_the_cas():
    """Nomad issue #10727: a duplicate allocation index. The index is a label;
    the slot row is the proof. The second claim wins; the first fences."""
    c, ctl = _cluster(2)
    a = c.worker(0, "srv-a"); ctl.assign("w-0", ["1", "2"]); a.reconcile_once()
    b = c.worker(0, "srv-b")                                              # same index, a second allocation
    assert b.name == "w-0" and ctl.slots()["w-0"].holder == b.instance and ctl.slots()["w-0"].gen == 2
    assert a.lease_pass() and not a.recording_allowed and "slot w-0" in a.fenced_reason
    assert b.reconcile_once() == [("start", 1), ("start", 2)] and b.lease_pass() == []


def test_the_acl_from_inside_an_allocation():
    """A worker's token writes its epochs and its slot; the controller's writes vms/*."""
    c, ctl = _cluster(1)
    w = c.worker(0, "srv-a")
    try:
        w.vars.put("vms/cameras/1", {"name": "tampered"}); assert False
    except Forbidden:
        pass
    w.take_epoch("1"); ctl.update_camera(1, {"name": "ok"})
    assert ctl.camera(1)["name"] == "ok" and c.vars.get("vms/epoch/1")[0] == {"epoch": "1"}
    # and the console's token: the operator's rows, never placement — two tokens, two prefixes, one class
    from vms.config import SPEC
    con = ClusterController(c.vars.as_writer("vmsconsole", SPEC.acl_console()), c.objects, wall=c.wall)
    assert con.create_camera({"source": "driverpack://file/2.mp4"})["id"] == 2 and con.update_camera(2, {"name": "from the console"})["revision"] == 2
    w.heartbeat_once()
    try:
        con.place(2); assert False
    except Forbidden:
        pass
    assert ctl.ensure_placed()[0].worker == "w-0"                         # the controller placed what the console created
