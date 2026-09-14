# pipeline.py — `CameraPipeline` state machine, the real `GstActuator`, and the `FakeActuator` for tests

**Role in the module.** Lesson 7 — fifty pipelines in one process. A `CameraPipeline` is a state machine (`IDLE → STARTING → RUNNING → FAILED → backoff → STARTING`), not a coroutine: buffers move on GStreamer's own threads in C, and Python does control only — state changes, bus messages, and naming a file once per segment. Banned in the recording path: `appsink`, identity handoff, buffer probes. GStreamer is imported lazily so the reconciler and its tests never need it (`tests/test_restart.py` constructs `CameraPipeline`s and calls `_format_location`/`_closed` without Gst). The GStreamer path itself is listed in the README as written to the documentation, not executed. Depends on `secrets.compose_rtsp_url` and `redact`; used by `worker.py`.

## Module-level names
- `log` — `worker.pipeline`.
- `IDLE`, `STARTING`, `RUNNING`, `FAILED` — the four states; `Worker.phases()` maps them to the `phase` column.
- `DESC` — the launch description template: `rtspsrc location={url} protocols=tcp latency={latency} name=src ! rtph264depay ! h264parse ! watchdog timeout={watchdog} ! splitmuxsink name=mux max-size-time={segment_ns} muxer-factory=mp4mux async-finalize=true`. `protocols=tcp` because RTSP over UDP with loss gives broken files, not worse pictures; `watchdog` is stall detection in C, reported on the bus as an ERROR; `max-size-time` is `SEGMENT_SECONDS` in nanoseconds; `async-finalize` lets the muxer close a fragment on a helper thread while the next one starts.
- `_gst` — the cached `Gst` module.
- `SegmentClosed` — callback type `(camera_id, start, end, path, size) -> None`.

## Functions
### `gst()`
On first call `import gi`, require `Gst 1.0`, `Gst.init(None)`, cache the module. Every method that needs GStreamer calls it, so import cost is paid once and only when a pipeline is really started.

## `class CameraPipeline`
One camera's pipeline and its bookkeeping: `cam` (the desired row), `id`, `settings`, `key`, `on_segment_closed`, `state`, `pipeline` (the `Gst.Pipeline` or `None`), `last_error` (redacted), `segments_written`, and the open segment's `_open_since`/`_open_path`. Created by `GstActuator.__call__` for each start/restart; never reused after failure.

### `__init__(self, cam, settings, key, on_segment_closed)`
Stores everything, state `IDLE`, no pipeline.

### `segment_dir(self)`
`<archive_dir>/<camera id>/e<epoch>/`. The epoch is in the path from day one; it is 1 and never changes in М9, and in М11 it is the fencing token that makes a zombie's writes land where nobody reads them (Lesson 8, Step 5).

### `_format_location(self, mux, fragment_id)`
The `splitmuxsink::format-location` handler: once per segment — control rate, therefore allowed — and it runs in a streaming thread, so it does nothing but record `now()` as `_open_since` and return `<segment_dir>/<YYYYmmddTHHMMSSZ>.mp4` as `_open_path`. The name is the wall-clock start, so a restarted instance always opens a new segment and never resumes the previous one (`test_restart_opens_a_new_segment_never_resumes`).

### `start(self) -> bool`
Creates the segment directory, composes the URL with the credential in memory, formats `DESC`, and `del`s the URL — never log it; log `cam["rtsp_url"]`. `Gst.parse_launch`; a parse exception becomes a redacted `last_error`, state `FAILED`, `False`. Connects `format-location` on `mux`, state `STARTING`, `set_state(PLAYING)`; `StateChangeReturn.FAILURE` tears down and returns `False`. Otherwise logs the start with the credential-free URL and returns `True`. The reconciler's verb succeeds here even though the camera has not yet answered — a later bus ERROR is what reports an unreachable camera.

### `stop(self)`
Log, `_teardown`, state `IDLE`.

### `_teardown(self)`
If a pipeline exists: send EOS (lets `splitmuxsink` finalize the open fragment), `set_state(NULL)` (releases every native thread), drop the reference (or PSS grows). Does not wait for EOS to propagate — the README's known gap: a graceful drain would need a blocking `timed_pop_filtered`, so on SIGTERM the open segment is lost as with SIGKILL.

