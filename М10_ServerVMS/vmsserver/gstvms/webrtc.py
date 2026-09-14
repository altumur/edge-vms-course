"""The gateway's media path, Track 2 (needs `gi`, gst-plugins-bad with
webrtcbin, libnice, dtls, srtp): one GStreamer pipeline per CAMERA on the
gateway — `udpsrc` on the worker's RTP port, a jitter buffer, a `tee` — and
one `webrtcbin` per VIEWER hung off that tee. The H.264 payload passes
through untouched: no decode, no encode. WHEP is answered without trickle —
the answer carries every ICE candidate — so one HTTP round trip is the whole
signalling. A camera whose stream a browser cannot decode (B-frames, H.265)
fails at the browser, and the gateway reports it as `codec` in its status;
transcoding is a placement decision (a GPU label), not a silent default.

    GstPeer(upstream)   the `Peer` the gateway's `peer_factory` builds: answer(offer) -> sdp, close()
"""
from __future__ import annotations

import logging
import threading

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
from gi.repository import Gst, GstSdp, GstWebRTC  # noqa: E402

log = logging.getLogger("gstvms.webrtc")
Gst.init(None)

RTP_CAPS = "application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000"
SOURCE = "udpsrc port={port} caps=\"{caps}\" ! rtpjitterbuffer latency=200 ! tee name=t allow-not-linked=true"


# One camera's subscription on the gateway: the pipeline every viewer of that camera branches from. Built once
# per Upstream (kept on it as `pipeline`), torn down when the gateway drops the upstream.
class _Source:
    def __init__(self, port: int):
        self.pipeline = Gst.parse_launch(SOURCE.format(port=port, caps=RTP_CAPS))
        self.tee = self.pipeline.get_by_name("t")
        self.pipeline.set_state(Gst.State.PLAYING)
        self.viewers = 0

    def close(self) -> None:
        self.pipeline.set_state(Gst.State.NULL)


class GstPeer:
    """One viewer: a `queue ! webrtcbin` branch on the camera's tee."""

    def __init__(self, upstream):
        self.up = upstream
        if getattr(upstream, "pipeline", None) is None:
            upstream.pipeline = _Source(upstream.port)
        self.src: _Source = upstream.pipeline
        self.queue = Gst.ElementFactory.make("queue"); self.queue.set_property("leaky", 2)   # downstream: a slow viewer drops
        self.webrtc = Gst.ElementFactory.make("webrtcbin")
        self.webrtc.set_property("bundle-policy", GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)
        for e in (self.queue, self.webrtc):
            self.src.pipeline.add(e); e.sync_state_with_parent()
        self.src.tee.link(self.queue); self.queue.link(self.webrtc)
        self.src.viewers += 1
        self._gathered = threading.Event()
        self.webrtc.connect("notify::ice-gathering-state", self._on_gathering)

    def _on_gathering(self, element, pspec):
        if element.get_property("ice-gathering-state") == GstWebRTC.WebRTCICEGatheringState.COMPLETE:
            self._gathered.set()

    # WHEP: the browser's offer in, our answer out — with all candidates gathered, so no trickle is needed.
    def answer(self, offer: str, timeout: float = 5.0) -> str:
        ok, msg = GstSdp.SDPMessage.new_from_text(offer)
        if ok != GstSdp.SDPResult.OK or "m=video" not in offer:
            raise ValueError("not an SDP offer with a video section")
        remote = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.OFFER, msg)
        self.webrtc.emit("set-remote-description", remote, None)
        done = threading.Event(); holder = {}

        def on_answer(promise, _):
            reply = promise.get_reply()
            holder["answer"] = reply.get_value("answer")
            done.set()
        self.webrtc.emit("create-answer", None, Gst.Promise.new_with_change_func(on_answer, None))
        if not done.wait(timeout) or holder.get("answer") is None:
            raise ValueError("webrtcbin produced no answer")
        self.webrtc.emit("set-local-description", holder["answer"], None)
        self._gathered.wait(timeout)                                   # every candidate in the SDP: WHEP without trickle
        return self.webrtc.get_property("local-description").sdp.as_text()

    def close(self) -> None:
        for e in (self.queue, self.webrtc):
            e.set_state(Gst.State.NULL); self.src.pipeline.remove(e)
        self.src.viewers -= 1
        if self.src.viewers <= 0:                                      # the last viewer of this camera: the subscription itself stays
            pass                                                       # until the gateway drops the upstream (grace), not here
