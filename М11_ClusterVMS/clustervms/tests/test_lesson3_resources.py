"""Lesson 3 — what stays on the server, and what does not. Configuration is
already in raft (the RPO is zero); footage stays on the resource — under
rec/<cam>, the recorder's tree; the manifest returns with it; a timeline
spans two resources and names the one that is unreachable."""
import json
import os
from cluster.controller import ClusterController
from cluster.resource import cluster_resource, resources_seen
from w2cplatform.spec import SpecController
from vms.config import REC_SPEC
from cluster.timeline import merged_timeline
from datetime import datetime, timezone
from vms.archive import Manifest, Segment, segment_path
from tests.conftest import Cluster


class DirReader:
    """The console's manifest reader, against directories instead of HTTP."""
    def __init__(self, c: Cluster): self.c = c
    def read(self, url, cam):
        server = url.rsplit("/", 1)[1]
        if self.c.servers[server].__dict__.get("down"):
            raise ConnectionError(server)
        return Manifest(self.c.servers[server].archive, cam).read()


def _segment(server, cam, epoch, start, seconds=600, size=1000):
    p = segment_path(server.archive, cam, epoch, datetime.fromtimestamp(start, timezone.utc))
    rel = os.path.relpath(p, server.archive); os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f: f.write(b"x" * size)
    Manifest(server.archive, cam).append(Segment(cam, epoch, start, start + seconds, rel, size))


def test_an_edit_during_the_failover_is_simply_there():
    """The RPO inside the cluster is zero: the controller's write went into raft,
    and the replacement worker reads raft."""
    c = Cluster(); ctl = ClusterController(c.vars, c.objects, wall=c.wall)
    ctl.create_camera({"source": "driverpack://file/1.mp4", "name": "before"})
    a = c.worker(1, "srv-a"); a.heartbeat_once(); ctl.ensure_placed(); a.reconcile_once()
    c.wall.advance(20)                                                     # srv-a is gone; w-1 is between instances
    ctl.update_camera(1, {"name": "edited during the failover"})          # acknowledged after the CAS commit: it is in raft
    b = c.worker(1, "srv-b")                                               # the replacement
    assert b.reconcile_once() == [("start", 1)] and b.rows[0]["name"] == "edited during the failover"
    assert c.objects.get("vms/config") is None                             # nothing was published for this to work


def test_a_timeline_spans_two_resources_and_names_the_unreachable_one():
    c = Cluster(); ctl = ClusterController(c.vars, c.objects, wall=c.wall)
    t = c.wall()
    _segment(c.servers["srv-a"], 7, 3, t - 1200); _segment(c.servers["srv-a"], 7, 3, t - 600)   # before the failure, on A
    _segment(c.servers["srv-b"], 7, 4, t - 300)                                                # after, on B, next epoch
    hbs = {s: cluster_resource(srv.resource, s, f"http://{s}", c.vars, c.objects, wall=c.wall) for s, srv in c.servers.items()}
    for hb in hbs.values(): hb.heartbeat()
    seen = resources_seen(c.objects)
    assert seen["srv-a"]["units"] == {"rec": ["7"]} and seen["srv-c"]["units"] == {} and seen["srv-a"]["usage"] > 2000   # media + the manifest, the recorder's tree
    tl = merged_timeline(seen, DirReader(c), 7, t - 2000, t, current_epoch=4, now=c.wall())
    assert [(s["server"], s["epoch"], s["fenced"]) for s in tl["segments"]] == [("srv-a", 3, True), ("srv-a", 3, True), ("srv-b", 4, False)]
    assert tl["unreachable"] == []
    # srv-a dies: its heartbeat goes stale; its ranges are unavailable, and the answer says so by name
    c.wall.advance(60); hbs["srv-b"].heartbeat(); hbs["srv-c"].heartbeat()
    tl = merged_timeline(resources_seen(c.objects), DirReader(c), 7, t - 2000, t, current_epoch=4, now=c.wall())
    assert [s["server"] for s in tl["segments"]] == ["srv-b"] and tl["unreachable"] == ["srv-a"]
    assert "unavailable until the server returns" in tl["note"] and "lost" in tl["note"] and "not lost" in tl["note"]
    # srv-a returns: its manifest came back with its disks — nothing was rebuilt
    hbs["srv-a"].heartbeat()
    tl = merged_timeline(resources_seen(c.objects), DirReader(c), 7, t - 2000, t, current_epoch=4, now=c.wall())
    assert len(tl["segments"]) == 3 and tl["unreachable"] == []


