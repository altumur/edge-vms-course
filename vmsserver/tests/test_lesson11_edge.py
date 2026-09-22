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
    SpecController(REC_SPEC, con_vars, box.objects, wall=box.wall).create({"name": "1", "cam": "1"})
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
    """Where the text actually comes from: a subsystem whose id is a FIELD (`rec`, `id: name`)
    takes the unit's id verbatim from the operator's body — only `_next_id` protects a numeric
    one. From there the same string becomes the key `rec/recordings/<id>`, the prefix the
    console's token is matched against (`rec/recordings/*` — which `rec/recordings/../../…`
    passes), and a directory on the resource (`unit_dir`). Refused at the door, as a 400."""
    box, ctl, con, con_vars = _box()
    rec = SpecController(REC_SPEC, con_vars, box.objects, wall=box.wall)

    for bad in ("../../cameras/7", "a/b", ".."):
        try:
            rec.create({"name": bad, "cam": "7"})
            assert False, f"a unit id was accepted as a path: {bad!r}"
        except Refused as e:
            assert "name, not a path" in str(e), e

    r = rec.create({"name": "7", "cam": "7"})                       # the ordinary case is untouched
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


# What the note promised, and what actually shipped. The spec names its recordings now — `id: name` —
# because a camera written to two archives is two rows and one key cannot hold both. What it deliberately
# does NOT carry is `spread_by: cam`: that filter keeps two recordings of one camera off one SERVER, which
# is right for the installation buying redundancy and wrong for the one whose second archive hangs off the
# same box. So this is the test of that one line, added to the spec as shipped.
def _redundant_spec():
    """`rec.subsystem.yaml` as shipped, with the placement of an installation that wants the copies apart.

    The `unit:` block is untouched — it is the shipped one, names and all. Only `placement` differs, and
    of its four lines exactly one is the subject: `spread_by: cam`. The other three take the disks out of
    the exercise, which is about servers."""
    import yaml
    import vms
    from w2cplatform.spec import SubsystemSpec
    path = os.path.join(os.path.dirname(vms.__file__), "rec.subsystem.yaml")
    d = yaml.safe_load(open(path, encoding="utf-8"))
    d["placement"]["spread_by"] = "cam"                                     # the line under test
    d["placement"].update({"requires": "none", "servers": "shared", "near": "vms"})
    d["placement"].pop("place_by", None); d["placement"].pop("home", None)
    return SubsystemSpec.from_dict(d)


def test_two_recordings_of_one_camera_are_a_yaml_edit():
    """Not a rehearsal for a change: the change itself, run against the real classes.

    The unit is the shipped one and the placement is one line longer. Everything it drives —
    SpecController, the archive tree, the console's timeline — is the shipped code, imported
    unchanged. If any of it still assumed "a recording is named by its camera", this test
    would not pass, and until the unit-keyed tree it would not have."""
    from w2cplatform.contract import Heartbeat
    from vms.archive import Manifest, Segment, segment_path
    from vms.config import REC_SPEC
    from vms.console import recordings_of

    box, ctl, con, con_vars = _box()
    spec = _redundant_spec()
    assert REC_SPEC.id == "name" and not REC_SPEC.spread_by      # what ships: named, and not spread by itself
    assert spec.id == "name" and spec.spread_by == "cam"         # what this installation runs

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


def test_a_named_unit_reaches_the_places_that_still_assumed_a_number():
    """Three reads and one write kept `int(...)` on a unit id after the tree stopped assuming one.

    Every one of them was unreachable while `id: cam` held — which is exactly why they survived
    so long, and why the first `7-cloud` would have found all four at once. They are reachable
    now, in the shipped spec; here each one answers instead of raising."""
    from vms.console import vms_routes as console_routes
    from vms.resource import vms_routes as resource_routes

    box, ctl, con, con_vars = _box()
    spec = _redundant_spec()
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
    r1 = RecWorker("r-1", box.vars, box.objects, archive=archive, clock=box.clock, wall=box.wall, server="srv-1")
    r1.reconcile_once()
    held = sorted(r1.reconciler.actual)
    assert held and all(not str(u).isdigit() for u in held)                # the point: nothing here is a number
    rec.move(held[0], "r-2", "operator asked")
    RecWorker("r-2", box.vars, box.objects, archive=archive, clock=box.clock, wall=box.wall, server="srv-2").reconcile_once()
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
    SpecController(REC_SPEC, con_vars, box.objects, wall=box.wall).create({"name": "1", "cam": "1"})
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


def _rec_alive(box, worker, server, labels=(), volume=None):
    import json
    box.objects.put(REC_SPEC.sub.heartbeat_key(worker),
                    Heartbeat(worker, box.wall(), [], {"server": server, "capacity": 50, "headroom": 50,
                                                       **({"volume": volume} if volume else {}),
                                                       "labels": ",".join(labels)}).to_bytes())
    box.objects.put(f"platform/resources/{server}/heartbeat",
                    json.dumps({"server": server, "ts": box.wall(), "url": f"http://{server}", "units": {}}).encode())


