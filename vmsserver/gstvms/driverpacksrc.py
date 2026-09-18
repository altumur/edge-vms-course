"""driverpacksrc — a source element whose URI names what the real DriverPack
will open. In this course: driverpack://file/<name> plays <MEDIA_DIR>/<name>
in a loop, paced by its own timestamps, with PTS rebased across each loop
so the pipeline's running time never goes backwards. That rebasing is the
one non-mechanical part of any source element — the part a real DriverPack
does with a vendor SDK's clock — which is why it is built and tested here.

    gst-launch-1.0 driverpacksrc uri=driverpack://file/lobby.mp4 ! h264parse ! fakesink -v
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # driverpacksrc.py — the source element: a media file played in a loop as if it were a camera, with PTS
# rebased across each loop
#
# **Role in the module.** Lesson 2, Track 2 (needs `gi`). A GStreamer bin registered as the element
# `driverpacksrc`, whose `uri` property names what the real DriverPack would open. Here
# `driverpack://file/<name>` plays `<MEDIA_DIR>/<name>`, paced by its own timestamps, and on EOS seeks back
# to zero while carrying a running offset forward so the pipeline's running time never goes backwards. The
# docstring says why this file exists at all: the rebasing is "the one non-mechanical part of any source
# element — the part a real DriverPack does with a vendor SDK's clock". Everything downstream (`h264parse`,
# `watchdog`, `archivesink`) sees one monotonic stream. The Lesson 2 deliverable is this element running for
# an hour with monotonic PTS (README: "the hour on a box with GStreamer"). Used by `actuator.py` through
# `Gst.parse_launch`; try it with `gst-launch-1.0 driverpacksrc uri=driverpack://file/lobby.mp4 ! h264parse
# ! fakesink -v`.
#
# ## Module-level names
# - `gi.require_version("Gst", "1.0")`, `Gst.init(None)` — run at import; importing this module initialises
#   GStreamer.
# - The two calls at the bottom, `GObject.type_register(DriverPackSrc)` and `Gst.Element.register(None,
#   "driverpacksrc", Gst.Rank.NONE, DriverPackSrc)`, make the class an element factory by name.
#   `test_the_element_runs_when_gstreamer_is_present` imports the module and asserts
#   `Gst.ElementFactory.make("driverpacksrc")` is not `None`.
#
# ## Notes
# - The `1/25 s` step assumes a 25 fps source; a file at another rate still loops and stays monotonic, only
#   the gap at the seam differs.
# - Nothing here knows about cameras, epochs or the archive: the element is a source. The epoch is a
#   property of `archivesink`, set by the worker.
# ================================================================================================
from __future__ import annotations

import os

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GObject, Gst  # noqa: E402

Gst.init(None)


from .uri import resolve  # noqa: E402


# A bin of `filesrc ! qtdemux ! h264parse ! identity sync=true`, with one ghost `src` pad on the identity's
# output. `__gstmetadata__` names it "DriverPack source", class `Source/Video`. `__gproperties__` declares
# one read/write string property, `uri`. State: `uri`, the four child elements (`src`, `demux`, `parse`,
# `pace`), `offset` (nanoseconds accumulated across loops) and `last_pts` (the last rebased PTS seen).
class DriverPackSrc(Gst.Bin):
    __gstmetadata__ = ("DriverPack source", "Source/Video", "Plays a media file as if it were a camera", "edge-vms-course")
    # Three properties, not one. `user` and `password` are what the REAL DriverPack takes for a vendor
    # camera, and this stand-in declares the same surface so the worker's code path is the same one that
    # runs in production: set after `parse_launch`, never inside the launch string. A file has no login, so
    # this element stores them and opens the file regardless — a stand-in that refused them would push the
    # difference into the worker, which is the one place it must not be.
    __gproperties__ = {
        "uri": (str, "uri", "driverpack://file/<name>", "", GObject.ParamFlags.READWRITE),
        "user": (str, "user", "the device login; unused by driverpack://file/", "", GObject.ParamFlags.READWRITE),
        "password": (str, "password", "the device password; unused by driverpack://file/", "", GObject.ParamFlags.READWRITE),
    }

    # Creates and adds the four elements; links `filesrc → qtdemux` statically, `h264parse → identity`
    # statically, and `qtdemux → h264parse` dynamically on `pad-added` (a demuxer's pads appear once it has
    # read the file). `identity sync=True` is the pacing: a file has no clock, so buffers are held until the
    # pipeline clock reaches their PTS — this is what makes a file behave like a live camera and gives
    # `archivesink` real ten-minute segments. Adds two pad probes on the identity's src pad: a BUFFER probe
    # (`_rebase`) and a downstream EVENT probe (`_on_event`).
    def __init__(self):
        super().__init__()
        self.uri = ""
        self.user = self.password = ""
        self.src = Gst.ElementFactory.make("filesrc", "file")
        self.demux = Gst.ElementFactory.make("qtdemux", "demux")
        self.parse = Gst.ElementFactory.make("h264parse", "parse")
        self.pace = Gst.ElementFactory.make("identity", "pace")
        self.pace.set_property("sync", True)              # a file has no clock; the pipeline's is used
        for e in (self.src, self.demux, self.parse, self.pace):
            self.add(e)
        self.src.link(self.demux)
        self.demux.connect("pad-added", self._on_pad)
        self.parse.link(self.pace)
        self.add_pad(Gst.GhostPad.new("src", self.pace.get_static_pad("src")))
        self.offset = 0                                    # the rebasing: accumulated across loops
        self.pace.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._rebase)
        self.pace.get_static_pad("src").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_event)
        self.last_pts = 0

    # The one property. Setting `uri` stores it and sets `filesrc.location` to `uri.resolve(value)` — so a
    # vendor URI or a bad name raises `ValueError` from the property setter, which `Gst.parse_launch` in the
    # actuator turns into a failed start (logged, `False` returned, the reconciler backs off).
    def do_get_property(self, prop):
        return getattr(self, prop.name, "")

    # The credential is stored and nothing else: a file is opened by path. `repr` of this element never
    # shows it, because `do_get_property` is the only way out and nothing prints all three.
    def do_set_property(self, prop, value):
        if prop.name in ("user", "password"):
            setattr(self, prop.name, value)
            return
        self.uri = value
        self.src.set_property("location", resolve(value))

    # Links the demuxer's pad to `h264parse` only if its caps start with `video/x-h264`; audio or other
    # tracks are ignored.
    def _on_pad(self, demux, pad):
        if pad.get_current_caps().to_string().startswith("video/x-h264"):
            pad.link(self.parse.get_static_pad("sink"))

    # The BUFFER probe: for every buffer with a valid PTS, add `offset`, set DTS equal to the new PTS,
    # remember it as `last_pts`. Returns `OK` (the buffer passes). On the first loop `offset` is 0 and
    # buffers are untouched.
    def _rebase(self, pad, info):
        buf = info.get_buffer()
        if buf.pts != Gst.CLOCK_TIME_NONE:
            buf.pts += self.offset
            buf.dts = buf.pts
            self.last_pts = buf.pts
        return Gst.PadProbeReturn.OK

    # The downstream EVENT probe: on EOS, do not let it through — set `offset = last_pts + 1/25 s` (one
    # frame past the last buffer, so the next file start lands strictly after it), flush-seek `filesrc` back
    # to 0 on a key unit, and `DROP` the event. Every other event passes. Because the EOS never reaches
    # downstream, `splitmuxsink` keeps its segment open across the loop and `watchdog` sees no gap; because
    # `offset` grows by the file's length each time, PTS keeps increasing for as long as the process runs.
    def _on_event(self, pad, info):
        ev = info.get_event()
        if ev.type == Gst.EventType.EOS:
            # loop: seek to zero and carry the offset forward so PTS keeps increasing
            self.offset = self.last_pts + Gst.SECOND // 25
            self.src.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 0)
            return Gst.PadProbeReturn.DROP
        return Gst.PadProbeReturn.OK


GObject.type_register(DriverPackSrc)
Gst.Element.register(None, "driverpacksrc", Gst.Rank.NONE, DriverPackSrc)
