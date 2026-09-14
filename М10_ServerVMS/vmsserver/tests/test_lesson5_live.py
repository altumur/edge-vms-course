"""Lesson 5, Step 9 — live video as the third subsystem. A gateway is a worker
whose unit is a camera's fan-out and whose capacity is viewers; the unit is
created by the first viewer and deleted after the last; one subscription per
camera whatever the audience; the worker never learns a viewer exists."""
import json
import threading
import urllib.error
import urllib.request

from psimplatform.spec import SpecController
from psimplatform.variables import Forbidden
from vms.config import LIVE_SPEC, SPEC
from vms.controller import VmsController
from vms.gateway import LiveGateway
from vms.worker import FakeActuator, VmsWorker, live_port
from vms.console import serve
from tests.conftest import Box

OFFER = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=recvonly\r\na=rtpmap:96 H264/90000\r\n"


def _box():
    """One box: a VMS worker recording camera 1, the two controllers, a console with both tokens."""
    box = Box()
    ctl = VmsController(box.vars.as_writer("vmscontroller", SPEC.acl_controller()), box.objects, wall=box.wall)
    con_vars = box.vars.as_writer("vmsconsole", SPEC.acl_console() + LIVE_SPEC.acl_console())
    con = VmsController(con_vars, box.objects, wall=box.wall)
    live_ctl = SpecController(LIVE_SPEC, box.vars.as_writer("livecontroller", LIVE_SPEC.acl_controller()), box.objects, wall=box.wall)
    w = VmsWorker("w-1", box.vars, box.objects, FakeActuator(), clock=box.clock, wall=box.wall, server="srv-1", archive_root=box.archive)
    w.heartbeat_once()
    con.create_camera({"name": "gate", "source": "driverpack://file/gate.mp4"}); ctl.ensure_placed()   # the console writes the row, the controller places
    w.reconcile_once(); w.heartbeat_once()
    srv = serve(con, None, port=0, wall=box.wall, live_ctl=SpecController(LIVE_SPEC, con_vars, box.objects, wall=box.wall))
    return box, ctl, live_ctl, w, srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _gateway(box, name, capacity=100, labels="", url=""):
    g = LiveGateway(name, box.vars.as_writer("livegateway", ["live/epoch/*", "live/slots/*", "live/streams/*"]), box.objects,
                    ctl=SpecController(LIVE_SPEC, box.vars.as_writer("livegateway", ["live/epoch/*", "live/slots/*", "live/streams/*"]), box.objects, wall=box.wall),
                    capacity=capacity, clock=box.clock, wall=box.wall, server="srv-1", env={"NOMAD_META_labels": labels})
    g.serve("127.0.0.1", 0); g.heartbeat_once()
    return g


