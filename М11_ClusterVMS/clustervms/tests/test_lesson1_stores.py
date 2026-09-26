"""Lesson 1 — the platform's stores become Nomad's. М10's contract against
the cluster's fakes: the same ModifyIndex and CAS, the same ACL, the same
base classes — nothing in vms/ notices."""
import threading
from cluster.variables import Conflict, FakeVariables, Forbidden
from w2cplatform.contract import Controller, Subsystem, Worker
from w2cplatform.epoch import next_epoch
from tests.conftest import Cluster


def test_cas_is_the_same_promise_as_the_files_made():
    v = FakeVariables()
    idx = v.put("vms/cameras/1", {"name": "gate"}, cas=0)
    v.put("vms/cameras/1", {"name": "gate 2"}, cas=idx)
    try:
        v.put("vms/cameras/1", {"name": "stale"}, cas=idx); assert False
    except Conflict:
        pass
    items, _ = v.get("vms/cameras/1")
    assert items == {"name": "gate 2"} and v.list("vms/") == ["vms/cameras/1"]


def test_one_writer_per_prefix_is_an_acl_policy():
    v = FakeVariables()
    ctl = v.as_writer("vmscontroller", ["vms/*"])
    wrk = v.as_writer("vmsworker", ["vms/epoch/*", "vms/slots/*"])
    ctl.put("vms/cameras/7", {"name": "x"})
    wrk.put("vms/epoch/7", {"epoch": 1})
    try:
        wrk.put("vms/cameras/7", {"name": "y"}); assert False
    except Forbidden:
        pass


def test_the_epoch_issuer_under_four_threads_on_the_raft_fake():
    v = FakeVariables(); got = []
    def take():
        for _ in range(50):
            got.append(next_epoch(v, "vms/epoch/7")[0])
    ts = [threading.Thread(target=take) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(got) == list(range(1, 201))


def test_m10s_base_classes_run_on_the_cluster_stores_unchanged():
    c = Cluster(); sub = Subsystem("thing")
    ctl = Controller(sub, c.vars, c.objects, wall=c.wall)
    w = Worker(sub, None, c.vars, c.objects, clock=c.clock, wall=c.wall, instance="A")
    assert w.claim_slot() == "w-1"
    w.heartbeat([{"id": 1, "phase": "running"}], server="srv-a")
    assert list(ctl.workers_seen()) == ["w-1"] and ctl.workers_seen()["w-1"].extra["server"] == "srv-a"
    ctl.assign("w-1", ["1"]); assert w.assignment().units == ["1"]
    assert w.take_epoch("1") == 1 and w.may_write("1")


def test_the_object_store_on_this_cluster_is_variables():
    """Heartbeats and the snapshot are ~10 KB every ten seconds from a dozen
    processes: not the volume the keep-raft-small rule was about. The contract
    is the point — nothing in vms/ knows which store it is talking to."""
    from cluster.objectstore import VariablesObjectStore
    from w2cplatform.contract import Controller, Subsystem, Worker
    v = FakeVariables(); objects = VariablesObjectStore(v.as_writer("vmsworker", ["objects/*", "vms/epoch/*", "vms/slots/*"]))
    sub = Subsystem("vms"); c = Cluster()
    w = Worker(sub, "w-1", v, objects, clock=c.clock, wall=c.wall)
    w.heartbeat([{"id": 7, "phase": "running"}], server="srv-a")
    assert v.list("objects/") == ["objects/vms/heartbeats/w-1"] and objects.list("vms/") == ["vms/heartbeats/w-1"]
    ctl = Controller(sub, v, objects, wall=c.wall)
    assert ctl.workers_seen()["w-1"].extra["server"] == "srv-a"
    try:
        VariablesObjectStore(v.as_writer("vmsworker", ["vms/epoch/*"])).put("vms/heartbeats/w-2", b"{}"); assert False
    except Forbidden:
        pass                                                                    # the ACL comes with the token, as for every Variable


def test_every_writer_sees_one_log():
    """A ModifyIndex comes from ONE raft log, whichever token wrote. Two writers' views of the store must
    never hand out the same index — or a stale CAS can match a write it never saw, and an edit is lost with
    no 409 to say so. The fake broke this once: each writer's view counted on its own."""
    v = FakeVariables()
    console, worker = v.as_writer("console", ["vms/cameras/*"]), v.as_writer("vmsworker", ["vms/epoch/*"])
    a = console.put("vms/cameras/1", {"name": "gate"}, cas=0)
    b = worker.put("vms/epoch/1", {"epoch": "1"}, cas=0)
    c = console.put("vms/cameras/1", {"name": "gate-2"}, cas=a)
    assert a < b < c and len({a, b, c}) == 3
