# archivesink.py — the sink element: splitmuxsink into the spool, and on fragment-closed, promote into the archive resource

**Role in the module.** Lesson 3, Track 2 (needs `gi`). A GStreamer bin registered as `archivesink` that wraps `splitmuxsink` (muxer `mp4mux`) and gives it the VMS's layout: every fragment is written into the spool at `<spool>/vms/<camera>/e<epoch>/<start>Z.mp4`, and when splitmuxsink reports a fragment closed the element calls `vms.archive.ArchiveResource.promote` — rename into the archive, then the manifest line: the acknowledgement order of М9 Lesson 4. The epoch is a property the worker sets when it starts the camera (`GstActuator` puts `cam["epoch"]` into the launch string), so it is in every path this element writes and a zombie's footage is in its own `e<n>` directory. This is "the archive as a resource, from the pipeline's side"; the resource itself is `vms/archive.py`. Used by `actuator.py`; try it with `gst-launch-1.0 driverpacksrc uri=… ! h264parse ! archivesink camera=7 epoch=3 spool=/data/spool archive=/data/archive segment-seconds=600`.

## Module-level names
- `gi.require_version`, `Gst.init(None)` at import; `GObject.type_register(ArchiveSink)` and `Gst.Element.register(None, "archivesink", …)` at the bottom register the factory by name.
- The import `from vms.archive import ArchiveResource, segment_path` is the one place `gstvms` depends on `vms`.

## `class ArchiveSink(Gst.Bin)`
A bin around one `splitmuxsink` named `mux`, with a ghost `sink` pad on the muxer's requested `video` pad. `__gproperties__` declares five read/write properties: `camera` (int id), `epoch` (int, "fencing epoch, in every path"), `spool` (root, default `/data/spool`), `archive` (resource root, default `/data/archive`), `segment-seconds` (1–86400, default 600). State: `props_` (the property values), `mux`, `resource` (an `ArchiveResource`, created lazily), `promoted` (a counter).

### `__init__(self)`
Defaults in `props_`; creates `splitmuxsink` with `muxer-factory=mp4mux` and `async-finalize=True` (the closed fragment is finalized on a separate thread so the next one opens without a gap); adds it, ghosts its `video` request pad as this bin's `sink`; connects `format-location` to `_location`. `resource` stays `None` until the first fragment asks for a location.

### `do_get_property(self, prop)` / `do_set_property(self, prop, value)`
Read and write `props_` by property name. Setting `segment-seconds` also sets splitmuxsink's `max-size-time` to that many seconds in nanoseconds — the segment length. The other properties are only read when a location is asked for, so they must be set before the pipeline starts (the actuator's launch string sets all five).

### `_location(self, mux, fragment_id)`
splitmuxsink's `format-location` callback: creates the `ArchiveResource(spool, archive)` on first use (which creates both roots), takes the current UTC time to the second as the segment start, and returns `segment_path(spool, camera, epoch, start)` after creating its directory. So segments are named by wall-clock start, and `vms.archive.parse` can read camera, epoch and start back from the path. `fragment_id` is not used: the timestamp is the name.

### `do_handle_message(self, msg)`
The bin's bus-message hook. On an element message named `splitmuxsink-fragment-closed` — "the acknowledgement point" — take its `location` string and `self.resource.promote(path)`; count it in `promoted`. Any exception (the archive unreachable, a different filesystem failing to copy) is logged as a GStreamer warning and the segment stays in the spool — where `ArchiveResource.closed_in_spool` finds it on the worker's next start and promotes it then. The message is then passed to `Gst.Bin.do_handle_message` so it still reaches the pipeline's bus (where `GstActuator._posted` filters it out as plumbing).

## Notes
- Ordering, as the archive's docstring states it: a closed segment is promoted (moved, then indexed) and the spool copy is gone only after that. This element never writes the manifest itself and never deletes anything.
- On an orderly stop the actuator sends EOS before `NULL`, which makes splitmuxsink close the open fragment and post `fragment-closed`, so the last segment is promoted; a `kill -9` loses exactly that open segment (`vmsworker@.container`'s comment on `StopTimeout`).
