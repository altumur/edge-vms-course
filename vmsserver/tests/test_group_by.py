"""One connection per device, enforced where the decision is made.

A recorder with sixteen channels is sixteen rows. Every other rule in the
catalogue spreads them — most free capacity, labels, the policy — and each move
away from the first one opens another session to a box that licenses two. The
failure then arrives in the device's own words ("too many sessions", a channel
that will not start, a stream that drops when the fifth is added), an hour after
the placement that caused it and on another machine.

`group_by: device` is the mirror of `spread_by`: units sharing a value go on the
SAME worker, as a filter. These tests are about that, and about the three places
a naive version of it goes wrong — a dead holder, a group that outgrows its
worker, and a spec that asks for both at once.
"""
import json

from w2cplatform.spec import SpecController, SubsystemSpec
from w2cplatform.contract import Heartbeat
from vms.config import SPEC
from vms.controller import VmsController
from tests.conftest import Box


def _ctl(box):
    """The pair every test here needs: the controller places, the console creates —
    two tokens, as on a real box."""
    ctl = VmsController(box.vars.as_writer("vmscontroller", SPEC.acl_controller()), box.objects, wall=box.wall)
    con = VmsController(box.vars.as_writer("console", SPEC.acl_console()), box.objects, wall=box.wall)
    return ctl, con


def _worker(box, name, server, capacity=50, labels=""):
    box.objects.put(SPEC.sub.heartbeat_key(name),
                    Heartbeat(name, box.wall(), [], {"server": server, "capacity": capacity,
                                                     "headroom": capacity, "labels": labels}).to_bytes())
    box.objects.put(f"platform/resources/{server}/heartbeat",
                    json.dumps({"server": server, "ts": box.wall(), "url": f"http://{server}", "units": {}}).encode())


def _cam(con, name, source):
    return con.create_camera({"name": name, "source": source})


NVR = "driverpack://acme/10.0.0.50/ch/"          # one box, many channels
CAM = "driverpack://acme/10.0.0.77/ch/1"         # a camera of its own


def test_channels_of_one_device_go_to_one_worker():
    """The point. Two workers, both empty, both eligible: capacity alone would
    put one channel on each, and that is two sessions to one recorder."""
    box = Box(); ctl, con = _ctl(box)
    _worker(box, "w-1", "srv-a"); _worker(box, "w-2", "srv-b")

    a = _cam(con, "ch1", NVR + "1")["id"]
    b = _cam(con, "ch2", NVR + "2")["id"]
    other = _cam(con, "gate", CAM)["id"]
    ctl.ensure_placed()

    assert ctl.where(a) == ctl.where(b), "two channels of one recorder, two sessions"
    assert ctl.group_value(ctl.camera(a)) == "acme/10.0.0.50" == ctl.group_value(ctl.camera(b))
    assert ctl.where(other) != ctl.where(a)          # a different device is free to balance away

    # and a channel added later joins them rather than the emptier worker
    c = _cam(con, "ch3", NVR + "3")["id"]
    ctl.ensure_placed()
    assert ctl.where(c) == ctl.where(a)


def test_a_row_with_no_source_groups_with_nothing():
    """An empty group is no group. A row half-created — or a unit of a kind that
    has no device at all — must not be dragged towards somebody else's box."""
    box = Box(); ctl, con = _ctl(box)
    assert ctl.group_value({"id": 1, "source": ""}) == ""
    assert ctl.group_value({"id": 1}) == ""
    _worker(box, "w-1", "srv-a")
    assert ctl.eligible({"id": 1, "labels": []}, ["w-1"]) == ["w-1"]


def test_a_dead_holder_does_not_pin_the_group_to_itself():
    """The mistake that turns a safety rule into an outage. The worker holding
    the group goes silent; its units have to move. Reading its placement as "the
    group lives there" would make every one of them unplaceable at exactly the
    moment they need somewhere to go."""
    box = Box(); ctl, con = _ctl(box)
    _worker(box, "w-1", "srv-a"); _worker(box, "w-2", "srv-b")
    a = _cam(con, "ch1", NVR + "1")["id"]
    b = _cam(con, "ch2", NVR + "2")["id"]
    ctl.ensure_placed()
    home = ctl.where(a)
    assert ctl.where(b) == home

    box.wall.advance(120)                                  # that worker stops heartbeating
    survivor = "w-2" if home == "w-1" else "w-1"
    _worker(box, survivor, "srv-b" if survivor == "w-2" else "srv-a")

    pool = ctl._pool(None)
    assert pool == [survivor]                              # the dead one is out of the pool
    assert ctl.eligible(ctl.camera(a), pool) == [survivor]  # …so the group is free to re-form there
    assert ctl.eligible(ctl.camera(b), pool) == [survivor]
    assert ctl.unplaceable() == []                         # and nothing reads as "nowhere to go"