### `pump(self) -> bool`
Drain this pipeline's bus without blocking: `bus.pop_filtered(ERROR | EOS | STATE_CHANGED | ELEMENT)` until `None`, handing each message to `_handle`. Returns `False` once any message declared the pipeline dead, so the actuator forgets it. With no pipeline, returns whether the state is not `FAILED`.

### `_handle(self, msg) -> bool`
- `STATE_CHANGED` from the pipeline itself reaching `PLAYING` while `STARTING` → `RUNNING`.
- `ELEMENT` named `splitmuxsink-fragment-closed` → `_closed(location)`.
- `ERROR` (a watchdog timeout arrives here too, "Watchdog triggered") → redacted `last_error`, warning log, teardown, `FAILED`, return `False`.
- `EOS` from the camera → `last_error = "EOS from camera"`, teardown, `FAILED`, `False`.
- Anything else → `True`.

### `_closed(self, path)`
`path` from the message (or the remembered `_open_path`); if there is no path or no `_open_since`, ignore. `end = now()`, `size = os.path.getsize(path)` (a vanished file is ignored), increment `segments_written`, and call `on_segment_closed(id, _open_since, end, path, size)`. "The spool became an archive because somebody kept a record of it." The Worker's callback only queues; the index row is written on the loop by `report_once()`. `test_segment_close_is_indexed_only_on_close` proves nothing is indexed at open.

## `class GstActuator`
The real actuator: `verb -> bool` as the reconciler expects, owning `pipelines: dict[id, CameraPipeline]`. The backoff policy in the reconciler needed no change when this replaced `print()`.

### `__init__(self, settings, key, on_segment_closed)` — stores the three; empty dict.
### `__call__(self, verb, cam) -> bool`
`stop`/`restart` first stop and remove any existing pipeline for the id; `stop` then returns `True`. `start`/`restart` build a new `CameraPipeline`, call `start()`, keep it in the dict on success, log the redacted `last_error` on failure, return the result.
### `pump(self) -> list[int]`
Drain every pipeline's bus; a pipeline whose `pump()` returned `False` is removed and its id returned so `Worker.pump_buses()` can call `Reconciler.lost()`.
### `state(self, cid) -> str` — the pipeline's state, or `IDLE` if none. `Worker.phases()` reads it.
### `stop_all(self)` — stop every pipeline and clear the dict; called at shutdown.

## `class FakeActuator`
Lesson 6's `print()`, grown a memory so tests can assert on it. `failing` is a set of ids or a predicate whose `start` fails; `calls` records `(verb, id)`; `running` is the set believed running; `log_calls` prints each call.

### `__init__(self, failing=frozenset(), log_calls=False)`.
### `__call__(self, verb, cam) -> bool` — record; `stop` discards and succeeds; a failing id is discarded and returns `False`; otherwise add to `running`, `True`.
### `pump(self) -> list[int]` — nothing ever dies on its own (`test_worker.py` monkeypatches this to simulate one death).
### `stop_all(self)` — clears `running`.

## Notes
- `_open_since`/`_open_path` are single slots overwritten by `_format_location` when the *next* fragment opens (on the streaming thread), while `splitmuxsink-fragment-closed` for the *previous* fragment is only seen when `pump()` runs on the loop (every `BUS_TICK`, 200 ms) — and with `async-finalize=true` the closed message is itself posted after the new fragment has started. So by the time `_closed()` runs for fragment N, `_open_since` is almost always fragment N+1's start: the index row gets the right `path` and `bytes` (from the message and the file) but a `span` starting at N+1's open time and ending "now", roughly a 0–200 ms range. `timeline()` and `silent_for` (which reads `upper(span)`) survive; the recorded start does not. Keeping a per-path map of open times, or reading the start from the filename, would fix it. `test_segment_close_is_indexed_only_on_close` calls `_closed` before any second `_format_location`, so it does not see this.
- `pump()` on a pipeline that is `None` and not `FAILED` returns `True`; `GstActuator` never holds such an object, since a failed `start()` is not stored.
