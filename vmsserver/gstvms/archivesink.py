"""archivesink — the archive as a resource, from the pipeline's side.

A sink bin wrapping splitmuxsink. Segments are written into the SPOOL
under <cam>/e<epoch>/<start>Z.mp4; on fragment-closed the segment is
PROMOTED into the archive resource and its manifest line appended — the
acknowledgement order of М9 Lesson 4. The epoch is a property set by the
worker when it starts the camera; it is in every path this element writes.

    gst-launch-1.0 driverpacksrc uri=driverpack://file/lobby.mp4 ! h264parse \\
        ! archivesink camera=7 epoch=3 spool=/data/spool archive=/data/archive segment-seconds=600
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # archivesink.py — the sink element: splitmuxsink into the spool, and on fragment-closed, promote into the
# archive resource
#
# **Role in the module.** Lesson 3, Track 2 (needs `gi`). A GStreamer bin registered as `archivesink` that
# wraps `splitmuxsink` (muxer `mp4mux`) and gives it the VMS's layout: every fragment is written into the
# spool at `<spool>/vms/<camera>/e<epoch>/<start>Z.mp4`, and when splitmuxsink reports a fragment closed the
# element calls `vms.archive.ArchiveResource.promote` — rename into the archive, then the manifest line: the
# acknowledgement order of М9 Lesson 4. The epoch is a property the worker sets when it starts the camera
# (`GstActuator` puts `cam["epoch"]` into the launch string), so it is in every path this element writes and
# a zombie's footage is in its own `e<n>` directory. This is "the archive as a resource, from the pipeline's
# side"; the resource itself is `vms/archive.py`. Used by `actuator.py`; try it with `gst-launch-1.0
# driverpacksrc uri=… ! h264parse ! archivesink camera=7 epoch=3 spool=/data/spool archive=/data/archive
# segment-seconds=600`.
#
# ## Module-level names
# - `gi.require_version`, `Gst.init(None)` at import; `GObject.type_register(ArchiveSink)` and
#   `Gst.Element.register(None, "archivesink", …)` at the bottom register the factory by name.
# - The import `from vms.archive import ArchiveResource, segment_path` is the one place `gstvms` depends on
#   `vms`.
#
# ## Notes
# - Ordering, as the archive's docstring states it: a closed segment is promoted (moved, then indexed) and
#   the spool copy is gone only after that. This element never writes the manifest itself and never deletes
#   anything.
# - On an orderly stop the actuator sends EOS before `NULL`, which makes splitmuxsink close the open
#   fragment and post `fragment-closed`, so the last segment is promoted; a `kill -9` loses exactly that
#   open segment (`vmsworker@.container`'s comment on `StopTimeout`).
# ================================================================================================
from __future__ import annotations

import os
from datetime import datetime, timezone

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GObject, Gst  # noqa: E402

from vms.archive import ArchiveResource, segment_path  # noqa: E402

Gst.init(None)


# A bin around one `splitmuxsink` named `mux`, with a ghost `sink` pad on the muxer's requested `video` pad.
# `__gproperties__` declares five read/write properties: `camera` (int id), `epoch` (int, "fencing epoch, in
# every path"), `spool` (root, default `/data/spool`), `archive` (resource root, default `/data/archive`),
# `segment-seconds` (1–86400, default 600). State: `props_` (the property values), `mux`, `resource` (an
# `ArchiveResource`, created lazily), `promoted` (a counter).
class ArchiveSink(Gst.Bin):
    __gstmetadata__ = ("Archive sink", "Sink/Video", "Segments into the spool, promoted to the archive resource", "edge-vms-course")
    __gproperties__ = {
        "camera": (int, "camera", "camera id", 0, 2 ** 31 - 1, 0, GObject.ParamFlags.READWRITE),
        "epoch": (int, "epoch", "fencing epoch, in every path", 0, 2 ** 31 - 1, 0, GObject.ParamFlags.READWRITE),
        "spool": (str, "spool", "spool root", "/data/spool", GObject.ParamFlags.READWRITE),
        "archive": (str, "archive", "archive resource root", "/data/archive", GObject.ParamFlags.READWRITE),
        "segment-seconds": (int, "segment-seconds", "segment length", 1, 86400, 600, GObject.ParamFlags.READWRITE),
        # Name each segment by the time its first frame was CAPTURED rather than the time it was opened.
        # The same for a live recording to within a buffer; thirty seconds apart for a prebuffer released
        # from a ring (Lesson 26), which is why the recorder sets it there.
        "capture-times": (bool, "capture-times", "name segments by capture time", False, GObject.ParamFlags.READWRITE),
    }

    # Defaults in `props_`; creates `splitmuxsink` with `muxer-factory=mp4mux` and `async-finalize=True`
    # (the closed fragment is finalized on a separate thread so the next one opens without a gap); adds it,
    # ghosts its `video` request pad as this bin's `sink`; connects `format-location` to `_location`.
    # `resource` stays `None` until the first fragment asks for a location.
    def __init__(self):
        super().__init__()
        self.props_ = {"camera": 0, "epoch": 0, "spool": "/data/spool", "archive": "/data/archive", "segment-seconds": 600,
                       "capture-times": False}
        self.mux = Gst.ElementFactory.make("splitmuxsink", "mux")
        self.mux.set_property("muxer-factory", "mp4mux")
        self.mux.set_property("async-finalize", True)
        self.add(self.mux)
        self.add_pad(Gst.GhostPad.new("sink", self.mux.get_request_pad("video")))
        self.mux.connect("format-location", self._location)
        self.mux.connect("format-location-full", self._location_full)
        self.resource: ArchiveResource | None = None
        self.promoted = 0

    # Read and write `props_` by property name. Setting `segment-seconds` also sets splitmuxsink's
    # `max-size-time` to that many seconds in nanoseconds — the segment length. The other properties are
    # only read when a location is asked for, so they must be set before the pipeline starts (the actuator's
    # launch string sets all five).
    def do_get_property(self, prop):
        return self.props_[prop.name]

    def do_set_property(self, prop, value):
        self.props_[prop.name] = value
        if prop.name == "segment-seconds":
            self.mux.set_property("max-size-time", int(value) * Gst.SECOND)

    # splitmuxsink's `format-location` callback: creates the `ArchiveResource(spool, archive)` on first use
    # (which creates both roots), takes the current UTC time to the second as the segment start, and returns
    # `segment_path(spool, camera, epoch, start)` after creating its directory. So segments are named by
    # wall-clock start, and `vms.archive.parse` can read camera, epoch and start back from the path.
    # `fragment_id` is not used: the timestamp is the name.
    def _location(self, mux, fragment_id, start: datetime | None = None):
        if self.resource is None:
            self.resource = ArchiveResource(self.props_["spool"], self.props_["archive"])
        start = start or datetime.now(timezone.utc).replace(microsecond=0)
        p = segment_path(self.props_["spool"], self.props_["camera"], self.props_["epoch"], start)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    # splitmuxsink asks with the fragment's first sample when a handler for this signal exists; it then does
    # not ask `format-location`. With `capture-times` the sample's running time is turned into a wall time —
    # now, minus how long ago on the pipeline clock that buffer was — and the segment is named by it.
    def _location_full(self, mux, fragment_id, first_sample):
        if not self.props_["capture-times"] or first_sample is None:
            return self._location(mux, fragment_id)
        import time
        buf, seg = first_sample.get_buffer(), first_sample.get_segment()
        clock, base = self.get_clock(), self.get_base_time()
        if buf is None or seg is None or clock is None or buf.pts == Gst.CLOCK_TIME_NONE:
            return self._location(mux, fragment_id)
        age = (clock.get_time() - base - seg.to_running_time(Gst.Format.TIME, buf.pts)) / Gst.SECOND
        start = datetime.fromtimestamp(time.time() - max(0.0, age), timezone.utc).replace(microsecond=0)
        return self._location(mux, fragment_id, start)

    # The bin's bus-message hook. On an element message named `splitmuxsink-fragment-closed` — "the
    # acknowledgement point" — take its `location` string and `self.resource.promote(path)`; count it in
    # `promoted`. Any exception (the archive unreachable, a different filesystem failing to copy) is logged
    # as a GStreamer warning and the segment stays in the spool — where `ArchiveResource.closed_in_spool`
    # finds it on the worker's next start and promotes it then. The message is then passed to
    # `Gst.Bin.do_handle_message` so it still reaches the pipeline's bus (where `GstActuator._posted`
    # filters it out as plumbing).
    def do_handle_message(self, msg):
        """splitmuxsink-fragment-closed: the acknowledgement point."""
        s = msg.get_structure()
        if s and s.get_name() == "splitmuxsink-fragment-closed":
            path = s.get_string("location")
            try:
                self.resource.promote(path)
                self.promoted += 1
            except Exception as e:                     # noqa: BLE001 — the resource is unreachable; the spool keeps it
                Gst.warning(f"archivesink: promote failed for {path}: {e}")
        Gst.Bin.do_handle_message(self, msg)


GObject.type_register(ArchiveSink)
Gst.Element.register(None, "archivesink", Gst.Rank.NONE, ArchiveSink)
