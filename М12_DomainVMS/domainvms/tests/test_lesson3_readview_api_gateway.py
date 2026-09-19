"""Lesson 3 — the camera list from the workers' heartbeats; staleness shown;
one cause per dead server; the API refuses placement at both levels and is
idempotent; the gateway fans out and the worker sees one viewer."""
import json
import urllib.request
from domain.api import ApiError, ConsoleAPI
from domain.console import Console
from domain.federation import DomainDirectory
from domain.gateway import Forbidden, Gateway, LiveTee, WorkerLiveEndpoint
from domain.readview import ReadView
from tests.conftest import Clock, Running, heartbeat, make_domain, snapshot


def _four_workers(wall):
    fed, links = make_domain({"north": (), "south": ()}, "north")
    n, s = fed.clusters["north"], fed.clusters["south"]
    for cl, w, srv, cams in (("north", "w-0", "srv-1", range(1, 51)), ("north", "w-1", "srv-1", range(51, 101)),
                             ("north", "w-2", "srv-2", range(101, 151)), ("south", "w-0", "srv-9", range(151, 201))):
        heartbeat(fed.clusters[cl], w, list(cams), ts=wall(), server=srv)
    return fed, links


def test_two_hundred_cameras_from_heartbeats_nobody_called():
    wall = Clock(10_000.0); fed, links = _four_workers(lambda: wall() - 3)
    view = ReadView(fed, lost_after=45, wall=wall)
    view.refresh()
    page = view.list(page=1, size=50)
    assert page["total"] == 200 and len(page["rows"]) == 50 and page["complete"]
    assert page["rows"][0]["as_of"] == "as of 3 s ago" and page["rows"][0]["worker_state"] == "live"
    assert view.list(q="cam175")["rows"][0]["cluster"] == "south" and view.list(q="cam175")["rows"][0]["worker"] == "w-0"
    assert view.list(cluster="south")["total"] == 50
    assert view.causes() == []


def test_the_list_from_a_real_cluster():
    """М11's controller and workers running; the domain's view is exactly their heartbeats."""
    fed, _ = make_domain({"north": (), "south": ()}, "north"); wall = Clock(10_000.0)
    n = Running(fed.clusters["north"], wall, workers=(("w-0", "srv-1"), ("w-1", "srv-2")), capacity=2)
    n.create(1, 2, 3)
    view = ReadView(fed, wall=wall); view.refresh()
    rows = view.list(size=10)["rows"]
    assert [r["phase"] for r in rows] == ["running"] * 3 and {r["server"] for r in rows} == {"srv-1", "srv-2"}
    assert {r["worker"] for r in rows} == {"w-0", "w-1"} and rows[0]["observed_revision"] == rows[0]["revision"] == 1


def test_a_camera_nobody_is_running_is_in_the_list_and_says_so():
    """The defect this closes: the list came only from heartbeats, so a camera that
    NOTHING is running was not in it at all. An operator could add a camera, watch
    the cluster fail to place it — no capacity, no worker with the right labels —
    and find nothing in the domain. Not an error, not a greyed row: absence, which
    is the most confusing shape a fault can take.

    A heartbeat is an observation; the snapshot is the configuration. The list now
    carries both and keeps them apart — the same pair М10A's console shows inside
    one cluster as `rows` beside `configured`."""
    fed, _ = make_domain({"north": ()}, "north")
    wall = Clock()
    # two cameras the cluster knows about: one held by a worker, one nobody could place
    snapshot(fed.clusters["north"], {7: ("w-0", "srv-1"), 9: ("", "?")}, ts=wall())
    heartbeat(fed.clusters["north"], "w-0", [7], ts=wall())
    view = ReadView(fed, wall=wall)
    view.refresh()

    rows = {r["ref"] or str(r["camera"]): r for r in view.list()["rows"]}
    assert set(rows) == {"7", "9"}, "the camera nobody runs is missing from the list"
    assert rows["7"]["worker_state"] == "live" and rows["7"]["phase"] == "running"
    assert rows["9"]["worker_state"] == "configured" and rows["9"]["worker"] == ""
    assert "no worker reports it" in rows["9"]["as_of"]
    assert view.list()["total"] == 2


def test_a_camera_is_not_listed_twice_when_both_sources_have_it():
    """The observation wins, because it says more. The two sources name a camera
    differently — the worker's status entry and the cluster's snapshot row — so the
    key is the one the DOMAIN uses, `ref`, and not the cluster's own number, of
    which every cluster has its own."""
    fed, _ = make_domain({"north": (), "south": ()}, "north")
    wall = Clock()
    snapshot(fed.clusters["north"], {7: ("w-0", "srv-1")}, ts=wall())
    heartbeat(fed.clusters["north"], "w-0", [7], ts=wall())
    snapshot(fed.clusters["south"], {7: ("w-0", "srv-9")}, ts=wall())   # a different camera 7
    heartbeat(fed.clusters["south"], "w-0", [7], ts=wall())
    view = ReadView(fed, wall=wall)
    view.refresh()

    rows = view.list()["rows"]
    assert len(rows) == 2, [r["cluster"] for r in rows]
    assert {r["cluster"] for r in rows} == {"north", "south"}
    assert all(r["worker_state"] == "live" for r in rows), "an observed camera came back as configured too"


