# driverpacksrc.py — the source element: a media file played in a loop as if it were a camera, with PTS rebased across each loop

**Role in the module.** Lesson 2, Track 2 (needs `gi`). A GStreamer bin registered as the element `driverpacksrc`, whose `uri` property names what the real DriverPack would open. Here `driverpack://file/<name>` plays `<MEDIA_DIR>/<name>`, paced by its own timestamps, and on EOS seeks back to zero while carrying a running offset forward so the pipeline's running time never goes backwards. The docstring says why this file exists at all: the rebasing is "the one non-mechanical part of any source element — the part a real DriverPack does with a vendor SDK's clock". Everything downstream (`h264parse`, `watchdog`, `archivesink`) sees one monotonic stream. The Lesson 2 deliverable is this element running for an hour with monotonic PTS (README: "the hour on a box with GStreamer"). Used by `actuator.py` through `Gst.parse_launch`; try it with `gst-launch-1.0 driverpacksrc uri=driverpack://file/lobby.mp4 ! h264parse ! fakesink -v`.

## Module-level names
- `gi.require_version("Gst", "1.0")`, `Gst.init(None)` — run at import; importing this module initialises GStreamer.
- The two calls at the bottom, `GObject.type_register(DriverPackSrc)` and `Gst.Element.register(None, "driverpacksrc", Gst.Rank.NONE, DriverPackSrc)`, make the class an element factory by name. `test_the_element_runs_when_gstreamer_is_present` imports the module and asserts `Gst.ElementFactory.make("driverpacksrc")` is not `None`.

## `class DriverPackSrc(Gst.Bin)`
A bin of `filesrc ! qtdemux ! h264parse ! identity sync=true`, with one ghost `src` pad on the identity's output. `__gstmetadata__` names it "DriverPack source", class `Source/Video`. `__gproperties__` declares one read/write string property, `uri`. State: `uri`, the four child elements (`src`, `demux`, `parse`, `pace`), `offset` (nanoseconds accumulated across loops) and `last_pts` (the last rebased PTS seen).

### `__init__(self)`
Creates and adds the four elements; links `filesrc → qtdemux` statically, `h264parse → identity` statically, and `qtdemux → h264parse` dynamically on `pad-added` (a demuxer's pads appear once it has read the file). `identity sync=True` is the pacing: a file has no clock, so buffers are held until the pipeline clock reaches their PTS — this is what makes a file behave like a live camera and gives `archivesink` real ten-minute segments. Adds two pad probes on the identity's src pad: a BUFFER probe (`_rebase`) and a downstream EVENT probe (`_on_event`).

### `do_get_property(self, prop)` / `do_set_property(self, prop, value)`
The one property. Setting `uri` stores it and sets `filesrc.location` to `uri.resolve(value)` — so a vendor URI or a bad name raises `ValueError` from the property setter, which `Gst.parse_launch` in the actuator turns into a failed start (logged, `False` returned, the reconciler backs off).

### `_on_pad(self, demux, pad)`
Links the demuxer's pad to `h264parse` only if its caps start with `video/x-h264`; audio or other tracks are ignored.

### `_rebase(self, pad, info)`
The BUFFER probe: for every buffer with a valid PTS, add `offset`, set DTS equal to the new PTS, remember it as `last_pts`. Returns `OK` (the buffer passes). On the first loop `offset` is 0 and buffers are untouched.

### `_on_event(self, pad, info)`
The downstream EVENT probe: on EOS, do not let it through — set `offset = last_pts + 1/25 s` (one frame past the last buffer, so the next file start lands strictly after it), flush-seek `filesrc` back to 0 on a key unit, and `DROP` the event. Every other event passes. Because the EOS never reaches downstream, `splitmuxsink` keeps its segment open across the loop and `watchdog` sees no gap; because `offset` grows by the file's length each time, PTS keeps increasing for as long as the process runs.

## Notes
- The `1/25 s` step assumes a 25 fps source; a file at another rate still loops and stays monotonic, only the gap at the seam differs.
- Nothing here knows about cameras, epochs or the archive: the element is a source. The epoch is a property of `archivesink`, set by the worker.
