"""The worker's RTSP fan-out — `rtsp://<server>:8554/<cam>` — over gst-rtsp-server.

One `GstRtspServer` per worker process, one SHARED media factory per camera:
its pipeline is the single legitimate receiver of the tee's loopback RTP
port (`udpsrc port=<live_port>`), and every RTSP client — a recorder on a
server with disks, a gateway on a public-facing server, a detector on a GPU
box — gets its own session from it. TCP-interleaved is allowed, so a VLAN
that passes no UDP still gets the stream. Nothing here talks to a camera;
nothing here knows what a camera is beyond a name in a URL.

    FanOut(port).publish(name, live_port)     mount /<name> over the loopback port
    FanOut(port).unpublish(name)              the camera stopped: the mount goes, sessions end
"""
from __future__ import annotations

import logging

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import GLib, Gst, GstRtspServer  # noqa: E402

log = logging.getLogger("gstvms.livesrv")
Gst.init(None)

RTP_CAPS = "application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000"
FACTORY = "( udpsrc port={port} caps=\"{caps}\" ! rtpjitterbuffer latency=100 ! rtph264depay ! h264parse config-interval=-1 ! rtph264pay name=pay0 pt=96 config-interval=1 )"


class FanOut:
    def __init__(self, port: int = 8554):
        self.server = GstRtspServer.RTSPServer()
        self.server.set_service(str(port))
        self.mounts = self.server.get_mount_points()
        self.published: dict[str, GstRtspServer.RTSPMediaFactory] = {}
        self.server.attach(None)
        self.loop = GLib.MainLoop()
        import threading
        threading.Thread(target=self.loop.run, daemon=True).start()
        log.info("RTSP fan-out on :%d", port)

    def publish(self, name: str, live_port: int) -> None:
        if name in self.published:
            return
        f = GstRtspServer.RTSPMediaFactory()
        f.set_launch(FACTORY.format(port=live_port, caps=RTP_CAPS))
        f.set_shared(True)                                 # one pipeline per camera, N sessions from it
        f.set_protocols(GstRtspServer.RTSPLowerTrans.TCP | GstRtspServer.RTSPLowerTrans.UDP)
        self.mounts.add_factory("/" + name, f)
        self.published[name] = f

    def unpublish(self, name: str) -> None:
        if self.published.pop(name, None) is not None:
            self.mounts.remove_factory("/" + name)
