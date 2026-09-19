"""`retire_when`: the word the first four subsystems never needed. A unit whose
work is over is not placed, gives back the budget it held, and stays that way."""
from w2cplatform.console import Heartbeat
from w2cplatform.spec import SpecController, SubsystemSpec
from vms.config import DETJOB_SPEC, SPEC
from vms.jobs import reap
from tests.conftest import Box


def _ctls(box):
    con = SpecController(DETJOB_SPEC, box.vars.as_writer("console", DETJOB_SPEC.acl_console()), box.objects, wall=box.wall)
    adm = SpecController(DETJOB_SPEC, box.vars.as_writer("detjobcontroller", DETJOB_SPEC.acl_controller()), box.objects, wall=box.wall)
    return con, adm


def _worker(box, worker, server, *status, capacity=2):
    box.objects.put(DETJOB_SPEC.sub.heartbeat_key(worker),
                    Heartbeat(worker, box.wall(), list(status),
                              {"server": server, "capacity": capacity, "headroom": capacity, "labels": "gpu"}).to_bytes())


def _job(con, name):
    con.create({"name": name, "cam": "7", "rec": "7", "kind": "lpr", "from": 100.0, "to": 200.0})
    return name


def test_finished_work_is_not_placed():
    box = Box(); con, adm = _ctls(box)
    _worker(box, "j-1", "srv-1")
    a, b = _job(con, "a"), _job(con, "b")
    con.update(b, {"state": "done"})
    adm.ensure_placed()
    assert adm.placement(a) is not None and adm.placement(b) is None


def test_finished_work_is_not_the_clusters_fault():
    """`/unplaceable` is the answer to "nothing here can serve this". A job that is
    over is unplaced for a completely different reason, and telling the operator
    their cluster cannot run it sends them looking for a worker to add."""
    box = Box(); con, adm = _ctls(box)
    _worker(box, "j-1", "srv-1")                                        # labels: gpu
    con.create({"name": "a", "cam": "7", "rec": "7", "kind": "lpr", "from": 100.0, "to": 200.0, "labels": ["fpga"]})
    con.create({"name": "b", "cam": "7", "rec": "7", "kind": "lpr", "from": 100.0, "to": 200.0, "labels": ["fpga"]})
    con.update("b", {"state": "done"})
    adm.ensure_placed()
    assert [u["id"] for u in adm.unplaceable()] == ["a"]                 # the one still waiting, and only it


def test_the_budget_a_finished_job_held_comes_back():
    """One worker, one slot. The second job waits; the first finishes; the second
    runs. Without the un-placing half, a cluster's whole capacity ends up held by
    work that is over."""
    box = Box(); con, adm = _ctls(box)
    _worker(box, "j-1", "srv-1", capacity=1)
    a, b = _job(con, "a"), _job(con, "b")
    adm.ensure_placed()
    assert adm.placement(a) is not None and adm.placement(b) is None

    con.update(a, {"state": "done"})
    adm.ensure_placed()
    assert adm.placement(a) is None and adm.placement(b) is not None
    assert adm.placement(b).worker == "j-1"


def test_the_reason_says_which_end_it_came_to():
    box = Box(); con, adm = _ctls(box)
    _worker(box, "j-1", "srv-1")
    a = _job(con, "a"); adm.ensure_placed()
    con.update(a, {"state": "failed"}); adm.ensure_placed()
    it, _ = adm.vars.get(DETJOB_SPEC.sub.config("placement", a))
    assert it["worker"] == "" and it["reason"] == "failed"     # "done" and "failed" are different news


def test_the_whole_cycle_does_not_start_the_scan_over():
    """The failure this design exists to avoid, walked end to end. The worker says
    done; the reaper writes the row; the controller takes the assignment back; the
    worker drops the job and its next heartbeat does not mention it — and the
    evidence of `done` is gone from the heartbeats. The ROW is what keeps it
    finished."""
    box = Box(); con, adm = _ctls(box)
    _worker(box, "j-1", "srv-1")
    a = _job(con, "a"); adm.ensure_placed()
    assert adm.placement(a).worker == "j-1"

    _worker(box, "j-1", "srv-1", {"id": a, "phase": "done", "covered": 600.0})     # the worker finished it
    assert reap(con)["done"] == 1
    adm.ensure_placed()
    assert adm.placement(a) is None                                                # the assignment is back

    _worker(box, "j-1", "srv-1")                                                   # the worker dropped it: silence
    assert [st for st in adm.read_model() if st["id"] == a] == []                   # no heartbeat says done any more
    for _ in range(3):
        adm.ensure_placed()
    assert adm.placement(a) is None                                                # and it is still not running

    con.update(a, {"state": "queued"})                                             # the operator asks again
    adm.ensure_placed()
    assert adm.placement(a) is not None                                            # the row decides, and only the row


def test_a_subsystem_that_never_ends_is_untouched():
    """`enabled` is not this. A disabled camera keeps its assignment and its line
    in the console's list; a unit that stays visible while doing nothing is a
    different thing from one that is over."""
    assert SPEC.retire_field == "" and SPEC.retire_values == ()
    box = Box()
    ctl = SpecController(SPEC, box.vars, box.objects, wall=box.wall)
    box.objects.put(SPEC.sub.heartbeat_key("w-1"),
                    Heartbeat("w-1", box.wall(), [], {"server": "srv-1", "capacity": 50, "headroom": 50}).to_bytes())
    ctl.create({"name": "gate", "source": "driverpack://file/gate.mp4"}); ctl.ensure_placed()
    uid = ctl.units()[0]["id"]
    ctl.update(uid, {"enabled": False}); ctl.ensure_placed()
    assert ctl.placement(uid) is not None and ctl.placement(uid).worker == "w-1"


def test_a_predicate_that_retires_nothing_is_refused_at_load():
    unit = {"rows": "u", "id": "name", "fields": {"name": {"type": "string"}, "state": {"type": "string"}}}
    for bad, want in (({"field": "phase", "in": ["done"]}, "names no field"),
                      ({"field": "state"}, "non-empty"),
                      ({"in": ["done"]}, "non-empty")):
        try:
            SubsystemSpec.from_dict({"name": "x", "unit": unit, "placement": {"retire_when": bad}})
            raise AssertionError(f"accepted retire_when: {bad}")
        except ValueError as e:
            assert want in str(e), str(e)
