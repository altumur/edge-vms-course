# actuator.py — the worker's verb → pipeline: `driverpacksrc ! h264parse ! watchdog ! tee ! archivesink` per camera, the bus drained into (dead, posted)

**Role in the module.** Lesson 4, Track 2 (needs `gi`). The real actuator `VmsWorker` runs its reconciler against when GStreamer is present (`vms/__main__.worker` tries to import it and falls back to `FakeActuator`). It has the same three-part surface as the fake — callable `(verb, cam) -> bool`, `pump() -> (dead, posted)`, `stop_all()` — so the worker does not know which it holds. It builds one `Gst.Pipeline` per camera from a launch string, keeps them in a dict, and turns bus messages into two lists the worker drains on every pass: cameras whose pipeline errored (`dead`) and element messages that are observations (`posted`). "The element never knows about buckets or epochs: it posts what it saw; the worker, which holds the epoch, turns it into a line." Importing `archivesink` and `driverpacksrc` here registers both elements.

## Module-level names
- `log` — logger `gstvms`.
- `DESC` — the launch template: `driverpacksrc uri={uri} name=src ! h264parse ! watchdog timeout={watchdog} ! tee name=t`, then branch one `t. ! queue ! archivesink name=sink camera={cam} epoch={epoch} spool={spool} archive={archive} segment-seconds={seg}`, and branch two `t. ! queue leaky=downstream max-size-buffers=30 ! fakesink sync=false` — a leaky queue into a fakesink "until М12's gateway subscribes to it" (a live viewer branch that never blocks recording). `watchdog` posts an error if no buffer passes for `timeout` ms, which is how a stalled source becomes a dead camera.

## `class GstActuator`
State: `spool`, `archive`, `seg` (segment seconds), `watchdog` (ms), `pipelines` (`{camera id: Gst.Pipeline}`), `dead` (ids whose bus posted an error since the last pump), `posted` (`(camera, kind, fields)` since the last pump).

### `__init__(self, spool, archive, segment_seconds=600, watchdog_ms=8000)`
Stores the knobs; `__main__` passes `$SPOOL`, `$ARCHIVE`, `$SEGMENT_SECONDS`.

### `__call__(self, verb, cam) -> bool`
The reconciler's actuator. For `stop` or `restart` with a running pipeline: pop it, send EOS (lets `splitmuxsink` finalize the open segment, so it is promoted rather than lost), then `NULL`. `stop` returns True there. For `start`/`restart`: format `DESC` with the row's `source` as the URI, the watchdog, the camera id, `cam["epoch"]` (added by `VmsWorker._actuate`; 0 if absent), the roots and the segment length; `Gst.parse_launch` — an exception (a refused URI from `uri.resolve`, a missing plugin) is logged and returns False, which the reconciler counts as a failure with backoff. Then a signal watch on the bus: `message::error` appends the camera to `dead`; `message::element` goes to `_posted`. `set_state(PLAYING)` returning `FAILURE` is False. Success stores the pipeline and returns True.

### `_posted(self, cid, msg)`
Filters an element message into an observation. Messages with no structure, or named `GstBinForwarded`, `splitmuxsink-fragment-opened` or `splitmuxsink-fragment-closed`, are plumbing and dropped. Otherwise the structure's name is the event kind (`motion`, `person`, … — whatever an analytics element posts) and its scalar fields (`int`, `float`, `str`, `bool`) are copied; appended to `posted`. The worker's `pump_once` turns each into `observe(cid, kind, **fields)`, a line in the camera's bucket, if it still holds the epoch.

### `pump(self) -> (dead, posted)`
Returns and clears both lists; every dead camera's pipeline is popped and set to `NULL`. The worker then calls `reconciler.lost(cid)` (restart after backoff) and writes a `silent` event for it.

### `stop_all(self)`
`self("stop", {"id": cid})` for every running pipeline: EOS then NULL for each. Called by `VmsWorker.fence` and at the end of `VmsWorker.run` — with `vmsworker@.container`'s `StopTimeout=20` giving the finalizations time.

## Notes
- Verified where: the README says `gstvms/` is written to GStreamer's Python binding and not exercised in the test run; the logic it calls (`promote`, `resolve`) is. The zombie with two real worker processes and `kill -9` mid-segment on real files are the box's exercises.
- Bus callbacks run on the GLib main context; since the worker runs no GLib main loop, `add_signal_watch` delivery depends on the default main context being iterated — the code as written relies on it, and the worker's loop only reads the lists `pump` hands back.
