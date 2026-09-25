"""Lesson 4 — vmsworker: М9's loop over an assignment; the epoch and the
lease; the restart with the controller stopped; the zombie on one box."""
import os
from vms.controller import VmsController
from vms.reconciler import CONVERGED, LAGGING, STALLED, Reconciler
from vms.worker import FakeActuator, VmsWorker
from tests.conftest import Box, Clock, FakeStore, cam


# -- М9 Lesson 6's seven, unchanged in meaning ------------------------------------------

def test_1_converge_then_idle():
    store, act = FakeStore([cam(1), cam(2)]), FakeActuator()
    r = Reconciler(store, act)
    assert r.reconcile() == [("start", 1), ("start", 2)] and r.reconcile() == [] and r.reconcile() == []


def test_2_revision_bump_restarts():
    store, act = FakeStore([cam(1)]), FakeActuator()
    r = Reconciler(store, act); r.reconcile()
    store.rows[0]["revision"] = 2
    assert r.reconcile() == [("restart", 1)] and r.reconcile() == [] and r.actual[1]["revision"] == 2


def test_3_disable_and_delete():
    store, act = FakeStore([cam(1), cam(2)]), FakeActuator()
    r = Reconciler(store, act); r.reconcile()
    store.rows[0]["enabled"] = False
    assert r.reconcile() == [("stop", 1)]
    del store.rows[1]
    assert r.reconcile() == [("stop", 2)] and r.actual == {}


def test_4_restart_re_derives_actual():
    store = FakeStore([cam(1)])
    Reconciler(store, FakeActuator()).reconcile()
    r2 = Reconciler(store, FakeActuator())
    assert r2.actual == {} and r2.reconcile() == [("start", 1)]


def test_5_persisted_actual_is_a_cache_that_lies():
    class Persisted(Reconciler):
        def __init__(self, *a, saved=None, **k):
            super().__init__(*a, **k); self.actual = saved or {}
    store, act = FakeStore([cam(1, revision=2)]), FakeActuator()
    liar = Persisted(store, act, saved={1: {"revision": 2}})
    assert liar.reconcile() == [] and liar.status()[1][0] == CONVERGED and act.running == set()


def test_6_backoff_with_jitter_spreads_200_cameras():
    r = Reconciler(FakeStore([cam(i) for i in range(200)]), FakeActuator(failing=lambda cid: True))
    r.reconcile(now=0)
    retries = sorted(f["retry_at"] for f in r.failures.values())
    assert 1.0 <= retries[0] and retries[-1] <= 2.0 and retries[-1] - retries[0] > 0.5
    assert r.reconcile(now=0.5) == [] and len(r.reconcile(now=2.0)) == 200


def test_7_lagging_vs_stalled():
    store = FakeStore([cam(1), cam(2)])
    r = Reconciler(store, FakeActuator(failing={2}), stall_failures=3)
    r.reconcile(now=0)
    assert r.status()[1] == (CONVERGED, 0) and r.status()[2] == (LAGGING, 1)
    now = 0.0
    for _ in range(3):
        now = r.failures[2]["retry_at"] + 0.01; r.reconcile(now=now)
    assert r.status()[2] == (STALLED, 1)
    r.lost(1, now); assert 1 not in r.actual and r.status()[1][0] == LAGGING


# -- the worker over an assignment ---------------------------------------------------------

def _box_with_cameras(n=2):
    box = Box()
    ctl = VmsController(box.vars, box.objects, capacity=50, wall=box.wall)
    for i in range(1, n + 1):
        ctl.create_camera({"name": f"cam{i}", "source": f"driverpack://file/cam{i}.mp4"})
    return box, ctl


def test_worker_runs_its_assignment_and_takes_an_epoch_per_camera():
    box, ctl = _box_with_cameras(2)
    act = FakeActuator()
    w = VmsWorker("w-1", box.vars, box.objects, act, clock=box.clock, wall=box.wall, server="srv-1")
    assert w.reconcile_once() == []                          # unassigned: it invents nothing
    ctl.assign("w-1", ["1", "2"])
    assert w.reconcile_once() == [("start", 1), ("start", 2)]
    assert act.epochs == {1: 1, 2: 1} and w.may_write("1") and w.may_write("2")
    assert box.vars.get("vms/epoch/1")[0] == {"epoch": "1"}
    ctl.update_camera(1, {"name": "gate"})                   # an edit: revision 2
    assert w.reconcile_once() == [("restart", 1)] and act.epochs[1] == 1     # a restart keeps its epoch
    ctl.assign("w-1", ["2"])                                  # camera 1 reassigned away
    assert w.reconcile_once() == [("stop", 1)] and "1" not in w.epochs
    w.heartbeat_once()
    hb = ctl.workers_seen()["w-1"]
    assert [s["id"] for s in hb.status] == [2] and hb.status[0]["phase"] == "running" and hb.extra["server"] == "srv-1"