def test_a_group_that_outgrows_its_worker_is_unplaceable_and_says_which_device():
    """The cost of the rule, stated rather than hidden. The worker holding a
    device is not chosen for its room, so a group can outgrow it — and then the
    honest answer is "nowhere", because the alternative is the second session
    this whole thing exists to prevent. The operator raises capacity or moves
    the group; `/unplaceable` is where they find out."""
    box = Box(); ctl, con = _ctl(box)
    _worker(box, "w-1", "srv-a", capacity=2); _worker(box, "w-2", "srv-b", capacity=50)

    a = _cam(con, "ch1", NVR + "1")["id"]
    b = _cam(con, "ch2", NVR + "2")["id"]
    ctl.ensure_placed()
    ctl.move(a, "w-1", "the operator put this recorder here"); ctl.move(b, "w-1", "…and its second channel")
    assert ctl.where(a) == ctl.where(b) == "w-1"           # two of two: the small worker is full

    c = _cam(con, "ch3", NVR + "3")["id"]
    ctl.ensure_placed()
    assert ctl.placement(c) is None                        # w-2 has room, and using it would be a second session

    # And the state it is in is "waiting", not "unplaceable" — `unplaceable` means nobody may EVER take
    # it, and somebody may: the worker holding its device, once it has room. The difference matters to
    # the operator in one way, and it is worth knowing: this one will not be helped by another worker.
    assert ctl.eligible(ctl.camera(c), ctl._pool(None)) == ["w-1"]
    assert ctl.unplaceable() == []

    # …and the way out is room on the worker that holds the device, not another worker
    _worker(box, "w-1", "srv-a", capacity=3)
    ctl.ensure_placed()
    assert ctl.where(c) == "w-1"


def test_a_spec_that_asks_for_both_on_one_field_is_refused_at_load():
    """`group_by: cam` with `spread_by: cam` says "one worker" and "different
    servers" about the same pair of units. A spec that contradicts itself must
    not load — the alternative is a placement pass whose answer depends on the
    order two filters happen to run in."""
    d = {"name": "x", "unit": {"rows": "units", "id": "name",
                               "fields": {"name": {"type": "string", "required": True},
                                          "cam": {"type": "string"}}},
         "placement": {"capacity": {"from": "capacity", "fallback": 8},
                       "spread_by": "cam", "group_by": "cam"}}
    try:
        SubsystemSpec.from_dict(d)
        raise AssertionError("a spec that says both was accepted")
    except ValueError as e:
        assert "group_by" in str(e) and "spread_by" in str(e)

    d["placement"]["group_by"] = "kind"                    # different fields: not a contradiction
    assert SubsystemSpec.from_dict({**d, "unit": {**d["unit"], "fields": {**d["unit"]["fields"],
                                                                         "kind": {"type": "string"}}}}).group_by == "kind"


def test_a_device_with_no_picture_is_a_unit_like_any_other():
    """The row that made `kind` necessary: a door controller. It is an IP device
    integrated the same way, so it is held (one connection), placed, given an
    epoch and it writes events — everything a camera gets except the picture.

    And the whole of its special treatment is an ABSENCE: no fan-out published.
    Nothing else needs a branch, because everything else already asks the
    heartbeat rather than the row — the recorder looks for a holder with a
    `live_url` and does not find one."""
    from w2cplatform.console import holder_of
    from vms.worker import FakeActuator, VmsWorker
    from vms.config import SPEC as VMS_SPEC

    box = Box(); ctl, con = _ctl(box)
    cam = con.create_camera({"name": "lobby", "source": CAM})["id"]
    door = con.create_camera({"name": "front door", "source": "driverpack://acme/10.0.0.90/ch/1",
                              "kind": "io"})["id"]
    assert con.camera(cam)["kind"] == "video"              # the default keeps every older row meaning what it meant

    w = VmsWorker("w-1", box.vars, box.objects, FakeActuator(), clock=box.clock, wall=box.wall,
                  server="srv-a", env={}, archive_root=box.archive)
    _worker(box, "w-1", "srv-a")
    ctl.ensure_placed()
    w.reconcile_once(); w.heartbeat_once()

    st = {s["id"]: s for s in w.status()}
    assert st[cam]["live_url"] and "live_url" not in st[door]      # the absence, and it is the only difference
    assert st[door]["kind"] == "io" and st[door]["phase"] == "running"
    assert w.epochs[str(door)] >= 1                                 # held, fenced, its own epoch

    # a subscriber looking for something to watch finds the camera and not the door — no new check anywhere
    assert holder_of(box.objects, "vms/", cam, box.wall(), phase="running", field="live_url") is not None
    assert holder_of(box.objects, "vms/", door, box.wall(), phase="running", field="live_url") is None

    # …and it observes: an input change is an event in its own bucket, like any other event
    assert w.observe(door, "io.input", port=1, value="closed") is not None
