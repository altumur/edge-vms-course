"""Lesson 5 — the controller as a job: placement under label constraints,
the directory in one scan, two controllers agreeing, the snapshot that
leaves the cluster, and the console over real HTTP."""
import json
import threading
import urllib.request
from cluster.console import serve
from cluster.controller import ClusterController
from cluster.directory import Directory
from tests.conftest import Cluster


def _three_workers(c, ctl):
    ws = {"w-0": c.worker(0, "srv-a"), "w-1": c.worker(1, "srv-b"), "w-2": c.worker(2, "srv-c")}
    for w in ws.values(): w.heartbeat_once()
    return ws


def test_placement_under_label_constraints():
    c = Cluster(); ctl = ClusterController(c.vars, c.objects, capacity=10, wall=c.wall)
    _three_workers(c, ctl)
    a = ctl.create_camera({"source": "driverpack://file/a.mp4", "labels": ["vlan:cctv-a"]})
    b = ctl.create_camera({"source": "driverpack://file/b.mp4", "labels": ["vlan:cctv-b"]})
    ab = ctl.create_camera({"source": "driverpack://file/ab.mp4", "labels": ["vlan:cctv-a", "vlan:cctv-b"]})
    x = ctl.create_camera({"source": "driverpack://file/x.mp4", "labels": ["vlan:cctv-x"]})
    ctl.ensure_placed()
    assert ctl.where(a["id"]) in ("w-0", "w-1") and ctl.where(b["id"]) in ("w-1", "w-2") and ctl.where(ab["id"]) == "w-1"
    assert "reaching vlan:cctv-a,vlan:cctv-b" in ctl.placement(ab["id"]).reason and "on srv-b" in ctl.placement(ab["id"]).reason
    assert ctl.where(x["id"]) is None and ctl.unplaceable() == [{"id": x["id"], "labels": ["vlan:cctv-x"], "workers_live": 3}]


def test_adding_a_worker_moves_nothing_even_with_constraints():
    c = Cluster(); ctl = ClusterController(c.vars, c.objects, capacity=2, wall=c.wall)
    c.worker(0, "srv-a", capacity=2).heartbeat_once()
    for i in range(3):
        ctl.create_camera({"source": f"driverpack://file/{i}.mp4", "labels": ["vlan:cctv-a"]})
    ctl.ensure_placed(); before = {i: ctl.where(i) for i in (1, 2, 3)}
    assert before == {1: "w-0", 2: "w-0", 3: None}
    c.worker(1, "srv-b", capacity=2).heartbeat_once(); ctl.ensure_placed()
    assert {i: ctl.where(i) for i in (1, 2)} == {1: "w-0", 2: "w-0"} and ctl.where(3) == "w-1"


def test_where_is_camera_7_in_one_scan():
    c = Cluster(); ctl = ClusterController(c.vars, c.objects, wall=c.wall)
    _three_workers(c, ctl)
    for i in range(9):
        ctl.create_camera({"source": f"driverpack://file/{i}.mp4"})
    ctl.ensure_placed()
    d = Directory(c.vars, ttl=5.0, clock=c.clock)
    assert d.where(7) == ctl.where(7) and d.scans == 1
    for i in range(1, 10):
        assert d.where(i) == ctl.where(i)
    assert d.scans == 1                                                    # nine answers, one scan
    assert sorted(sum((d.holdings(w) for w in ("w-0", "w-1", "w-2")), [])) == list(range(1, 10))


def test_two_controllers_agree_under_constraints():
    c = Cluster()
    _three_workers(c, ClusterController(c.vars, c.objects, wall=c.wall))
    a = ClusterController(c.vars, c.objects, capacity=100, wall=c.wall)
    b = ClusterController(c.vars, c.objects, capacity=100, wall=c.wall)
    for i in range(40):
        a.create_camera({"source": f"driverpack://file/{i}.mp4", "labels": ["vlan:cctv-b"] if i % 2 else []})
    ts = [threading.Thread(target=x.ensure_placed) for x in (a, b, a, b)]
    [t.start() for t in ts]; [t.join() for t in ts]
    where = {i: a.where(i) for i in range(1, 41)}
    assert all(where.values())
    units = sum((a.assignment(w).units for w in ("w-0", "w-1", "w-2")), [])
    assert sorted(int(u) for u in units) == list(range(1, 41))             # each camera in exactly one assignment
    assert all(where[i] != "w-0" for i in range(2, 41, 2))                 # cctv-b cameras never on srv-a


def test_the_snapshot_is_the_only_thing_that_leaves_the_cluster():
    c = Cluster(); ctl = ClusterController(c.vars, c.objects, wall=c.wall, cluster="north")
    _three_workers(c, ctl)
    ctl.create_camera({"source": "driverpack://file/1.mp4", "name": "gate"}); ctl.ensure_placed()
    ctl.publish_snapshot()
    snap = json.loads(c.objects.get("vms/snapshot"))
    assert snap["cluster"] == "north" and snap["cameras"][0]["name"] == "gate" and snap["cameras"][0]["server"] in ("srv-a", "srv-b", "srv-c")
    assert snap["ts"] == c.wall()                                          # a copy, with an age — the domain's RPO is this


