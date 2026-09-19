"""Two jobs in one loop, and what it costs to report them as one.

The controller's pass does two different things: it PLACES units, and it
PUBLISHES a copy of where they went for the layer above. They fail
independently and mean different things to whoever is woken up — and until
now they shared one `try` and one sentence in the log.

The sentence was "placement pass failed", and `publish_snapshot()` is the last
call in the block, so the message named the one thing that had *not* failed.
The other direction was worse: a placement that threw skipped the publish, the
layer above quietly went stale, and the word "snapshot" appeared nowhere.
"""
import logging

from vms.config import SPEC
from vms.controller import VmsController
from vms.__main__ import _controller_loop, stop
from w2cplatform.console import SpecConsole
from w2cplatform.objects import FsObjectStore
from tests.conftest import Box


class _Recorder(logging.Handler):
    def __init__(self):
        super().__init__(); self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _one_pass(ctl) -> list[str]:
    """One turn of the REAL loop — the same function `__main__.controller` runs.

    Driven by the loop's own `stop.wait(5)` at the end of a pass: it sets the flag,
    so the pass runs once and the `while` exits. The point of going through
    `_controller_loop` rather than calling the four methods by hand is that the
    thing under test IS the try blocks."""
    rec = _Recorder()
    logging.getLogger().addHandler(rec)
    real_wait = stop.wait

    def wait_once(timeout=None):
        stop.set()
        return True

    stop.wait = wait_once
    try:
        _controller_loop(ctl)
    finally:
        stop.wait = real_wait
        logging.getLogger().removeHandler(rec)
        stop.clear()
    return rec.lines


def _cluster(box, cameras=2, objects=None):
    ctl = VmsController(box.vars, objects or box.objects, capacity=50, wall=box.wall)
    for i in range(cameras):
        ctl.create_camera({"source": f"driverpack://file/{i}.mp4"})
    return ctl


def test_a_failed_publish_is_not_reported_as_a_failed_placement():
    """The bug this was written for. Placement worked; the log must not say it did not."""
    box = Box()
    tiny = FsObjectStore(box.root + "/tiny", max_bytes=64)          # the shard will not fit
    ctl = _cluster(box, cameras=2, objects=tiny)
    ctl.ensure_placed(workers=["w-1"])

    lines = _one_pass(ctl)
    assert any("publishing the snapshot failed" in ln for ln in lines), lines
    assert not any("placement pass failed" in ln for ln in lines), lines
    # …and the placement it did is still there: the pass did its first job
    assert ctl.where(1) == "w-1"


def test_a_failed_placement_still_publishes_what_is_known():
    """The other direction. Placement threw, so the two are now separate — and the
    copy the layer above reads still moves forward instead of silently freezing."""
    box = Box()
    ctl = _cluster(box, cameras=1)

    def boom(*a, **kw):
        raise RuntimeError("the store went away mid-pass")

    ctl.ensure_placed = boom
    lines = _one_pass(ctl)
    assert any("placement pass failed" in ln for ln in lines), lines
    assert not any("publishing the snapshot failed" in ln for ln in lines), lines
    assert box.objects.list("vms/snapshot/"), "the publish was skipped because placement failed"


def test_the_age_of_the_published_copy_is_on_metrics():
    """A log line is on a host nobody is looking at. This is the number that climbs.

    Read from the STORE and not from the process: the controller has no port, is
    restarted freely, and two may be running — so the question is answered by
    whoever can read the objects, which is what lets a console put it on /metrics."""
    box = Box()
    ctl = _cluster(box, cameras=1)
    con = SpecConsole(ctl)

    # never published is not the same as published a moment ago, and the gauge says so
    assert ctl.snapshot_age() is None
    assert "vms_snapshot_age_seconds -1" in con.metrics_text()

    ctl.ensure_placed(workers=["w-1"])
    ctl.publish_snapshot()
    assert "vms_snapshot_age_seconds 0" in con.metrics_text()

    box.wall.advance(90)                                            # the controller stopped publishing
    assert round(ctl.snapshot_age()) == 90
    assert "vms_snapshot_age_seconds 90" in con.metrics_text()


def test_the_age_is_the_stalest_shard_and_never_the_freshest():
    """A number like this must only ever be wrong in the pessimistic direction.
    One shard that stopped being rewritten IS the cluster being behind, and taking
    the newest would report an RPO better than the real one."""
    import json
    box = Box()
    ctl = _cluster(box, cameras=4)
    ctl.ensure_placed(workers=["w-1", "w-2"])
    ctl.publish_snapshot()

    stale = json.loads(box.objects.get("vms/snapshot/w-1"))
    stale["ts"] = box.wall() - 300                                  # w-1's shard stopped moving
    box.objects.put("vms/snapshot/w-1", json.dumps(stale).encode())

    assert round(ctl.snapshot_age()) == 300, "the freshest shard was taken, and the RPO looked better than it is"