def test_restart_with_the_controller_stopped():
    """The controller is never on the recovery path: a fresh worker reads
    its assignment and records; nothing is asked of anyone."""
    box, ctl = _box_with_cameras(3)
    ctl.assign("w-1", ["1", "2", "3"])
    w1 = VmsWorker("w-1", box.vars, box.objects, FakeActuator(), clock=box.clock, wall=box.wall)
    w1.reconcile_once()
    del ctl                                                   # the controller is gone
    act2 = FakeActuator()
    w2 = VmsWorker("w-1", box.vars, box.objects, act2, clock=box.clock, wall=box.wall)   # kill -9, restart
    assert w2.reconciler.actual == {}                         # a fresh process knows nothing
    assert w2.reconcile_once() == [("start", 1), ("start", 2), ("start", 3)]
    assert act2.epochs == {1: 2, 2: 2, 3: 2}                  # the next epoch for each: the old instance is fenced by construction


def test_the_zombie_on_one_box():
    """Two instances of w-1 given the same assignment (a pause, then a
    replacement): the second takes the slot and the next epochs; the first
    fences itself on renewal — at the slot, and the epochs agree — and stops
    everything."""
    box, ctl = _box_with_cameras(1)
    ctl.assign("w-1", ["1"])
    a_act, b_act = FakeActuator(), FakeActuator()
    a = VmsWorker("w-1", box.vars, box.objects, a_act, clock=box.clock, wall=box.wall)
    a.reconcile_once(); assert a_act.running == {1} and a_act.epochs[1] == 1
    b = VmsWorker("w-1", box.vars, box.objects, b_act, clock=box.clock, wall=box.wall)     # the replacement
    b.reconcile_once(); assert b_act.running == {1} and b_act.epochs[1] == 2
    assert a.lease_pass() == ["1"] and not a.recording_allowed and a_act.running == set()   # A wakes, renews, fences
    assert "slot w-1" in a.fenced_reason                      # fenced at the slot first...
    assert a.renew_leases() == ["1"] and a.conflicts() == 1   # ...and the camera's epoch says the same
    assert a.reconcile_once() == [("failed", 1)]              # it may start nothing
    assert b.lease_pass() == [] and b_act.running == {1}      # B is fine


def test_a_replacement_without_a_name_inherits_the_lapsed_slot():
    """Nomad started `count = 2` workers and nobody told them their names.
    One dies; its replacement claims whatever is free — the lapsed slot first —
    and records the dead one's cameras from the assignment, asking nobody."""
    box, ctl = _box_with_cameras(4)
    a = VmsWorker(None, box.vars, box.objects, FakeActuator(), clock=box.clock, wall=box.wall)
    b = VmsWorker(None, box.vars, box.objects, FakeActuator(), clock=box.clock, wall=box.wall)
    assert (a.name, b.name) == ("w-1", "w-2")
    ctl.assign("w-1", ["1", "2"]); ctl.assign("w-2", ["3", "4"])
    a.reconcile_once(); b.reconcile_once()
    box.wall.advance(46)                                      # A is dead: its slot lapsed, its cameras are listed on w-1
    b.lease_pass()                                            # B is alive and renews
    act = FakeActuator()
    c = VmsWorker(None, box.vars, box.objects, act, clock=box.clock, wall=box.wall)     # the replacement alloc
    assert c.name == "w-1"                                    # not w-3: the lapsed slot, and with it the assignment
    assert c.reconcile_once() == [("start", 1), ("start", 2)] and act.epochs == {1: 2, 2: 2}
    assert not a.renew_slot() and c.lease_pass() == []        # A, wherever it is, is fenced at the slot; C is fine


