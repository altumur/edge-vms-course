# worker.py — the Worker process: four tasks per concern, the glue between store, reconciler, actuator and console

**Role in the module.** Lessons 6–9 assembled. One process, one asyncio loop, and one task per *concern* — not one per camera, whatever the camera count: `reconcile()` every `POLL_INTERVAL` and on NOTIFY (desired in Postgres vs actual in a dict); `pump_buses()` every `BUS_TICK` (non-blocking pop on every pipeline's bus); `report()` every `REPORT_INTERVAL` (observed_revision, phase, conditions, and the queued segment index rows); `retention()` every `RETENTION_INTERVAL`. "The timer is correctness. The notification is latency." The console (`console/app.py`) is served by uvicorn on the same loop and reads this object's `store`, `settings` and `key`. Entered through `main()` from `__main__.py`; `tools/provision.py` imports `MIGRATIONS` from here. Tested by `tests/test_worker.py` with `FakeAsyncStore` and `FakeActuator`.

## Module-level names
- `log` — `worker`.
- `MIGRATIONS` — `recorder/migrations/`, computed from this file's location; passed to `PgStore.migrate()` here and by `provision.py migrate`.

## `class Worker`
State: `settings`, `store` (`PgStore` or a test double), `key` (`ColumnKey` or `None`), `desired` (a `store.Desired` the reconciler reads), `actuator` (`GstActuator` by default, injected in tests), `reconciler`, `recording_allowed` (Lesson 8: the disk-full policy may clear it), `pending_index` (segment rows queued from streaming threads, written by `report()`), `wake` and `stopping` events, `started_at` (monotonic) and `passes`.

### `__init__(self, settings, store, actuator=None, key=None)`
Builds the actuator with `_on_segment_closed` as its callback and the `Reconciler` with `_actuate` as its actuator and `max_backoff`/`stall_failures` from settings. No I/O.

### `_actuate(self, verb, cam) -> bool`
The actuator the reconciler sees. Refuses `start`/`restart` while `recording_allowed` is false (so the camera fails, enters backoff and is retried once storage returns — `test_storage_unavailable_is_a_reason_not_a_phase`); everything else goes to the real actuator. Storage unavailable is a REASON, not a phase: the camera stays desired, converges as far as it can, and the condition explains why.

### `_on_segment_closed(self, cid, start, end, path, size)`
Called from a GStreamer streaming thread once per segment. Queue only — append `(cid, start, end, path, size, epoch)`; the database write happens on the loop in `report_once()` (`test_segment_closed_is_queued_then_indexed_on_the_loop`).

### `now(self) -> float` — seconds since start, monotonic; the reconciler's clock (tests move `started_at` to jump past a backoff).

### `reconcile_once(self) -> list[(verb, id)]`
Refresh `desired.rows` from `store.fetch_desired()`, run one `reconciler.reconcile(now)`, count the pass, log each action, return them.

### `reconcile(self)`
Loop until `stopping`: `reconcile_once` (exceptions logged, loop continues), then wait on `wake` **or** `poll_interval`, then clear `wake`. A NOTIFY that arrives during a pass sets `wake` and the next wait returns at once; one that is lost is caught by the timer.

### `pump_buses(self)`
Loop until `stopping`: for every id `actuator.pump()` reports dead, `reconciler.lost(cid, now)` (forget it, count the failure, retry with backoff) and set `wake` so the reconciler acts without waiting for the timer; sleep `bus_tick`. `test_dead_pipeline_is_forgotten_and_restarted`.

### `phases(self) -> dict[id, (observed_revision, phase)]`
Phase is a POSITION. A disabled camera reports its own `revision` and `pending` — disabled means stopped, which is "applied". Otherwise `have` is the revision in `reconciler.actual` (0 if none), the actuator's `state(cid)` (or `RUNNING`/`IDLE` derived from `actual` for an actuator without `state`) is mapped to `running`/`starting`/`failed`/`pending`, and a camera with a failure record whose pipeline is idle or failed is `failed`. Never sets a phase from a condition.

### `report_once(self)`
Three writes, all controller-owned: (1) `store.report()` with the phases; (2) per camera, three conditions — `camera_reachable` (false while a failure record exists, reason from the pipeline's `last_error` or `"failing, retry in Ns"`), `storage_available` (mirrors `recording_allowed`, reason `"disk full; policy=stop_recording"`), `licensed` (always true; М14 wires this); (3) swap out `pending_index` and `index_segment` every queued row — the spool became an archive. `test_reconcile_and_report_write_only_controller_columns`.

### `report(self)` — `report_once` every `report_interval`; exceptions logged ("convergence continues regardless").

### `retention(self)`
Every `retention_interval`: `enforce_retention(store, RealFs(), settings)`; if `recording_allowed` changed, copy it, warn, and set `wake` so cameras are stopped or restarted promptly; log when partitions were dropped or a policy applied.

### `run(self, serve_console=True)`
Startup order: `store.migrate(MIGRATIONS)` (a failure is logged, "the box keeps recording" on the previous schema); `store.listen("cameras", set wake)` (a failure means "polling alone — slower, still correct"); SIGTERM/SIGINT handlers set `stopping`; create the four tasks; if `serve_console`, import `console.app.create_app(self)` and run a `uvicorn.Server` on `console_host:console_port` as a fifth task. Then wait for `stopping`. Shutdown: `actuator.stop_all()` (each pipeline is sent EOS and set to NULL), cancel and gather the tasks, one final `report_once()` so the database says `pending` rather than `running` for a stopped recorder, and release the LISTEN connection.

## Functions
### `main()`
Configure logging (`LOG_LEVEL`, default `INFO`), build `Settings()`, load the column key if the file exists (else warn: cameras with credentials will not start), `PgStore.connect`, run a `Worker`, close the store on the way out.

## Notes
- The `storage_available` reason string names `stop_recording` unconditionally, but `recording_allowed` also goes false under `degrade_retention`/`by_priority` when nothing is left to sacrifice (`retention._apply_policy`); the condition would then name the wrong policy. The `retention.degraded` event carries the right one.
- `report_once` looks for the reason in `actuator.pipelines[cid].last_error`, but `GstActuator.pump()` removes a dead pipeline from that dict and a failed `start()` never adds one, so in practice the reason is always the `"failing, retry in Ns"` fallback; the watchdog's "Watchdog triggered" text reaches the log, not the condition.
- `from console.app import create_app` inside `run()` requires the `recorder/` directory on `sys.path` (`/app` in the container, the cwd on the bench); the import is deferred so tests can build a `Worker` without FastAPI installed.
- The final `report_once()` writes `last_seen = now()`, so right after a clean stop the console still reports the node for up to 3 × `REPORT_INTERVAL`.