def test_the_resource_policy_needs_neither_worker_nor_controller():
    c = Cluster(); ctl = ClusterController(c.vars, c.objects, wall=c.wall)
    ctl.create_camera({"source": "driverpack://file/1.mp4"})
    SpecController(REC_SPEC, c.vars, c.objects, wall=c.wall).create({"cam": "1", "retention_days": 1})   # retention is the RECORDING's row
    srv = c.servers["srv-a"]; t = c.wall()
    _segment(srv, 1, 1, t - 3 * 86400); _segment(srv, 1, 1, t - 3600)
    os.remove(os.path.join(srv.archive, Manifest(srv.archive, 1).read()[1].path))   # a file gone behind the manifest's back
    rep = cluster_resource(srv.resource, "srv-a", "http://srv-a", c.vars, c.objects, wall=c.wall).pass_()
    assert rep == {"rec.added": 0, "rec.dropped": 1, "rec.media_removed": 1, "removed": 0,
                   "space": "off", "enabled": False, "mirrored": 0, "peers": []}   # the watermark is a knob, and it is off
    assert Manifest(srv.archive, 1).read() == []


def test_a_worker_with_no_assignment_invents_nothing():
    c = Cluster(); w = c.worker(5, "srv-c")
    assert w.reconcile_once() == [] and w.name == "w-5"
    w.heartbeat_once()
    hb = json.loads(c.objects.get("vms/w-5/heartbeat"))
    assert hb["status"] == [] and hb["headroom"] == 50


def test_the_timeline_route_asks_for_a_unit_and_a_unit_is_named():
    """`/timeline/<id>` names a RECORDING, and a recording is named, not numbered — the
    day `rec.subsystem.yaml` says `id: name`. Nothing here needs that day to have come:
    the route reads the id, the heartbeat lists the directory, the manifest is per unit.
    A route that parses the id as a number answers 500 to the first `7-backup`."""
    from cluster.console import cluster_routes
    c = Cluster(); ctl = ClusterController(c.vars, c.objects, wall=c.wall)
    t = c.wall()
    _segment(c.servers["srv-a"], "7-main", 1, t - 1200)          # two recordings of one camera…
    _segment(c.servers["srv-b"], "7-backup", 1, t - 600)         # …on two servers, each its own tree
    for s, srv in c.servers.items():
        cluster_resource(srv.resource, s, f"http://{s}", c.vars, c.objects, wall=c.wall).heartbeat()
    assert resources_seen(c.objects)["srv-b"]["units"] == {"rec": ["7-backup"]}

    routes = cluster_routes(ctl, DirReader(c))
    status, body = routes(None, "GET", "/timeline/7-backup", {"from": t - 2000, "to": t})
    assert status == 200 and [s["server"] for s in body["segments"]] == ["srv-b"]
    status, body = routes(None, "GET", "/timeline/7-main", {"from": t - 2000, "to": t})
    assert status == 200 and [s["server"] for s in body["segments"]] == ["srv-a"]


# -- the watermark, and why evacuation has no button ------------------------------------------------

import io  # noqa: E402
from cluster.resource import PeerClient  # noqa: E402,F401
from vms.resource import vms_routes as vms_reads, vms_writes  # noqa: E402
from vms.space import cut, depth_days, foreign  # noqa: E402
from w2cplatform.resource import SPACE_KEY  # noqa: E402


class DirPeers:
    """A PeerClient over the servers' OWN routes: no sockets, the same handlers."""
    def __init__(self, c: Cluster): self.c = c

    def _archive(self, url):
        return self.c.servers[url.rsplit("/", 1)[1]].resource

    def put_raw(self, url, path, data, headers=None):
        h = dict(headers or {}); h["Content-Length"] = str(len(data))
        r = vms_writes(self._archive(url))("/" + path.lstrip("/"), h, io.BytesIO(data))
        if r is None or r[0] not in (200, 201, 204):
            raise IOError(f"PUT {path}: {r}")
        return r[0]

    def get_raw(self, url, path):
        r = vms_reads(self._archive(url))("/" + path.lstrip("/"), {})
        if r is None or r[0] != 200:
            raise IOError(f"GET {path}: {r}")
        return r[1]


