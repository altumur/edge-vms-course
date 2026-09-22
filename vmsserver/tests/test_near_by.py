"""Following a subsystem whose units are named differently: `by` and `of`.

A detector's unit is `7-linecross`; no recorder ever reports that id. The bare `near: rec`
therefore matches nothing on it — an affinity that reads as followed and is not. `by: cam`
says which value of MINE to look for. `of: cam` says where in THEIR status to look for it,
and it arrived when recordings stopped being named by their camera: `7-cloud` is a recording
of camera 7, and nothing but the recorder's own `cam` field says so. These tests are about
both differences being visible."""
import json

from w2cplatform.console import Heartbeat
from w2cplatform.spec import SpecController, SubsystemSpec
from vms.config import DET_SPEC, DETJOB_SPEC, REC_SPEC
from tests.conftest import Box


def _worker(box, spec, worker: str, server: str, capacity: int = 8, status=None):
    box.objects.put(spec.sub.heartbeat_key(worker),
                    Heartbeat(worker, box.wall(), status or [], {"server": server, "capacity": capacity,
                                                                 "headroom": capacity, "labels": "gpu"}).to_bytes())
    box.objects.put(f"platform/resources/{server}/heartbeat",
                    json.dumps({"server": server, "ts": box.wall(), "url": f"http://{server}", "units": {}}).encode())


def _recorder_holding(box, worker: str, server: str, *recordings):
    """A recorder whose heartbeat says it is running these recordings, each entry carrying both
    identities the way `RecWorker.status_extra` does: `id` is WHICH recording, `cam` is WHOSE
    footage. Since `id: name` those are two strings — write `"7-cloud:7"` when they differ, `"7"`
    when the operator left the name to default to the camera."""
    status = []
    for r in recordings:
        uid, _, cam = str(r).partition(":")
        status.append({"id": uid, "cam": cam or uid, "phase": "running"})
    _worker(box, REC_SPEC, worker, server, status=status)


def test_the_detector_lands_beside_the_recorder_holding_its_camera():
    box = Box()
    ctl = SpecController(DET_SPEC, box.vars, box.objects, wall=box.wall)
    _worker(box, DET_SPEC, "d-1", "srv-1")
    _worker(box, DET_SPEC, "d-2", "srv-2")
    _recorder_holding(box, "r-2", "srv-2", "7")                 # camera 7's footage is on srv-2

    ctl.create({"name": "7-linecross", "cam": "7", "kind": "linecross"})
    ctl.ensure_placed()

    where = ctl.placement("7-linecross")
    assert where is not None and ctl.server_of(where.worker) == "srv-2"
    assert "beside r-2" in where.reason                          # and it says so, in the operator's words


def test_the_short_form_would_follow_nothing_here():
    """The same cluster, the same recorder, the only difference being `by`. The bare
    form looks for a recorder reporting `7-linecross` and no recorder ever will."""
    box = Box()
    short = SubsystemSpec.from_dict({**{"name": "det2", "unit": {"rows": "units", "id": "name", "fields": {
        "name": {"type": "string", "required": True}, "cam": {"type": "string", "required": True},
        "kind": {"type": "string", "required": True}}},
        "placement": {"capacity": {"from": "capacity", "fallback": 8}, "near": "rec"}}})
    assert short.near == "rec" and short.near_by == "id"

    ctl = SpecController(short, box.vars, box.objects, wall=box.wall)
    _worker(box, short, "d-1", "srv-1")
    _recorder_holding(box, "r-2", "srv-2", "7")
    ctl.create({"name": "7-linecross", "cam": "7", "kind": "linecross"})
    assert ctl.holder_near("7-linecross") is None                # the recorder is right there, and is not found

    full = SubsystemSpec.from_dict({**{"name": "det3", "unit": short_unit()},
                                    "placement": {"capacity": {"from": "capacity", "fallback": 8},
                                                  "near": {"sub": "rec", "by": "cam"}}})
    ctl2 = SpecController(full, box.vars, box.objects, wall=box.wall)
    ctl2.create({"name": "7-linecross", "cam": "7", "kind": "linecross"})
    assert ctl2.holder_near("7-linecross") == ("r-2", "srv-2")   # same cluster, one word of YAML


def short_unit():
    return {"rows": "units", "id": "name", "fields": {
        "name": {"type": "string", "required": True}, "cam": {"type": "string", "required": True},
        "kind": {"type": "string", "required": True}}}


