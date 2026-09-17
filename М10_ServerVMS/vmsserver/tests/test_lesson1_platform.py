"""Lesson 1 — the subsystem contract, and the platform that knows nothing."""
import os
import threading
from w2cplatform.contract import Assignment, Controller, Heartbeat, Subsystem, Worker
from w2cplatform.epoch import Lease, current_epoch, next_epoch
from w2cplatform.variables import Conflict, FileVariables, Forbidden
from tests.conftest import Box, Clock


def test_the_config_store_survives_a_restart_and_refuses_a_stale_cas():
    box = Box()
    idx = box.vars.put("vms/cameras/7", {"name": "gate", "revision": 1}, cas=0)
    assert idx == 1001
    again = FileVariables(box.vars.root)                       # a new process, same directory
    items, idx2 = again.get("vms/cameras/7")
    assert items == {"name": "gate", "revision": "1"} and idx2 == idx
    try:
        again.put("vms/cameras/7", {"name": "x"}, cas=idx - 1); raise AssertionError("must conflict")
    except Conflict:
        pass
    assert again.put("vms/cameras/7", {"name": "x"}, cas=idx) == 1002
    assert again.list("vms/") == ["vms/cameras/7"] and again.get("nope") == (None, 0)


def test_two_processes_one_cas_winner():
    box = Box()
    results = []
    def race(n):
        v = FileVariables(box.vars.root)                       # each "process" opens the store itself
        for _ in range(40):
            items, idx = v.get("counter")
            try:
                v.put("counter", {"n": int(items["n"]) + 1 if items else 1}, cas=idx); results.append(n)
            except Conflict:
                pass
    ts = [threading.Thread(target=race, args=(i,)) for i in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    n = int(box.vars.get("counter")[0]["n"])
    assert n == len(results)                                   # every successful write counted exactly once


def test_one_writer_per_prefix():
    box = Box()
    ctl = box.vars.as_writer("vmscontroller", ["vms/*"])
    wrk = box.vars.as_writer("vmsworker-1", ["vms/epoch/*"])
    ctl.put("vms/cameras/7", {"name": "gate"})
    wrk.put("vms/epoch/7", {"epoch": 1})
    try:
        wrk.put("vms/cameras/7", {"name": "mine now"}); raise AssertionError("a worker never writes configuration")
    except Forbidden:
        pass


def test_epoch_issuer_never_reuses_a_number():
    box = Box()
    issued = []
    def race():
        v = FileVariables(box.vars.root)
        for _ in range(25):
            issued.append(next_epoch(v, "vms/epoch/7")[0])
    ts = [threading.Thread(target=race) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(issued) == list(range(1, 101)) and current_epoch(box.vars, "vms/epoch/7") == 100


def test_lease_on_a_monotonic_clock():
    box = Box(); clk = Clock()
    e, _ = next_epoch(box.vars, "vms/epoch/7")
    lease = Lease(box.vars, "vms/epoch/7", e, ttl=30, margin=5, clock=clk)
    clk.advance(24.9); assert lease.may_write()
    clk.advance(0.2);  assert not lease.may_write() and lease.seconds_left() == 0
    assert lease.renew() and lease.may_write()
    next_epoch(box.vars, "vms/epoch/7")                        # somebody else took camera 7
    assert lease.renew() is False and lease.fenced and lease.conflicts == 1


def test_the_platform_knows_nothing_about_video():
    """No import from vms/ anywhere under w2cplatform/."""
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "w2cplatform")
    for f in os.listdir(here):
        if f.endswith(".py"):
            src = "\n".join(l for l in open(os.path.join(here, f)) if not l.lstrip().startswith("#"))   # the code, not the notes
            assert "from vms" not in src and "import vms" not in src, f
            if f != "__init__.py":
                assert "camera" not in src.lower(), f              # not even the word
    sub = Subsystem("vms")
    assert sub.assignment("w-1") == "vms/workers/w-1" and sub.epoch_key("7") == "vms/epoch/7"
    assert sub.heartbeat_key("w-1") == "vms/w-1/heartbeat" and sub.acl_controller() == ["vms/*"]


def test_controller_and_worker_bases_speak_only_the_contract():
    box = Box()
    sub = Subsystem("thing")
    ctl = Controller(sub, box.vars, box.objects, wall=box.wall)
    w = Worker(sub, "t-1", box.vars, box.objects, clock=box.clock, wall=box.wall)
    assert ctl.workers_seen() == {}                            # nobody has heartbeaten
    w.heartbeat([{"id": 1, "phase": "running"}], server="srv-1")
    seen = ctl.workers_seen()
    assert list(seen) == ["t-1"] and seen["t-1"].extra["server"] == "srv-1"
    a = ctl.assign("t-1", ["3", "1", "2"])
    assert a.units == ["1", "2", "3"] and a.rev == 1 and w.assignment().units == ["1", "2", "3"]
    assert ctl.assign("t-1", ["1"]).rev == 2
    assert w.take_epoch("1") == 1 and w.may_write("1")
    box.wall.advance(100)
    assert ctl.workers_seen(max_age=45) == {}                  # a silent worker is not a worker


def test_identity_by_claim_is_a_platform_piece():
    """A name is a slot: taken by CAS, renewed, released on purpose or lapsed
    by silence. Two processes claiming without a preference get two names;
    a third, after the first lapsed, gets the first's name back — and with
    it, its assignment. The controller hands nothing out."""
    box = Box()
    sub = Subsystem("thing")
    ctl = Controller(sub, box.vars, box.objects, wall=box.wall)
    a = Worker(sub, None, box.vars, box.objects, clock=box.clock, wall=box.wall, instance="A")
    b = Worker(sub, None, box.vars, box.objects, clock=box.clock, wall=box.wall, instance="B")
    assert a.claim_slot() == "w-1" and b.claim_slot() == "w-2"        # `count = 2`: two names, in order
    ctl.assign("w-1", ["1", "2"])
    assert a.renew_slot() and b.renew_slot()
    box.wall.advance(46)                                               # A went silent for longer than the slot TTL
    c = Worker(sub, None, box.vars, box.objects, clock=box.clock, wall=box.wall, instance="C")
    assert c.claim_slot() == "w-1" and c.assignment().units == ["1", "2"]   # the replacement inherits
    assert not a.renew_slot()                                          # A, if it is still alive, finds out
    assert ctl.released_slots() == []                                  # a lapse is not a release
    b.release_slot()                                                   # scale-in: B is told to stop and says so
    ctl.assign("w-2", ["3"])
    assert ctl.released_slots() == ["w-2"]                             # what the subsystem redistributes
    d = Worker(sub, None, box.vars, box.objects, clock=box.clock, wall=box.wall, instance="D")
    assert d.claim_slot(prefer="w-7") == "w-7"                         # the scheduler's index wins, and creates
    assert sorted(ctl.slots()) == ["w-1", "w-2", "w-7"] and sub.slot_key("w-1") == "thing/slots/w-1"
    assert sub.acl_worker() == ["thing/epoch/*", "thing/slots/*"]


def test_the_resource_is_a_platform_job_that_mirrors_any_subsystems_buckets():
    """Two resources on one box (two roots), one raft. The knob is one Variable;
    each resource copies its CLOSED buckets — whatever subsystem wrote them — to
    the next live resource after it; a resource back with an empty disk pulls
    its own buckets home. Nothing here knows what a bucket is about."""
    import os, shutil, tempfile
    from w2cplatform.events import EventLog, buckets_under
    from w2cplatform.resource import MIRROR_KEY, Resource, mirrored_buckets, peers_of, resources_seen
    box = Box(); t = box.wall() - 7200
    roots = {s: tempfile.mkdtemp(prefix=f"res-{s}-") for s in ("srv-a", "srv-b", "srv-c")}

    class Local:                                     # PeerClient's three calls, against directories
        def mirrored(self, url, server): return mirrored_buckets(roots[url], server)
        def put(self, url, server, path, data):
            p = os.path.join(roots[url], ".mirror", server, path); os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").write(data)
        def get(self, url, server, path): return open(os.path.join(roots[url], ".mirror", server, path), "rb").read()

    res = {s: Resource(r, s, s, box.vars, box.objects, wall=box.wall, peers=Local()) for s, r in roots.items()}
    EventLog(roots["srv-a"], "thing", "x", 1).append(t + 5, "tick", n=1)         # some subsystem's bucket, closed
    EventLog(roots["srv-a"], "other", "y", 2).append(t + 9, "seen")               # another's
    EventLog(roots["srv-a"], "thing", "x", 1).append(t + 7000, "tick", n=2)      # the open one
    for r in res.values(): r.heartbeat()
    assert resources_seen(box.objects)["srv-a"]["units"] == {"other": ["y"], "thing": ["x"]}
    assert peers_of("srv-a", list(res), 1) == ["srv-b"] and peers_of("srv-c", list(res), 1) == ["srv-a"]
    assert res["srv-a"].pass_()["enabled"] is False                              # knob off: nothing leaves
    box.vars.put(MIRROR_KEY, {"enabled": "true", "copies": "1"})
    r = res["srv-a"].pass_(); assert (r["mirrored"], r["peers"]) == (2, ["srv-b"])
    assert res["srv-a"].pass_()["mirrored"] == 0                                  # once
    res["srv-b"].heartbeat()
    assert resources_seen(box.objects)["srv-b"]["mirrors"] == {"srv-a": 2}
    assert ".mirror" not in res["srv-b"].units()                                  # a copy is not srv-b's data
    shutil.rmtree(roots["srv-a"]); os.makedirs(roots["srv-a"])                    # srv-a back with a replaced disk
    assert res["srv-a"].restore()["pulled"] == 2
    assert [b.events for b in buckets_under(roots["srv-a"], "thing", "x", 600)] == [1]   # the closed one is home; the open one was the RPO
    box.vars.put("other/retention", {"days": 1})
    box.wall.advance(3 * 86400)
    assert res["srv-a"].retain() == 1 and buckets_under(roots["srv-a"], "other", "y", 600) == []   # each subsystem's days, from its own row


def test_a_worker_that_released_its_slot_stops_receiving_units():
    """Deregistration, the one thing a service catalogue has that a heartbeat does not.

    A worker that lets go of its slot has said it is leaving. Its heartbeat is still
    seconds old and will stay "live" for `lost_after`, so without this the controller
    keeps placing NEW units on a process on its way out — and the next pass moves them
    off again. Churn at every scale-in and every rolling update. `redistribute` already
    read `Slot.released`; placement did not."""
    from vms.config import SPEC
    from vms.controller import VmsController
    from vms.worker import FakeActuator, VmsWorker
    from tests.conftest import Box

    box = Box()
    ctl = VmsController(box.vars.as_writer("vmscontroller", SPEC.acl_controller()), box.objects, wall=box.wall)
    con = VmsController(box.vars.as_writer("console", SPEC.acl_console()), box.objects, wall=box.wall)
    w1 = VmsWorker("w-1", box.vars, box.objects, FakeActuator(), clock=box.clock, wall=box.wall, server="srv-1", archive_root=box.archive)
    w2 = VmsWorker("w-2", box.vars, box.objects, FakeActuator(), clock=box.clock, wall=box.wall, server="srv-2", archive_root=box.archive)
    w1.heartbeat_once(); w2.heartbeat_once()

    w1.heartbeat_once(); w1.release_slot()                    # an orderly stop: a last word, then let go
    assert "w-1" in ctl.workers_seen()                        # still in the catalogue: its heartbeat is seconds old
    con.create_camera({"source": "driverpack://file/a.mp4"})
    assert ctl.ensure_placed()[0].worker == "w-2"             # …and still not a place to put work


def test_a_subscriber_is_not_handed_a_holder_that_has_gone_silent():
    """`heartbeats()` returns every last word whatever its age — the read model wants the
    stale ones, to show them muted. A SUBSCRIBER wants only who is reachable, and each of
    the four that ask (recorder, gateway, detector, console) used to decide that for
    itself: three leant on a dead worker's last `phase: running`, the fourth checked
    nothing. `holders()`/`holder_of()` put the filter in the catalogue, where it cannot
    be forgotten."""
    from w2cplatform.console import heartbeats, holder_of, holders
    from vms.config import SPEC
    from vms.controller import VmsController
    from vms.worker import FakeActuator, VmsWorker
    from tests.conftest import Box

    box = Box()
    ctl = VmsController(box.vars.as_writer("vmscontroller", SPEC.acl_controller()), box.objects, wall=box.wall)
    con = VmsController(box.vars.as_writer("console", SPEC.acl_console()), box.objects, wall=box.wall)
    w = VmsWorker("w-1", box.vars, box.objects, FakeActuator(), clock=box.clock, wall=box.wall, server="srv-1", archive_root=box.archive)
    w.heartbeat_once(); con.create_camera({"source": "driverpack://file/a.mp4"}); ctl.ensure_placed()
    w.reconcile_once(); w.heartbeat_once()

    assert holder_of(box.objects, "vms/", 1, box.wall(), phase="running", field="live_url") is not None

    box.wall.advance(60)                                      # the holder stops saying anything
    assert heartbeats(box.objects, "vms/")["w-1"].status[0]["phase"] == "running"   # its LAST word still says so
    assert holders(box.objects, "vms/", box.wall()) == {}                            # …and it is not reachable
    assert holder_of(box.objects, "vms/", 1, box.wall(), phase="running", field="live_url") is None


def test_the_watermark_asks_and_never_deletes():
    """Lesson 21. The resource measures the DISK (a test cannot fill one, so the probe
    is a seam), and over the high mark it says how many bytes to free — down to the LOW
    mark, or the next write puts it straight back over. What to give up is the
    subsystem's to decide: the platform calls `free` and touches nothing itself.

    The subsystem here keeps no video and no buckets — it counts. Nothing in this test
    knows what a camera is, which is the point of the door being a method name."""
    import tempfile
    from w2cplatform.resource import SPACE_KEY, Resource

    class Counter:
        """A subsystem hook: a pass that does nothing, and a `free` that gives up ticks."""
        def __init__(self): self.asked, self.ticks = [], 10 * [50_000]

        def pass_(self, now): return {"ticks": len(self.ticks)}

        def free(self, need, now, min_days=3.0):
            self.asked.append(need)
            freed = 0
            while self.ticks and freed < need:                 # one tick at a time, like a segment
                freed += self.ticks.pop(0)
            return {"freed": freed, "dropped": 10 - len(self.ticks)}

    box = Box()
    hook = Counter()
    res = Resource(tempfile.mkdtemp(prefix="space-"), "srv-1", "http://srv-1", box.vars, box.objects,
                   wall=box.wall, space_probe=lambda root: (1_000_000, 500_000))
    res.register("counter", hook)

    assert res.relieve() == {"space": "off"}                   # a knob, and it is off until an operator says otherwise
    box.vars.put(SPACE_KEY, {"enabled": "true", "high": "0.85", "low": "0.75"}, cas=0)
    assert res.relieve() == {"space": "ok", "full": 0.5} and hook.asked == []
    assert res.heartbeat()["space"] == {"total": 1_000_000, "free": 500_000, "used": 500_000, "full": 0.5}

    res.space_probe = lambda root: (1_000_000, 100_000)        # 90 % full
    rep = res.relieve()
    assert hook.asked == [150_000]                             # to the LOW mark, not to the high one
    assert rep["space"] == "over" and rep["need"] == 150_000 and rep["freed"] == 150_000 and rep["short"] == 0
    assert rep["counter.dropped"] == 3 and len(hook.ticks) == 7   # three ticks of fifty kB, and not one more

    # and a subsystem with no `free` is simply not asked: `retain` by days is its whole policy
    class Bucketsonly:
        def pass_(self, now): return {}
    res.hooks = {"other": Bucketsonly()}
    rep = res.relieve()
    assert rep["short"] == rep["need"] > 0                     # nobody could give anything: said, not hidden


def test_the_tree_is_walked_once_a_pass_and_never_on_a_heartbeat():
    """Lesson 21's other half. `usage` answers "how much do we hold" and can only be
    answered by walking; `space` answers "how much is left" and is one syscall. The
    first is measured with the policy pass and published from the cache with the time
    it was taken; the second is live in every heartbeat.

    At fifty cameras and ten-minute segments a month of archive is a quarter of a
    million files. Walking them every ten seconds does not merely cost a second — it
    touches every inode in the tree, so the cache holds the archive's metadata instead
    of the video the machine exists to serve."""
    import tempfile
    from w2cplatform.resource import Resource

    box = Box()
    res = Resource(tempfile.mkdtemp(prefix="usage-"), "srv-1", "http://srv-1", box.vars, box.objects,
                   wall=box.wall, space_probe=lambda root: (1_000_000, 400_000))
    walks = []
    res.usage = lambda: (walks.append(box.wall()), 4_100_000_000)[1]

    hb = res.heartbeat()                                   # the first one of a process pays for it once
    assert len(walks) == 1 and hb["usage"] == 4_100_000_000 and hb["usage_at"] == box.wall()
    box.wall.advance(10)
    for _ in range(59):                                    # ten minutes of heartbeats, one per ten seconds
        hb = res.heartbeat()
    assert len(walks) == 1                                 # not one more walk
    assert hb["usage_at"] < hb["ts"]                       # and the number says how old it is
    assert hb["space"]["free"] == 400_000                  # while `space` is measured every time

    res.pass_()
    assert len(walks) == 2 and res.usage_at == box.wall()  # the pass is where the walk belongs
    assert res.heartbeat()["usage_at"] == box.wall()