def test_the_zombie_is_fenced_at_the_slot_first():
    """The replacement that Nomad starts under the same index takes the slot
    outright; the paused instance finds out at its next renewal, before any
    epoch is looked at."""
    box, ctl = _box_with_cameras(1)
    ctl.assign("w-1", ["1"])
    a = VmsWorker("w-1", box.vars, box.objects, FakeActuator(), clock=box.clock, wall=box.wall)
    a.reconcile_once()
    b = VmsWorker("w-1", box.vars, box.objects, FakeActuator(), clock=box.clock, wall=box.wall)   # same index, new alloc
    assert b.slot.gen == 2 and a.lease_pass() == ["1"] and "slot w-1" in a.fenced_reason
    assert b.lease_pass() == []


def test_the_worker_observes_what_it_holds_recording_or_not():
    """An event is written by the worker that holds the camera's epoch, into
    the camera's bucket on this server's resource. Not recording is not a
    reason; not holding it is. A lost pipeline writes `silent` — the event
    that cannot have a segment."""
    from w2cplatform.events import read_bucket
    box, ctl = _box_with_cameras(2)
    ctl.assign("w-1", ["1"])
    act = FakeActuator(); w = VmsWorker("w-1", box.vars, box.objects, act, clock=box.clock, wall=box.wall, archive_root=box.archive)
    assert w.observe(1, "motion") is None                          # no epoch held yet: not mine to observe
    w.reconcile_once()
    p = w.observe(1, "motion", zone="gate")
    assert p and p.startswith(os.path.join(box.archive, "vms", "1", "e1")) and read_bucket(p)[0]["zone"] == "gate"
    assert w.observe(2, "motion") is None                          # camera 2 is not assigned to me
    act.post(1, "person", score=0.91)                              # an element posted on the bus...
    w.pump_once()                                                  # ...and the worker, holding the epoch, made it a line
    act.dead = [1]                                                 # the pipeline died
    w.pump_once()
    assert [e["kind"] for e in read_bucket(p)] == ["motion", "person", "silent"] and w.reconciler.actual.get(1) is None
    assert read_bucket(p)[1]["score"] == 0.91
    w.fence("test"); act.post(1, "motion"); w.pump_once()
    assert len(read_bucket(p)) == 3                                # a fenced instance's bus still posts; observe drops it
    assert box.vars.list("vms/events") == [] and ctl.workers_seen() == {}    # nobody was told; nothing went to the store


def test_a_reassignment_is_not_a_zombie():
    """The same lease loss, but the camera is no longer mine: let it go quietly."""
    box, ctl = _box_with_cameras(1)
    ctl.assign("w-1", ["1"])
    w1 = VmsWorker("w-1", box.vars, box.objects, FakeActuator(), clock=box.clock, wall=box.wall); w1.reconcile_once()
    ctl.move(1, "w-2", "operator asked")
    w2 = VmsWorker("w-2", box.vars, box.objects, FakeActuator(), clock=box.clock, wall=box.wall); w2.reconcile_once()
    assert w1.lease_pass() == ["1"] and w1.recording_allowed and w1.reconciler.actual == {}   # released, not fenced
    assert w1.reconcile_once() == []


def test_lease_expiry_without_renewal_stops_starts():
    box, ctl = _box_with_cameras(1)
    ctl.assign("w-1", ["1"])
    act = FakeActuator()
    w = VmsWorker("w-1", box.vars, box.objects, act, lease_ttl=30, lease_margin=5, clock=box.clock, wall=box.wall)
    w.reconcile_once()
    box.clock.advance(26)
    assert not w.may_write("1")
    w.reconciler.lost(1, w.now())                             # the pipeline died meanwhile
    box.clock.advance(5)                                      # past its backoff
    assert w.reconcile_once() == [("start", 1)] and act.epochs[1] == 2     # a start takes a fresh epoch and lease


def test_the_first_heartbeat_does_not_wait_for_the_first_tick():
    """A worker nobody can see is a worker nothing is placed on.

    The loop heartbeats every ten seconds, and for a long time the FIRST one arrived only because
    `time.monotonic()` counts from boot, so `clock() - 0 >= 10` was true on the very first pass. Run the
    same loop on a clock that starts at zero — which is what Go's monotonic does, and what a fake clock
    does here — and that accident disappears: the worker claims its slot, says nothing for ten seconds,
    and its cameras sit unplaced for as long. So the first heartbeat is sent before the loop, said out
    loud, and this is the test that keeps it said.

    Counted rather than timed: an orderly stop heartbeats too, so one pass with the announcement is two
    heartbeats and one without it is one."""
    import threading
    box = Box()
    clock = Clock(0.0)                     # counts from process start, as Go's Monotonic() does
    w = VmsWorker("w-1", box.vars, box.objects, FakeActuator(), clock=clock, wall=box.wall, server="srv-1")
    sent = []
    real = w.heartbeat_once
    w.heartbeat_once = lambda *a, **k: (sent.append(clock()), real(*a, **k))[1]

    stop = threading.Event()
    original_wait = stop.wait
    stop.wait = lambda timeout=None: (stop.set(), original_wait(0))[1]   # one pass, then out
    w.run(poll=0.01, stop=stop)

    assert sent and sent[0] == 0.0, f"the first heartbeat waited: {sent}"
    assert len(sent) == 2, sent          # the announcement, and the orderly stop's own
    assert box.objects.get("vms/heartbeats/w-1")


