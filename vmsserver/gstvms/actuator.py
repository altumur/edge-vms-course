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
# - `DESC` — the worker's launch template: `driverpacksrc name=src ! h264parse ! watchdog
#   timeout={watchdog} ! tee name=t`, then two leaky branches: `rtph264pay ! udpsink 127.0.0.1:{port}` (the
#   loopback RTP the RTSP fan-out re-serves as rtsp://<server>:8554/<cam> — subscribers on any server) and
#   `shmsink socket-path=<SHM_DIR>/<cam>.shm` (the same bytes in shared memory — subscribers on THIS server,
#   the recorder first, read it with `shmsrc`: no RTSP hop, no fan-out process on the recording path). No
#   `archivesink`: the worker records nothing. `watchdog` posts an error if no buffer passes for `timeout`
#   ms, which is how a stalled source becomes a dead camera.
# - `REC_DESC` / `REC_SHM_DESC` — the recorder's: `rtspsrc location=<live_url> ! rtph264depay` or
#   `shmsrc socket-path=<…>.shm` (the source the recorder chose by where the worker is), then `h264parse !
#   watchdog ! archivesink camera={cam} epoch={epoch} …` under the RECORDER's epoch.
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

# No `uri=` and no credential in the template. A launch string is what ends up in a log line, in a crash
# dump and in `ps`, so the three things worth hiding are set as PROPERTIES after `parse_launch` — by
# construction, rather than by remembering to redact at every place that prints one.
DESC = ("driverpacksrc name=src ! h264parse ! watchdog timeout={watchdog} ! tee name=t "
        "t. ! queue leaky=downstream max-size-buffers=30 ! {live} "
        "t. ! queue leaky=downstream max-size-buffers=30 ! {shm}")
SHM = "shmsink socket-path={path} shm-size=20000000 wait-for-connection=false sync=false"      # the tee's same-server branch: any number of shmsrc readers
LIVE = "rtph264pay config-interval=1 pt=96 ! udpsink host=127.0.0.1 port={port} sync=false"   # the tee's branch: RTP to the loopback port
IDLE = "fakesink sync=false"                                                                    # the RTSP fan-out (livesrv) serves from
REC_SINK = "h264parse ! watchdog timeout={watchdog} ! archivesink name=sink camera={cam} epoch={epoch} spool={spool} archive={archive} segment-seconds={seg}"
REC_DESC = "rtspsrc location={source} latency=200 protocols=tcp name=src ! rtph264depay ! " + REC_SINK       # another server's worker: its fan-out
# Backfill (Lesson 16): a range out of the holder's playback door, written as segments in the spool exactly
# the way a live recording is. `souphttpsrc` because the door is HTTP — a browser has to seek it too — and
# the range is in the query, so nothing here knows how the vendor addresses time.
REC_RANGE_DESC = ("souphttpsrc location={source} ! qtdemux ! h264parse ! "
                  "archivesink name=sink camera={cam} epoch={epoch} spool={spool} archive={archive} segment-seconds={seg}")
REC_SHM_DESC = "shmsrc socket-path={path} is-live=true do-timestamp=true name=src ! video/x-h264,stream-format=byte-stream ! " + REC_SINK   # this server's worker: its tee, directly


