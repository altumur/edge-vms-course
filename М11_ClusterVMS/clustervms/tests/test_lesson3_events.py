"""Lesson 3, second half — events. Written by the worker holding a unit's
epoch into the unit's bucket on its server's resource; any subsystem, its
own prefix; indexed by a job that is a cache and proves it by being
deleted and rebuilt; unavailable — by name — when the resource is, never
lost; a detector's event about camera 7 found by a field, not by living in
camera 7's bucket."""
import os
from datetime import datetime, timezone
from psimplatform.eventindex import EventIndex
from cluster.resource import cluster_resource, peers_of, resources_seen
from vms.archive import Manifest, event_log, segment_path
from psimplatform.events import EventLog, buckets_under, read_bucket, subsystems_under
from tests.conftest import Cluster

B = 600


class DirReader:
    """The index's reader and the resources' peer client, against directories instead of HTTP."""
    def __init__(self, c): self.c = c
    def _srv(self, url):
        s = self.c.servers[url.rsplit("/", 1)[1]]
        if getattr(s, "down", False): raise ConnectionError(s.name)
        return s
    def buckets(self, url, sub, unit): return buckets_under(self._srv(url).archive, sub, unit, B)
    def events(self, url, b): return read_bucket(os.path.join(self._srv(url).archive, b.path))
    # the mirror, as the index reads it
    def mirrored(self, url, server):
        from psimplatform.resource import mirrored_buckets
        return mirrored_buckets(self._srv(url).archive, server, B)
    def mirrored_events(self, url, server, b): return read_bucket(os.path.join(self._srv(url).archive, ".mirror", server, b.path))
    # the mirror, as a resource writes and restores it (PeerClient's three calls)
    def put(self, url, server, path, data):
        dest = os.path.join(self._srv(url).archive, ".mirror", server, path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f: f.write(data)
    def get(self, url, server, path):
        with open(os.path.join(self._srv(url).archive, ".mirror", server, path), "rb") as f: return f.read()


def _media(c, server, cam, epoch, start):
    srv = c.servers[server]
    p = segment_path(srv.spool, cam, epoch, datetime.fromtimestamp(start, timezone.utc))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f: f.write(b"x" * 1000)
    os.utime(p, (start + B, start + B))
    return srv.resource.promote(p)


def _observe(c, server, sub, unit, epoch, t, kind, **fields):
    """A worker of `sub` holding `unit`'s epoch on `server` observed something."""
    return EventLog(c.servers[server].archive, sub, unit, epoch, B).append(t, kind, **fields)


def _resources(c, peers=None):
    rs = {s: cluster_resource(srv.resource, s, f"http://{s}", c.vars, c.objects, wall=c.wall, peers=peers) for s, srv in c.servers.items()}
    for r in rs.values(): r.heartbeat()
    return rs


def test_events_are_indexed_across_resources_and_subsystems_and_the_index_is_a_cache():
    c = Cluster(); t = c.wall() - 3600
    _media(c, "srv-a", 7, 3, t)
    _observe(c, "srv-a", "vms", "7", 3, t + 12, "motion", zone="gate")            # the VMS worker, recording camera 7
    _observe(c, "srv-a", "vms", "7", 3, t + 40, "silent")
    _observe(c, "srv-b", "vms", "7", 4, t + 1205, "motion")                        # after a failover: next epoch, other server, not recording
    _observe(c, "srv-c", "det", "d-12", 1, t + 30, "person", cam=7, score=0.9)   # a detector on a GPU server, ABOUT camera 7
    _observe(c, "srv-c", "counter", "a", 1, t + 5, "round", value=10)             # a third subsystem, its own prefix
    hbs = _resources(c)
    assert resources_seen(c.objects)["srv-c"]["units"] == {"counter": ["a"], "det": ["d-12"]}
    idx = EventIndex(DirReader(c), wall=c.wall)
    rep = idx.rebuild(resources_seen(c.objects))
    assert rep == {"added": 5, "unreachable": [], "from_mirror": [], "segments": 4} and idx.state == "live"
    q = idx.query(t, t + 3600, cam=7, current_epochs={("vms", "7"): 4})
    assert [(e["subsystem"], e["unit"], e["kind"], e["server"], e["epoch"], e["fenced"]) for e in q["events"]] == [
        ("vms", "7", "motion", "srv-a", 3, True), ("det", "d-12", "person", "srv-c", 1, False),
        ("vms", "7", "silent", "srv-a", 3, True), ("vms", "7", "motion", "srv-b", 4, False)]
    assert q["events"][1]["score"] == 0.9 and q["events"][1]["bucket"].startswith("det/d-12/e1/")   # found by the field; it lives in ITS bucket
    assert [e["value"] for e in idx.query(t, t + 3600, subsystem="counter")["events"]] == [10]
    assert [e["cam"] for e in idx.query(t, t + 3600, kind="motion")["events"]] == [7, 7]
    # the index is a cache: a new instance after a failover rebuilds to the same answer from the resources alone
    idx2 = EventIndex(DirReader(c), wall=c.wall); idx2.rebuild(resources_seen(c.objects))
    assert idx2.query(t, t + 3600)["events"] == idx.query(t, t + 3600)["events"]
    # tail: a new bucket on srv-c
    _observe(c, "srv-c", "det", "d-12", 1, t + 700, "person", cam=9); hbs["srv-c"].heartbeat()
    assert idx.tail(resources_seen(c.objects))["added"] == 1


def test_a_dead_resource_makes_the_answer_incomplete_by_name_not_wrong():
    c = Cluster(); t = c.wall() - 3600
    _observe(c, "srv-a", "vms", "7", 3, t + 12, "motion")
    _observe(c, "srv-b", "vms", "8", 1, t + 20, "motion")
    hbs = _resources(c)
    c.wall.advance(60); hbs["srv-b"].heartbeat(); hbs["srv-c"].heartbeat()                  # srv-a went silent
    idx = EventIndex(DirReader(c), wall=c.wall)                                     # a fresh eventindex, after a failover of its own
    rep = idx.rebuild(resources_seen(c.objects))
    assert rep["added"] == 1 and rep["unreachable"] == ["srv-a"] and idx.state == "live; srv-a unreachable"
    assert [e["cam"] for e in idx.query(t, t + 3600)["events"]] == [8]              # srv-a's events are unavailable, and the state says so
    hbs["srv-a"].heartbeat()
    assert idx.tail(resources_seen(c.objects))["added"] == 1 and idx.state == "live"   # back with its disks: indexed, not rebuilt
    # retention on srv-b removed a bucket: the index forgets, by (server, path)
    b = buckets_under(c.servers["srv-b"].archive, "vms", "8", B)[0]
    assert idx.forget("srv-b", [b.path]) == 1 and [e["cam"] for e in idx.query(t, t + 3600)["events"]] == [7]
    assert c.vars.list("vms/events") == [] and c.objects.list("vms/events") == []   # no controller, no database, wrote an event


def test_the_resource_policy_retains_each_subsystems_buckets_by_its_own_row():
    from cluster.controller import ClusterController
    c = Cluster(); ctl = ClusterController(c.vars, c.objects, wall=c.wall); srv = c.servers["srv-a"]
    ctl.create_camera({"source": "driverpack://file/7.mp4", "retention_days": 1, "events_retention_days": 30})
    assert c.vars.get("vms/retention/1")[0] == {"days": "30"}                     # the VMS's policy for its unit, as a row the platform reads
    now = c.wall()
    p1 = _observe(c, "srv-a", "vms", "1", 1, now - 40 * 86400, "motion")       # older than the VMS's policy
    p2 = _observe(c, "srv-a", "vms", "1", 1, now - 3600, "motion")             # recent
    p3 = _observe(c, "srv-a", "det", "d-1", 1, now - 400 * 86400, "person")   # another subsystem: a year by default
    for p in (p1, p2, p3): os.utime(p, (now - 100, now - 100))
    res = cluster_resource(srv.resource, "srv-a", "http://srv-a", c.vars, c.objects, wall=c.wall)
    rep = res.pass_()
    assert rep["vms.added"] == 2 and rep["removed"] == 2 and os.path.exists(p2) and not os.path.exists(p1) and not os.path.exists(p3)
    assert len(Manifest(srv.archive, 1).buckets()) == 2                         # the VMS's lines: its pass ran before the platform removed the file...
    assert res.pass_()["vms.dropped"] == 1 and len(Manifest(srv.archive, 1).buckets()) == 1   # ...and drops it on the next pass


def test_the_events_knob_is_a_peer_copy_and_the_owner_restores():
    """The storage knob's events row, as a copy between resources — no store in
    between. Off: a silent server's events are unavailable by name. On: each
    resource copied its CLOSED buckets to the next live resource after it, and
    a fresh index answers completely from the peer, saying so. Back with an
    empty disk, the owner pulls its buckets home; nobody else ever writes them."""
    from psimplatform.resource import MIRROR_KEY, mirrored_buckets
    import shutil
    assert peers_of("srv-a", ["srv-a", "srv-b", "srv-c"], 1) == ["srv-b"] and peers_of("srv-c", ["srv-a", "srv-b", "srv-c"], 1) == ["srv-a"]
    assert peers_of("srv-b", ["srv-a", "srv-b", "srv-c"], 2) == ["srv-c", "srv-a"] and peers_of("srv-a", ["srv-a"], 1) == []
    c = Cluster(); t = c.wall() - 7200; rd = DirReader(c)
    _observe(c, "srv-a", "vms", "7", 3, t + 12, "motion", zone="gate")
    _observe(c, "srv-a", "det", "d-12", 1, t + 30, "person", cam=7)
    _observe(c, "srv-a", "vms", "7", 3, t + 6800, "motion")                      # in the OPEN bucket: not closed, not mirrored
    _observe(c, "srv-b", "vms", "8", 1, t + 20, "motion")
    pol = hbs = _resources(c, peers=rd)
    assert pol["srv-a"].pass_()["enabled"] is False and mirrored_buckets(c.servers["srv-b"].archive, "srv-a") == []   # knob off: nothing leaves
    c.vars.put(MIRROR_KEY, {"enabled": "true", "copies": "1"})                                       # the knob: one Variable
    r = pol["srv-a"].pass_(); assert (r["mirrored"], r["peers"]) == (2, ["srv-b"])                        # a -> b, closed buckets only
    r = pol["srv-b"].pass_(); assert (r["mirrored"], r["peers"]) == (1, ["srv-c"])                        # b -> c
    assert pol["srv-a"].pass_()["mirrored"] == 0                                                          # exactly once: the peer said what it holds
    for hb in hbs.values(): hb.heartbeat()
    assert resources_seen(c.objects)["srv-b"]["mirrors"] == {"srv-a": 2} and resources_seen(c.objects)["srv-c"]["mirrors"] == {"srv-b": 1}
    assert [b.path for b in mirrored_buckets(c.servers["srv-b"].archive, "srv-a")][0].startswith("det/d-12/e1/")   # the ORIGINAL path, under .mirror/srv-a/
    assert ".mirror" not in subsystems_under(c.servers["srv-b"].archive)
    # srv-a dies
    c.wall.advance(60); hbs["srv-b"].heartbeat(); hbs["srv-c"].heartbeat()
    idx = EventIndex(rd, wall=c.wall)                                                                # a fresh index after its own failover
    rep = idx.rebuild(resources_seen(c.objects))
    assert rep["unreachable"] == [] and rep["from_mirror"] == ["srv-a"] and rep["added"] == 3 and idx.state == "live; srv-a from mirror"
    ev = idx.query(t, t + 7200, cam=7)["events"]
    assert [(e["subsystem"], e["kind"], e["server"]) for e in ev] == [("vms", "motion", "srv-a"), ("det", "person", "srv-a")]   # complete; the open bucket is the RPO
    # srv-a returns — with a REPLACED, empty disk
    shutil.rmtree(c.servers["srv-a"].archive); os.makedirs(c.servers["srv-a"].archive)
    hbs["srv-a"].heartbeat()
    r = pol["srv-a"].restore()
    assert r["pulled"] == 2 and r["vms.added"] == 1                                                  # its two closed buckets are home; the vms manifest line rebuilt
    assert [b.path for b in buckets_under(c.servers["srv-a"].archive, "vms", "7", B)] == [ev[0]["bucket"]]
    hbs["srv-a"].heartbeat()
    rep = idx.tail(resources_seen(c.objects))
    assert rep["added"] == 0 and rep["from_mirror"] == [] and idx.state == "live"                    # seen already; the resource is the source again
    assert c.objects.list("platform/mirror") == [] and c.vars.list("vms/mirror") == []               # no store in between, ever; and not the VMS's knob
