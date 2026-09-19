"""The scan worker: work with both ends. A budget per pass, media time on the
events, progress that survives a restart, and the only worker that can say done."""
import json
import os

from w2cplatform.events import read_bucket
from w2cplatform.spec import SpecController
from vms.archive import Manifest, Segment
from vms.config import DETJOB_SPEC
from vms.detjobworker import DetJobWorker
from vms.scan import ScanLog
from tests.conftest import Box

T = 1_757_500_000.0 - 7 * 24 * 3600      # the footage is a week older than the worker's clock


def m(n):
    return T + n * 60


class Every:
    """A model that fires on every look and reports HOW MANY looks it has had.
    The count is what makes "the file is decoded from its head" observable: a
    model handed only the asked-for window would be on its first look there."""
    def __init__(self, row): self.row, self.looks = row, 0
    def observe(self, now):
        self.looks += 1
        return [(self.row["kind"], {"at": now, "looks": self.looks})]
    def close(self): pass


def _footage(box, rec, epoch, a, b):
    s = Segment(str(rec), epoch, m(a), m(b), f"rec/{rec}/e{epoch}/{int(m(a))}.mp4", 1000)
    Manifest(box.archive, rec).append(s)
    return s


def _worker(box, name="j-1", **kw):
    return DetJobWorker(name, box.vars.as_writer("detjobworker", DETJOB_SPEC.sub.acl_worker()), box.objects,
                        models={"lpr": Every}, clock=box.clock, wall=box.wall, server="srv-1",
                        archive_root=box.archive, env={"LABELS": "gpu"}, step=60.0, **kw)


def _job(box, name="7-lpr-1", frm=0, to=10, rec="7", cam="7"):
    ctl = SpecController(DETJOB_SPEC, box.vars, box.objects, wall=box.wall)
    ctl.create({"name": name, "cam": cam, "rec": rec, "kind": "lpr", "from": m(frm), "to": m(to)})
    box.vars.put(DETJOB_SPEC.sub.assignment("j-1"), {"units": name, "rev": 1}, cas=0)   # no controller here: the assignment by hand
    return ctl


def test_a_scan_writes_what_it_saw_into_its_own_tree_under_its_epoch():
    box = Box(); _footage(box, "7", 1, 0, 10); _job(box)
    w = _worker(box)
    w.reconcile_once(); w.heartbeat_once()

    root = os.path.join(box.archive, "detjob", "7-lpr-1", f"e{w.epochs['7-lpr-1']}")
    lines = [l for f in sorted(os.listdir(root)) for l in read_bucket(os.path.join(root, f))]
    assert lines and all(l["cam"] == 7 and l["source"] == "archive" and l["job"] == "7-lpr-1" for l in lines)
    assert not os.path.exists(os.path.join(box.archive, "det", "7-lpr-1"))     # never the live detector's tree


def test_the_events_carry_media_time_not_the_clock():
    """A scan of last Tuesday writes events dated last Tuesday. The worker's own
    wall clock is thirty years away from the footage in this test, on purpose."""
    box = Box(); _footage(box, "7", 1, 0, 10); _job(box)
    w = _worker(box)
    w.reconcile_once()
    root = os.path.join(box.archive, "detjob", "7-lpr-1", f"e{w.epochs['7-lpr-1']}")
    ts = [l["t"] for f in sorted(os.listdir(root)) for l in read_bucket(os.path.join(root, f))]
    assert ts and all(m(0) <= t < m(10) for t in ts)
    assert all(box.wall() - t > 6 * 24 * 3600 for t in ts)                      # a week back, not now


def test_what_the_operator_did_not_ask_for_does_not_become_an_event():
    """The segment opens at 10:00 and the job asks from 10:05. The file is read
    from its head — the model sees 10:00 — and none of it is in the answer."""
    box = Box(); _footage(box, "7", 1, 0, 10); _job(box, frm=5, to=8)
    w = _worker(box)
    w.reconcile_once()
    root = os.path.join(box.archive, "detjob", "7-lpr-1", f"e{w.epochs['7-lpr-1']}")
    lines = sorted((l for f in sorted(os.listdir(root)) for l in read_bucket(os.path.join(root, f))),
                   key=lambda l: l["t"])
    ts = [l["t"] for l in lines]
    assert ts and min(ts) >= m(5) and max(ts) < m(8)
    # …and the five minutes before it WERE decoded: the model's first reported look is not its first look.
    assert lines[0]["looks"] > 1, "the model was handed the window, not the file — then nothing needed clipping"


def test_a_long_job_advances_by_a_budget_and_the_heartbeat_moves_each_pass():
    """Six stretches, four per pass. A pass that ran the job to the end is a
    worker that stops heartbeating while it does."""
    box = Box()
    for i in range(6):
        _footage(box, "7", 1, i * 10, (i + 1) * 10)
    _job(box, frm=0, to=60)
    w = _worker(box)

    w.reconcile_once(); first = w.status_by_unit["7-lpr-1"]
    assert first["phase"] == "running" and first["done_through"] == m(40)       # four of six
    w.reconcile_once(); second = w.status_by_unit["7-lpr-1"]
    assert second["done_through"] == m(60) and second["events"] > first["events"]
    w.reconcile_once()
    assert w.status_by_unit["7-lpr-1"]["phase"] == "done"


def test_a_restarted_worker_resumes_and_does_not_double_the_events():
    box = Box()
    for i in range(3):
        _footage(box, "7", 1, i * 10, (i + 1) * 10)
    _job(box, frm=0, to=30)
    w = _worker(box); w.reconcile_once()
    before = ScanLog(box.archive, "7-lpr-1").events()

    w2 = _worker(box, name="j-1")                                               # same slot, new process
    w2.reconcile_once()
    assert ScanLog(box.archive, "7-lpr-1").events() == before                   # nothing was scanned twice
    assert w2.status_by_unit["7-lpr-1"]["phase"] == "done"