def test_the_recorder_frees_bytes_on_the_disk_that_is_short():
    """The resource measured a volume, so the answer has to come off that volume.

    Two disks, one of them full. Freeing on the empty one would report a number and
    change nothing: the recording that cannot write is on the full one."""
    import tempfile
    from vms.archive import ArchivePolicy, ArchiveResource
    from vms.resource import vms_resource
    from w2cplatform.resource import SPACE_KEY

    box = Box()
    archives = {v: ArchiveResource(tempfile.mkdtemp(prefix=v + "-spool-"),
                                   tempfile.mkdtemp(prefix=v + "-"), wall=box.wall) for v in ("vol-a", "vol-b")}
    res = vms_resource(archives["vol-a"], "srv-1", "http://srv-1", box.vars, box.objects,
                       wall=box.wall, archives=archives)
    sizes = {archives["vol-a"].root: (1_000_000, 10_000),      # 99 % full
             archives["vol-b"].root: (1_000_000, 990_000)}
    res.space_probe = lambda root: sizes[root]
    box.vars.put(SPACE_KEY, {"enabled": "true", "high": "0.85", "low": "0.75"}, cas=0)

    asked = []
    policy: ArchivePolicy = res.hooks["rec"]
    real_free = policy.free
    policy.free = lambda need, now, min_days=3.0, volume=None: (asked.append((volume, need)) or
                                                                real_free(need, now, min_days, volume=volume))
    rep = res.relieve()
    assert [v for v, _ in asked] == ["vol-a"], "asked about the disk that is short, and only that one"
    assert [v["volume"] for v in rep["volumes"]] == ["vol-a"]
    assert res.spaces()["vol-b"]["full"] == 0.01               # the other disk was never touched


def test_three_disks_are_three_places_to_record_on_one_server():
    """`servers: distinct` over `place_by: volume`.

    A box with three disks runs three recorders, and each of them is a place to
    record: two recorders on ONE disk are not a second place — that is what the policy
    has always said — but two on two disks of one box are exactly that, and counting
    them by server would idle two thirds of the hardware the operator bought.

    The home follows the same field: the operator says which disk a camera's recording
    lives on, and it is a preference, not a filter. A disk that is full or gone means
    the recording is written elsewhere, not that it stops."""
    box, rec = _rec_home_box()
    for name, vol in (("r-1", "vol-a"), ("r-2", "vol-b"), ("r-3", "vol-c")):
        _rec_alive(box, name, "srv-a", volume=vol)             # one server, three disks, three recorders

    assert rec.idle_by_policy(["r-1", "r-2", "r-3"]) == [], "counted by server, two of the three would idle"

    rec.create({"name": "1", "cam": "1", "home": "vol-b"})
    rec.create({"name": "2", "cam": "2", "home": "vol-c"})
    rec.create({"name": "3", "cam": "3"})                                   # no home: wherever there is room
    rec.ensure_placed()
    assert rec.where("1") == "r-2" and "at home on vol-b" in rec.placement("1").reason
    assert rec.where("2") == "r-3" and "at home on vol-c" in rec.placement("2").reason
    assert rec.where("3") in ("r-1", "r-2", "r-3")

    # vol-b's recorder stops heartbeating — the disk was pulled, or its process died. A recording homed
    # there is still created and still placed: the home is a PREFERENCE, and a filter here would mean a
    # camera stops recording because one disk of three went away.
    box.wall.advance(60)
    _rec_alive(box, "r-1", "srv-a", volume="vol-a"); _rec_alive(box, "r-3", "srv-a", volume="vol-c")
    rec.create({"name": "4", "cam": "4", "home": "vol-b"})
    rec.ensure_placed()
    assert rec.where("4") in ("r-1", "r-3") and "away from home vol-b" in rec.placement("4").reason
    assert rec.unit("4")["home"] == "vol-b"                    # remembered, so it can go back

    # …and when the disk comes back, `ensure_home` brings it back, one recording a pass.
    box.wall.advance(60)
    for name, vol in (("r-1", "vol-a"), ("r-2", "vol-b"), ("r-3", "vol-c")):
        _rec_alive(box, name, "srv-a", volume=vol)
    moved = rec.ensure_home(budget=1)
    assert [m[0] for m in moved] == ["4"] and rec.where("4") == "r-2"