def test_no_recorder_for_this_camera_is_a_fallback_not_a_refusal():
    """A camera nobody records still gets a detector: the affinity is a preference,
    and a fan-out is readable over RTSP from any server."""
    box = Box()
    ctl = SpecController(DET_SPEC, box.vars, box.objects, wall=box.wall)
    _worker(box, DET_SPEC, "d-1", "srv-1")
    ctl.create({"name": "9-motion", "cam": "9", "kind": "motion"})       # nothing records camera 9
    ctl.ensure_placed()
    assert ctl.placement("9-motion") is not None and ctl.placement("9-motion").worker == "d-1"


def test_the_scan_follows_the_recording_it_reads_not_its_own_name():
    box = Box()
    ctl = SpecController(DETJOB_SPEC, box.vars, box.objects, wall=box.wall)
    _worker(box, DETJOB_SPEC, "j-1", "srv-1", capacity=2)
    _worker(box, DETJOB_SPEC, "j-2", "srv-2", capacity=2)
    _recorder_holding(box, "r-2", "srv-2", "7")

    ctl.create({"name": "7-lpr-1", "cam": "7", "rec": "7", "kind": "lpr", "from": 100.0, "to": 200.0})
    ctl.ensure_placed()
    assert ctl.server_of(ctl.placement("7-lpr-1").worker) == "srv-2"     # where the footage is


def test_neither_detectors_nor_scans_have_a_home():
    """A recording has one because an operator can answer "which disk"; these have
    nobody to answer "which server", and a preference nobody sets is a field nobody
    maintains."""
    assert REC_SPEC.home == "home"
    assert DET_SPEC.home == "" and DETJOB_SPEC.home == ""
    assert "home" not in DETJOB_SPEC.fields and "home" not in DET_SPEC.fields


def test_a_near_by_naming_no_field_is_refused_at_load():
    for bad in ({"sub": "rec", "by": "recording"}, {"by": "cam"}):
        try:
            SubsystemSpec.from_dict({"name": "x", "unit": short_unit(), "placement": {"near": bad}})
            raise AssertionError(f"accepted near: {bad}")
        except ValueError as e:
            assert "near" in str(e)


def test_the_affinity_survives_the_name_the_operator_chose():
    """The reason `of` exists. Camera 7's only recording is called `7-cloud` — the operator
    picked the network archive and the page named the row after it. Nothing in det's row, and
    nothing in det's controller, can know that string; the recorder's `cam` says it instead."""
    box = Box()
    ctl = SpecController(DET_SPEC, box.vars, box.objects, wall=box.wall)
    _worker(box, DET_SPEC, "d-1", "srv-1")
    _worker(box, DET_SPEC, "d-2", "srv-2")
    _recorder_holding(box, "r-2", "srv-2", "7-cloud:7")          # the recording of camera 7, by another name

    ctl.create({"name": "7-linecross", "cam": "7", "kind": "linecross"})
    ctl.ensure_placed()
    assert ctl.server_of(ctl.placement("7-linecross").worker) == "srv-2"

    # and matching their id, as the spec did while `id: cam` held, would find nothing at all
    short = SubsystemSpec.from_dict({"name": "det4", "unit": short_unit(),
                                     "placement": {"capacity": {"from": "capacity", "fallback": 8},
                                                   "near": {"sub": "rec", "by": "cam"}}})
    other = SpecController(short, box.vars, box.objects, wall=box.wall)
    other.create({"name": "7-linecross", "cam": "7", "kind": "linecross"})
    assert other.holder_near("7-linecross") is None


def test_two_recordings_of_one_camera_settle_the_tie_the_same_way_twice():
    """Two archives, two recordings, two recorders: the affinity has to pick one, and pick the
    same one next pass, or the follower walks between them forever. The smallest id wins."""
    box = Box()
    ctl = SpecController(DET_SPEC, box.vars, box.objects, wall=box.wall)
    _worker(box, DET_SPEC, "d-1", "srv-1")
    _worker(box, DET_SPEC, "d-2", "srv-2")
    _recorder_holding(box, "r-1", "srv-1", "7-cloud:7")
    _recorder_holding(box, "r-2", "srv-2", "7:7")

    ctl.create({"name": "7-linecross", "cam": "7", "kind": "linecross"})
    assert ctl.holder_near("7-linecross") == ("r-2", "srv-2")     # "7" sorts before "7-cloud"
    assert ctl.holder_near("7-linecross") == ("r-2", "srv-2")