def _writer(c: Cluster, server: str, unit: str, now: float):
    """A recorder on `server` saying in its heartbeat that it holds `unit` — the only fact
    `foreign` needs, and the same one the console reads to find a camera's holder."""
    c.objects.put(f"rec/r-{server}/heartbeat", json.dumps(
        {"worker": f"r-{server}", "ts": now, "status": [{"id": unit, "phase": "running"}], "server": server}).encode())


def _hb(c: Cluster, server: str, free: int, total: int = 1_000_000):
    """That server's resource heartbeat, with a disk of our choosing — a test cannot fill one."""
    res = cluster_resource(c.servers[server].resource, server, f"http://{server}", c.vars, c.objects,
                           wall=c.wall, peers=DirPeers(c))
    res.space_probe = lambda root, t=total, f=free: (t, f)
    res.heartbeat()
    return res


def test_a_server_that_needs_room_sends_back_what_it_wrote_for_a_neighbour():
    """The whole point of having no evacuation button.

    srv-b wrote camera 1 while srv-a was away (Lesson 4 has the failover itself; here the
    recording is already back on srv-a). Nothing happens while srv-b has room — the console
    merges timelines across resources, so the footage is neither lost nor in the way. It
    becomes work only when srv-b's own disk goes over the high mark, and then srv-b, the
    server that needs the space, is the one that acts."""
    c = Cluster()
    c.vars.put(SPACE_KEY, {"enabled": "true", "high": "0.85", "low": "0.75", "min_days": "3"}, cas=0)
    t = c.wall()
    for i in range(4):                                  # what srv-b wrote for srv-a: four segments of 50 kB
        _segment(c.servers["srv-b"], "1", 5, t - 4000 + i * 600, size=50_000)
    first = sorted(Manifest(c.servers["srv-b"].archive, "1").read(), key=lambda s: s.start)[0].path
    _writer(c, "srv-a", "1", t)                         # the recorder writing camera 1 is on srv-a now

    assert foreign(c.servers["srv-b"].resource, c.objects, "srv-b", t) == {"1": "srv-a"}
    assert foreign(c.servers["srv-b"].resource, c.objects, "srv-a", t) == {}   # from srv-a it is nobody's business

    _hb(c, "srv-a", free=500_000)
    b = _hb(c, "srv-b", free=500_000)                   # both have room: nothing to do, and that is the answer
    assert b.relieve()["space"] == "ok"
    assert len(Manifest(c.servers["srv-b"].archive, "1").read()) == 4
    assert not os.path.isdir(os.path.join(c.servers["srv-a"].archive, "rec", "1"))

    b.space_probe = lambda root: (1_000_000, 100_000)   # 90 % full: over the high mark
    rep = b.relieve()
    assert rep["space"] == "over" and rep["need"] == 150_000
    assert rep["rec.evacuated"] == 3 and rep["freed"] == 150_000 and rep["short"] == 0
    assert len(Manifest(c.servers["srv-b"].archive, "1").read()) == 1          # gone from here, file and line
    assert not os.path.exists(os.path.join(c.servers["srv-b"].archive, first))
    there = Manifest(c.servers["srv-a"].archive, "1").read()
    assert [s.epoch for s in there] == [5, 5, 5]        # arrived with the epoch srv-b wrote them under
    assert all(os.path.isfile(os.path.join(c.servers["srv-a"].archive, s.path)) for s in there)