def test_a_cluster_that_stopped_publishing_is_a_different_silence_from_a_silent_worker():
    """Two silences, and an operator does different things about them.

    A silent WORKER means its cameras are not running: `causes()` reports it. A stale
    SNAPSHOT means the cluster is running perfectly well and the domain's picture of
    it has stopped moving — nothing is broken where the operator would look.

    `ages()` has computed this since Lesson 1 and nothing outside a test ever called
    it, so the one number that says "this cluster's controller stopped publishing"
    was computable and never computed. Now the list carries it."""
    fed, _ = make_domain({"north": (), "south": ()}, "north")
    wall = Clock()
    snapshot(fed.clusters["north"], {1: ("w-0", "srv-1")}, ts=wall())
    snapshot(fed.clusters["south"], {2: ("w-0", "srv-9")}, ts=wall())
    heartbeat(fed.clusters["north"], "w-0", [1], ts=wall())
    heartbeat(fed.clusters["south"], "w-0", [2], ts=wall())
    view = ReadView(fed, wall=wall)
    view.refresh()
    assert view.list()["rpo"] == {"north": 0.0, "south": 0.0}

    # north's controller stops publishing; its worker keeps reporting, so nothing else notices
    wall.advance(120)
    heartbeat(fed.clusters["north"], "w-0", [1], ts=wall())
    heartbeat(fed.clusters["south"], "w-0", [2], ts=wall())
    snapshot(fed.clusters["south"], {2: ("w-0", "srv-9")}, ts=wall())
    view.refresh()

    assert view.list()["rpo"] == {"north": 120.0, "south": 0.0}
    assert view.causes() == [], "a stale snapshot is not a silent worker, and must not be reported as one"
    assert all(r["worker_state"] == "live" for r in view.list()["rows"]), "the cameras are fine, and the list says so"


def test_kill_a_server_one_cause_displayed():
    wall = Clock(10_000.0); fed, links = _four_workers(wall)
    view = ReadView(fed, lost_after=45, wall=wall)
    view.refresh()
    wall.advance(100)                                                       # srv-1 died: w-0 and w-1 go silent together
    heartbeat(fed.clusters["north"], "w-2", list(range(101, 151)), ts=wall(), server="srv-2")
    heartbeat(fed.clusters["south"], "w-0", list(range(151, 201)), ts=wall(), server="srv-9")
    view.refresh()
    causes = view.causes()
    assert len(causes) == 1 and causes[0].scope == "server" and causes[0].name == "north/srv-1"
    assert causes[0].workers == ["w-0", "w-1"] and causes[0].cameras == 100
    assert causes[0].sentence().startswith("server silent: north/srv-1 for 100 s")
    rows = view.list(size=200)["rows"]
    stale = [r for r in rows if r["worker_state"] == "stale"]
    assert len(stale) == 100 and "last known state" in stale[0]["as_of"]   # still listed, greyed, with their age


def test_unreachable_cluster_keeps_last_known_rows_and_says_so():
    wall = Clock(10_000.0); fed, links = _four_workers(wall)
    view = ReadView(fed, lost_after=45, wall=wall)
    view.refresh()
    links["south"].up = False
    wall.advance(30)
    view.refresh()
    page = view.list(cluster="south", size=100)
    assert page["total"] == 50 and page["clusters"]["south"] == "unreachable" and not page["complete"]
    assert page["rows"][0]["worker_state"] == "unreachable"
    assert view.causes()[0].scope == "cluster" and view.causes()[0].cameras == 50


class FakeClusterConsole:
    def __init__(self): self.edits = []; self.creates = []
    def update_camera(self, camera, fields, subject):
        self.edits.append((camera, fields, subject)); return {"revision": len(self.edits) + 1}
    def create_camera(self, fields, subject):
        self.creates.append(fields); return {"id": len(self.creates), "worker": "w-0"}