def test_a_storm_becomes_one_line_and_a_count_of_what_it_swallowed():
    """A storm of events is normal, not a fault: a contact bouncing, a link
    flapping, a device re-reporting for as long as the thing it watches keeps
    happening. Nothing downstream can undo one — an index reads what is
    written, a timeline draws what it reads, and every reader that collapsed
    repeats on its own would disagree with the others. The one place a repeat
    costs nothing to recognise is the process holding the epoch, one line
    before the write.

    Two properties make that honest rather than a quiet loss of data. The
    FIRST line of a window is written immediately and unchanged, because
    whatever the repeats are worth, the first occurrence is the observation.
    And what was swallowed is SAID: `repeats`, `since` and `until`, so a quiet
    log and a suppressed storm are different things on the timeline."""
    from w2cplatform.events import read_bucket
    box, ctl = _box_with_cameras(1)
    ctl.assign("w-1", ["1"])
    act = FakeActuator(); w = VmsWorker("w-1", box.vars, box.objects, act, clock=box.clock, wall=box.wall, archive_root=box.archive)
    w.reconcile_once()

    p = w.observe(1, "io.input", port="1", value="closed")        # the first one is the news…
    for _ in range(200):                                          # …and the next two hundred are the same news
        box.wall.advance(0.1); w.observe(1, "io.input", port="1", value="closed")
    assert len(read_bucket(p)) == 1, "the window is open: repeats are counted, not written"

    box.wall.advance(30)                                          # the window closes, and the next observation carries the count out
    w.observe(1, "io.input", port="1", value="closed")
    rows = read_bucket(p)
    assert [r.get("repeats") for r in rows] == [None, 200, None]  # summary between the two windows' first lines
    assert abs((rows[1]["until"] - rows[1]["since"]) - 20.0) < 0.01 and rows[1]["value"] == "closed"
    assert rows[1]["t"] == rows[1]["until"], "the summary is dated when the storm ended, not when it was reported"


def test_a_contact_that_changes_is_never_one_event_and_a_storm_that_ends_is_counted():
    """Sameness is every field by default, which is the reading that cannot
    lose an observation: a contact that opens and then closes differs in
    `value`, so it is two events however fast it moves. That default is why
    suppression can be turned on for a kind without first proving which of its
    fields matter.

    And a burst that ENDS still gets counted. Nothing observes it closed — the
    storm stopped — so the pass flushes the window: without that, the quiet
    minute and the swallowed thousand look the same in the log."""
    from w2cplatform.events import read_bucket
    box, ctl = _box_with_cameras(1)
    ctl.assign("w-1", ["1"])
    act = FakeActuator(); w = VmsWorker("w-1", box.vars, box.objects, act, clock=box.clock, wall=box.wall, archive_root=box.archive)
    w.reconcile_once()

    p = w.observe(1, "io.input", port="1", value="closed")
    box.wall.advance(0.1); w.observe(1, "io.input", port="1", value="open")      # a different thing…
    box.wall.advance(0.1); w.observe(1, "io.input", port="2", value="closed")    # …and a different contact
    assert [r["value"] for r in read_bucket(p)] == ["closed", "open", "closed"]

    box.wall.advance(0.1)
    for _ in range(9):
        box.wall.advance(0.1); w.observe(1, "io.input", port="2", value="closed")
    assert w.flush_suppressed() == 0, "the window has not closed yet: nothing to report"
    box.wall.advance(30)
    assert w.flush_suppressed() == 1
    last = read_bucket(p)[-1]
    assert last["repeats"] == 9 and last["port"] == "2"
    assert w.flush_suppressed() == 0, "a window is reported once"