def test_a_destination_with_no_room_is_not_where_the_problem_goes():
    """Two tight servers must not trade gigabytes. The destination's free space comes from
    its own heartbeat and is read BEFORE anything is sent; a destination that cannot take
    the batch is skipped, and the floor below answers instead."""
    c = Cluster()
    c.vars.put(SPACE_KEY, {"enabled": "true", "high": "0.85", "low": "0.75", "min_days": "3"}, cas=0)
    t = c.wall()
    for i in range(4):
        _segment(c.servers["srv-b"], "1", 5, t - 4000 + i * 600, size=50_000)
    _writer(c, "srv-a", "1", t)
    _hb(c, "srv-a", free=1_000)                                   # srv-a is tight too
    b = _hb(c, "srv-b", free=500_000)
    b.space_probe = lambda root: (1_000_000, 100_000)

    rep = b.relieve()
    assert rep["rec.skipped"] == {"1": "srv-a has no room"} and rep["rec.evacuated"] == 0
    assert len(Manifest(c.servers["srv-b"].archive, "1").read()) == 4          # nothing sent, nothing deleted
    # and nothing cut either: an hour of footage is under the three-day floor, so the honest
    # answer is a shortfall said out loud rather than a quiet cut into yesterday
    assert rep["rec.cut"] == 0 and rep["rec.shortfall"] == 150_000 and rep["short"] == 150_000


def test_over_the_floor_the_deepest_unit_gives_up_its_oldest():
    """Nothing foreign, nothing to evacuate: the cut. Not the oldest segments on the disk —
    that empties the camera with the longest retention, which is the one that was paid for."""
    c = Cluster()
    srv = c.servers["srv-b"]; t = c.wall()
    for i in range(10):                                           # camera 1: ten days deep
        _segment(srv, "1", 1, t - (10 - i) * 86400, size=50_000)
    for i in range(4):                                            # camera 2: four days
        _segment(srv, "2", 1, t - (4 - i) * 86400, size=50_000)
    assert round(depth_days(srv.resource, "1", t)) == 10 and round(depth_days(srv.resource, "2", t)) == 4

    rep = cut(srv.resource, 150_000, t, min_days=3)
    assert rep["removed"] == 3 and rep["freed"] == 150_000
    assert len(Manifest(srv.archive, "1").read()) == 7 and len(Manifest(srv.archive, "2").read()) == 4
    assert round(depth_days(srv.resource, "1", t)) == 7           # the deepest gave up its oldest, three times

    rep = cut(srv.resource, 10_000_000, t, min_days=3)            # ask for more than there is above the floor
    assert rep["freed"] < 10_000_000
    assert depth_days(srv.resource, "1", t) <= 4 and depth_days(srv.resource, "2", t) <= 4   # both at the floor, neither below


def test_nothing_is_deleted_on_a_204_alone():
    """The deletion follows an observed fact, not an answer. If the destination cannot be
    asked what it holds, the segments stay here — a copy that may not have arrived is a
    copy we still have. The next pass asks again."""
    c = Cluster()
    c.vars.put(SPACE_KEY, {"enabled": "true", "high": "0.85", "low": "0.75", "min_days": "3"}, cas=0)
    t = c.wall()
    for i in range(4):
        _segment(c.servers["srv-b"], "1", 5, t - 4000 + i * 600, size=50_000)
    _writer(c, "srv-a", "1", t)
    _hb(c, "srv-a", free=500_000)

    class Mute(DirPeers):
        """Takes the bytes, says 204, and then goes quiet when asked what it has."""
        def get_raw(self, url, path):
            raise IOError("srv-a stopped answering")

    b = cluster_resource(c.servers["srv-b"].resource, "srv-b", "http://srv-b", c.vars, c.objects,
                         wall=c.wall, peers=Mute(c))
    b.space_probe = lambda root: (1_000_000, 100_000)
    rep = b.relieve()
    assert rep["rec.evacuated"] == 0 and rep["freed"] == 0
    assert len(Manifest(c.servers["srv-b"].archive, "1").read()) == 4     # still here, file and line
    assert all(os.path.isfile(os.path.join(c.servers["srv-b"].archive, s.path))
               for s in Manifest(c.servers["srv-b"].archive, "1").read())


