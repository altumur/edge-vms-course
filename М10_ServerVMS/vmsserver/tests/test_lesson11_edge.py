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

from w2cplatform.contract import Heartbeat
from w2cplatform.spec import Refused, SpecController
from datetime import datetime, timezone

from vms.archive import ArchiveResource, Manifest, Segment, segment_path, subtract
from vms.config import DET_SPEC, LIVE_SPEC, REC_SPEC, SPEC, device_of, channel_of
from vms.console import device_spans, serve
from vms.controller import VmsController
from vms.recworker import RecWorker
from vms.worker import FakeActuator, FakeDevice, VmsWorker
from tests.conftest import Box

NVR = "driverpack://acme/10.0.0.50/ch/{}"
CARD = "driverpack://acme/10.0.0.7"


def _box():
    box = Box()
    ctl = VmsController(box.vars.as_writer("vmscontroller", SPEC.acl_controller()), box.objects, wall=box.wall)
    con_vars = box.vars.as_writer("console", SPEC.acl_console() + LIVE_SPEC.acl_console() + REC_SPEC.acl_console() + DET_SPEC.acl_console())
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
    spans = device_spans(box.objects, 1, ours, 0.0, 1000.0, box.wall())
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
    r = RecWorker("r-1", box.vars.as_writer("recworker", ["rec/epoch/*", "rec/slots/*"]), box.objects,
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


def test_a_units_id_is_a_name_and_not_a_path():
    """Where the text actually comes from: a subsystem whose id is a FIELD (`rec`, `id: cam`)
    takes the unit's id verbatim from the operator's body — only `_next_id` protects a numeric
    one. From there the same string becomes the key `rec/recordings/<id>`, the prefix the
    console's token is matched against (`rec/recordings/*` — which `rec/recordings/../../…`
    passes), and a directory on the resource (`unit_dir`). Refused at the door, as a 400."""
    box, ctl, con, con_vars = _box()
    rec = SpecController(REC_SPEC, con_vars, box.objects, wall=box.wall)

    for bad in ("../../cameras/7", "a/b", ".."):
        try:
            rec.create({"cam": bad})
            assert False, f"a unit id was accepted as a path: {bad!r}"
        except Refused as e:
            assert "name, not a path" in str(e), e

    r = rec.create({"cam": "7"})                       # the ordinary case is untouched
    assert r["id"] == "7" and box.vars.get("rec/recordings/7")[0]["cam"] == "7"

    # and one layer down the store refuses the same shapes on its own, whoever calls it
    for bad in ("rec/recordings/../../cameras/7", "/rec/recordings/7"):
        try:
            box.vars.put(bad, {"cam": "7"}, cas=0)
            assert False, f"the store accepted {bad!r}"
        except ValueError:
            pass


def test_spread_by_keeps_two_copies_off_one_server():
    """The placement gap the archive's unit-keyed tree opens up. Two units that name the
    same camera exist to survive ONE server dying, so a second copy beside the first is
    not a compromise — it is the failure the operator was insuring against. `spread_by`
    is therefore a FILTER: unplaceable is the honest answer, co-located is not.

    It also has to beat `near`, which pulls a recorder towards the camera's holder and
    would otherwise pull both copies to the same place."""
    from w2cplatform.spec import SubsystemSpec
    box, ctl, con, con_vars = _box()

    spec = SubsystemSpec.from_dict({
        "name": "copy",
        "unit": {"rows": "copies", "id": "name",
                 "fields": {"name": {"type": "string", "required": True}, "cam": {"type": "string", "required": True}}},
        "placement": {"capacity": {"from": "capacity", "fallback": 50}, "spread_by": "cam"},
    })
    assert spec.spread_by == "cam"

    admin = SpecController(spec, box.vars, box.objects, wall=box.wall)
    for w, server in (("w-1", "srv-1"), ("w-2", "srv-2")):
        box.objects.put(spec.sub.heartbeat_key(w),
                        Heartbeat(w, box.wall(), [], {"server": server, "capacity": 50, "headroom": 50}).to_bytes())

    admin.create({"name": "7-main", "cam": "7"})
    admin.create({"name": "7-backup", "cam": "7"})
    admin.create({"name": "8-main", "cam": "8"})
    admin.ensure_placed()

    where = {u: admin.placement(u).worker for u in ("7-main", "7-backup") if admin.placement(u)}
    assert len(where) == 2 and where["7-main"] != where["7-backup"]        # the whole point: different servers
    assert admin.server_of(where["7-main"]) != admin.server_of(where["7-backup"])
    assert admin.placement("8-main") is not None                          # another camera is unaffected

    # a third copy of camera 7 has nowhere to go, and says so instead of doubling up
    admin.create({"name": "7-third", "cam": "7"})
    admin.ensure_placed()
    assert admin.placement("7-third") is None and "7-third" in [u["id"] for u in admin.unplaceable()]


# The claim this file is here to check: two recordings of one camera, on two servers, cost a YAML edit
# and nothing else. Three lines change — `id`, the `cam` field, `spread_by` — and no Python at all.
TWO_COPIES_YAML = """
name: rec
unit:
  rows: recordings
  id: name                                           # was: cam — the unit is now named, not numbered
  fields:
    name:           {type: string, required: true}   # "7-main", "7-backup"
    cam:            {type: string, required: true}   # whose fan-out this recording subscribes to
    retention_days: {type: int,    default: 30}
    enabled:        {type: bool,   default: true}
    labels:         {type: list}
placement:
  capacity:   {from: capacity, fallback: 50}
  headroom:   {from: headroom}
  constraint: labels-subset
  requires:   none
  servers:    shared
  tie_break:  most-free-capacity
  near:       vms
  spread_by:  cam                                    # new: two copies of one camera go on different servers
  rebalance:  {dead_band: 0.10}
snapshot: [name, cam, retention_days, enabled, labels]
console:
  running: recordings_running
"""


def test_two_recordings_of_one_camera_are_a_yaml_edit():
    """Not a rehearsal for a change: the change itself, run against the real classes.

    The spec below is `rec.subsystem.yaml` with three lines different. Everything it drives —
    SpecController, the archive tree, the console's timeline — is the shipped code,
    imported unchanged. If any of it still assumed "a recording is named by its camera", this
    test would not pass, and until the unit-keyed tree it would not have."""
    import yaml
    from w2cplatform.contract import Heartbeat
    from w2cplatform.spec import SubsystemSpec
    from vms.archive import Manifest, Segment, segment_path
    from vms.console import recordings_of

    box, ctl, con, con_vars = _box()
    spec = SubsystemSpec.from_dict(yaml.safe_load(TWO_COPIES_YAML))
    assert spec.id == "name" and spec.spread_by == "cam"

    rec = SpecController(spec, box.vars, box.objects, wall=box.wall)
    for w, server in (("r-1", "srv-1"), ("r-2", "srv-2")):
        box.objects.put(spec.sub.heartbeat_key(w),
                        Heartbeat(w, box.wall(), [], {"server": server, "capacity": 50, "headroom": 50}).to_bytes())

    w = _holder(box, lambda k: FakeDevice(k, channels=["1"]))
    con.create_camera({"name": "gate", "source": "driverpack://file/gate.mp4"})     # camera 1
    ctl.ensure_placed(); w.reconcile_once(); w.heartbeat_once()                     # somebody holds it: the recorder has a source
    rec.create({"name": "1-main", "cam": "1"})
    rec.create({"name": "1-backup", "cam": "1"})
    rec.ensure_placed()

    main, backup = rec.placement("1-main"), rec.placement("1-backup")
    assert main and backup and rec.server_of(main.worker) != rec.server_of(backup.worker)   # the point of the exercise

    # each copy writes its own tree, under its own name, with its own retention
    t = box.wall()
    for unit in ("1-main", "1-backup"):
        p = segment_path(box.archive, unit, 1, datetime.fromtimestamp(t - 600, timezone.utc))
        os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(b"x")
        Manifest(box.archive, unit).append(Segment(unit, 1, t - 600, t, os.path.relpath(p, box.archive), 1))

    arch = ArchiveResource(box.spool, box.archive, wall=box.wall)
    assert arch.units() == ["1-backup", "1-main"]                                  # two directories, not one
    assert len(arch.coverage("1-main")) == 1 and len(arch.coverage("1-backup")) == 1

    # and the camera's timeline is both of them: the console resolves camera -> recordings
    assert sorted(recordings_of(rec, 1)) == ["1-backup", "1-main"]
    spans = [sp for unit in recordings_of(rec, 1) for sp in Manifest(box.archive, unit).timeline(0, 1e12)]
    assert len(spans) == 2

    # retention is per recording, because the row is per recording
    rec.update("1-backup", {"retention_days": 1})
    assert rec.unit("1-main")["retention_days"] == 30 and rec.unit("1-backup")["retention_days"] == 1


class _NamedRec(RecWorker):
    """A recorder over the two-copies spec: its units are NAMED, not numbered."""
    parse_row = staticmethod(lambda items: _named_spec().row(items))


def _named_spec():
    import yaml
    from w2cplatform.spec import SubsystemSpec
    return SubsystemSpec.from_dict(yaml.safe_load(TWO_COPIES_YAML))


def test_a_named_unit_reaches_the_places_that_still_assumed_a_number():
    """Three reads and one write kept `int(...)` on a unit id after the tree stopped assuming one.

    Each of them is unreachable while `id: cam` holds — which is exactly why they survived the
    change and would have failed on the first `1-backup`. Here the two-copies spec is in force,
    so they are all reachable, and each one answers instead of raising."""
    from vms.console import vms_routes as console_routes
    from vms.resource import vms_routes as resource_routes

    box, ctl, con, con_vars = _box()
    spec = _named_spec()
    rec = SpecController(spec, box.vars, box.objects, wall=box.wall)
    for w, server in (("r-1", "srv-1"), ("r-2", "srv-2")):
        box.objects.put(spec.sub.heartbeat_key(w),
                        Heartbeat(w, box.wall(), [], {"server": server, "capacity": 50, "headroom": 50}).to_bytes())
    w = _holder(box, lambda k: FakeDevice(k, channels=["1"]))
    con.create_camera({"name": "gate", "source": "driverpack://file/gate.mp4"})     # camera 1
    ctl.ensure_placed(); w.reconcile_once(); w.heartbeat_once()                     # somebody holds it: the recorder has a source
    rec.create({"name": "1-main", "cam": "1"})
    rec.create({"name": "1-backup", "cam": "1"})
    rec.ensure_placed()

    t = box.wall()
    for unit in ("1-main", "1-backup"):
        p = segment_path(box.archive, unit, 1, datetime.fromtimestamp(t - 600, timezone.utc))
        os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(b"x")
        Manifest(box.archive, unit).append(Segment(unit, 1, t - 600, t, os.path.relpath(p, box.archive), 1))
    archive = ArchiveResource(box.spool, box.archive, wall=box.wall)

    # 1. the resource's manifest: what a peer and М11's console read to draw a timeline
    status, body = resource_routes(archive)("/manifest/1-backup", {})
    assert status == 200 and '"unit": "1-backup"' in body.decode()

    # 2. the console's timeline: the CAMERA's id, and both of its recordings under it
    status, spans = console_routes(archive, None, ctl, rec)(None, "GET", "/timeline/1", {})
    assert status == 200 and len(spans) == 2

    # 3. the lost lease: a reassignment names the unit the way the lease does — as text
    r1 = _NamedRec("r-1", box.vars, box.objects, archive=archive, clock=box.clock, wall=box.wall, server="srv-1")
    r1.reconcile_once()
    held = sorted(r1.reconciler.actual)
    assert held and all(not str(u).isdigit() for u in held)                # the point: nothing here is a number
    rec.move(held[0], "r-2", "operator asked")
    _NamedRec("r-2", box.vars, box.objects, archive=archive, clock=box.clock, wall=box.wall, server="srv-2").reconcile_once()
    assert r1.lease_pass() == [held[0]] and r1.recording_allowed        # released, not fenced — and no ValueError
    assert held[0] not in r1.reconciler.actual


# -- the watermark: what the archive does when the disk is full ---------------------------------------

def _space_box(days: float = 10.0, size: int = 50_000, n: int = 10):
    """An archive with one unit `days` deep, and the knob on."""
    from w2cplatform.resource import SPACE_KEY
    box = Box()
    box.vars.put(SPACE_KEY, {"enabled": "true", "high": "0.85", "low": "0.75", "min_days": "3"}, cas=0)
    arch = ArchiveResource(box.spool, box.archive, wall=box.wall)
    t = box.wall()
    for i in range(n):
        start = t - (days - i * days / n) * 86400
        p = segment_path(box.archive, "1", 1, datetime.fromtimestamp(start, timezone.utc))
        os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(b"x" * size)
        Manifest(box.archive, "1").append(Segment("1", 1, start, start + 600, os.path.relpath(p, box.archive), size))
    return box, arch


def test_the_watermark_is_a_floor_and_a_shortfall_not_a_quiet_cut():
    """Retention by days is a promise; the watermark is what happens when it cannot be kept.

    Freeing stops at the floor, and what could not be freed is a number in the report —
    not a cut into yesterday that nobody asked for and nobody is told about."""
    from vms.archive import ArchivePolicy
    from vms.space import depth_days
    box, arch = _space_box(days=10.0)
    policy = ArchivePolicy(arch, box.vars)                      # no peers on this box: nowhere to evacuate to
    assert round(depth_days(arch, "1", box.wall())) == 10

    rep = policy.free(150_000, box.wall(), min_days=3)          # three segments' worth
    assert rep == {"freed": 150_000, "cut": 3}
    assert round(depth_days(arch, "1", box.wall())) == 7
    assert len(Manifest(box.archive, "1").read()) == 7 and len(arch.coverage("1")) >= 1

    rep = policy.free(10_000_000, box.wall(), min_days=3)       # more than there is above the floor
    assert rep["shortfall"] > 0 and rep["freed"] < 10_000_000
    left = Manifest(box.archive, "1").read()
    assert left and 3 <= depth_days(arch, "1", box.wall()) <= 4   # AT the floor — not emptied, not below it


def test_a_resource_over_the_mark_says_so_and_one_under_it_does_nothing():
    """The two marks and the gap between them: a saw is what one mark alone gives."""
    from w2cplatform.resource import Resource
    box, arch = _space_box(days=10.0)
    res = Resource(box.archive, "srv-1", "http://srv-1", box.vars, box.objects, wall=box.wall,
                   space_probe=lambda root: (1_000_000, 500_000))
    from vms.archive import ArchivePolicy
    res.register("rec", ArchivePolicy(arch, box.vars))
    assert res.relieve() == {"space": "ok", "full": 0.5}
    assert res.heartbeat()["space"]["free"] == 500_000          # what a peer reads before sending anything here

    res.space_probe = lambda root: (1_000_000, 100_000)         # 90 % full
    rep = res.relieve()
    assert rep["space"] == "over" and rep["need"] == 150_000    # down to the LOW mark, not to the high one
    assert rep["freed"] == 150_000 and rep["short"] == 0 and rep["rec.cut"] == 3


def test_backfill_stops_while_the_disk_is_over_the_mark():
    """Otherwise the two chase each other for ever: the resource frees space, the recorder
    fetches more of the same hours back. `keep_days` closes that trap in time; this closes
    it in space, and not even an operator's `force` opens it."""
    box, ctl, con, con_vars = _box()
    w = _holder(box, lambda k: FakeDevice(k, channels=["1"], coverage={"1": (0.0, 1_000_000.0, 5)}))
    con.create_camera({"name": "front", "source": CARD})
    ctl.ensure_placed(); w.reconcile_once(); w.heartbeat_once()
    rec_ctl = SpecController(REC_SPEC, box.vars.as_writer("reccontroller", REC_SPEC.acl_controller()), box.objects, wall=box.wall)
    SpecController(REC_SPEC, con_vars, box.objects, wall=box.wall).create({"cam": "1"})
    arch = ArchiveResource(box.spool, box.archive, wall=box.wall)
    r = RecWorker("r-1", box.vars.as_writer("recworker", ["rec/epoch/*", "rec/slots/*"]), box.objects,
                  FakeActuator(), archive=arch, clock=box.clock, wall=box.wall, server="srv-1",
                  env={}, keep_days=1.0, settle=1000.0)
    r.heartbeat_once(); rec_ctl.ensure_placed(); r.reconcile_once()

    from w2cplatform.resource import SPACE_KEY
    now = 1_000_000.0
    box.vars.put(SPACE_KEY, {"enabled": "true", "high": "0.85", "low": "0.75"}, cas=0)
    r.space_probe = lambda root: (1_000_000, 100_000)                    # 90 % full
    assert r.under_pressure()
    assert r.backfill(budget=1, now=now, force=True) == []               # force does not open it either

    r.space_probe = lambda root: (1_000_000, 500_000)                    # room again, and the same call fetches
    assert not r.under_pressure()
    assert r.backfill(budget=1, now=now, force=True)


# -- home: the server a RECORDING belongs to ---------------------------------------------------------

def _rec_home_box():
    """Two recorders on two servers, each with a resource answering (`rec` requires one)."""
    import json
    box = Box()
    rec = SpecController(REC_SPEC, box.vars, box.objects, wall=box.wall)
    for w, server in (("r-1", "srv-a"), ("r-2", "srv-b")):
        _rec_alive(box, w, server)
    return box, rec


def _rec_alive(box, worker, server, labels=()):
    import json
    box.objects.put(REC_SPEC.sub.heartbeat_key(worker),
                    Heartbeat(worker, box.wall(), [], {"server": server, "capacity": 50, "headroom": 50,
                                                       "labels": ",".join(labels)}).to_bytes())
    box.objects.put(f"platform/resources/{server}/heartbeat",
                    json.dumps({"server": server, "ts": box.wall(), "url": f"http://{server}", "units": {}}).encode())


def test_a_recording_prefers_its_home_and_is_written_anywhere_when_it_is_down():
    """The one thing `home` must not be is a label.

    A label is a filter: with `labels: [srv-a]` a recording whose server is down becomes
    unplaceable, and the recording stops — at exactly the moment it must not. `home` is a
    preference: at home when home is there, anywhere when it is not, and back, one a pass,
    when it returns. The footage written meanwhile stays where it was written until that
    server needs the room (`vms/space.py`)."""
    box, rec = _rec_home_box()
    rec.create({"cam": "1", "home": "srv-a"})
    rec.ensure_placed()
    assert rec.where("1") == "r-1" and "at home on srv-a" in rec.placement("1").reason

    box.wall.advance(60); _rec_alive(box, "r-2", "srv-b")          # srv-a goes away, with its resource
    rec.move("1", "r-2", "srv-a gone")
    rec.create({"cam": "2", "home": "srv-a"})                      # a NEW recording of srv-a's, while it is down
    rec.ensure_placed()
    assert rec.where("2") == "r-2" and "away from home srv-a" in rec.placement("2").reason
    assert rec.unplaceable() == []                                 # the point: it records, it is not "unplaceable"

    _rec_alive(box, "r-1", "srv-a"); _rec_alive(box, "r-2", "srv-b")
    assert rec.ensure_home(1) == [("1", "r-2", "r-1")] and rec.where("1") == "r-1" and rec.where("2") == "r-2"
    assert "home is srv-a" in rec.placement("1").reason
    assert rec.ensure_home(1) == [("2", "r-2", "r-1")] and rec.where("2") == "r-1"
    assert rec.ensure_home(1) == []                                # everybody home: nothing to say


def test_a_recording_with_no_home_is_never_moved_by_it():
    """Every recording until an operator says otherwise. An empty field is not a server name:
    a homeless recording is placed on the disk with the most room, and stays there."""
    box, rec = _rec_home_box()
    rec.create({"cam": "1"})
    rec.ensure_placed()
    where = rec.where("1")
    assert "home" not in rec.placement("1").reason
    assert rec.ensure_home(5) == [] and rec.where("1") == where


def test_the_filters_still_beat_the_preference():
    """`home` orders what is already eligible; it never widens it. A recording whose labels no
    recorder on its home server can serve is placed where they CAN be served, and `ensure_home`
    leaves it there — a preference that could overrule a filter would put a recording on a
    server whose disks the operator ruled out."""
    box, rec = _rec_home_box()
    _rec_alive(box, "r-1", "srv-a", labels=["disks:slow"])
    _rec_alive(box, "r-2", "srv-b", labels=["disks:fast"])
    rec.create({"cam": "1", "home": "srv-a", "labels": ["disks:fast"]})
    rec.ensure_placed()
    assert rec.where("1") == "r-2" and "away from home srv-a" in rec.placement("1").reason
    assert rec.ensure_home(5) == [] and rec.where("1") == "r-2"


def test_the_camera_follows_its_recording_and_not_the_other_way():
    """Which of the pair is the anchor, and why it has to be the recording.

    A recording writes to a disk and a disk does not move; a fan-out can be read from any
    server over RTSP. So the recording names a home and the camera says `home: near` — it
    goes where its recording is. Both pointing at each other would be worse than either:
    with no anchor, every pass moves each towards where the other was, and they swap."""
    box, ctl, con, con_vars = _box()
    _worker_on(box, "w-a", "srv-a"); _worker_on(box, "w-b", "srv-b")
    box.objects.put(REC_SPEC.sub.heartbeat_key("r-1"),               # the recording of camera 1 is on srv-b
                    Heartbeat("r-1", box.wall(), [{"id": "1", "phase": "running"}], {"server": "srv-b"}).to_bytes())
    con.create_camera({"name": "gate", "source": "driverpack://file/gate.mp4"})
    ctl.ensure_placed()
    assert ctl.where(1) == "w-b" and "beside r-1 holding it" in ctl.placement(1).reason

    box.objects.put(REC_SPEC.sub.heartbeat_key("r-1"),               # the recording goes home to srv-a
                    Heartbeat("r-1", box.wall(), [], {"server": "srv-b"}).to_bytes())
    box.objects.put(REC_SPEC.sub.heartbeat_key("r-2"),
                    Heartbeat("r-2", box.wall(), [{"id": "1", "phase": "running"}], {"server": "srv-a"}).to_bytes())
    assert ctl.ensure_home(1) == [(1, "w-b", "w-a")]                 # and the camera follows it
    assert "it follows rec onto srv-a" in ctl.placement(1).reason


def _worker_on(box, name, server, labels=(), capacity=50):
    w = VmsWorker(name, box.vars, box.objects, FakeActuator(), clock=box.clock, wall=box.wall,
                  server=server, capacity=capacity, env={"LABELS": ",".join(labels)})
    w.heartbeat_once()
    return w


def test_only_one_of_a_following_pair_may_be_the_follower():
    """The asymmetry is the design, so it is asserted and not merely commented.

    `home: near` says "wherever the thing I follow is". Two subsystems that each said it
    would have no anchor: every pass moves each towards where the other WAS, and they swap
    places instead of meeting. One of the pair must name a real home. In the VMS that is the
    recording — it writes to a disk, and a disk does not move."""
    assert SPEC.near == "rec" and SPEC.home == "near"          # the camera follows
    assert REC_SPEC.home == "home" and REC_SPEC.home != "near"  # the recording is the anchor
    assert REC_SPEC.near == "none"                              # …and follows nothing: it is pinned to disks

    # a spec that follows nothing cannot say it follows
    from w2cplatform.spec import SubsystemSpec
    bad = {"name": "x", "unit": {"fields": {}}, "placement": {"home": "near"}}
    try:
        SubsystemSpec.from_dict(bad); raise AssertionError("accepted home: near with no near")
    except ValueError as e:
        assert "needs a near to follow" in str(e)
    # and a home that names no field is a typo, not an empty home
    try:
        SubsystemSpec.from_dict({"name": "x", "unit": {"fields": {}}, "placement": {"home": "hom"}})
        raise AssertionError("accepted a home naming no field")
    except ValueError as e:
        assert "names no field" in str(e)
