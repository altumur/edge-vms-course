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


def test_a_worker_runs_where_a_resource_answers_and_leaves_when_it_stops():
    """Who guarantees a worker runs where the archive is: Nomad, by `meta.archive`
    on the job — a label. This is the live fact behind the label: the spec says
    `requires: resource`, so a worker whose server's resource has gone silent is
    not placed on, its cameras are moved to servers whose resource answers, and
    the reason says so. A resource never seen is not a fact and passes."""
    from cluster.resource import cluster_resource
    c = Cluster(); ctl = ClusterController(c.vars, c.objects, capacity=10, wall=c.wall)
    ws = _three_workers(c, ctl)
    rs = {s: cluster_resource(srv.resource, s, f"http://{s}", c.vars, c.objects, wall=c.wall) for s, srv in c.servers.items()}
    for r in rs.values(): r.heartbeat()
    a = ctl.create_camera({"source": "driverpack://file/a.mp4", "labels": ["vlan:cctv-a"]})["id"]   # srv-a or srv-b
    b = ctl.create_camera({"source": "driverpack://file/b.mp4", "labels": ["vlan:cctv-a"]})["id"]
    for pl in ctl.ensure_placed():
        assert pl.reason.endswith(", whose resource is live")                                     # the label, and the fact behind it
    # srv-a's disks die: its resource job stops heartbeating; its worker does not — it has nowhere to write
    c.wall.advance(60)
    for w in ws.values(): w.heartbeat_once()
    rs["srv-b"].heartbeat(); rs["srv-c"].heartbeat()
    assert ctl.resource_state("srv-a") == "silent" and ctl.without_resource(["w-0", "w-1", "w-2"]) == ["w-0"]
    on_a = [u for u in ctl.assignment("w-0").units]
    moves = ctl.redistribute()
    assert sorted(m[1] for m in moves) == ["w-0"] * len(on_a) and all(m[2] == "w-1" for m in moves)   # off srv-a, onto srv-b (cctv-a reaches it; srv-c cannot)
    assert ctl.assignment("w-0").units == [] and ctl.placement(a).reason.startswith("resource on srv-a silent; ")
    assert ctl.placement(a).reason.endswith("; on srv-b")
    # the console shows the label beside the fact: what each server's workers record into, and whether its resource answers
    from cluster.console import make_console
    sv = make_console(ctl).root.servers()["servers"]
    assert sv["srv-a"]["resource"] == "silent" and sv["srv-a"]["placeable"] is False and sv["srv-a"]["why"] == "resource on srv-a silent"
    assert sv["srv-b"]["resource"] == "live" and sv["srv-b"]["placeable"] is True and [w["worker"] for w in sv["srv-b"]["workers"]] == ["w-1"]
    assert sv["srv-a"]["archive"] == "/data/archive"                                                # the worker's $ARCHIVE — Nomad's meta.archive on a cluster
    x = ctl.create_camera({"source": "driverpack://file/x.mp4", "labels": ["vlan:cctv-a"]})["id"]
    assert ctl.place(x).worker == "w-1"                                                            # never w-0 while srv-a's resource is silent
    assert ctl.unplaceable() == []                                                                 # srv-b reaches cctv-a too; nothing waits
    # the resource comes back: srv-a is a place to record again; nothing moves back (adding a worker moves nothing)
    rs["srv-a"].heartbeat()
    assert ctl.resource_state("srv-a") == "live" and ctl.redistribute() == [] and ctl.assignment("w-1").units == sorted(map(str, [a, b, x]), key=int)
    # the box has no resource heartbeat before its resource process starts: unknown is not silent
    assert ctl.resource_state("srv-z") == "unknown"


def test_the_snapshot_is_the_only_thing_that_leaves_the_cluster():
    c = Cluster(); ctl = ClusterController(c.vars, c.objects, wall=c.wall, cluster="north")
    ws = _three_workers(c, ctl)
    for i in range(3):
        ctl.create_camera({"source": f"driverpack://file/{i}.mp4", "name": "gate" if i == 0 else f"cam{i}"})
    ctl.ensure_placed()
    ctl.publish_snapshot()
    # ONE OBJECT PER WORKER, the shape the heartbeats already have. Three cameras spread over three
    # workers is three objects — a cluster that grows publishes MORE of them, never a bigger one. That is
    # the whole difference from the single object this used to be, which grew with the cluster under a
    # store that caps an object at 64 KiB.
    keys = c.objects.list("vms/snapshot/")
    assert keys == ["vms/snapshot/" + w for w in sorted(ws)]
    held = [json.loads(c.objects.get(k)) for k in keys]
    rows = [r for sh in held for r in sh["cameras"]]
    assert len(rows) == 3                                                  # every camera, once
    gate = next(r for r in rows if r["name"] == "gate")
    assert gate["server"] in ("srv-a", "srv-b", "srv-c")
    # each shard says whose it is, and every row in it agrees
    assert all(all(r["worker"] == sh["worker"] for r in sh["cameras"]) for sh in held)
    assert all(sh["cluster"] == "north" for sh in held)
    assert all(sh["ts"] == c.wall() for sh in held)                        # a copy, with an age — the domain's RPO is this


