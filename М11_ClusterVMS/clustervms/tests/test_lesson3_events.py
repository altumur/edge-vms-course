"""Lesson 3, second half — events. Written by the worker holding a unit's
epoch into the unit's bucket on its server's resource; any subsystem, its
own prefix; indexed by EACH RESOURCE over its own tree — a cache that proves
it by being deleted and rebuilt — and merged by the console, which holds
none; unavailable — by name — when the resource is, never lost; a detector's
event about camera 7 found by a field, not by living in camera 7's bucket."""
import os
from datetime import datetime, timezone
from psimplatform.eventdatabase import EventDatabase, MergedIndex
from cluster.resource import cluster_resource, peers_of, resources_seen
from vms.archive import Manifest, event_log, segment_path
from psimplatform.events import EventLog, buckets_under, subsystems_under
from tests.conftest import Cluster

B = 600


class DirReader:
    """The resources' peer client, against directories instead of HTTP (PeerClient's three calls)."""
    def __init__(self, c): self.c = c
    def _srv(self, url):
        s = self.c.servers[url.rsplit("/", 1)[1]]
        if getattr(s, "down", False): raise ConnectionError(s.name)
        return s
    def mirrored(self, url, server):
        from psimplatform.resource import mirrored_buckets
        return mirrored_buckets(self._srv(url).archive, server, B)
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


def _index(c, rs, server):
    """What the resource job does after restore: its EventDatabase over ITS tree, rebuilt; the job's `/events` answers from it."""
    return rs[server].database.rebuild()


def _merged(c, rs):
    """The console's side: no index — `GET <resource>/events` on every live resource, here a call instead of HTTP."""
    def fetch(url, p):
        s = url.rsplit("/", 1)[1]
        if getattr(c.servers[s], "down", False):
            raise ConnectionError(s)
        return rs[s].database.query(float(p["from"]), float(p["to"]), int(p["cam"]) if "cam" in p else None, p.get("kind"),
                                 p.get("subsystem"), p.get("unit"), limit=int(p.get("limit", 1000)))
    return MergedIndex(c.objects, fetch=fetch, wall=c.wall)


def test_events_are_indexed_by_each_resource_and_merged_by_the_console_which_holds_none():
    c = Cluster(); t = c.wall() - 3600
    _media(c, "srv-a", 7, 3, t)
    _observe(c, "srv-a", "vms", "7", 3, t + 12, "motion", zone="gate")            # the VMS worker, recording camera 7
    _observe(c, "srv-a", "vms", "7", 3, t + 40, "silent")
    _observe(c, "srv-b", "vms", "7", 4, t + 1205, "motion")                        # after a failover: next epoch, other server, not recording
    _observe(c, "srv-c", "det", "d-12", 1, t + 30, "person", cam=7, score=0.9)   # a detector on a GPU server, ABOUT camera 7
    _observe(c, "srv-c", "lpr", "lane-1", 1, t + 5, "plate", plate="AB123")      # a third subsystem, its own prefix
    rs = _resources(c)
    assert resources_seen(c.objects)["srv-c"]["units"] == {"det": ["d-12"], "lpr": ["lane-1"]}
    # one database per resource, over its own tree only — nothing cluster-wide
    reps = {s: _index(c, rs, s) for s in rs}
    assert reps["srv-a"] == {"added": 2, "segments": 1, "mirrored": []} and rs["srv-a"].database.state == "live"
    assert reps["srv-c"] == {"added": 2, "segments": 2, "mirrored": []}
    assert [e["server"] for e in rs["srv-a"].database.query(t, t + 3600)["events"]] == ["srv-a", "srv-a"]
    # the console merges by time, and fences by the epochs only the cluster's rows know
    m = _merged(c, rs)
    q = m.query(t, t + 3600, cam=7, current_epochs={("vms", "7"): 4})
    assert q["state"] == "live" and [(e["subsystem"], e["unit"], e["kind"], e["server"], e["epoch"], e["fenced"]) for e in q["events"]] == [
        ("vms", "7", "motion", "srv-a", 3, True), ("det", "d-12", "person", "srv-c", 1, False),
        ("vms", "7", "silent", "srv-a", 3, True), ("vms", "7", "motion", "srv-b", 4, False)]
    assert q["events"][1]["score"] == 0.9 and q["events"][1]["bucket"].startswith("det/d-12/e1/")   # found by the field; it lives in ITS bucket
    assert [e["plate"] for e in m.query(t, t + 3600, subsystem="lpr")["events"]] == ["AB123"]
    assert [e["cam"] for e in m.query(t, t + 3600, kind="motion")["events"]] == [7, 7]
    # the database is a cache: a resource job restarted rebuilds to the same answer from its tree alone
    before = rs["srv-a"].database.query(t, t + 3600)["events"]
    again = EventDatabase(c.servers["srv-a"].archive, "srv-a", wall=c.wall, bucket_seconds=B)
    assert again.rebuild()["added"] == 2 and again.query(t, t + 3600)["events"] == before
    # tail: a new bucket on srv-c is on the merged timeline after srv-c's next tail — nobody else is told
    _observe(c, "srv-c", "det", "d-12", 1, t + 700, "person", cam=9)
    assert rs["srv-c"].database.tail()["added"] == 1 and [e["cam"] for e in m.query(t, t + 3600, kind="person")["events"]] == [7, 9]


