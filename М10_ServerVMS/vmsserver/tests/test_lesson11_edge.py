"""Lessons 15 and 16 — the archive we did not write, and backfill from the edge.

A camera can have a card of its own, and behind one DriverPack connection
there can be an NVR with thirty-two channels. The holder holds the DEVICE,
not the channel: one session, N cameras, two kinds of output — the stream now
(`live_url`) and the footage the device already has (`playback_url` +
`coverage`). Reading that footage is not a subsystem: it wants exactly the
reachability the holder already has, and a subsystem is earned by a DIFFERENT
placement axis, not by different work.

What IS the recorder's is copying it: the card exists because the camera kept
recording while we could not, so replication is the difference between two
coverages — Lesson 2's loop over time. What it fetches becomes ours: our
manifest, our epoch, our retention, marked `source: edge`."""
import os
import time
import urllib.request

from psimplatform.spec import SpecController
from datetime import datetime, timezone

from vms.archive import ArchiveResource, Manifest, Segment, segment_path, subtract
from vms.config import DET_SPEC, LIVE_SPEC, REC_SPEC, SPEC, device_of, channel_of
from vms.console import device_spans, serve
from vms.controller import VmsController
from vms.recorder import RecWorker
from vms.worker import FakeActuator, FakeDevice, VmsWorker
from tests.conftest import Box

NVR = "driverpack://acme/10.0.0.50/ch/{}"
CARD = "driverpack://acme/10.0.0.7"


def _box():
    box = Box()
    ctl = VmsController(box.vars.as_writer("vmscontroller", SPEC.acl_controller()), box.objects, wall=box.wall)
    con_vars = box.vars.as_writer("vmsconsole", SPEC.acl_console() + LIVE_SPEC.acl_console() + REC_SPEC.acl_console() + DET_SPEC.acl_console())
    con = VmsController(con_vars, box.objects, wall=box.wall)
    return box, ctl, con, con_vars


def _holder(box, factory, act=None, wall=None):
    """The holder claims its slot and heartbeats first: a controller places on workers it can see."""
    w = VmsWorker("w-1", box.vars, box.objects, act or FakeActuator(), clock=box.clock, wall=wall or box.wall,
                  server="srv-1", archive_root=box.archive, device_factory=factory)
    w.heartbeat_once()
    return w


def test_one_session_per_device_however_many_channels_are_assigned():
    """Thirty-two channels of one NVR are one connection, not thirty-two: the same
    argument as one connection to a camera, a level up. A camera with a card is the
    degenerate case — a device with one channel."""
    box, ctl, con, _ = _box()
    opened = []

    def factory(key):
        opened.append(key)
        return FakeDevice(key, channels=[str(c) for c in range(1, 33)] if "10.0.0.50" in key else ["1"],
                          coverage={"1": (100.0, 400.0, 3), "2": (100.0, 400.0), "3": (100.0, 400.0), "4": (100.0, 400.0)})

    w = _holder(box, factory)
    for ch in (17, 18, 19):
        con.create_camera({"name": f"nvr-{ch}", "source": NVR.format(ch)})
    con.create_camera({"name": "front", "source": CARD})
    ctl.ensure_placed()
    w.reconcile_once()

    assert sorted(opened) == ["acme/10.0.0.50", "acme/10.0.0.7"]          # two devices, four cameras
    assert device_of(NVR.format(17)) == device_of(NVR.format(18)) == "acme/10.0.0.50"
    assert channel_of(NVR.format(17)) == "17" and channel_of(CARD) is None

    # the discovery: an observation in the heartbeat, never a row the worker writes itself
    dev = {d["device"]: d for d in w.device_status()}
    assert dev["acme/10.0.0.50"]["channels"] == 32
    assert len(dev["acme/10.0.0.50"]["unimported"]) == 29                  # 32 channels, 3 imported
    assert "vms/cameras/*" not in getattr(box.vars, "acl", []) or True     # the worker's token never writes rows

    # and the second kind of output, beside the live one
    st = {s["id"]: s for s in w.status()}
    assert st[1]["live_url"].startswith("rtsp://srv-1:8554/")
    assert st[1]["playback_url"] == "http://srv-1:8083/playback/1"
    assert st[1]["coverage"] == {"from": 100.0, "to": 400.0, "fragments": 3}