def test_the_console_over_http():
    c = Cluster(); ctl = ClusterController(c.vars, c.objects, wall=c.wall)
    ws = _three_workers(c, ctl)
    from w2cplatform.spec import SpecController
    from vms.config import REC_SPEC
    rec_con = SpecController(REC_SPEC, c.vars.as_writer("console", REC_SPEC.acl_console()), c.objects, wall=c.wall)   # the console's door to recordings
    srv = serve(ctl, "127.0.0.1", 0, worst_failover=48.0, archive_root=c.servers["srv-a"].archive, rec_ctl=rec_con); port = srv.server_address[1]
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
    assert 'vms_failover_seconds{kind="worst"} 48.0' in out and "vms_workers_live 3" in out and "vms_cameras_running 1" in out
    st, out = call("GET", "/resources"); assert st == 200 and json.loads(out) == {}
    # the administrator's knob: one row, the console's to write, the controller's to read on its next pass
    st, out = call("GET", "/policy"); assert st == 200 and json.loads(out)["servers"] == "shared"
    assert call("PUT", "/policy", {"servers": "distinct"})[0] == 200 and ctl.policy() == {"servers": "distinct"}
    assert call("PUT", "/policy", {"servers": "everywhere"})[0] == 400 and ctl.policy() == {"servers": "distinct"}
    st, out = call("GET", "/servers"); assert json.loads(out)["policy"] == {"servers": "distinct"}
    call("PUT", "/policy", {"servers": "shared"})
    # the recorder at /rec/…: the page's Record toggle writes a recording row; placement is the rec controller's, not the console's
    st, out = call("GET", "/mounts"); assert json.loads(out) == {"root": "vms", "mounts": {"rec": json.loads(call("GET", "/rec/spec")[1])}}
    st, out = call("POST", "/rec/recordings", {"cam": "1", "retention_days": 7}, {"Idempotency-Key": "r1"}); assert st == 201 and json.loads(out)["worker"] is None
    assert c.vars.get("rec/recordings/1")[0]["retention_days"] == "7" and json.loads(call("GET", "/rec/policy")[1])["servers"] == "distinct"   # the recorder's own knob
    assert call("PUT", "/rec/recordings/1", {"worker": "r-0"})[0] == 400
    assert call("DELETE", "/rec/recordings/1")[0] in (200, 204)
    st, out = call("GET", "/unplaceable"); assert json.loads(out) == []
    # srv-a's resource job, over real HTTP: the platform's routes, the VMS's reads, and the event database over ITS tree
    from w2cplatform.resource import serve as serve_resource
    from cluster.resource import cluster_resource, vms_routes
    from tests.test_lesson3_resources import _segment
    _segment(c.servers["srv-a"], 1, 1, c.wall() - 600, size=256)
    res = cluster_resource(c.servers["srv-a"].resource, "srv-a", "http://127.0.0.1:0", c.vars, c.objects, wall=c.wall)
    rsrv = serve_resource(res, "127.0.0.1", 0, extra=vms_routes(c.servers["srv-a"].resource)); res.url = f"http://127.0.0.1:{rsrv.server_address[1]}"; res.heartbeat()
    res.database.rebuild()
    # an operator's mark: the console's own bucket on srv-a's resource; the console has no database — it asks srv-a's, by HTTP, and finds the `cam` field
    st, out = call("POST", "/marks", {"cam": 1, "note": "check the gate"}, {"Idempotency-Key": "m1", "X-User": "murat"})
    m = json.loads(out); assert st == 201 and m["bucket"].startswith(f"console/{m['unit']}/e1/")
    assert res.database.tail()["added"] == 1
    st, out = call("GET", "/events?cam=1"); ev = json.loads(out)
    assert st == 200 and [(e["subsystem"], e["kind"], e["user"], e["server"]) for e in ev["events"]] == [("console", "mark", "murat", "srv-a")] and ev["state"] == "live"
    # the page, and playback across the cluster: a segment on srv-a's resource, served through the console by server
    assert "<video" in call("GET", "/")[1] and "/segment/" in call("GET", "/")[1]
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
