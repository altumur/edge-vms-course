# worker.py — vmsworker: DriverPack as the worker — N pipelines against an assignment, an epoch per camera, a lease, a heartbeat with server, labels and capacity

**Role in the module.** Lesson 4. One process, N pipelines, its own loop. `VmsWorker` extends `vmsplatform.contract.Worker` (see `contract.py.md` for `claim_slot`, `renew_slot`, `release_slot`, `take_epoch`, `renew_leases`, `heartbeat`) and is the thing that knows what a camera is. It reads its assignment `vms/workers/<me>` and the camera rows it names, runs М9's `Reconciler` over them (`reconciler.py`) with an actuator that builds `driverpacksrc ! tee ! archivesink` (`gstvms/actuator.py`; `FakeActuator` here without GStreamer), takes an epoch per camera by CAS when it starts one, holds a lease per camera, writes events into the camera's bucket on this server's resource, and publishes a heartbeat carrying its status. It never writes configuration: its token is `vms/epoch/*` and `vms/slots/*`. Nomad or systemd supervises the process; the process supervises its pipelines; nothing supervises the loop, because the loop is the process. The docstring names what the environment hands a process: `WORKER_NAME` / `NOMAD_ALLOC_INDEX` (the slot preference; the CAS claim on `vms/slots/w-N` is the proof), `NOMAD_NODE_NAME` or the hostname (`server`: which resource it records into), `NOMAD_META_labels` (`labels`: what this server can reach; the controller places by them), `NOMAD_ALLOC_ID` (the instance), `CAPACITY` (the worker's own number, from М9 Lesson 7's probe). Run by `__main__.worker`; tested in `tests/test_lesson4_worker.py` and used across Lesson 5's tests.

## Module-level names
- `log` — logger `vmsworker`.
- `VMS` — `Subsystem("vms")`, the key layout.

## `class FakeActuator`
М9 Lesson 6's `print()` with a memory — the actuator without GStreamer, and the one `__main__` falls back to. It mirrors `GstActuator`'s surface: callable, `pump()`, `stop_all()`, plus test hooks. State: `failing` (a set of camera ids, or a predicate, whose start fails), `calls` (every `(verb, id)`), `running` (ids started and not stopped), `epochs` (`{id: epoch}` as passed in the row at start), `dead` and `posted` (what tests push in to simulate a bus).

### `__init__(self, failing=frozenset())`
### `__call__(self, verb, cam) -> bool`
Records the call. `stop` always succeeds and removes the id. A start/restart on a failing id fails (and clears `running`); otherwise the id is running and `cam["epoch"]` is remembered — the tests read `act.epochs` to see which epoch the worker handed the pipeline.

### `pump(self) -> (dead, posted)`
Returns and clears `dead` and `posted`; dead ids leave `running`. Same contract as `GstActuator.pump`.

### `post(self, cid, kind, **fields)`
What an element would post on the bus: appends to `posted`.

### `stop_all(self)`
Clears `running`.

## Functions

### `slot_from_environment(env) -> str | None`
`WORKER_NAME` if set; else `w-<NOMAD_ALLOC_INDEX>`; else `None` — claim whatever is free, a lapsed slot first.

### `labels_from_environment(env) -> list[str]`
`NOMAD_META_labels` split on commas, empties dropped.

## `class VmsWorker(Worker)`
`name` is a slot. Given (systemd's `%i`, Nomad's alloc index) it is claimed by that name — taken outright, even from a holder that has not lapsed, because the scheduler is the authority on which process is current; `None` means the environment's, and failing that "whichever slot is free" — a lapsed one first, so a replacement inherits its assignment. "A worker on a cluster is a worker on a box whose stores happen to be raft: same class, same heartbeat." It is also the `Store` of its own `Reconciler` (`desired()`).

State beyond the base class: `archive_root` (this server's resource), `bucket_seconds`, `observed` (every `(cid, t, kind)` this instance wrote), `capacity`, `actuator`, `rows` (the assignment's camera rows, refreshed each pass), `assignment_rev`, `reconciler`, `recording_allowed` / `fenced_reason` (the instance-wide fence), `server`, `labels`, `alloc`, `started_at` (monotonic) and `_started_wall`, `passes`, `previous_hb` / `previous_instance` (what failover is measured from).