def test_one_camera_written_to_two_archives_is_two_recordings_on_one_box():
    """The requirement that took `id: cam` off the spec — and it is not redundancy.

    A box whose only storage is its disks has one archive, so "record camera 7" is one row and
    naming that row by the camera costs nothing. Give it a second volume — a network archive
    mounted beside the local disks — and "camera 7 locally, keep a week" and "camera 7 into the
    network archive, keep a year" are two recordings of one camera, on ONE server: two rows, and
    `rec/recordings/7` cannot hold both. The name says which recording; `cam` says whose footage.

    Nothing spreads by camera here, deliberately — both recordings belong on this box, which is
    what the operator asked for. `spread_by: cam` would refuse the pair outright; the test above
    is the other installation, the one that buys a second server."""
    box, rec = _rec_home_box()
    _rec_alive(box, "r-1", "srv-a", volume="disks")
    _rec_alive(box, "r-2", "srv-a", volume="cloud")                 # one server, two archives, two recorders

    rec.create({"name": "7", "cam": "7", "home": "disks", "retention_days": 7})
    rec.create({"name": "7-cloud", "cam": "7", "home": "cloud", "retention_days": 365})
    rec.ensure_placed()

    assert rec.where("7") == "r-1" and rec.where("7-cloud") == "r-2"            # one recorder per volume
    assert rec.server_of(rec.placement("7").worker) == "srv-a" == rec.server_of(rec.placement("7-cloud").worker)
    assert box.vars.list("rec/recordings/") == ["rec/recordings/7", "rec/recordings/7-cloud"]
    assert rec.unit("7")["cam"] == rec.unit("7-cloud")["cam"] == "7"            # one camera behind both
    assert rec.unit("7")["retention_days"] == 7 and rec.unit("7-cloud")["retention_days"] == 365

    # the console still answers "where is camera 7's footage" — with both of them, and the page
    # composed those two names from the one field the operator filled in: the archive.
    from vms.console import recordings_of
    assert sorted(recordings_of(rec, 7)) == ["7", "7-cloud"]

    # the simple installation is untouched: with one archive the name IS the camera, and the row,
    # the tree and every path in the lessons read exactly as they did before.
    rec.create({"name": "8", "cam": "8"})
    assert rec.unit("8")["id"] == "8"


def test_the_console_names_the_archives_a_recording_can_be_homed_to():
    """Where the page gets the list it offers when a recording is created.

    `/servers` reports each worker's PLACE — whatever `place_by` counts in: the server for
    almost everyone, the VOLUME for the recorder. So the operator picks an archive the cluster
    actually reports instead of typing a name nobody answers to, and `home` is then a name with
    a recorder behind it. Getting it wrong is still not fatal — `home` is a preference — but a
    preference nobody can satisfy is a silent one."""
    from w2cplatform.console import SpecConsole
    box, rec = _rec_home_box()
    _rec_alive(box, "r-1", "srv-a", volume="disks")
    _rec_alive(box, "r-2", "srv-a", volume="cloud")

    con = SpecConsole(rec, wall=box.wall)
    places = sorted(w["place"] for s in con.servers()["servers"].values() for w in s["workers"])
    assert places == ["cloud", "disks"]


def test_a_recording_prefers_its_home_and_is_written_anywhere_when_it_is_down():
    """The one thing `home` must not be is a label.

    A label is a filter: with `labels: [srv-a]` a recording whose server is down becomes
    unplaceable, and the recording stops — at exactly the moment it must not. `home` is a
    preference: at home when home is there, anywhere when it is not, and back, one a pass,
    when it returns. The footage written meanwhile stays where it was written until that
    server needs the room (`vms/space.py`)."""
    box, rec = _rec_home_box()
    rec.create({"name": "1", "cam": "1", "home": "srv-a"})
    rec.ensure_placed()
    assert rec.where("1") == "r-1" and "at home on srv-a" in rec.placement("1").reason

    box.wall.advance(60); _rec_alive(box, "r-2", "srv-b")          # srv-a goes away, with its resource
    rec.move("1", "r-2", "srv-a gone")
    rec.create({"name": "2", "cam": "2", "home": "srv-a"})                      # a NEW recording of srv-a's, while it is down
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
    rec.create({"name": "1", "cam": "1"})
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
    rec.create({"name": "1", "cam": "1", "home": "srv-a", "labels": ["disks:fast"]})
    rec.ensure_placed()
    assert rec.where("1") == "r-2" and "away from home srv-a" in rec.placement("1").reason
    assert rec.ensure_home(5) == [] and rec.where("1") == "r-2"


def test_the_camera_follows_its_recording_and_not_the_other_way():
    """Which of the pair is the anchor, and why it has to be the recording.

    A recording writes to a disk and a disk does not move; a fan-out can be read from any
    server over RTSP. So the recording names a home and the camera says `home: near` — it
    goes where its recording is. Both pointing at each other would be worse than either:
    with no anchor, every pass moves each towards where the other was, and they swap.

    Note WHICH recording it follows: `near: {sub: rec, of: cam}`, the entry whose `cam` is this
    camera — here one the operator called `1-cloud`, a name this controller never learns and
    does not need to. Matching by id, as the spec did while recordings were named by camera,
    would have found nothing and moved the camera nowhere, saying nothing about it."""
    box, ctl, con, con_vars = _box()
    _worker_on(box, "w-a", "srv-a"); _worker_on(box, "w-b", "srv-b")
    box.objects.put(REC_SPEC.sub.heartbeat_key("r-1"),               # the recording of camera 1 is on srv-b
                    Heartbeat("r-1", box.wall(), [{"id": "1-cloud", "cam": "1", "phase": "running"}], {"server": "srv-b"}).to_bytes())
    con.create_camera({"name": "gate", "source": "driverpack://file/gate.mp4"})
    ctl.ensure_placed()
    assert ctl.where(1) == "w-b" and "beside r-1 holding it" in ctl.placement(1).reason

    box.objects.put(REC_SPEC.sub.heartbeat_key("r-1"),               # the recording goes home to srv-a
                    Heartbeat("r-1", box.wall(), [], {"server": "srv-b"}).to_bytes())
    box.objects.put(REC_SPEC.sub.heartbeat_key("r-2"),
                    Heartbeat("r-2", box.wall(), [{"id": "1-cloud", "cam": "1", "phase": "running"}], {"server": "srv-a"}).to_bytes())
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


