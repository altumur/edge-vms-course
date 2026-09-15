"""The live gateway — the second subsystem's worker. A gateway is a worker in
the platform's sense (a slot claimed by CAS, an assignment read from the
store, a heartbeat with capacity and headroom, count = N by demand) whose
unit is a camera's live FAN-OUT: one subscription to the worker's RTP tee,
N browsers behind it over WebRTC. Capacity is counted in viewers.

    live/streams/<cam>      the unit — created by the console on the first viewer, deleted by the gateway
                            a grace period after the last one leaves (demand-created placement)
    live/workers/<g>        the assignment, written by the live controller (the platform's SpecController
                            run from live.subsystem.yaml)
    live/<g>/heartbeat      capacity, headroom (viewers it could still take), url, per-stream status

    POST   /whep/<cam>            WHEP: an SDP offer in, 201 + the SDP answer out, Location: /whep/session/<id>
    DELETE /whep/session/<id>     the viewer hangs up
    GET    /metrics               live_sessions, live_streams_up, live_headroom

Rules it keeps: it subscribes to a camera ONCE whatever the viewer count;
it finds the worker's fan-out URL from the VMS heartbeat, never by calling a
worker; a viewer never reaches a worker; it holds nothing a restart cannot
rediscover. The media path is a `Peer` — `FakePeer` here (signalling only),
`gstvms.webrtc.GstPeer` on a box (webrtcbin: ICE, DTLS-SRTP, RTP).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from psimplatform.console import SendMixin, heartbeats
from psimplatform.contract import Worker
from psimplatform.spec import SpecController
from psimplatform.variables import Variables

from .config import LIVE_SPEC

LIVE = LIVE_SPEC.sub
log = logging.getLogger("vms.gateway")


# One WebRTC peer for one viewer. `answer(offer) -> sdp`; `close()`. The fake answers any offer with a minimal
# recvonly-compatible SDP so signalling can be tested end to end; the real one is `gstvms.webrtc.GstPeer`.
class FakePeer:
    def __init__(self, upstream):
        self.upstream, self.closed = upstream, False

    def answer(self, offer: str) -> str:
        if "m=video" not in offer:
            raise ValueError("the offer has no video section")
        return ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\na=fingerprint:sha-256 FA:KE\r\n"
                "m=video 9 UDP/TLS/RTP/SAVPF 96\r\na=sendonly\r\na=rtpmap:96 H264/90000\r\n")

    def close(self) -> None:
        self.closed = True


# One camera's subscription: the RTP source (server, port) it listens to and the peers fanned out from it.
class Upstream:
    def __init__(self, cam: str, server: str, url: str, epoch: int):
        self.cam, self.server, self.url, self.epoch = cam, server, url, epoch
        self.peers: dict[str, object] = {}
        self.idle_since: float | None = None      # wall time the last viewer left; None while watched

    def to_status(self) -> dict:
        return {"id": self.cam, "phase": "live" if self.peers else "idle", "sessions": len(self.peers),
                "server": self.server, "source": self.url, "epoch": self.epoch}


class LiveGateway(Worker):
    """`name` is a slot (`g-1`); `url` is where the console proxies WHEP to; `capacity` is viewers."""

    def __init__(self, name: str | None, vars_: Variables, objects, ctl: SpecController | None = None, url: str = "",
                 capacity: int | None = None, clock=time.monotonic, wall=time.time, server: str | None = None,
                 peer_factory=None, env: dict | None = None):
        env = dict(os.environ if env is None else env)
        super().__init__(LIVE, None, vars_, objects, clock=clock, wall=wall)
        self.claim_slot(prefer=name if name is not None else env.get("GATEWAY_NAME") or (f"g-{env['NOMAD_ALLOC_INDEX']}" if "NOMAD_ALLOC_INDEX" in env else None))
        self.ctl = ctl                                              # the live SpecController with the gateway's token: deletes its own idle units
        self.url = url or env.get("GATEWAY_URL", "")
        self.capacity = capacity if capacity is not None else int(env.get("CAPACITY", "100"))
        self.server = server or env.get("NOMAD_NODE_NAME") or os.uname().nodename
        self.labels = [l for l in env.get("NOMAD_META_labels", "").split(",") if l]
        self.peer_factory = peer_factory or FakePeer
        self.upstreams: dict[str, Upstream] = {}
        self.sessions: dict[str, tuple[str, object]] = {}          # session id -> (cam, peer)
        self.subscriptions = 0                                      # how many times an RTP source was opened — the test's number
        self.lock = threading.Lock()

    # -- where a camera's RTP is: the VMS heartbeat, never a call to the worker ----------------------
    def rtp_source(self, cam: str):
        """`(server, live_url, epoch)` of the worker holding the camera — its RTSP fan-out, on any server."""
        for hb in heartbeats(self.objects, "vms/").values():
            for st in hb.status:
                if str(st.get("id")) == str(cam) and st.get("phase") == "running" and st.get("live_url"):
                    return hb.extra.get("server", "?"), st["live_url"], int(st.get("epoch", 0))
        return None

    # -- the reconcile pass: make the subscriptions equal the assignment -------------------------------
    def reconcile_once(self, now: float | None = None) -> list[str]:
        now = self.wall() if now is None else now
        a = self.assignment()
        wanted = set(a.units)
        with self.lock:
            for cam in wanted - set(self.upstreams):
                src = self.rtp_source(cam)
                if src is None:
                    continue                                        # the camera is not recording anywhere: wait, say so in the heartbeat
                if cam not in self.epochs:
                    self.take_epoch(cam)                            # one gateway per fan-out, fenced like any unit
                self.upstreams[cam] = Upstream(cam, *src); self.subscriptions += 1
                self.upstreams[cam].idle_since = now
            for cam in set(self.upstreams) - wanted:                # taken away (rebalanced, deleted): drop viewers, close the source
                self._drop(cam)
            # the grace period: an idle fan-out is deleted by the gateway itself — demand-created, demand-deleted
            for cam, up in list(self.upstreams.items()):
                if not up.peers and up.idle_since is not None and self.ctl is not None:
                    row = self.ctl.unit(cam)
                    if row is not None and now - up.idle_since >= int(row.get("grace", 30)):
                        self.ctl.delete(cam)                        # the controller's next pass takes the placement back
        self.renew_leases()
        return sorted(self.upstreams)

    def _drop(self, cam: str) -> None:
        up = self.upstreams.pop(cam)
        for sid, (c, peer) in list(self.sessions.items()):
            if c == cam:
                peer.close(); del self.sessions[sid]
        if getattr(up, "pipeline", None) is not None:              # the real media path (gstvms.webrtc): close the source
            up.pipeline.close()
        self.release(cam)

    # -- WHEP ----------------------------------------------------------------------------------------
    def offer(self, cam: str, sdp: str) -> tuple[str, str]:
        """A viewer's offer: refuse if the camera is not this gateway's or the gateway is full; else a session."""
        with self.lock:
            up = self.upstreams.get(str(cam))
            if up is None:
                raise KeyError(cam)
            if len(self.sessions) >= self.capacity:
                raise OverflowError("full")
            peer = self.peer_factory(up)
            answer = peer.answer(sdp)
            sid = uuid.uuid4().hex
            up.peers[sid] = peer; up.idle_since = None
            self.sessions[sid] = (str(cam), peer)
            return sid, answer

    def hangup(self, sid: str) -> bool:
        with self.lock:
            if sid not in self.sessions:
                return False
            cam, peer = self.sessions.pop(sid)
            peer.close()
            up = self.upstreams.get(cam)
            if up is not None:
                up.peers.pop(sid, None)
                if not up.peers:
                    up.idle_since = self.wall()
            return True

    # -- what it reports -----------------------------------------------------------------------------
    def headroom(self) -> int:
        return max(0, self.capacity - len(self.sessions))

    def heartbeat_once(self) -> None:
        self.heartbeat([up.to_status() for up in self.upstreams.values()], server=self.server, instance=self.instance,
                       labels=",".join(self.labels), url=self.url, capacity=self.capacity, headroom=self.headroom(),
                       sessions=len(self.sessions), subscriptions=self.subscriptions, conflicts=self.conflicts())

    def metrics_text(self) -> str:
        return (f"# TYPE live_sessions gauge\nlive_sessions {len(self.sessions)}\n"
                f"# TYPE live_streams_up gauge\nlive_streams_up {len(self.upstreams)}\n"
                f"# TYPE live_headroom gauge\nlive_headroom {self.headroom()}\n")

    # -- the WHEP server -------------------------------------------------------------------------------
    def handler(self):
        gw = self

        class H(SendMixin, BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def do_POST(self):
                if not self.path.startswith("/whep/"):
                    return self._send(404, {"error": "no such path"})
                cam = self.path[len("/whep/"):].split("?")[0]
                sdp = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                try:
                    sid, answer = gw.offer(cam, sdp)
                except KeyError:
                    return self._send(404, {"error": f"stream {cam} is not on this gateway"})
                except OverflowError:
                    return self._send(503, {"error": "this gateway is full"})
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                data = answer.encode()
                self.send_response(201); self.send_header("Content-Type", "application/sdp")
                self.send_header("Location", f"/whep/session/{sid}"); self.send_header("Content-Length", str(len(data)))
                self.end_headers(); self.wfile.write(data)

            def do_DELETE(self):
                if not self.path.startswith("/whep/session/"):
                    return self._send(404, {"error": "no such path"})
                ok = gw.hangup(self.path[len("/whep/session/"):])
                self._send(200 if ok else 404, {"closed": ok})

            def do_GET(self):
                if self.path == "/metrics":
                    return self._send(200, gw.metrics_text(), raw=True)
                if self.path == "/sessions":
                    return self._send(200, {cam: up.to_status() for cam, up in gw.upstreams.items()})
                self._send(404, {"error": "no such path"})

        return H

    def serve(self, host: str = "127.0.0.1", port: int = 8082) -> ThreadingHTTPServer:
        srv = ThreadingHTTPServer((host, port), self.handler())
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        if not self.url:
            self.url = f"http://{host}:{srv.server_address[1]}"
        return srv

    def run(self, poll: float = 2.0, stop=None) -> None:
        """One box: the loop as a process. Nomad or systemd restarts it."""
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                self.reconcile_once(); self.heartbeat_once()
            except Exception:                                       # noqa: BLE001
                log.exception("gateway pass failed")
            stop.wait(poll)
        for cam in list(self.upstreams):
            self._drop(cam)
        self.release_slot()
