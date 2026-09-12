"""Lesson 7 — fifty pipelines in one process.

A CameraPipeline is a state machine, not a coroutine:

    IDLE -> STARTING -> RUNNING -> FAILED -> (backoff, Lesson 6) -> STARTING

Buffers move on GStreamer's own threads, in C. Python here does control only:
state changes, bus messages, and naming a file once per segment.

    Banned in the recording path: appsink, identity handoff, buffer probes.
    Fine: bus messages, state changes, splitmuxsink::format-location.

GStreamer is imported lazily so the reconciler and its tests never need it.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Callable

from .secrets import compose_rtsp_url, redact

log = logging.getLogger("apphost.pipeline")

IDLE, STARTING, RUNNING, FAILED = "idle", "starting", "running", "failed"

# Ten minutes in nanoseconds is the number the lesson quotes; SEGMENT_SECONDS
# is the knob. protocols=tcp: RTSP over UDP with loss gives you broken files,
# not worse pictures. watchdog: stall detection in C, reported on the bus.
DESC = (
    "rtspsrc location={url} protocols=tcp latency={latency} name=src ! "
    "rtph264depay ! h264parse ! "
    "watchdog timeout={watchdog} ! "
    "splitmuxsink name=mux max-size-time={segment_ns} "
    "muxer-factory=mp4mux async-finalize=true"
)

_gst = None


def gst():
    """Import PyGObject/GStreamer on first use."""
    global _gst
    if _gst is None:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst  # type: ignore
        Gst.init(None)
        _gst = Gst
    return _gst


SegmentClosed = Callable[[int, datetime, datetime, str, int], None]


class CameraPipeline:
    def __init__(self, cam: dict, settings, key, on_segment_closed: SegmentClosed):
        self.cam = cam
        self.id = cam["id"]
        self.settings = settings
        self.key = key
        self.on_segment_closed = on_segment_closed
        self.state = IDLE
        self.pipeline = None
        self.last_error: str | None = None
        self.segments_written = 0
        self._open_since: datetime | None = None
        self._open_path: str | None = None

    # -- paths ------------------------------------------------------------
    def segment_dir(self) -> str:
        # epoch is in the path from day one. It is 1 and never changes in
        # М9; in М11 it is the fencing token that makes a zombie's writes
        # land where nobody reads them (Lesson 8, Step 5).
        return os.path.join(self.settings.archive_dir, str(self.id), f"e{self.settings.epoch}")

    def _format_location(self, mux, fragment_id: int) -> str:
        # Once per segment: control rate, therefore allowed. Runs in a
        # streaming thread, so it does nothing but name a file.
        # On restart, never resume the previous segment: the name is the
        # wall-clock start, so a restarted instance always opens a new one.
        self._open_since = datetime.now(timezone.utc)
        self._open_path = os.path.join(
            self.segment_dir(), self._open_since.strftime("%Y%m%dT%H%M%SZ") + ".mp4")
        return self._open_path

    # -- control ----------------------------------------------------------
    def start(self) -> bool:
        Gst = gst()
        os.makedirs(self.segment_dir(), exist_ok=True)
        url = compose_rtsp_url(self.cam, self.key)     # decrypts, in memory only
        desc = DESC.format(url=url, latency=self.settings.rtsp_latency_ms,
                           watchdog=self.settings.watchdog_ms,
                           segment_ns=self.settings.segment_seconds * 1_000_000_000)
        del url                                        # never log it; log cam["rtsp_url"]
        try:
            self.pipeline = Gst.parse_launch(desc)
        except Exception as e:                         # noqa: BLE001
            self.last_error = redact(str(e))
            self.state = FAILED
            return False
        self.pipeline.get_by_name("mux").connect("format-location", self._format_location)
        self.state = STARTING
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            self.last_error = "set_state(PLAYING) failed"
            self._teardown()
            self.state = FAILED
            return False
        log.info("camera %s starting %s", self.id, self.cam["rtsp_url"])
        return True

    def stop(self) -> None:
        log.info("camera %s stopping", self.id)
        self._teardown()
        self.state = IDLE

    def _teardown(self) -> None:
        if self.pipeline is not None:
            Gst = gst()
            # EOS lets splitmuxsink finalize the open fragment; NULL releases
            # every native thread. Then drop the reference or PSS grows.
            self.pipeline.send_event(Gst.Event.new_eos())
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    # -- bus --------------------------------------------------------------
    def pump(self) -> bool:
        """Drain this pipeline's bus without blocking. Returns False if the
        pipeline has failed and must be forgotten by the reconciler."""
        if self.pipeline is None:
            return self.state != FAILED
        Gst = gst()
        bus = self.pipeline.get_bus()
        alive = True
        while True:
            msg = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS |
                                   Gst.MessageType.STATE_CHANGED | Gst.MessageType.ELEMENT)
            if msg is None:
                break
            alive = self._handle(msg) and alive
        return alive

    def _handle(self, msg) -> bool:
        Gst = gst()
        t = msg.type
        if t == Gst.MessageType.STATE_CHANGED:
            if msg.src == self.pipeline:
                _, new, _ = msg.parse_state_changed()
                if new == Gst.State.PLAYING and self.state == STARTING:
                    self.state = RUNNING
            return True
        if t == Gst.MessageType.ELEMENT:
            s = msg.get_structure()
            name = s.get_name() if s is not None else ""
            if name == "splitmuxsink-fragment-closed":
                self._closed(s.get_string("location"))
            return True
        if t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            # A watchdog timeout arrives here too: "Watchdog triggered".
            self.last_error = redact(f"{err.message} ({dbg or ''})")
            log.warning("camera %s error: %s", self.id, self.last_error)
            self._teardown()
            self.state = FAILED
            return False
        if t == Gst.MessageType.EOS:
            self.last_error = "EOS from camera"
            self._teardown()
            self.state = FAILED
            return False
        return True

    def _closed(self, path: str | None) -> None:
        path = path or self._open_path
        if not path or self._open_since is None:
            return
        end = datetime.now(timezone.utc)
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        self.segments_written += 1
        # The spool became an archive because somebody kept a record of it.
        self.on_segment_closed(self.id, self._open_since, end, path, size)


class GstActuator:
    """The real actuator: verb -> bool, as the reconciler expects. Owns the
    dict of CameraPipeline state machines. The backoff policy in the
    reconciler needed no change when this replaced print()."""

    def __init__(self, settings, key, on_segment_closed: SegmentClosed):
        self.settings = settings
        self.key = key
        self.on_segment_closed = on_segment_closed
        self.pipelines: dict[int, CameraPipeline] = {}

    def __call__(self, verb: str, cam: dict) -> bool:
        cid = cam["id"]
        if verb in ("stop", "restart") and cid in self.pipelines:
            self.pipelines.pop(cid).stop()
        if verb == "stop":
            return True
        p = CameraPipeline(cam, self.settings, self.key, self.on_segment_closed)
        ok = p.start()
        if ok:
            self.pipelines[cid] = p
        else:
            log.warning("camera %s failed to start: %s", cid, p.last_error)
        return ok

    def pump(self) -> list[int]:
        """Drain every bus. Returns the ids of pipelines that died."""
        dead = []
        for cid, p in list(self.pipelines.items()):
            if not p.pump():
                dead.append(cid)
                del self.pipelines[cid]
        return dead

    def state(self, cid: int) -> str:
        p = self.pipelines.get(cid)
        return p.state if p else IDLE

    def stop_all(self) -> None:
        for p in self.pipelines.values():
            p.stop()
        self.pipelines.clear()


class FakeActuator:
    """Lesson 6's print(), grown a memory so tests can assert on it.
    `failing` is a set of camera ids (or a predicate) whose start fails."""

    def __init__(self, failing=frozenset(), log_calls: bool = False):
        self.failing = failing
        self.calls: list[tuple[str, int]] = []
        self.running: set[int] = set()
        self.log_calls = log_calls

    def __call__(self, verb: str, cam: dict) -> bool:
        cid = cam["id"]
        self.calls.append((verb, cid))
        if self.log_calls:
            print(f"  actuator: {verb} camera {cid} (rev {cam.get('revision')})")
        if verb == "stop":
            self.running.discard(cid)
            return True
        fails = self.failing(cid) if callable(self.failing) else cid in self.failing
        if fails:
            self.running.discard(cid)
            return False
        self.running.add(cid)
        return True

    def pump(self) -> list[int]:
        return []

    def stop_all(self) -> None:
        self.running.clear()