def test_api_refuses_placement_at_both_levels_and_is_idempotent():
    fed, _ = make_domain({"north": (), "south": ()}, "north")
    snapshot(fed.clusters["south"], {7: ("w-0", "srv-9")}, ts=0)
    consoles = {"south": FakeClusterConsole()}
    api = ConsoleAPI(DomainDirectory(fed), consoles.__getitem__)
    r1 = api.update_camera(7, {"name": "gate"}, idempotency_key="k1")
    r2 = api.update_camera(7, {"name": "gate"}, idempotency_key="k1")            # the same PUT, not a second edit
    assert r1 is r2 and len(consoles["south"].edits) == 1 and r1["cluster"] == "south" and r1["worker"] == "w-0" and not r1["authenticated"]
    for bad in ({"worker": "w-1"}, {"cluster": "north"}, {"server": "srv-1"}, {"placement": {}}, {"phase": "running"}, {"epoch": 9}):
        try:
            api.update_camera(7, bad, idempotency_key="k2"); raise AssertionError("must refuse")
        except ApiError as e:
            assert e.status == 400 and "may not set" in e.detail
    try:
        api.update_camera(99, {"name": "x"}, idempotency_key="k3"); raise AssertionError("must 404")
    except ApiError as e:
        assert e.status == 404
    c = api.create_camera({"name": "new", "source": "driverpack://file/n.mp4", "ref": "12"}, cluster="south", idempotency_key="k4")
    assert c["cluster"] == "south" and c["result"]["worker"] == "w-0"              # the cluster chose the worker; the domain forwarded


def test_api_says_503_not_404_when_a_cluster_is_unreachable():
    fed, links = make_domain({"north": (), "south": ()}, "north")
    snapshot(fed.clusters["south"], {7: ("w-0", "srv-9")}, ts=0)
    links["south"].up = False
    api = ConsoleAPI(DomainDirectory(fed), lambda n: FakeClusterConsole())
    try:
        api.update_camera(7, {"name": "x"}, idempotency_key="k"); raise AssertionError()
    except ApiError as e:
        assert e.status == 503 and "unreachable" in e.detail


def test_gateway_fans_out_and_the_worker_sees_one_viewer():
    tees = {7: LiveTee(7)}
    def authorise(token, camera):
        if token != "alice-token": raise Forbidden(token)
        return "alice"
    ep = WorkerLiveEndpoint("w-0", tees, authorise)
    gw = Gateway("gw-1", where=lambda c: "w-0", endpoint=lambda n: ep)
    viewers = [gw.watch(7, "alice-token", f"browser-{i}", maxsize=5) for i in range(50)]
    assert tees[7].viewers == 1 and gw.viewers(7) == 50                  # ONE subscription on the worker, fifty out
    try:
        gw.watch(7, "bad-token", "browser-x"); raise AssertionError("the cluster's authoriser decides")
    except Forbidden:
        pass
    for f in range(100):
        tees[7].push(f"frame-{f}")                                       # the worker's push never blocks
    assert gw.pump() == 30 and tees[7].subscribers["gw-1"].dropped == 70  # upstream leaky queue (30) leaked
    assert all(len(v.q) == 5 and v.dropped == 25 for v in viewers)       # every slow viewer leaked its own
    for i in range(50):
        gw.leave(7, f"browser-{i}")
    assert tees[7].viewers == 0 and gw.viewers(7) == 0


def test_gateway_follows_a_failover():
    eps = {"w-0": WorkerLiveEndpoint("w-0", {7: LiveTee(7)}, lambda t, c: "alice"),
           "w-1": WorkerLiveEndpoint("w-1", {7: LiveTee(7)}, lambda t, c: "alice")}
    home = {"cam": "w-0"}
    gw = Gateway("gw", where=lambda c: home["cam"], endpoint=eps.__getitem__)
    gw.watch(7, "t", "b1")
    home["cam"] = "w-1"                                                   # the controller moved it; the directory says so
    assert gw.reconnect(7, "t") == "w-1" and eps["w-1"].tees[7].viewers == 1 and gw.viewers(7) == 1


def test_console_over_http():
    fed, _ = make_domain({"north": (), "south": ()}, "north")
    snapshot(fed.clusters["south"], {7: ("w-0", "srv-9")}, ts=1000.0)
    heartbeat(fed.clusters["south"], "w-0", [7], ts=1000.0, server="srv-9")
    view = ReadView(fed, wall=lambda: 1002.0)
    api = ConsoleAPI(DomainDirectory(fed), lambda n: FakeClusterConsole())
    con = Console(DomainDirectory(fed), view, api, refresh_interval=0.05)
    srv = con.serve(port=0)
    port = srv.server_address[1]
    try:
        import time; time.sleep(0.2)
        body = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/cameras"))
        assert body["total"] == 1 and body["rows"][0]["as_of"] == "as of 2 s ago"
        w = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/where/7"))
        assert w["cluster"] == "south" and w["worker"] == "w-0" and w["complete"]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/cameras/7", data=b'{"name":"x"}', method="PUT",
                                     headers={"Idempotency-Key": "abc"})
        assert json.load(urllib.request.urlopen(req))["cluster"] == "south"
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/cameras/7", data=b'{"worker":"w-1"}', method="PUT",
                                     headers={"Idempotency-Key": "def"})
        try:
            urllib.request.urlopen(req); raise AssertionError()
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        con.stop(srv)