### `__init__(self, name, vars_, objects, actuator=None, lease_ttl=30.0, lease_margin=5.0, clock=time.monotonic, wall=time.time, server=None, capacity=None, instance=None, slot_ttl=45.0, archive_root=None, bucket_seconds=600, env=None)`
`env` defaults to `os.environ` (tests pass a dict). `instance` defaults to `NOMAD_ALLOC_ID`, else the base class's `hostname:pid:6hex`. Calls `Worker.__init__` with `name=None` and then `claim_slot(prefer=name or slot_from_environment(env))` — so construction *is* the claim, and `self.name` is set afterwards. Then: `archive_root` from the argument or `$ARCHIVE` (`/data/archive`); `capacity` from the argument or `$CAPACITY` (50) — "М9 Lesson 7's B + n·I, measured on ITS server"; the actuator (`FakeActuator()` if none); an empty `rows`; the `Reconciler(self, self._actuate)`; `recording_allowed = True`; `server` from the argument, `NOMAD_NODE_NAME`, `NOMAD_NODE_ID`, else the hostname; `labels`, `alloc`; the two start clocks. Finally it reads the previous heartbeat object of this slot name: if one exists and was written by a different instance, `previous_hb` is its `ts` and `previous_instance` its instance — the controller's `failover_seconds` computes `started − previous_hb` from these, measured from what the workers wrote. `test_a_replacement_without_a_name_inherits_the_lapsed_slot`: two nameless workers get `w-1`, `w-2`; after `w-1` lapses (46 s of wall clock) a third nameless worker gets `w-1` back and starts its two cameras with epoch 2.

### `desired(self) -> list[dict]`
The reconciler's store: `self.rows`.

### `refresh(self)`
Read the assignment (`assignment_rev` kept for the heartbeat) and, for each unit it names, the row `vms/cameras/<id>`; rows that are missing or marked `deleted: "true"` are skipped. "A fresh worker knows nothing and reads everything; nothing about what is running is stored." An unassigned worker has no rows and invents nothing (`test_worker_runs_its_assignment…`: the first `reconcile_once` is `[]`).

