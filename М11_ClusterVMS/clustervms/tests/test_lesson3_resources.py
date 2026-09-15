"""Lesson 3 — what stays on the server, and what does not. Configuration is
already in raft (the RPO is zero); footage stays on the resource — under
rec/<cam>, the recorder's tree; the manifest returns with it; a timeline
spans two resources and names the one that is unreachable."""
import json
import os
from cluster.controller import ClusterController
from cluster.resource import cluster_resource, resources_seen
from psimplatform.spec import SpecController
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
    assert rep == {"rec.added": 0, "rec.dropped": 1, "rec.media_removed": 1, "removed": 0, "enabled": False, "mirrored": 0, "peers": []}
    assert Manifest(srv.archive, 1).read() == []


def test_a_worker_with_no_assignment_invents_nothing():
    c = Cluster(); w = c.worker(5, "srv-c")
    assert w.reconcile_once() == [] and w.name == "w-5"
    w.heartbeat_once()
    hb = json.loads(c.objects.get("vms/w-5/heartbeat"))
    assert hb["status"] == [] and hb["headroom"] == 50