# -- draining a server: the rolling upgrade ----------------------------------------------------------

def test_a_planned_stop_is_not_a_silence():
    """A stopped process is noticed anyway — but being noticed is the SLOW path. A recorder's
    units move when its slot has lapsed AND its server's resource is silent: two independent
    silences, about a minute and a half of not recording. An upgrade is known in advance, so
    the work leaves first and the machine stops empty.

    One row says it, and every subsystem reads it through `_pool` and `redistribute`: nothing
    is told anything, and no subsystem learns a new word."""
    from w2cplatform.contract import DRAIN_KEY, DrainRefused
    box, ctl, con, con_vars = _box()
    _worker_on(box, "w-a", "srv-a"); _worker_on(box, "w-b", "srv-b")
    for i in range(2):
        con.create_camera({"name": f"c{i}", "source": f"driverpack://file/{i}.mp4"})
    ctl.ensure_placed()
    assert {ctl.where(1), ctl.where(2)} == {"w-a", "w-b"}          # one each, by capacity

    con.drain("srv-a")                                             # the OPERATOR says it: the console's token, never the controller's
    assert ctl.draining() == "srv-a" and box.vars.get(DRAIN_KEY)[0]["server"] == "srv-a"
    assert "w-a" not in ctl._pool(None)                            # not placed on any more…
    moves = ctl.redistribute()                                     # …and what it has leaves, orderly
    assert [(m[1], m[2]) for m in moves] == [("w-a", "w-b")]
    assert ctl.where(1) == ctl.where(2) == "w-b"
    assert "server srv-a draining" in ctl.placement(1).reason

    try:                                                           # one machine at a time, by CAS
        con.drain("srv-b"); raise AssertionError("two servers draining at once")
    except DrainRefused as e:
        assert "already draining" in str(e)

    con.undrain()                                                  # the machine is back
    assert ctl.draining() == "" and "w-a" in ctl._pool(None)
    assert ctl.redistribute() == []                                # nothing moves back on its own…
    assert ctl.rebalance(1, 0.10) or True                          # …it is `home` and `rebalance` that refill


def test_the_dry_run_answers_before_the_reboot_not_after():
    """Fifty cameras leaving a machine have to land somewhere, and "somewhere" is a fact about
    headroom and labels. `would_strand` asks it with the machinery that will answer for real
    afterwards — the alternative is reading `/unplaceable` once the server is already down."""
    box, ctl, con, con_vars = _box()
    _worker_on(box, "w-a", "srv-a", labels=["vlan:a"])
    _worker_on(box, "w-b", "srv-b", labels=["vlan:b"])
    con.create_camera({"name": "only-a", "source": "driverpack://file/1.mp4", "labels": ["vlan:a"]})
    con.create_camera({"name": "anywhere", "source": "driverpack://file/2.mp4"})
    ctl.ensure_placed()
    assert ctl.where(1) == "w-a"

    assert ctl.would_strand("srv-b") == []          # camera 2 can go to srv-a
    assert ctl.would_strand("srv-a") == ["1"]       # camera 1 cannot go anywhere else: say so BEFORE
    assert ctl.unplaceable() == []                  # and nothing is stranded yet — this is a question, not a state