def test_the_console_over_http():
    c = Cluster(); ctl = ClusterController(c.vars, c.objects, wall=c.wall)
    ws = _three_workers(c, ctl)
    srv = serve(ctl, "127.0.0.1", 0, worst_failover=48.0, archive_root=c.servers["srv-a"].archive); port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    def call(method, path, body=None, headers=None):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r: return r.status, r.read().decode()
        except urllib.error.HTTPError as e: return e.code, e.read().decode()
    st, out = call("POST", "/cameras", {"source": "driverpack://file/1.mp4", "labels": ["vlan:cctv-b"]}, {"Idempotency-Key": "k1"})
    assert st == 201 and json.loads(out)["worker"] is None                    # the console wrote the row; placement is the controller's
    assert call("POST", "/cameras", {"source": "driverpack://file/1.mp4"}, {"Idempotency-Key": "k1"})[0] == 201 and len(ctl.cameras()) == 1
    assert call("PUT", "/cameras/1", {"worker": "w-0"})[0] == 400
    placed = ctl.ensure_placed()[0].worker; assert placed in ("w-1", "w-2")   # the controller's pass, under the label
    ws[placed].reconcile_once(); ws[placed].heartbeat_once()
    st, out = call("GET", "/where/1"); d = json.loads(out)
    assert st == 200 and d["worker"] == d["directory"] and "on srv-" in d["reason"]
    st, out = call("GET", "/metrics")
    assert 'vms_failover_seconds{kind="worst"} 48.0' in out and "vms_workers_live 3" in out and "vms_cameras_recording 1" in out
    st, out = call("GET", "/resources"); assert st == 200 and json.loads(out) == {}
    st, out = call("GET", "/unplaceable"); assert json.loads(out) == []
    # an operator's mark: the console's own bucket on srv-a's resource, found by the index on its `cam` field
    st, out = call("POST", "/marks", {"cam": 1, "note": "check the gate"}, {"Idempotency-Key": "m1", "X-User": "murat"})
    m = json.loads(out); assert st == 201 and m["bucket"].startswith(f"console/{m['unit']}/e1/")
    from cluster.resource import cluster_resource, resources_seen
    from psimplatform.eventindex import EventIndex
    from tests.test_lesson3_events import DirReader
    cluster_resource(c.servers["srv-a"].resource, "srv-a", "http://srv-a", c.vars, c.objects, wall=c.wall).heartbeat()
    idx = EventIndex(DirReader(c), wall=c.wall); idx.rebuild(resources_seen(c.objects))
    ev = idx.query(0, 1e12, cam=1)["events"]
    assert [(e["subsystem"], e["kind"], e["user"]) for e in ev] == [("console", "mark", "murat")]
    # the page, and playback across the cluster: a segment on srv-a's resource, served through the console by server
    assert "<video" in call("GET", "/")[1] and "/segment/" in call("GET", "/")[1]
    from psimplatform.resource import serve as serve_resource
    from cluster.resource import vms_routes
    from tests.test_lesson3_resources import _segment
    _segment(c.servers["srv-a"], 1, 1, c.wall() - 600, size=256)
    res = cluster_resource(c.servers["srv-a"].resource, "srv-a", "http://127.0.0.1:0", c.vars, c.objects, wall=c.wall)
    rsrv = serve_resource(res, "127.0.0.1", 0, extra=vms_routes(c.servers["srv-a"].resource)); res.url = f"http://127.0.0.1:{rsrv.server_address[1]}"; res.heartbeat()
    st, out = call("GET", "/timeline/1"); seg = json.loads(out)["segments"][0]
    assert st == 200 and seg["server"] == "srv-a"
    req = urllib.request.Request(f"{base}/segment/{seg['path']}?server=srv-a", headers={"Range": "bytes=0-9"})
    with urllib.request.urlopen(req) as r:
        assert r.status == 206 and len(r.read()) == 10 and r.headers["Content-Range"] == "bytes 0-9/256"
    assert call("GET", f"/segment/{seg['path']}?server=srv-b")[0] == 404          # srv-b has never heartbeaten
    assert call("PUT", "/cameras/1", {"enabled": False})[0] == 200 and ctl.camera(1)["enabled"] is False
    assert call("DELETE", "/cameras/1")[0] == 200 and ctl.cameras() == [] and call("DELETE", "/cameras/1")[0] == 404
    assert ctl.unplace_deleted() == [1] and ctl.assignment(placed).units == []   # the controller takes the placement back
    rsrv.shutdown()
    srv.shutdown()
