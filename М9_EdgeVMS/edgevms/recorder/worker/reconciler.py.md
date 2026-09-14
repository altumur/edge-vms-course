# reconciler.py — the reconcile loop, with nothing in it: desired vs actual over an injected store and actuator

**Role in the module.** Lesson 6. Pure logic — no database, no GStreamer, no network — over a `Store` that returns desired rows and an `Actuator` that turns a verb into a boolean. This is the part of the product that survives the rewrite (Lesson 9, Step 5): only the actuator changes, and the README notes М10 copies it unchanged. Desired state is persisted (Postgres, through `store.Desired`); actual state is derived, held IN MEMORY ONLY, and rebuilt by re-running the loop after a restart. Exercised by `tests/test_converge.py` (Lesson 6's seven, plus the backoff cap), `test_offline.py`, `test_restart.py`; driven for real by `Worker.reconcile_once()` with `GstActuator`.

## Module-level names
- `CONVERGED`, `LAGGING`, `STALLED` — the three positions of Lesson 6, Step 7 (`"converged"`, `"lagging"`, `"stalled"`); the console adds a fourth, `unreachable`, for "the Worker has not reported".
- `Store` (Protocol) — anything with `desired() -> list[dict]`; each row needs `id`, `enabled`, `revision`.
- `Actuator` — `Callable[[verb, cam], bool]`; verbs are `start`, `restart`, `stop`.

## `class Reconciler`
Holds `store`, `actuator`, `actual` (`camera_id -> {"revision": n}`, the cameras believed running and at which revision), `failures` (`camera_id -> {"n", "retry_at", "delay"}`), `max_backoff`, `stall_failures`. Created once per Worker; `test_4` shows a fresh instance starts with empty `actual` and rebuilds it from the store, and `test_5` keeps the lying `Persisted` subclass (actual loaded from disk) as a test, not a class.

### `__init__(self, store, actuator, max_backoff=60.0, stall_failures=3)`
Stores the collaborators and the two policy numbers; state dicts start empty.

### `reconcile(self, now=0.0) -> list[(verb, id)]`
One pass. `desired` is the enabled rows keyed by id. For each: skip if `actual[id].revision >= revision` (already applied — `>=` is the "applied" test the README maps to Lesson 5's `observed_revision >= revision`); skip if in backoff (`now < retry_at`); otherwise `start` (not running) or `restart` (running at an older revision). On success record the revision in `actual` and forget the failure record; on failure call `_fail` and record `("failed", id)`. Then the stop loop walks `actual`, not `desired` — "you cannot learn about a deletion by looking at rows that exist" — and stops anything no longer desired (deleted or disabled). Returns the actions taken, in order; a converged pass returns `[]` (`test_1`).

### `_fail(self, cid, now)`
Increments the failure count, base delay `min(2**n, max_backoff)`, actual delay `base × (0.5 + random() × 0.5)` — jitter, 50–100 % of base — and records `retry_at = now + delay`. `test_offline.py::test_without_jitter_they_bunch` pins `random()` and shows the spread collapses to exactly 0.000 s; with jitter 200 cameras spread over more than half a second.

### `lost(self, cid, now)`
A running pipeline died (bus error, watchdog): drop it from `actual` so the next pass restarts it, and count a failure so backoff applies. Called by `Worker.pump_buses()` for every id `GstActuator.pump()` reports dead.

### `status(self) -> dict[id, (position, lag)]`
For each enabled desired camera: `lag = max(revision - actual revision, 0)`; lag 0 is `CONVERGED`; lag with `failures[n] >= stall_failures` is `STALLED`; otherwise `LAGGING`. Positions only — reasons live on the conditions axis (Lesson 9). Used by tests; the Worker derives phases from the actuator's state instead and the console recomputes positions from the view.

## Notes
- The actuator is called for `stop` even when the pipeline was never started successfully (a failing camera that gets disabled is not in `actual`, so no stop is issued — correct: nothing is running).
- `reconcile()` never raises on an actuator exception; the actuator is expected to return `False`. `GstActuator.__call__` catches parse errors itself.