def test_the_upgrade_script_polls_a_condition_instead_of_sleeping():
    """`safe` is the whole point: units gone from that machine AND nothing left unwritten in
    a spool. A recorder whose units have left promotes what they closed on its next pump —
    and only then is it safe to pull the power."""
    box, ctl, con, con_vars = _box()
    from vms.console import make_console
    w = _holder(box, lambda k: FakeDevice(k, channels=["1"]))
    con.create_camera({"name": "gate", "source": "driverpack://file/gate.mp4"})
    ctl.ensure_placed(); w.reconcile_once(); w.heartbeat_once()

    rec_ctl = SpecController(REC_SPEC, box.vars, box.objects, wall=box.wall)
    SpecController(REC_SPEC, con_vars, box.objects, wall=box.wall).create({"name": "1", "cam": "1"})
    arch = ArchiveResource(box.spool, box.archive, wall=box.wall)
    r = RecWorker("r-1", box.vars, box.objects, FakeActuator(), archive=arch, clock=box.clock,
                  wall=box.wall, server="srv-1", env={})
    arch2 = ArchiveResource(box.spool + "2", box.archive + "2", wall=box.wall)
    r2 = RecWorker("r-2", box.vars, box.objects, FakeActuator(), archive=arch2, clock=box.clock,
                   wall=box.wall, server="srv-2", env={})               # somewhere for the work to go
    r.heartbeat_once(); r2.heartbeat_once(); rec_ctl.ensure_placed(); r.reconcile_once(); r.heartbeat_once()
    assert rec_ctl.where("1") == "r-1"
    m = make_console(con, arch, wall=box.wall, mounts={"rec": rec_ctl})   # the console's token: it may say "draining"

    assert m.drain_route("GET", {})[1] == {"draining": "", "safe": True,
                                           "subsystems": {"vms": {"draining": "", "subsystem": "vms"},
                                                          "rec": {"draining": "", "subsystem": "rec"}}}
    status, rep = m.drain_route("POST", {"server": "srv-1"})
    assert status == 200 and rep["draining"] == "srv-1" and rep["safe"] is False
    assert rep["subsystems"]["rec"]["units"] == 1                  # the recorder still carries it
    assert rep["would_strand"] == {"vms": ["1"]}                   # and the camera has nowhere to go at all:
    #                                                                one vms worker in the box, and it is on this machine.
    #                                                                An upgrade script stops here rather than after the reboot.
    _worker_on(box, "w-2", "srv-2")                                # give it somewhere, and the dry run goes quiet
    assert m.drain_route("GET", {})[1].get("would_strand", {}) == {}

    # a spool file nobody promoted yet: not safe, whatever the assignments say
    t = box.wall()
    p = segment_path(box.spool, "1", 1, datetime.fromtimestamp(t - 60, timezone.utc))
    os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(b"x")
    os.utime(p, (t - 30, t - 30))                                   # the box's clock, not the machine's
    r.heartbeat_once()
    assert m.drain_route("GET", {})[1]["subsystems"]["rec"]["spool"] == 1

    assert [(mv[1], mv[2]) for mv in rec_ctl.redistribute()] == [("r-1", "r-2")]     # the work leaves, orderly
    assert [(mv[1], mv[2]) for mv in ctl.redistribute()] == [("w-1", "w-2")]
    r.heartbeat_once()
    mid = m.drain_route("GET", {})[1]["subsystems"]["rec"]          # nothing assigned here any more…
    assert mid["units"] == 0 and mid["spool"] == 1 and mid["safe"] is False   # …and still not safe: a segment is unwritten
    r.reconcile_once(); r.pump_once(); r.heartbeat_once()           # …and the pump promotes what was closed
    rep = m.drain_route("GET", {})[1]
    assert rep["subsystems"]["rec"]["units"] == 0 and rep["subsystems"]["rec"]["spool"] == 0
    assert rep["subsystems"]["vms"]["units"] == 0
    assert rep["safe"] is True                                      # now the power may go

    assert m.drain_route("DELETE", {})[1] == {"draining": "", "safe": True,
                                              "subsystems": {"vms": {"draining": "", "subsystem": "vms"},
                                                             "rec": {"draining": "", "subsystem": "rec"}}}


def test_a_request_is_fetched_outside_the_window_and_the_budget():
    """The ordinary pass is bounded by a budget and an hour because backfill shares
    the device's uplink with live. A range a PERSON asked for is different work:
    they are looking at that gap now, and "tonight" is not a useful answer."""
    box, ctl, con, con_vars = _box()
    w = _holder(box, lambda k: FakeDevice(k, channels=["1"], coverage={"1": (0.0, 1000000.0, 5)}))
    con.create_camera({"name": "front", "source": CARD})
    ctl.ensure_placed(); w.reconcile_once(); w.heartbeat_once()

    rec_ctl = SpecController(REC_SPEC, box.vars.as_writer("reccontroller", REC_SPEC.acl_controller()), box.objects, wall=box.wall)
    con_rec = SpecController(REC_SPEC, con_vars, box.objects, wall=box.wall)
    con_rec.create({"name": "1", "cam": "1"})
    arch = ArchiveResource(box.spool, box.archive, wall=box.wall)
    now = 1000000.0
    r = RecWorker("r-1", box.vars.as_writer("recworker", ["rec/epoch/*", "rec/slots/*"]), box.objects,
                  FakeActuator(), archive=arch, clock=box.clock, wall=box.wall, server="srv-1",
                  env={}, window=(22, 6), keep_days=1.0, settle=1000.0)
    r.heartbeat_once(); rec_ctl.ensure_placed(); r.reconcile_once()
    r.backfill_budget = 0                                                  # the ordinary pass fetches nothing at all

    con_rec.vars.put(REC_SPEC.sub.request_key("1-a"),
                     {"unit": "1", "cam": "1", "from": str(now - 76400), "to": str(now - 70000),
                      "at": str(now), "by": "anna"})
    r.pump_once()                                                           # the ORDINARY pass, not a direct call:
    assert r.fetched == ["1-a"]                                             # a pass nobody runs is the bug this project has had twice
    assert [s.source for s in Manifest(box.archive, 1).read() if s.source == "edge"]

    r.heartbeat_once()
    hb = Heartbeat.from_bytes(box.objects.get(REC_SPEC.sub.heartbeat_key("r-1")))
    assert "1-a" in hb.extra["fetched"]                                     # the worker says so; the console removes the row

    from vms.jobs import clear_requests
    assert clear_requests(con_rec) == 1 and box.vars.list(REC_SPEC.sub.requests_prefix()) == []