def test_the_round_trip_a_server_leaves_comes_back_and_takes_its_footage_with_it():
    """The scenario end to end, with nothing in it that knows it is a scenario.

    srv-a goes away; the recording continues on srv-b. srv-a comes back; the recording goes
    home because its row names the server whose archive holds it, and the camera follows the
    recording, because the recording writes to a disk and a disk does not move. The footage
    written on srv-b stays there — it is playable and nobody is short of room — until srv-b's
    own disk goes over its mark, and then srv-b sends it to where the recording lives now.

    Four mechanisms, no coordinator, and not one of them mentions an outage."""
    from cluster.controller import ClusterController
    from vms.config import REC_SPEC
    from vms.worker import FakeActuator
    c = Cluster()
    c.vars.put(SPACE_KEY, {"enabled": "true", "high": "0.85", "low": "0.75", "min_days": "3"}, cas=0)
    ctl = ClusterController(c.vars, c.objects, wall=c.wall)
    rec = SpecController(REC_SPEC, c.vars, c.objects, wall=c.wall)
    rs = {s: _hb(c, s, free=500_000) for s in c.servers}
    ctl.create_camera({"source": "driverpack://file/1.mp4"})
    rec.create({"cam": "1", "home": "srv-a"})            # the operator names the disk the footage lives on

    # at home: the camera on srv-a, the recording beside it because `near: vms`
    a = c.worker(1, "srv-a"); a.heartbeat_once(); ctl.ensure_placed(); a.reconcile_once(); a.heartbeat_once()
    ra = c.recorder(1, "srv-a"); ra.heartbeat_once(); rec.ensure_placed(); ra.reconcile_once(); ra.heartbeat_once()
    assert rec.where("1") == "r-1" and "at home on srv-a" in rec.placement("1").reason
    assert ctl.where(1) == "w-1"

    # srv-a leaves. Lesson 4 has the failover itself; what matters here is that the work
    # continues on srv-b — the recording moves because its spec requires a resource and
    # srv-a's has gone silent.
    b = c.worker(2, "srv-b"); rb = c.recorder(2, "srv-b")
    for _ in range(2):                                     # two silences from one server: a fact about the server
        c.wall.advance(2 * 45 + 3)
        b.heartbeat_once(); rb.heartbeat_once()
        rs["srv-b"].heartbeat(); rs["srv-c"].heartbeat()
    ctl.move(1, "w-2", "srv-a gone")                       # the camera: Lesson 4's business
    assert rec.gone_servers() == {"r-1": "srv-a"}
    assert [(m[1], m[2]) for m in rec.redistribute()] == [("r-1", "r-2")]
    b.reconcile_once(); b.heartbeat_once(); rb.reconcile_once(); rb.heartbeat_once()
    assert rec.where("1") == "r-2"
    t = c.wall()
    for i in range(4):                                     # four segments written on srv-b, under its epoch
        _segment(c.servers["srv-b"], "1", 2, t - 4000 + i * 600, size=50_000)

    # srv-a comes back, and nothing is asked to "recover"
    for r in rs.values(): r.heartbeat()
    a.heartbeat_once(); ra.heartbeat_once()
    moves = rec.ensure_home(1)                             # the recording, because its row names a home
    assert [(m[1], m[2]) for m in moves] == [("r-2", "r-1")]
    assert "home is srv-a" in rec.placement("1").reason
    ra.reconcile_once(); ra.heartbeat_once()
    assert ctl.ensure_home(1) == [(1, "w-2", "w-1")]        # the camera, because it follows the recording
    assert "it follows rec onto srv-a" in ctl.placement(1).reason

    # and the footage on srv-b: nothing happens while srv-b has room
    bres = cluster_resource(c.servers["srv-b"].resource, "srv-b", "http://srv-b", c.vars, c.objects,
                            wall=c.wall, peers=DirPeers(c))
    bres.space_probe = lambda root: (1_000_000, 500_000)
    assert bres.relieve()["space"] == "ok"
    assert len(Manifest(c.servers["srv-b"].archive, "1").read()) == 4

    bres.space_probe = lambda root: (1_000_000, 100_000)   # until srv-b needs the room
    rep = bres.relieve()
    assert rep["rec.evacuated"] == 3 and rep["short"] == 0
    assert len(Manifest(c.servers["srv-a"].archive, "1").read()) == 3
    assert [s.epoch for s in Manifest(c.servers["srv-a"].archive, "1").read()] == [2, 2, 2]