# State: `spool`, `archive`, `seg` (segment seconds), `watchdog` (ms), `pipelines` (`{camera id:
# Gst.Pipeline}`), `dead` (ids whose bus posted an error since the last pump), `posted` (`(camera, kind,
# fields)` since the last pump).
class GstActuator:
    """The worker's: holds the camera, serves the fan-out, records nothing."""

    def __init__(self, watchdog_ms: int = 8000, rtsp_port: int = 8554):
        self.watchdog = watchdog_ms
        from .livesrv import FanOut
        self.fanout = FanOut(rtsp_port)
        self.rtsp_port = self.fanout.port                # what the OS gave, when `rtsp_port` was 0                  # rtsp://<server>:8554/<cam>: one shared factory per camera over its loopback port
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
            src = p.get_by_name("src")
            src.set_property("uri", cam["source"])                    # the URI, after the parse: see DESC
            if cam.get("cred_username"):
                src.set_property("user", cam["cred_username"])
            if cam.get("cred_secret"):
                src.set_property("password", cam["cred_secret"])
        except Exception as e:                        # noqa: BLE001
            log.error("camera %s: %s", cid, e)        # the message may name the URI; it can no longer name the password
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
        shm = SHM.format(path=cam["live_shm"][len("shm://"):]) if cam.get("live_shm") else IDLE
        return DESC.format(watchdog=self.watchdog, live=live, shm=shm)

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
    """The recorder's: `rtspsrc` on the camera's fan-out URL — or `shmsrc` on the worker's shared-memory
    branch when the worker is on this server — then `archivesink` into the spool under the recorder's
    epoch. No fan-out of its own; nothing here reads a camera."""

    def __init__(self, spool: str, archive: str, segment_seconds: int = 600, watchdog_ms: int = 8000):
        self.spool, self.archive, self.seg, self.watchdog = spool, archive, segment_seconds, watchdog_ms
        self.fanout = None
        self.range_error = ""                            # why the last range pipeline failed, if it did (Lesson 16)
        self.pipelines, self.dead, self.posted = {}, [], []

    # One range from the device's own archive, run to completion: the pipeline ends by itself at EOS, and
    # every fragment it closed was promoted with `source="edge"` by the caller. Returns what landed in the
    # spool, so `RecWorker.fetch` can drop anything live recording reached first.
    def record_range(self, cam, source: str, epoch: int, t0: float, t1: float, spool: str, seg: int = 600) -> list[str]:
        import os
        from vms.archive import parse
        before = {os.path.join(d, f) for d, _, fs in os.walk(spool) for f in fs}
        p = Gst.parse_launch(REC_RANGE_DESC.format(source=source, cam=int(cam), epoch=epoch, spool=spool,
                                                   archive=self.archive, seg=seg))
        p.set_state(Gst.State.PLAYING)
        msg = p.get_bus().timed_pop_filtered(Gst.CLOCK_TIME_NONE, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        p.send_event(Gst.Event.new_eos())                # finalize whatever fragment is open
        p.set_state(Gst.State.NULL)
        self.range_error = ""
        if msg is not None and msg.type == Gst.MessageType.ERROR:
            log.error("camera %s: backfill %s-%s: %s", cam, t0, t1, msg.parse_error()[0])
            # Said, not only logged: a range that failed half way is not a range the source does not have,
            # and the recorder must not remember it as "nowhere" (Lesson 16).
            self.range_error = str(msg.parse_error()[0])
        after = {os.path.join(d, f) for d, _, fs in os.walk(spool) for f in fs}
        return sorted(p for p in after - before if parse(p, spool))

    # A range copied out of ANOTHER archive of ours — a backup recording's segment, served by its recorder's
    # door (Lesson 26). `source` is the segment's URL and `#<start>` the time its file begins at; the range
    # is the part of it to take. The same demux into the same sink as `record_range`, seeked to the range, so
    # the copy is the footage as stored and not a re-encode. Not exercised by the test suite, which has no
    # GStreamer; the fake writes the files.
    def copy_range(self, cam, source: str, epoch: int, t0: float, t1: float, spool: str, seg: int = 600) -> list[str]:
        import os
        from vms.archive import parse
        url, _, start = source.partition("#")
        begins = float(start or t0)
        before = {os.path.join(d, f) for d, _, fs in os.walk(spool) for f in fs}
        p = Gst.parse_launch(REC_RANGE_DESC.format(source=url, cam=cam, epoch=epoch, spool=spool,
                                                   archive=self.archive, seg=seg))
        p.set_state(Gst.State.PAUSED)
        p.get_state(Gst.CLOCK_TIME_NONE)
        p.seek(1.0, Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE, Gst.SeekType.SET,
               int((t0 - begins) * Gst.SECOND), Gst.SeekType.SET, int((t1 - begins) * Gst.SECOND))
        p.set_state(Gst.State.PLAYING)
        msg = p.get_bus().timed_pop_filtered(Gst.CLOCK_TIME_NONE, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        p.send_event(Gst.Event.new_eos())
        p.set_state(Gst.State.NULL)
        self.range_error = ""
        if msg is not None and msg.type == Gst.MessageType.ERROR:
            log.error("recording %s: copy %s-%s: %s", cam, t0, t1, msg.parse_error()[0])
            self.range_error = str(msg.parse_error()[0])
        after = {os.path.join(d, f) for d, _, fs in os.walk(spool) for f in fs}
        return sorted(p for p in after - before if parse(p, spool))

    def describe(self, cam: dict) -> str:
        kw = dict(watchdog=self.watchdog, cam=cam["id"], epoch=cam.get("epoch", 0), spool=self.spool, archive=self.archive, seg=self.seg)
        if cam["source"].startswith("shm://"):                                 # the worker is on this server: read its tee's shared memory
            return REC_SHM_DESC.format(path=cam["source"][len("shm://"):], **kw)
        return REC_DESC.format(source=cam["source"], **kw)