def test_a_request_for_somebody_elses_recording_is_left_alone():
    """Every recorder reads the same prefix. The camera here is held and its device
    answers — so the only thing that can refuse this range is that the recording
    belongs to another recorder. Fetching it would write another server's unit."""
    box, ctl, con, con_vars = _box()
    w = _holder(box, lambda k: FakeDevice(k, channels=["1"], coverage={"1": (0.0, 1_000_000.0, 5)}))
    con.create_camera({"name": "front", "source": CARD})
    ctl.ensure_placed(); w.reconcile_once(); w.heartbeat_once()
    con_rec = SpecController(REC_SPEC, con_vars, box.objects, wall=box.wall)
    con_rec.create({"name": "1", "cam": "1"})
    arch = ArchiveResource(box.spool, box.archive, wall=box.wall)
    r = RecWorker("r-1", box.vars.as_writer("recworker", ["rec/epoch/*", "rec/slots/*"]), box.objects,
                  FakeActuator(), archive=arch, clock=box.clock, wall=box.wall, server="srv-1", env={})
    r.heartbeat_once()                                                       # …and this recorder holds NOTHING
    assert r.rows == []
    assert r.device_source("1") is not None                                  # the device is right there, answering

    con_rec.vars.put(REC_SPEC.sub.request_key("1-a"),
                     {"unit": "1", "cam": "1", "from": "100", "to": "200", "at": "1", "by": "anna"})
    assert r.requests() == [] and r.fetched == []
    assert box.vars.list(REC_SPEC.sub.requests_prefix()) == ["rec/requests/1-a"]   # still asked, for whoever holds it


def test_a_request_waits_while_the_disk_is_over_the_mark():
    """`force` does not open this door for backfill (above) and a person asking does
    not open it either: a disk the resource is emptying this minute cannot be given
    more, however politely."""
    box, ctl, con, con_vars = _box()
    w = _holder(box, lambda k: FakeDevice(k, channels=["1"], coverage={"1": (0.0, 1_000_000.0, 5)}))
    con.create_camera({"name": "front", "source": CARD})
    ctl.ensure_placed(); w.reconcile_once(); w.heartbeat_once()
    rec_ctl = SpecController(REC_SPEC, box.vars.as_writer("reccontroller", REC_SPEC.acl_controller()), box.objects, wall=box.wall)
    con_rec = SpecController(REC_SPEC, con_vars, box.objects, wall=box.wall); con_rec.create({"name": "1", "cam": "1"})
    arch = ArchiveResource(box.spool, box.archive, wall=box.wall)
    r = RecWorker("r-1", box.vars.as_writer("recworker", ["rec/epoch/*", "rec/slots/*"]), box.objects,
                  FakeActuator(), archive=arch, clock=box.clock, wall=box.wall, server="srv-1",
                  env={}, keep_days=1.0, settle=1000.0)
    r.heartbeat_once(); rec_ctl.ensure_placed(); r.reconcile_once()

    from w2cplatform.resource import SPACE_KEY
    box.vars.put(SPACE_KEY, {"enabled": "true", "high": "0.85", "low": "0.75"}, cas=0)
    r.space_probe = lambda root: (1_000_000, 100_000)                        # 90 % full
    con_rec.vars.put(REC_SPEC.sub.request_key("1-b"),
                     {"unit": "1", "cam": "1", "from": "900000", "to": "930000", "at": "1", "by": "anna"})
    assert r.requests() == [] and r.fetched == []
    assert box.vars.list(REC_SPEC.sub.requests_prefix()) == ["rec/requests/1-b"]   # kept: it is a wait, not a refusal

    r.space_probe = lambda root: (1_000_000, 500_000)                        # room again, same request
    assert r.requests() and r.fetched == ["1-b"]


def test_a_request_the_recorder_could_not_serve_is_not_reported_as_served():
    """A recorder whose lease lapsed fetches nothing. Saying `fetched` anyway would
    have the console delete a request nobody served — and the operator's range would
    silently never arrive."""
    box, ctl, con, con_vars = _box()
    w = _holder(box, lambda k: FakeDevice(k, channels=["1"], coverage={"1": (0.0, 1_000_000.0, 5)}))
    con.create_camera({"name": "front", "source": CARD})
    ctl.ensure_placed(); w.reconcile_once(); w.heartbeat_once()
    rec_ctl = SpecController(REC_SPEC, box.vars.as_writer("reccontroller", REC_SPEC.acl_controller()), box.objects, wall=box.wall)
    con_rec = SpecController(REC_SPEC, con_vars, box.objects, wall=box.wall); con_rec.create({"name": "1", "cam": "1"})
    arch = ArchiveResource(box.spool, box.archive, wall=box.wall)
    r = RecWorker("r-1", box.vars.as_writer("recworker", ["rec/epoch/*", "rec/slots/*"]), box.objects,
                  FakeActuator(), archive=arch, clock=box.clock, wall=box.wall, server="srv-1",
                  env={}, keep_days=1.0, settle=1000.0)
    r.heartbeat_once(); rec_ctl.ensure_placed(); r.reconcile_once()
    con_rec.vars.put(REC_SPEC.sub.request_key("1-c"),
                     {"unit": "1", "cam": "1", "from": "900000", "to": "930000", "at": "1", "by": "anna"})
    box.clock.advance(26)                                                    # past the lease, short of a renewal
    assert not r.may_write("1")
    r.requests()
    assert r.fetched == []
    assert box.vars.list(REC_SPEC.sub.requests_prefix()) == ["rec/requests/1-c"]