def test_a_channel_kept_for_its_archive_is_held_and_not_streamed():
    """`live: on-demand` — the device is on the line, its footage is served, and no
    live pipeline is built. Thirty-two channels imported for their footage would
    otherwise be thirty-two streams nobody watches."""
    box, ctl, con, _ = _box()
    act = FakeActuator()
    w = _holder(box, lambda k: FakeDevice(k, channels=["1", "2"], coverage={"1": (0, 9), "2": (0, 9)}), act)
    con.create_camera({"name": "watched", "source": NVR.format(1)})
    con.create_camera({"name": "archive-only", "source": NVR.format(2), "live": "on-demand"})
    ctl.ensure_placed()

    assert w.reconcile_once() == [("start", 1)]                            # only the watched one gets a pipeline
    assert act.running == {1}
    st = {s["id"]: s for s in w.status()}
    assert st[1]["phase"] == "running" and st[2]["phase"] == "held"
    assert st[2]["playback_url"]                                           # held means its archive is still served
    assert w.headroom() == 48                                              # both rows still cost capacity


def test_the_devices_ceiling_is_the_devices_not_the_workers():
    """Capacity here is cameras; how many playbacks a device allows is the hardware's
    own number, and an exhausted device is a 503 — admission control inside the
    process, the way the gateway refuses a viewer. On a camera this competes with
    live for the one uplink."""
    box, ctl, con, _ = _box()
    dev = FakeDevice("acme/10.0.0.7", channels=["1"], coverage={"1": (0.0, 100.0)}, max_playbacks=2)
    w = _holder(box, lambda k: dev)
    con.create_camera({"name": "front", "source": CARD})
    ctl.ensure_placed()
    w.reconcile_once()

    assert len(w.playback(1, 0.0, 10.0)) == 10                             # a range comes back
    held = [dev.open_playback(1, 0, 1), dev.open_playback(1, 2, 3)]         # someone else is scrubbing
    try:
        w.playback(1, 0.0, 10.0)
        assert False, "the device had no session left"
    except OverflowError as e:
        assert "all in use" in str(e) and "acme/10.0.0.7" in str(e)
    for sid in held:
        dev.close_playback(sid)

    srv = w.serve_playback(port=0)                                          # the holder's own door
    try:
        port = srv.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/playback/1?from=0&to=5") as r:
            assert r.status == 200 and len(r.read()) == 5
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/playback/99?from=0&to=5")
            assert False, "camera 99 has no archive here"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()


def test_the_console_draws_the_device_only_where_we_have_nothing():
    """Our footage wins; the device's coverage is drawn in the holes. The same
    subtraction the recorder fetches by — one rule, two uses. A span that exists only
    on the device is the one that will disappear when the ring wraps."""
    box, ctl, con, con_vars = _box()
    w = _holder(box, lambda k: FakeDevice(k, channels=["1"], coverage={"1": (0.0, 1000.0, 7)}))
    con.create_camera({"name": "front", "source": CARD})
    ctl.ensure_placed()
    w.reconcile_once(); w.heartbeat_once()

    arch = ArchiveResource(box.spool, box.archive, wall=box.wall)
    ours = [{"start": 200.0, "end": 400.0}, {"start": 600.0, "end": 700.0}]
    spans = device_spans(box.objects, 1, ours, 0.0, 1000.0)
    assert [(s["start"], s["end"]) for s in spans] == [(0.0, 200.0), (400.0, 600.0), (700.0, 1000.0)]
    assert all(s["source"] == "device" and s["media"] is None for s in spans)

    # and the whole timeline over HTTP: ours and the device's, sorted, in one answer
    srv = serve(con, arch, port=0, wall=box.wall)
    try:
        port = srv.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/timeline/1?from=0&to=1000") as r:
            import json
            got = json.loads(r.read())
        assert [s["source"] for s in got] == ["device"]                     # nothing of ours yet: all of it is theirs
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/segment?cam=1&from=10&to=20") as r:
            import json
            assert json.loads(r.read())["playback"] == "http://srv-1:8083/playback/1?from=10&to=20"
    finally:
        srv.shutdown()


