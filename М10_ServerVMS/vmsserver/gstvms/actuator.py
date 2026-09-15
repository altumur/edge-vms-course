"""The two actuators — a worker's verb -> pipeline.

    GstActuator     the WORKER's: `driverpacksrc ! h264parse ! watchdog ! tee`, the tee's branch RTP to the
                    loopback port the RTSP fan-out (`livesrv.py`) serves as rtsp://<server>:8554/<cam>. It holds
                    the camera and records nothing.
    GstRecActuator  the RECORDER's: `rtspsrc location=<live_url> ! rtph264depay ! h264parse ! archivesink` —
                    subscribed to the fan-out, writing segments into the spool under the recorder's epoch.
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # actuator.py — the worker's verb → pipeline: `driverpacksrc ! h264parse ! watchdog ! tee ! archivesink`
# per
# camera, the bus drained into (dead, posted)
#
# **Role in the module.** Lesson 4, Track 2 (needs `gi`). The real actuator `VmsWorker` runs its reconciler
# against when GStreamer is present (`vms/__main__.worker` tries to import it and falls back to
# `FakeActuator`). It has the same three-part surface as the fake — callable `(verb, cam) -> bool`, `pump()
# -> (dead, posted)`, `stop_all()` — so the worker does not know which it holds. It builds one
# `Gst.Pipeline` per camera from a launch string, keeps them in a dict, and turns bus messages into two
# lists the worker drains on every pass: cameras whose pipeline errored (`dead`) and element messages that
# are observations (`posted`). "The element never knows about buckets or epochs: it posts what it saw; the
# worker, which holds the epoch, turns it into a line." Importing `archivesink` and `driverpacksrc` here
# registers both elements.
#
# ## Module-level names
# - `log` — logger `gstvms`.
# - `DESC` — the launch template: `driverpacksrc uri={uri} name=src ! h264parse ! watchdog
#   timeout={watchdog} ! tee name=t`, then branch one `t. ! queue ! archivesink name=sink camera={cam}
#   epoch={epoch} spool={spool} archive={archive} segment-seconds={seg}`, and branch two `t. ! queue
#   leaky=downstream max-size-buffers=30 ! fakesink sync=false` — a leaky queue into a fakesink "until М12's
#   gateway subscribes to it" (a live viewer branch that never blocks recording). `watchdog` posts an error
#   if no buffer passes for `timeout` ms, which is how a stalled source becomes a dead camera.
#
# ## Notes
# - Verified where: the README says `gstvms/` is written to GStreamer's Python binding and not exercised in
#   the test run; the logic it calls (`promote`, `resolve`) is. The zombie with two real worker processes
#   and `kill -9` mid-segment on real files are the box's exercises.
# - Bus callbacks run on the GLib main context; since the worker runs no GLib main loop, `add_signal_watch`
#   delivery depends on the default main context being iterated — the code as written relies on it, and the
#   worker's loop only reads the lists `pump` hands back.
# ================================================================================================
from __future__ import annotations

import logging
import os

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from . import archivesink, driverpacksrc  # noqa: E402,F401 — registers the elements

log = logging.getLogger("gstvms")
Gst.init(None)

DESC = ("driverpacksrc uri={uri} name=src ! h264parse ! watchdog timeout={watchdog} ! tee name=t "
        "t. ! queue leaky=downstream max-size-buffers=30 ! {live}")
LIVE = "rtph264pay config-interval=1 pt=96 ! udpsink host=127.0.0.1 port={port} sync=false"   # the tee's branch: RTP to the loopback port
IDLE = "fakesink sync=false"                                                                    # the RTSP fan-out (livesrv) serves from
REC_DESC = ("rtspsrc location={source} latency=200 protocols=tcp name=src ! rtph264depay ! h264parse ! watchdog timeout={watchdog} ! "
            "archivesink name=sink camera={cam} epoch={epoch} spool={spool} archive={archive} segment-seconds={seg}")


# State: `spool`, `archive`, `seg` (segment seconds), `watchdog` (ms), `pipelines` (`{camera id:
# Gst.Pipeline}`), `dead` (ids whose bus posted an error since the last pump), `posted` (`(camera, kind,
# fields)` since the last pump).
class GstActuator:
    """The worker's: holds the camera, serves the fan-out, records nothing."""

    def __init__(self, watchdog_ms: int = 8000, rtsp_port: int = 8554):
        self.watchdog = watchdog_ms
        from .livesrv import FanOut
        self.fanout = FanOut(rtsp_port)                  # rtsp://<server>:8554/<cam>: one shared factory per camera over its loopback port
        self.pipelines: dict[int, Gst.Pipeline] = {}
        self.dead: list[int] = []
        self.posted: list[tuple[int, str, dict]] = []   # what elements posted on the bus: (camera, kind, fields)

    # The reconciler's actuator. For `stop` or `restart` with a running pipeline: pop it, send EOS (lets
    # `splitmuxsink` finalize the open segment, so it is promoted rather than lost), then `NULL`. `stop`
    # returns True there. For `start`/`restart`: format `DESC` with the row's `source` as the URI, the
    # watchdog, the camera id, `cam["epoch"]` (added by `VmsWorker._actuate`; 0 if absent), the roots and
    # the segment length; `Gst.parse_launch` — an exception (a refused URI from `uri.resolve`, a missing
    # plugin) is logged and returns False, which the reconciler counts as a failure with backoff. Then a
    # signal watch on the bus: `message::error` appends the camera to `dead`; `message::element` goes to
    # `_posted`. `set_state(PLAYING)` returning `FAILURE` is False. Success stores the pipeline and returns
    # True.
    def __call__(self, verb: str, cam: dict) -> bool:
        cid = cam["id"]
        if verb in ("stop", "restart") and cid in self.pipelines:
            p = self.pipelines.pop(cid)
            p.send_event(Gst.Event.new_eos())            # lets splitmuxsink finalize the open segment
            p.set_state(Gst.State.NULL)
        if verb == "stop":
            self._unpublish(cid)
            return True
        try:
            p = Gst.parse_launch(self.describe(cam))
        except Exception as e:                        # noqa: BLE001
            log.error("camera %s: %s", cid, e)
            return False
        bus = p.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", lambda b, m, c=cid: self.dead.append(c))
        bus.connect("message::element", lambda b, m, c=cid: self._posted(c, m))     # motion, person, ...: an element saw something
        if p.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            return False
        self.pipelines[cid] = p
        self._publish(cid, cam)
        return True

    # The pipeline for this verb's row: the worker's DESC with the tee's loopback branch.
    def describe(self, cam: dict) -> str:
        live = LIVE.format(port=cam["live_port"]) if cam.get("live_port") else IDLE
        return DESC.format(uri=cam["source"], watchdog=self.watchdog, live=live)

    def _publish(self, cid: int, cam: dict) -> None:
        if self.fanout is not None and cam.get("live_port"):
            self.fanout.publish(str(cid), cam["live_port"])

    def _unpublish(self, cid: int) -> None:
        if self.fanout is not None:
            self.fanout.unpublish(str(cid))

    # Filters an element message into an observation. Messages with no structure, or named
    # `GstBinForwarded`, `splitmuxsink-fragment-opened` or `splitmuxsink-fragment-closed`, are plumbing and
    # dropped. Otherwise the structure's name is the event kind (`motion`, `person`, … — whatever an
    # analytics element posts) and its scalar fields (`int`, `float`, `str`, `bool`) are copied; appended to
    # `posted`. The worker's `pump_once` turns each into `observe(cid, kind, **fields)`, a line in the
    # camera's bucket, if it still holds the epoch.
    def _posted(self, cid: int, msg) -> None:
        st = msg.get_structure()
        if st is None or st.get_name() in ("GstBinForwarded", "splitmuxsink-fragment-opened", "splitmuxsink-fragment-closed"):
            return                                       # plumbing, not an observation
        fields = {}
        for i in range(st.n_fields()):
            name = st.nth_field_name(i)
            v = st.get_value(name)
            if isinstance(v, (int, float, str, bool)):
                fields[name] = v
        self.posted.append((cid, st.get_name(), fields))

    # Returns and clears both lists; every dead camera's pipeline is popped and set to `NULL`. The worker
    # then calls `reconciler.lost(cid)` (restart after backoff) and writes a `silent` event for it.
    def pump(self) -> tuple[list[int], list[tuple[int, str, dict]]]:
        """(dead cameras, posted observations) since the last pump. The element
        never knows about buckets or epochs: it posts what it saw; the worker,
        which holds the epoch, turns it into a line."""
        dead, self.dead = self.dead, []
        posted, self.posted = self.posted, []
        for cid in dead:
            p = self.pipelines.pop(cid, None)
            if p:
                p.set_state(Gst.State.NULL)
        return dead, posted

    # `self("stop", {"id": cid})` for every running pipeline: EOS then NULL for each. Called by
    # `VmsWorker.fence` and at the end of `VmsWorker.run` — with `vmsworker@.container`'s `StopTimeout=20`
    # giving the finalizations time.
    def stop_all(self) -> None:
        for cid in list(self.pipelines):
            self("stop", {"id": cid})


class GstRecActuator(GstActuator):
    """The recorder's: `rtspsrc` on the camera's fan-out URL, `archivesink` into the spool under the
    recorder's epoch. No fan-out of its own; nothing here reads a camera."""

    def __init__(self, spool: str, archive: str, segment_seconds: int = 600, watchdog_ms: int = 8000):
        self.spool, self.archive, self.seg, self.watchdog = spool, archive, segment_seconds, watchdog_ms
        self.fanout = None
        self.pipelines, self.dead, self.posted = {}, [], []

    def describe(self, cam: dict) -> str:
        return REC_DESC.format(source=cam["source"], watchdog=self.watchdog, cam=cam["id"], epoch=cam.get("epoch", 0),
                               spool=self.spool, archive=self.archive, seg=self.seg)