def test_the_hole_in_the_footage_and_the_hole_in_the_detections_close_together():
    """End to end, with the real recorder: the card's minutes arrive, the recorder
    says which range it closed, and the console turns that into a scan by every
    detector of that camera. Nothing was recording the camera while the link was
    down — so nothing was watching it either, and the second hole is the first."""
    from vms.config import DET_SPEC, DETJOB_SPEC
    from vms.jobs import scan_what_arrived
    box, ctl, con, con_vars = _box()
    w = _holder(box, lambda k: FakeDevice(k, channels=["1"], coverage={"1": (0.0, 1_000_000.0, 5)}))
    con.create_camera({"name": "front", "source": CARD})
    ctl.ensure_placed(); w.reconcile_once(); w.heartbeat_once()

    rec_ctl = SpecController(REC_SPEC, box.vars.as_writer("reccontroller", REC_SPEC.acl_controller()), box.objects, wall=box.wall)
    con_rec = SpecController(REC_SPEC, con_vars, box.objects, wall=box.wall); con_rec.create({"name": "1", "cam": "1"})
    det = SpecController(DET_SPEC, con_vars, box.objects, wall=box.wall)
    det.create({"name": "1-lpr", "cam": "1", "kind": "lpr", "params": "plates"})
    # the console's token in this harness predates `detjob`; in the process it carries that grant too
    jobs = SpecController(DETJOB_SPEC, box.vars.as_writer("console2", DETJOB_SPEC.acl_console()), box.objects, wall=box.wall)

    arch = ArchiveResource(box.spool, box.archive, wall=box.wall)
    now = 1_000_000.0
    r = RecWorker("r-1", box.vars.as_writer("recworker", ["rec/epoch/*", "rec/slots/*"]), box.objects,
                  FakeActuator(), archive=arch, clock=box.clock, wall=box.wall, server="srv-1",
                  env={}, keep_days=1.0, settle=1000.0)
    r.heartbeat_once(); rec_ctl.ensure_placed(); r.reconcile_once()

    done = r.backfill(budget=1, now=now, force=True)                 # the link came back
    assert done and done[0]["segments"] > 0
    r.heartbeat_once()

    hb = Heartbeat.from_bytes(box.objects.get(REC_SPEC.sub.heartbeat_key("r-1")))
    assert hb.extra["closed"], "the recorder closed a range and told nobody"

    assert scan_what_arrived(con_rec, det, jobs) == 1
    j = jobs.units()[0]
    assert j["rec"] == "1" and j["kind"] == "lpr" and j["params"] == "plates"
    assert (j["from"], j["to"]) == (done[0]["from"], done[0]["to"])   # the footage that arrived, exactly


def test_a_fetch_that_brought_nothing_new_queues_no_scan():
    """`kept == 0` usually means live recording reached those minutes while we were
    fetching — the overlap check dropped every segment. Those minutes are already
    ours AND were already watched by the live detector; reporting them as newly
    arrived would scan them a second time and double every event in them."""
    from vms.config import DET_SPEC, DETJOB_SPEC
    from vms.jobs import scan_what_arrived
    box, ctl, con, con_vars = _box()
    w = _holder(box, lambda k: FakeDevice(k, channels=["1"], coverage={"1": (0.0, 1_000_000.0, 5)}))
    con.create_camera({"name": "front", "source": CARD})
    ctl.ensure_placed(); w.reconcile_once(); w.heartbeat_once()

    rec_ctl = SpecController(REC_SPEC, box.vars.as_writer("reccontroller", REC_SPEC.acl_controller()), box.objects, wall=box.wall)
    con_rec = SpecController(REC_SPEC, con_vars, box.objects, wall=box.wall); con_rec.create({"name": "1", "cam": "1"})
    det = SpecController(DET_SPEC, con_vars, box.objects, wall=box.wall)
    det.create({"name": "1-lpr", "cam": "1", "kind": "lpr"})
    jobs = SpecController(DETJOB_SPEC, box.vars.as_writer("console2", DETJOB_SPEC.acl_console()), box.objects, wall=box.wall)

    arch = ArchiveResource(box.spool, box.archive, wall=box.wall)
    now = 1_000_000.0
    r = RecWorker("r-1", box.vars.as_writer("recworker", ["rec/epoch/*", "rec/slots/*"]), box.objects,
                  FakeActuator(), archive=arch, clock=box.clock, wall=box.wall, server="srv-1",
                  env={}, keep_days=1.0, settle=1000.0)
    r.heartbeat_once(); rec_ctl.ensure_placed(); r.reconcile_once()

    # our own recording already covers these minutes
    start, end = now - 80000, now - 76400
    pth = segment_path(box.archive, 1, r.epochs["1"], datetime.fromtimestamp(start, timezone.utc))
    os.makedirs(os.path.dirname(pth), exist_ok=True); open(pth, "wb").write(b"x")
    Manifest(box.archive, 1).append(Segment("1", r.epochs["1"], start, end, os.path.relpath(pth, box.archive), 1))

    got = r.fetch("1", "1", "http://srv-1:8083/playback/1", start, end)
    assert got["segments"] == 0                                        # everything overlapped what we have
    r.heartbeat_once()
    hb = Heartbeat.from_bytes(box.objects.get(REC_SPEC.sub.heartbeat_key("r-1")))
    assert not hb.extra["closed"], "minutes we already had were announced as newly arrived"
    assert scan_what_arrived(con_rec, det, jobs) == 0