def test_no_footage_here_is_not_no_events():
    """`near` is a preference, so a job can be placed away from what it reads.
    Saying which of the two silences it is, is the whole point of the phase."""
    box = Box(); _job(box)                                                      # no manifest on this server
    w = _worker(box)
    w.reconcile_once()
    st = w.status_by_unit["7-lpr-1"]
    assert st["phase"] == "waiting" and "another server" in st["why"]
    assert not os.path.exists(os.path.join(box.archive, "detjob", "7-lpr-1"))


def test_the_heartbeat_says_how_much_of_the_interval_had_footage():
    """Asked for an hour, recorded forty minutes: a finished scan with no events
    has to be able to say which nothing it is."""
    box = Box()
    _footage(box, "7", 1, 0, 20); _footage(box, "7", 1, 40, 60)
    _job(box, frm=0, to=60)
    w = _worker(box)
    w.reconcile_once()
    st = w.status_by_unit["7-lpr-1"]
    assert st["asked"] == 3600 and st["covered"] == 2400


def test_a_terminal_row_is_reported_and_not_worked_on():
    """Nothing in placement reads `done` yet, so the row stays assigned until the
    console's reaper moves it. An UNFINISHED job whose state went terminal must
    stop where it is — the operator cancelled it, or the reaper called it failed."""
    box = Box()
    for i in range(6):
        _footage(box, "7", 1, i * 10, (i + 1) * 10)
    ctl = _job(box, frm=0, to=60)
    w = _worker(box); w.reconcile_once()
    done_after_one_pass = len(ScanLog(box.archive, "7-lpr-1").read())
    assert 0 < done_after_one_pass < 6                                          # really unfinished

    ctl.update("7-lpr-1", {"state": "done"})
    w2 = _worker(box, name="j-1"); w2.reconcile_once()
    assert w2.status_by_unit["7-lpr-1"]["phase"] == "done"
    assert "7-lpr-1" not in w2.epochs                                           # no epoch taken
    assert len(ScanLog(box.archive, "7-lpr-1").read()) == done_after_one_pass    # and no stretch worked


def test_a_stretch_that_failed_halfway_is_not_recorded_as_done():
    """The line goes in AFTER the events, and this is what that buys: a crash
    between the work and the line costs a re-scan of one stretch, never a
    stretch nobody will look at again. Written the other way round the failure
    is silent and permanent."""
    import vms.detjobworker as mod
    box = Box()
    for i in range(3):
        _footage(box, "7", 1, i * 10, (i + 1) * 10)
    _job(box, frm=0, to=30)
    w = _worker(box)

    real, seen = mod.EventLog, {"n": 0}

    class Exploding(real):
        def append(self, t, kind, **fields):
            seen["n"] += 1
            if seen["n"] > 3:
                raise OSError("the disk went away mid-stretch")
            return real.append(self, t, kind, **fields)

    mod.EventLog = Exploding
    try:
        w.reconcile_once()
        raise AssertionError("the failure was swallowed")
    except OSError:
        pass
    finally:
        mod.EventLog = real

    log = ScanLog(box.archive, "7-lpr-1")
    assert len(log.read()) == 0, "a stretch that never finished is written down as finished"
    w2 = _worker(box, name="j-1"); w2.reconcile_once()
    assert len(ScanLog(box.archive, "7-lpr-1").read()) == 3                     # it was redone, and finished


def _holder_of_camera(box, cam, cov=None):
    """A VMS worker holding the camera, its status carrying the DEVICE's coverage
    summary — `from`, `to`, `fragments`, never an index (М10B Lesson 15)."""
    from w2cplatform.console import Heartbeat
    st = {"id": str(cam), "phase": "running", "live_url": f"rtsp://srv-1:8554/{cam}"}
    if cov is not None:
        st["coverage"] = cov
    box.objects.put("vms/heartbeats/w-1",
                    Heartbeat("w-1", box.wall(), [st], {"server": "srv-1", "capacity": 50, "headroom": 49}).to_bytes())


def test_footage_the_device_has_and_we_do_not_is_a_step_not_a_dead_end():
    """`fetching`, not `waiting`: the minutes exist, they are simply not ours yet.
    The console turns this into a request to the recorder; the scan never opens the
    device's own door, because those two sessions belong to the operator watching
    and to the recorder saving."""
    box = Box(); _job(box)                                                  # no manifest on this server
    _holder_of_camera(box, 7, {"from": m(-100), "to": m(100), "fragments": 5})
    w = _worker(box)
    w.reconcile_once()
    st = w.status_by_unit["7-lpr-1"]
    assert st["phase"] == "fetching" and "device" in st["why"]
    assert "7-lpr-1" not in w.epochs                                        # nothing is being written yet


def test_nobody_recorded_it_is_a_different_answer():
    box = Box(); _job(box)
    _holder_of_camera(box, 7, None)                                         # held, but the device has no archive
    w = _worker(box)
    w.reconcile_once()
    assert w.status_by_unit["7-lpr-1"]["phase"] == "waiting"


def test_a_device_whose_coverage_misses_the_interval_is_not_asked():
    """The card keeps three days. A search over last month is not a fetch that will
    ever succeed, and saying `fetching` would leave the job hopeful for ever."""
    box = Box(); _job(box, frm=0, to=10)
    _holder_of_camera(box, 7, {"from": m(500), "to": m(900), "fragments": 5})
    w = _worker(box)
    w.reconcile_once()
    assert w.status_by_unit["7-lpr-1"]["phase"] == "waiting"