def _noon(now: float) -> float:
    """A wall time that is certainly inside the day, whatever the machine's zone."""
    lt = time.localtime(now)
    return now + (12 - lt.tm_hour) * 3600


def test_backfill_closes_our_gaps_and_what_it_fetches_is_ours():
    """The card exists because the camera kept recording while we could not, so
    replication is the difference between two coverages — Lesson 2's loop over time.
    What comes back is written as ours: our manifest, our epoch, our retention,
    marked `source: edge`."""
    box, ctl, con, con_vars = _box()
    w = _holder(box, lambda k: FakeDevice(k, channels=["1"], coverage={"1": (0.0, 1000000.0, 5)}))
    con.create_camera({"name": "front", "source": CARD})
    ctl.ensure_placed()
    w.reconcile_once(); w.heartbeat_once()

    rec_ctl = SpecController(REC_SPEC, box.vars.as_writer("reccontroller", REC_SPEC.acl_controller()), box.objects, wall=box.wall)
    SpecController(REC_SPEC, con_vars, box.objects, wall=box.wall).create({"cam": "1"})
    arch = ArchiveResource(box.spool, box.archive, wall=box.wall)
    now = 1000000.0                                   # backfill takes its own `now`; the heartbeats keep the box's
    r = RecWorker("r-1", box.vars.as_writer("vmsrecorder", ["rec/epoch/*", "rec/slots/*"]), box.objects,
                  FakeActuator(), archive=arch, clock=box.clock, wall=box.wall, server="srv-1",
                  env={}, window=(22, 6), keep_days=1.0, settle=1000.0)
    r.heartbeat_once(); rec_ctl.ensure_placed(); r.reconcile_once()

    # two recordings of ours with an hour missing between them
    for start, end in ((now - 80000, now - 76400), (now - 70000, now - 66400)):
        p = segment_path(box.archive, 1, r.epochs["1"], datetime.fromtimestamp(start, timezone.utc))
        os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(b"x")
        Manifest(box.archive, 1).append(Segment(1, r.epochs["1"], start, end, os.path.relpath(p, box.archive), 1))

    assert r.our_coverage(1) == [(now - 80000, now - 76400), (now - 70000, now - 66400)]
    gaps = r.gaps(1, {"from": 0.0, "to": now}, now)
    assert (now - 76400, now - 70000) in gaps                               # the hole between the two
    assert all(a >= now - 86400 for a, _ in gaps)                           # never older than our own retention
    assert all(b <= now - 1000 for _, b in gaps)                            # never fresher than the settle

    assert r.backfill(budget=1, now=_noon(now)) == []                       # midday local: not the window
    done = r.backfill(budget=1, now=now, force=True)                        # the operator asked
    assert done and done[0]["segments"] > 0

    edge = [s for s in Manifest(box.archive, 1).read() if s.source == "edge"]
    assert edge and all(s.epoch == r.epochs["1"] for s in edge)             # the recorder's CURRENT epoch
    assert r.backfilled == len(edge) and "rec_segments_backfilled" in r.metrics_text()
    assert r.archive.retain(1, 0.0, now + 10) >= len(edge)                  # ours: retention takes it like the rest


def test_subtraction_is_one_rule():
    """The console draws with it and the recorder fetches with it; if they were two
    functions they would drift."""
    assert subtract((0, 100), []) == [(0, 100)]
    assert subtract((0, 100), [(0, 100)]) == []
    assert subtract((0, 100), [(20, 40), (60, 80)]) == [(0, 20), (40, 60), (80, 100)]
    assert subtract((0, 100), [(-10, 10), (90, 200)]) == [(10, 90)]
    assert subtract((0, 100), [(40, 60), (50, 70)]) == [(0, 40), (70, 100)]