def _whep(base, cam, headers=None, method="POST", path=None):
    req = urllib.request.Request(base + (path or f"/whep/{cam}"), data=OFFER.encode() if method == "POST" else None, method=method,
                                 headers={"Content-Type": "application/sdp", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode(), r.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), ""


def test_the_first_viewer_creates_the_stream_and_the_controller_places_it():
    box, ctl, live_ctl, w, srv, base = _box()
    try:
        g = _gateway(box, "g-1")
        # the worker's heartbeat says where the RTP is; nobody asked the worker
        st = [s for s in ctl.workers_seen()["w-1"].status if s["id"] == 1][0]
        assert st["live_port"] == live_port(1) == 20001 and "viewers" not in st
        # first viewer: the console creates live/streams/1 and says "retry" — placement is the controller's pass
        code, body, _ = _whep(base, 1)
        assert code == 503 and json.loads(body)["retry_after"] == 2
        assert live_ctl.unit("1") == {"id": "1", "cam": "1", "labels": [], "grace": 30, "revision": 1}
        assert box.vars.list("live/placement/") == []                                 # the console could not place it
        try:
            SpecController(LIVE_SPEC, box.vars.as_writer("vmsconsole", SPEC.acl_console() + LIVE_SPEC.acl_console()), box.objects, wall=box.wall).place("1")
            raise AssertionError("a console token never writes placement")
        except Forbidden:
            pass
        assert live_ctl.ensure_placed()[0].worker == "g-1"                              # the live controller's pass
        assert g.reconcile_once() == ["1"] and g.subscriptions == 1 and g.upstreams["1"].port == 20001 and g.upstreams["1"].server == "srv-1"
        g.heartbeat_once()
        # second try: 201 with the gateway's SDP answer, and a session URL that goes back through the console
        code, answer, loc = _whep(base, 1)
        assert code == 201 and "m=video" in answer and "a=sendonly" in answer and loc.startswith("/whep/session/") and loc.endswith("?gateway=g-1")
        g.heartbeat_once()                                                               # what the page reads: the gateway's word, from its heartbeat
        st = json.loads(urllib.request.urlopen(f"{base}/whep/1").read())
        assert st["gateway"] == "g-1" and st["status"]["phase"] == "live" and st["status"]["sessions"] == 1
        # unknown camera: 404, no unit created
        assert _whep(base, 9)[0] == 404 and live_ctl.unit("9") is None
    finally:
        srv.shutdown(); srv.server_close()


def test_fifty_viewers_one_subscription_and_the_worker_unchanged():
    box, ctl, live_ctl, w, srv, base = _box()
    try:
        g = _gateway(box, "g-1", capacity=60)
        _whep(base, 1); live_ctl.ensure_placed(); g.reconcile_once(); g.heartbeat_once()
        before = box.objects.get("vms/w-1/heartbeat")
        sessions = [_whep(base, 1)[2] for _ in range(50)]
        assert all(s.startswith("/whep/session/") for s in sessions) and len(g.sessions) == 50
        assert g.subscriptions == 1 and len(g.upstreams) == 1                            # fifty browsers, one tee subscription
        assert g.headroom() == 10                                                        # capacity is viewers: what the autoscaler moves N on
        assert box.objects.get("vms/w-1/heartbeat") == before                            # the worker never learned a viewer exists
        # full: the sixty-first viewer is refused by the gateway, through the console
        for _ in range(10):
            _whep(base, 1)
        code, body, _ = _whep(base, 1)
        assert code == 503 and "full" in json.loads(body)["detail"]
        # hang up one through the console: the session is gone on the gateway
        sid = sessions[0].split("/")[-1].split("?")[0]
        assert _whep(base, 1, method="DELETE", path=sessions[0])[0] == 200 and sid not in g.sessions and len(g.sessions) == 59
        assert "live_sessions 59" in urllib.request.urlopen(f"{g.url}/metrics").read().decode()
    finally:
        srv.shutdown(); srv.server_close()


def test_the_last_viewer_leaves_and_the_gateway_deletes_the_unit_after_grace():
    box, ctl, live_ctl, w, srv, base = _box()
    try:
        g = _gateway(box, "g-1")
        _whep(base, 1); live_ctl.ensure_placed(); g.reconcile_once(); g.heartbeat_once()
        _, _, loc = _whep(base, 1)
        _whep(base, 1, method="DELETE", path=loc)
        box.wall.advance(10); g.reconcile_once()
        assert live_ctl.unit("1") is not None and "1" in g.upstreams                     # inside the grace: kept, a returning viewer costs nothing
        box.wall.advance(25); g.reconcile_once()
        assert live_ctl.unit("1") is None                                                # the gateway deleted the unit it held
        assert live_ctl.unplace_deleted() == ["1"] and live_ctl.where("1") is None      # the controller's half
        assert g.reconcile_once() == [] and "1" not in g.epochs                           # the subscription is closed
        # a viewer comes back: the unit is created again under its name, placed, served
        assert _whep(base, 1)[0] == 503 and live_ctl.unit("1")["revision"] == 2
        live_ctl.ensure_placed(); g.reconcile_once(); g.heartbeat_once()
        assert _whep(base, 1)[0] == 201 and g.subscriptions == 2
    finally:
        srv.shutdown(); srv.server_close()


def test_a_dead_gateway_loses_its_fan_outs_to_the_survivor_and_viewers_reconnect():
    box, ctl, live_ctl, w, srv, base = _box()
    try:
        g1, g2 = _gateway(box, "g-1", capacity=100), _gateway(box, "g-2", capacity=50)
        _whep(base, 1); assert live_ctl.ensure_placed()[0].worker == "g-1"               # the most free capacity
        g1.reconcile_once(); g1.heartbeat_once()
        assert _whep(base, 1)[0] == 201
        # g-1 dies: its slot lapses, the controller redistributes, g-2 subscribes, the viewer's next offer lands there
        box.clock.advance(60); box.wall.advance(60); g2.heartbeat_once()               # g-1 silent for a minute; g-2 still here
        assert live_ctl.released_slots() == []                                           # a crash releases nothing…
        assert live_ctl.redistribute() == []                                             # …and the controller moves nothing on its own
        live_ctl.retire("g-1")                                                           # the operator (or Nomad's stop) releases the slot
        assert [m[:3] for m in live_ctl.redistribute()] == [("1", "g-1", "g-2")]
        g2.reconcile_once(); g2.heartbeat_once()
        code, _, loc = _whep(base, 1)
        assert code == 201 and loc.endswith("?gateway=g-2") and g2.subscriptions == 1
        w.reconcile_once(); w.heartbeat_once()
        assert ctl.workers_seen()["w-1"].status[0]["phase"] == "running" and w.actuator.running == {1}   # recording did not notice any of it
    finally:
        srv.shutdown(); srv.server_close()


def test_placement_by_label_a_stream_for_outside_viewers_needs_a_public_address():
    box, ctl, live_ctl, w, srv, base = _box()
    try:
        inside, outside = _gateway(box, "g-1", labels=""), _gateway(box, "g-2", labels="public-address")
        code, body, _ = _whep(base, 1, path="/whep/1?labels=public-address")
        assert code == 503 and live_ctl.unit("1")["labels"] == ["public-address"]
        assert live_ctl.ensure_placed()[0].worker == "g-2"                              # only the gateway with a public address is eligible
        assert "reaching public-address" in live_ctl.placement("1").reason
    finally:
        srv.shutdown(); srv.server_close()


def test_the_two_subsystems_share_the_platform_and_see_nothing_of_each_other():
    box, ctl, live_ctl, w, srv, base = _box()
    try:
        g = _gateway(box, "g-1")
        _whep(base, 1); live_ctl.ensure_placed(); g.reconcile_once(); g.heartbeat_once(); _whep(base, 1)
        assert sorted(p.split("/")[0] for p in box.vars.list("")) and all(p.startswith(("vms/", "live/")) for p in box.vars.list(""))
        assert [p for p in box.vars.list("live/") if "/idem/" not in p] == [
            "live/epoch/1", "live/placement/1", "live/slots/g-1", "live/streams/1", "live/workers/g-1"]
        assert not any("live" in p for p in box.vars.list("vms/"))                        # the VMS's rows carry nothing about viewers
        live_ctl.publish_snapshot()                                                      # the live controller publishes its own snapshot
        snap = json.loads(box.objects.get("live/snapshot"))
        assert snap["streams"][0]["cam"] == "1" and snap["streams"][0]["worker"] == "g-1"
    finally:
        srv.shutdown(); srv.server_close()