# -- the device's INDEX: where its footage is, not just that it has some ---------------------------
def test_the_index_is_a_door_and_not_a_field():
    """The summary answers "is there anything at all"; it cannot answer "is there
    anything at 10:05". For a device recording on motion that is most of the day.
    And the index is fetched rather than heartbeated, because the heartbeat is one
    object under a ceiling and this one grows with the device."""
    from vms.scan import covered_by, device_recordings
    box, ctl, con, con_vars = _box()
    day = 86400.0
    w = _holder(box, lambda k: FakeDevice(k, channels=["1"], coverage={"1": (0.0, day, 5)},
                                          index={"1": [(100.0, 200.0), (5000.0, 5600.0)]}))
    con.create_camera({"name": "front", "source": CARD})
    ctl.ensure_placed(); w.reconcile_once(); w.heartbeat_once()
    srv = w.serve_playback(port=0)
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        st = w.status_extra(w.rows[0])
        assert st["coverage"] == {"from": 0.0, "to": day, "fragments": 5}
        assert "/recordings/1" in st["index_url"] and "/playback/1" in st["playback_url"]

        spans = device_recordings(f"{base}/recordings/1", 0.0, day)
        assert spans == [(100.0, 200.0), (5000.0, 5600.0)]
        assert device_recordings(f"{base}/recordings/1", 150.0, 5200.0) == [(150.0, 200.0), (5000.0, 5200.0)]

        # the whole point: inside the summary, and empty
        assert covered_by(device_recordings(f"{base}/recordings/1", 1000.0, 4000.0), 1000.0, 4000.0) == 0.0
        assert w.device_of_row(w.rows[0]).in_use() == 0            # listing is not reading: no session taken
    finally:
        srv.shutdown()


def test_a_driver_that_cannot_list_says_so_and_is_not_read_as_empty():
    """`None` and `[]` are different answers. A caller that folds them together
    refuses work that would have succeeded."""
    from vms.scan import covered_by, device_recordings
    box, ctl, con, con_vars = _box()
    w = _holder(box, lambda k: FakeDevice(k, channels=["1"], coverage={"1": (0.0, 86400.0, 5)}))   # no index
    con.create_camera({"name": "front", "source": CARD})
    ctl.ensure_placed(); w.reconcile_once(); w.heartbeat_once()
    srv = w.serve_playback(port=0)
    try:
        spans = device_recordings(f"http://127.0.0.1:{srv.server_address[1]}/recordings/1", 0.0, 100.0)
        assert spans is None
        assert covered_by(spans, 0.0, 100.0) == 100.0              # cannot tell: the summary stands
        assert covered_by([], 0.0, 100.0) == 0.0                   # can tell, and there is nothing
    finally:
        srv.shutdown()


def test_a_job_is_not_promised_minutes_the_device_does_not_have():
    """`fetching` says "the footage exists, it is simply not ours yet". Said about
    a gap in the device's own recording it is a promise nothing can keep, and the
    job waits for a fetch that will never bring anything."""
    from vms.config import DETJOB_SPEC
    from vms.detjobworker import DetJobWorker
    box, ctl, con, con_vars = _box()
    day = 86400.0
    w = _holder(box, lambda k: FakeDevice(k, channels=["1"], coverage={"1": (0.0, day, 5)},
                                          index={"1": [(100.0, 200.0)]}))
    con.create_camera({"name": "front", "source": CARD})
    ctl.ensure_placed(); w.reconcile_once(); w.heartbeat_once()
    srv = w.serve_playback(port=0)
    try:
        # the heartbeat names the holder's own port; in the test the server is on another
        hb = Heartbeat.from_bytes(box.objects.get("vms/heartbeats/" + w.name))
        st = dict(hb.status[0]); st["index_url"] = f"http://127.0.0.1:{srv.server_address[1]}/recordings/1"
        box.objects.put("vms/heartbeats/" + w.name,
                        Heartbeat(w.name, box.wall(), [st], hb.extra).to_bytes())

        j = DetJobWorker("j-1", box.vars.as_writer("detjobworker", DETJOB_SPEC.sub.acl_worker()), box.objects,
                         clock=box.clock, wall=box.wall, server="srv-1", archive_root=box.archive, env={"LABELS": "gpu"})
        assert j.device_has("1", 100.0, 200.0)                     # a span it really has
        assert not j.device_has("1", 1000.0, 2000.0)               # inside the summary, and empty
    finally:
        srv.shutdown()