def test_a_dead_resource_makes_the_answer_incomplete_by_name_not_wrong():
    c = Cluster(); t = c.wall() - 3600
    _observe(c, "srv-a", "vms", "7", 3, t + 12, "motion")
    _observe(c, "srv-b", "vms", "8", 1, t + 20, "motion")
    rs = _resources(c)
    for s in rs: _index(c, rs, s)
    c.wall.advance(60); rs["srv-b"].heartbeat(); rs["srv-c"].heartbeat()                  # srv-a went silent
    m = _merged(c, rs)
    assert [e["cam"] for e in m.query(t, t + 3600)["events"]] == [8] and m.state == "live; srv-a unreachable"   # unavailable, and the state says so
    rs["srv-a"].heartbeat()
    assert [e["cam"] for e in m.query(t, t + 3600)["events"]] == [7, 8] and m.state == "live"   # back with its disks: its database answers again, nothing rebuilt
    c.servers["srv-b"].down = True                                                     # live by heartbeat, not answering: named too
    assert [e["cam"] for e in m.query(t, t + 3600)["events"]] == [7] and m.state == "live; srv-b unreachable"
    c.servers["srv-b"].down = False
    # retention on srv-b removed a bucket: ITS database forgets, by (server, path) — the console is not told, it asks
    c.vars.put("vms/retention/8", {"days": "0.01"})
    assert rs["srv-b"].retain() == 1 and [e["cam"] for e in m.query(t, t + 3600)["events"]] == [7]
    assert c.vars.list("vms/events") == [] and c.objects.list("vms/events") == []   # no controller, no database, wrote an event


def test_the_resource_policy_retains_each_subsystems_buckets_by_its_own_row():
    from cluster.controller import ClusterController
    c = Cluster(); ctl = ClusterController(c.vars, c.objects, wall=c.wall); srv = c.servers["srv-a"]
    ctl.create_camera({"source": "driverpack://file/7.mp4", "events_retention_days": 30})       # the camera's events: the VMS row's knob
    assert c.vars.get("vms/retention/1")[0] == {"days": "30"}                     # the VMS's policy for its unit, as a row the platform reads
    assert c.vars.get("rec/recordings/1")[0] is None                              # no recording: the camera is watched, its footage nobody's
    now = c.wall()
    p1 = _observe(c, "srv-a", "vms", "1", 1, now - 40 * 86400, "motion")       # older than the VMS's policy
    p2 = _observe(c, "srv-a", "vms", "1", 1, now - 3600, "motion")             # recent
    p3 = _observe(c, "srv-a", "det", "d-1", 1, now - 400 * 86400, "person")   # another subsystem: a year by default
    for p in (p1, p2, p3): os.utime(p, (now - 100, now - 100))
    res = cluster_resource(srv.resource, "srv-a", "http://srv-a", c.vars, c.objects, wall=c.wall)
    rep = res.pass_()
    assert rep["removed"] == 2 and os.path.exists(p2) and not os.path.exists(p1) and not os.path.exists(p3)
    assert rep["rec.added"] == 0 and rep["rec.media_removed"] == 0 and Manifest(srv.archive, 1).read() == []   # the recorder's pass: no footage here, nothing to do
    assert not os.path.exists(os.path.join(srv.archive, "rec"))                   # events are the worker's tree (vms/); footage would be the recorder's (rec/)


def test_the_events_knob_is_a_peer_copy_and_the_owner_restores():
    """The storage knob's events row, as a copy between resources — no store in
    between. Off: a silent server's events are unavailable by name. On: each
    resource copied its CLOSED buckets to the next live resource after it, and
    a fresh merge answers completely from the peer, saying so. Back with an
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
    # srv-b's own database covers the copies it holds — under srv-a's name, since only the source differs
    rep = _index(c, rs := pol, "srv-b")
    assert rep == {"added": 3, "segments": 3, "mirrored": ["srv-a"]}
    assert [e["server"] for e in rs["srv-b"].database.query(t, t + 7200)["events"]] == ["srv-a", "srv-b", "srv-a"]   # by time; two of them under srv-a's name
    _index(c, rs, "srv-a"); _index(c, rs, "srv-c"); m = _merged(c, rs)
    ev = m.query(t, t + 7200, cam=7)["events"]
    assert [(e["subsystem"], e["kind"], e["server"]) for e in ev] == [("vms", "motion", "srv-a"), ("det", "person", "srv-a"), ("vms", "motion", "srv-a")]
    assert m.state == "live"                                                                         # the owner answers, open bucket included; srv-b's copies are dropped
    # srv-a dies
    c.wall.advance(60); hbs["srv-b"].heartbeat(); hbs["srv-c"].heartbeat()
    ev = m.query(t, t + 7200, cam=7)["events"]
    assert [(e["subsystem"], e["kind"], e["server"]) for e in ev] == [("vms", "motion", "srv-a"), ("det", "person", "srv-a")]   # complete, from the peer; the open bucket is the RPO
    assert m.state == "live; srv-a from mirror"
    # srv-a returns — with a REPLACED, empty disk
    shutil.rmtree(c.servers["srv-a"].archive); os.makedirs(c.servers["srv-a"].archive)
    hbs["srv-a"].heartbeat()
    r = pol["srv-a"].restore()
    assert r["pulled"] == 2 and r["rec.added"] == 0                                                  # its two closed buckets are home; no footage was ever here (rec/ is the recorder's)
    assert [b.path for b in buckets_under(c.servers["srv-a"].archive, "vms", "7", B)] == [ev[0]["bucket"]]
    hbs["srv-a"].heartbeat()
    assert _index(c, rs, "srv-a")["added"] == 2                                                      # its job restarts: the index over the restored tree
    ev2 = m.query(t, t + 7200, cam=7)["events"]
    assert [(e["kind"], e["server"], e["bucket"]) for e in ev2] == [(e["kind"], e["server"], e["bucket"]) for e in ev] and m.state == "live"   # the owner is the source again; no duplicates
    assert c.objects.list("platform/mirror") == [] and c.vars.list("vms/mirror") == []               # no store in between, ever; and not the VMS's knob
