"""The gateway's media path, Track 2 (needs `gi`, gst-plugins-bad with
webrtcbin, libnice, dtls, srtp): one GStreamer pipeline per CAMERA on the
gateway — `rtspsrc` on the worker's fan-out URL (`live_url`, any server), a `tee` — and
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

from .payload import codec_note, h264_payload_type  # noqa: E402

log = logging.getLogger("gstvms.webrtc")
Gst.init(None)

# The tee carries the ELEMENTARY stream, not RTP. The payload type is a number the viewer's browser
# assigns in its offer, so packing into RTP belongs to the viewer's branch — see `payload.py`. One
# payloader here, fixed at one number, could serve exactly one browser and would silently feed every
# other one packets it throws away.
SOURCE = "rtspsrc location={url} latency=200 protocols=tcp ! rtph264depay ! h264parse name=p config-interval=-1 ! tee name=t allow-not-linked=true"


# One camera's subscription on the gateway: the pipeline every viewer of that camera branches from. Built once
# per Upstream (kept on it as `pipeline`), torn down when the gateway drops the upstream.
class _Source:
    def __init__(self, url: str):
        self.pipeline = Gst.parse_launch(SOURCE.format(url=url))
        self.tee = self.pipeline.get_by_name("t")
        self.parse = self.pipeline.get_by_name("p")
        self.pipeline.set_state(Gst.State.PLAYING)
        self.viewers = 0

    # What the stream turned OUT to be, read off the caps `h264parse` negotiated — the one place where
    # that is known, as opposed to what the camera's papers claim. "" while a browser can play it, and ""
    # while nothing has flowed yet: unknown is not the same as wrong.
    def codec_note(self) -> str:
        pad = self.parse.get_static_pad("src") if self.parse else None
        caps = pad.get_current_caps() if pad else None
        if not caps or caps.get_size() == 0:
            return ""
        st = caps.get_structure(0)
        return codec_note(st.get_name(), st.get_string("profile"))

    # What the camera turned out to be sending, for the gateway's status. Asked after every answer:
    # the first viewer may arrive before anything has flowed and the profile is known.
    def codec_note(self) -> str:
        return self.src.codec_note()

    def close(self) -> None:
        self.pipeline.set_state(Gst.State.NULL)


class GstPeer:
    """One viewer: a `queue ! webrtcbin` branch on the camera's tee."""

    def __init__(self, upstream):
        self.up = upstream
        if getattr(upstream, "pipeline", None) is None:
            upstream.pipeline = _Source(upstream.url)
        self.src: _Source = upstream.pipeline
        # The branch is NOT built here: it cannot be. It needs the payload type out of the viewer's
        # offer, and the offer arrives with `answer`.
        self.queue = self.pay = self.caps = self.webrtc = None
        self.src.viewers += 1
        self._gathered = threading.Event()

    # `queue ! rtph264pay ! capsfilter ! webrtcbin` on the camera's tee, with THIS viewer's number.
    def _build(self, pt: int) -> None:
        self.queue = Gst.ElementFactory.make("queue"); self.queue.set_property("leaky", 2)   # downstream: a slow viewer drops
        self.pay = Gst.ElementFactory.make("rtph264pay")
        self.pay.set_property("config-interval", 1)   # parameter sets ride with every key frame: a late viewer decodes from the next one
        self.pay.set_property("pt", pt)
        self.caps = Gst.ElementFactory.make("capsfilter")
        self.caps.set_property("caps", Gst.Caps.from_string(
            f"application/x-rtp,media=(string)video,encoding-name=(string)H264,clock-rate=(int)90000,payload=(int){pt}"))
        self.webrtc = Gst.ElementFactory.make("webrtcbin")
        self.webrtc.set_property("bundle-policy", GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)
        for e in (self.queue, self.pay, self.caps, self.webrtc):
            self.src.pipeline.add(e); e.sync_state_with_parent()
        self.src.tee.link(self.queue); self.queue.link(self.pay); self.pay.link(self.caps)
        self.caps.link(self.webrtc)                   # webrtcbin's sink is a request pad; the caps tell it what the stream is
        self.webrtc.connect("notify::ice-gathering-state", self._on_gathering)

    def _on_gathering(self, element, pspec):
        if element.get_property("ice-gathering-state") == GstWebRTC.WebRTCICEGatheringState.COMPLETE:
            self._gathered.set()

    # WHEP: the browser's offer in, our answer out — with all candidates gathered, so no trickle is needed.
    def answer(self, offer: str, timeout: float = 5.0) -> str:
        ok, msg = GstSdp.SDPMessage.new_from_text(offer)
        if ok != GstSdp.SDPResult.OK or "m=video" not in offer:
            raise ValueError("not an SDP offer with a video section")
        pt = h264_payload_type(offer)
        if pt is None:
            raise ValueError("the viewer offers no H.264 this gateway can answer with")
        if self.webrtc is None:
            self._build(pt)
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
        for e in (self.queue, self.pay, self.caps, self.webrtc):
            if e is not None:
                e.set_state(Gst.State.NULL); self.src.pipeline.remove(e)
        self.src.viewers -= 1
        if self.src.viewers <= 0:                                      # the last viewer of this camera: the subscription itself stays
            pass                                                       # until the gateway drops the upstream (grace), not here