### `_actuate(self, verb, cam) -> bool`
The gate between the reconciler and the real actuator. For `start`/`restart`: refuse if the instance is fenced (`recording_allowed` false); on `start`, or if no epoch is held for the unit, `take_epoch(unit)` — a new epoch for a new writer — else reuse the held epoch (an edit's restart keeps epoch 1); refuse if `may_write(unit)` is false (no lease, or a lost one); then call the actuator with `epoch` added to the row — the number archivesink puts in every path. For `stop`: call the actuator and `release(unit)` (forget epoch and lease). `test_lease_expiry_without_renewal_stops_starts`: after 26 s without renewal `may_write` is false; a later start takes epoch 2.

### `now(self) -> float`
Seconds since start on the monotonic clock — the reconciler's `now` for backoff.

### `reconcile_once(self, now=None) -> list[(verb, id)]`
One pass: `refresh`, `reconciler.reconcile(now)`, count the pass, log each action. The base class's abstract method; called by `run` and directly by every test.

### `lease_pass(self) -> list[str]`
Renew the slot and every lease, and decide what a lost lease means. First `renew_slot()`: if the slot is held by another instance now, `fence("slot w-N is held by another instance now")` and return every held unit — the zombie is fenced at the slot *before* any epoch is looked at (`test_the_zombie_is_fenced_at_the_slot_first`: the replacement's `slot.gen == 2`). Then `renew_leases()`; for each lost unit: if it is no longer in my assignment this is a reassignment — stop the pipeline, drop it from `reconciler.actual`, `release` it, and carry on recording the rest (`test_a_reassignment_is_not_a_zombie`: released, not fenced, `recording_allowed` still true); if it *is* still mine, another instance of me took the epoch — I am the zombie — `fence` and stop looking. Returns the lost units. `test_the_zombie_on_one_box`: A's `lease_pass` returns `["1"]`, A is fenced with "slot w-1" in the reason, its epoch lease also reports a conflict, and it may start nothing (`("failed", 1)`); B is fine.

### `fence(self, why)`
Once: log at error, set `recording_allowed = False` and `fenced_reason`, `actuator.stop_all()`, `reconciler.clear()` — the pipelines were stopped underneath the loop. Idempotent (a second call returns immediately). After this the heartbeat says `fenced: true`, `_actuate` refuses every start, and `observe` writes nothing.

### `observe(self, cid, kind, **fields) -> str | None`
An event: written by this worker, now (`wall()`), into the camera's bucket on this server's resource under the epoch this worker holds for it — recording or not. `None` if no epoch is held for the camera (not mine to observe) or the instance is fenced. Records `(cid, t, kind)` in `observed` and returns the bucket path from `event_log(archive_root, cid, epoch, bucket_seconds).append(...)`. Nothing else is told — no store write, no controller. `test_the_worker_observes_what_it_holds_recording_or_not`: before the first reconcile `observe(1, ...)` is `None`; after it the line lands in `<archive>/vms/1/e1/…`; camera 2 (not assigned) is `None`; after `fence` a post is dropped; `vms/events` in the store stays empty.

### `pump_once(self)`
The bus, drained: `actuator.pump()` gives `(dead, posted)`; every posted `(cid, kind, fields)` becomes `observe(...)` — a line only if I still hold the epoch; every dead camera becomes `reconciler.lost(cid, now)` (restart after backoff) plus `observe(cid, "silent")` — "the event with no segment open, by definition".

### `status(self) -> list[dict]`
The read model, per assigned row: `id`, `ref`, `name`, `enabled`, `phase` (`running` if in `reconciler.actual`; `pending` if disabled; `failed` if in `reconciler.failures`; else `pending`), `position` (`converged | lagging | stalled`), `revision`, `observed_revision` (what is actually running), `epoch` (held, or 0). This list is the heartbeat's `status`; the controller's `read_model` and the console's `/cameras` show it, and `/metrics` counts `phase == running` into `vms_cameras_recording`.

### `headroom(self) -> int`
`max(0, capacity − len(rows))`: cameras this worker could still take. "Not CPU — a worker at 40 % CPU with no assignment left is full." What the autoscaler reads via the controller's `headroom()` and `/metrics`.

### `heartbeat_once(self)`
`Worker.heartbeat(status, …)` to the object `vms/<name>/heartbeat` with the extras the platform reads by name: `server`, `instance`, `alloc`, `labels` (comma-joined), `assignment_rev`, `fenced`, `conflicts`, `passes`, `capacity`, `headroom`, `started`, `previous_hb`, `previous_instance`. The controller's `capacity_of`, `labels_of`, `server_of`, `headroom`, `failover_seconds` and the console's metrics all read from here.

### `run(self, poll=2.0, stop=None)`
The loop as a process. Every `poll` seconds: `reconcile_once`, `pump_once`, `lease_pass` every `max(1, (lease_ttl − lease_margin)/3)` s (≈8.3 s by default, well inside the 25 s the lease allows), `heartbeat_once` every 10 s; any exception is logged and the loop continues. On `stop`: `actuator.stop_all()` (with GStreamer, EOS lets each splitmuxsink finalize its open segment — `vmsworker@.container` gives it `StopTimeout=20`), a last heartbeat, then `release_slot()` — "an orderly stop says so; a crash says nothing", which is what lets the controller tell scale-in (redistribute) from a crash (leave it to the scheduler).

## Notes
- Ordering: the slot is claimed in the constructor, before any assignment is read (the name is the row key); an epoch is taken in `_actuate` before the pipeline starts; `lease_pass` checks the slot before the leases.
- Recovery needs no controller: `test_restart_with_the_controller_stopped` deletes the controller, starts a fresh `w-1` with an empty `actual`, and it starts all three cameras from its assignment with epoch 2 each — the old instance is fenced by construction.
- Three exits from a lost lease, all in `lease_pass`: reassignment (stop that one, continue), zombie (fence everything), and slot taken (fence everything, first). A lease that merely expired because the loop stalled shows up as `may_write` false in `_actuate` and a fresh epoch on the next start.
- `observe` returns the bucket path, which the tests read back with `read_bucket`; the worker keeps `observed` only for tests and diagnostics.
