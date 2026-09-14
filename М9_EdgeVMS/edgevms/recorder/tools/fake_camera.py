#!/usr/bin/env python3
"""A camera you can unplug and stall without hardware (Lesson 8).

    python3 tools/fake_camera.py --port 8554 --count 50
        serves rtsp://127.0.0.1:8554/cam0 .. cam49 (H.264 test pattern)

    kill -USR1 <pid>    stall every stream: sockets stay open, no buffers flow
                        (this is the failure watchdog exists for — the socket
                        does NOT close)
    kill -USR2 <pid>    resume

Needs gst-rtsp-server: `apt install gir1.2-gst-rtsp-server-1.0`.
"""
from __future__ import annotations

import argparse
import signal

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import GLib, Gst, GstRtspServer  # noqa: E402

Gst.init(None)

LAUNCH = ("( videotestsrc is-live=true pattern=ball ! video/x-raw,width=640,height=360,framerate=25/1 "
          "! valve name=valve drop=false ! x264enc tune=zerolatency key-int-max=25 speed-preset=ultrafast "
          "! rtph264pay name=pay0 pt=96 )")


class Factory(GstRtspServer.RTSPMediaFactory):
    def __init__(self, valves: list):
        super().__init__()
        self.valves = valves
        self.set_launch(LAUNCH)
        self.set_shared(True)
        self.connect("media-configure", self._configured)

    def _configured(self, factory, media):
        valve = media.get_element().get_by_name("valve")
        self.valves.append(valve)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8554)
    p.add_argument("--count", type=int, default=1)
    a = p.parse_args()

    valves: list = []
    server = GstRtspServer.RTSPServer()
    server.set_service(str(a.port))
    mounts = server.get_mount_points()
    for i in range(a.count):
        mounts.add_factory(f"/cam{i}", Factory(valves))
    server.attach(None)

    def stall(*_):
        for v in valves:
            v.set_property("drop", True)
        print(f"STALLED {len(valves)} streams (sockets open, no buffers)", flush=True)
        return True

    def resume(*_):
        for v in valves:
            v.set_property("drop", False)
        print("RESUMED", flush=True)
        return True

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, stall)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR2, resume)
    print(f"rtsp://127.0.0.1:{a.port}/cam0 .. cam{a.count - 1}   (USR1 stall, USR2 resume)", flush=True)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
