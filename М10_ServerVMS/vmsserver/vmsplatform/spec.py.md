# spec.py — the controller as data: SubsystemSpec from `<sub>.subsystem.yaml`, and SpecController, the one controller every subsystem runs

**Role in the module.** Lesson 5. A subsystem gives the platform one YAML file and the platform runs its controller from it. The spec says: `name` (the prefix `<name>/*`); `unit` (where rows live — `<name>/<rows>/<id>` — how an id is made — `numeric` or a field name — the operator's fields with types and defaults, and derived rows kept beside the unit, such as the VMS's `vms/retention/<id>` that the resource reads); `placement` (which heartbeat fields carry capacity and headroom, a constraint and a tie-break chosen *by name* from a short catalogue — never an expression — and the rebalance dead band); `snapshot` (the fields that leave the cluster); `console` (the name of the running gauge). What is not in a spec: anything about what a unit does — that is the worker, and the worker is the subsystem. `SpecController` extends `contract.Controller` and adds units, placement, redistribution, rebalance, the read model and the snapshot. `vms/controller.py` is this class with the VMS's spec and VMS names for the methods; `vms/config.py` exposes `row()`/`items()`; `console.py` runs over the same spec. `tests/test_second_subsystem.py` runs a counter through it with a different YAML.

## Module-level names
- `PLATFORM_FIELDS = ("worker", "placement", "epoch", "revision", "observed_revision", "phase", "id")` — never the operator's. `SubsystemSpec.refuse` rejects any of them in a create/update body: placement is decided and stored by the controller with a reason; revision, epoch and phase are not the operator's.
- `CONSTRAINTS` — the catalogue: `"none"` (always eligible) and `"labels-subset"` (`_labels_subset`). Extended only by `register_constraint`.

## `class Refused(Exception)`
A write the spec does not allow: a platform field, an unknown field, a missing required field, an id for a numeric-id subsystem, a duplicate id. The console turns it into HTTP 400.

## `class Placement` (dataclass)
A stored placement decision: `unit` (int for numeric ids, str otherwise), `worker`, `reason` (a sentence naming the free capacity, the labels reached and the server), `at` (wall time), `rev`. Lives at `<name>/placement/<id>`.

## `class Field` (dataclass)
One operator field from the spec: `name`, `type` (`string | int | float | bool | list`), `default`, `required`.

### `parse(self, v)`
Convert an item string or JSON value to the typed value; `None` gives the default. Bools accept a real bool or the string `"true"`; lists accept a list or a comma-separated string.

### `default_value(self)`
The declared default, else the type's zero (`0`, `0.0`, `False`, `[]`, `""`).

### `to_item(self, v) -> str`
The Variables form: bools as `"true"/"false"`, lists comma-joined, else `str`.

## `class Derived` (dataclass)
A second row the platform keeps beside the unit: `row` (a path template under the prefix with `{id}`, e.g. `retention/{id}`), `items` (`{item name: field name}` — which unit field feeds each item), `on_delete` (what the row becomes when the unit is deleted; `None` leaves it alone). The VMS's derived row makes `events_retention_days` visible to the resource as `vms/retention/<id> {days}` and sets `{days: 0}` on delete so the buckets go at the next pass.

## `class SubsystemSpec` (dataclass)
The parsed YAML. Fields: `name`; `rows` (`"units"`; the VMS says `cameras`); `id` (`"numeric"` or a field name); `fields`; `derived`; `capacity_from` / `capacity_fallback` (heartbeat key for a worker's capacity, and the number for a worker that said nothing); `headroom_from`; `constraint`; `tie_break` (only `most-free-capacity` exists); `dead_band`; `snapshot` (field names); `running_gauge` (`units_running` by default; `cameras_recording` for the VMS).

### `from_dict(cls, d) -> SubsystemSpec`
Builds the spec from the YAML dict, tolerating absent sections. Field defaults are parsed to their type once here (strings kept as strings so `"cam{id}"` survives). `snapshot` defaults to every field.

### `load(cls, path) -> SubsystemSpec`
`yaml.safe_load` then `from_dict`. PyYAML is imported lazily so the rest of the platform has no dependency on it.

### `sub` (property)
`Subsystem(name)` — the key layout from `contract.py`.

### `acl_console(self) -> list[str]`
The operator's rows: `<name>/<rows>/*`, `<name>/next_id`, `<name>/idem/*` (a retried POST answered the same by any instance) and the first segment of every derived row (`<name>/retention/*`). Never placement. This is the console process's token.

### `acl_controller(self) -> list[str]`
Placement: `<name>/workers/*`, `<name>/placement/*`, `<name>/slots/*` — never a unit's row. The controller process's token (count = 1). Together the two ACLs split the old `<name>/*` so that the console cannot place and the controller cannot edit; `test_the_console_over_http` proves `con.place(1)` raises `Forbidden`.

### `numeric` (property) / `parse_id(self, v)`
Whether ids are numbers; convert a string id accordingly.

### `row(self, items) -> dict`
Variables items to a typed row: `id`, every spec field (default if absent), `revision` (default 1).

### `items(self, row) -> dict`
The inverse, all strings.

### `refuse(self, fields) -> None`
Raise `Refused` for any `PLATFORM_FIELDS` key or any key not in the spec. Called first by `create` and `update`.

### `new_row(self, uid, fields) -> dict`
A fresh row: each required field must be present and truthy (`"a vms unit needs a source"`), others get their default; a string value containing `{id}` has it substituted (the VMS's `name: "cam{id}"`); `revision` is 1.

## Functions (the catalogue)
### `_labels_subset(row, worker_labels) -> bool`
The unit's `labels` must be a subset of what the worker's server reports.

### `register_constraint(name, fn) -> None`
A subsystem's own rule, as code under a name — never as YAML. The same door the resource opens for its hooks.

### `_unit_key(u)`
Sort key: numeric ids before others, numbers by value.

## `class SpecController(Controller)`
The only writer of `<name>/*`, from a spec. Holds nothing; two instances are harmless; never on the recovery path. The VMS is one spec; a counter, a detector, are others — same code.

### `__init__(self, spec, vars_, objects, capacity=None, wall=time.time, cluster=None)`
`capacity` is only the fallback for a worker whose heartbeat says nothing (defaults to the spec's). `cluster` is the name the snapshot carries (`$CLUSTER`, else `cluster-a`); one box is a cluster of one.

### `_row_key(self, uid) -> str`
`<name>/<rows>/<id>`.

### `capacity_of(self, worker) -> int`
The worker's own number from its latest heartbeat (`extra[capacity_from]`, any age), else the fallback. `test_capacity_is_the_workers_word_not_the_controllers`: workers saying 2 and 6 are placed by 2 and 6; an unknown worker gets 50.

### `labels_of(self, worker) -> set[str]`
The `labels` string of its heartbeat, split on commas.

### `server_of(self, worker) -> str`
The heartbeat's `server`, `"?"` if unknown. Goes into placement reasons and the snapshot.

### `headroom(self) -> int`
Sum of `extra[headroom_from]` over workers seen in the last 45 s — what the autoscaler reads via `/metrics`. Stale until the workers heartbeat again after a placement.

### `_next_id(self)`
For numeric ids, bump `<name>/next_id {n}` by CAS and return it; otherwise `Refused` (the unit is named by its field).

### `_derived(self, row, uid, deleted=False) -> None`
Keep every derived row in step: on create/update write `{item: to_item(row[field])}` only if it differs; on delete write `on_delete` if set and the row exists.

### `create(self, fields) -> dict`
`refuse`, choose the id (numeric: `_next_id`; else the field's value, which must be present and not already exist), build the row with `new_row`, write it with `cas=0` (create-only), write derived rows, return the row. Placement is not done here — the controller's pass does it; the console reports `worker: None`.

### `update(self, uid, fields) -> dict`
`refuse`, then read-modify-write the row: `KeyError` if missing or marked deleted; parse each field into the row; bump `revision` (the trigger from М9 Lesson 5, now in the controller — the worker restarts what it runs on a new revision); write. Derived rows are refreshed only if one of their source fields changed.

### `delete(self, uid) -> None`
The operator's half: the row is marked `deleted: "true"` (not removed) and derived rows get their `on_delete`. Its placement is the controller's half, taken back on the next pass by `unplace_deleted` — a console's token cannot touch an assignment, and does not need to.

### `unplace_deleted(self) -> list`
The controller's half of a delete: for every `placement/<id>` row with a worker whose unit no longer exists, remove the unit from that worker's assignment and rewrite the placement as `{worker: "", reason: "deleted", at, rev+1}`. Runs first in `ensure_placed` and `redistribute`. The console test: after `DELETE /cameras/1` the placement still says `w-1` until `unplace_deleted()` returns `[1]`.

### `unit(self, uid) -> dict | None`
The row, or `None` if absent or deleted.

### `units(self) -> list[dict]`
Every live row under `<name>/<rows>/`, sorted by `_unit_key`.

### `placement(self, uid) -> Placement | None`
The stored decision, `None` if no row or the worker is empty (unplaced).

### `load(self, worker) -> int`
Assigned units on that worker — from the assignment row, not from the heartbeat.

### `eligible(self, row, workers) -> list[str]`
Workers passing the spec's constraint against their labels.

### `_pool(self, workers) -> list[str]`
The given list, or the workers seen heartbeating in the last 45 s; sorted.

### `_best(self, pool) -> (worker | None, free)`
`most-free-capacity`: the worker with the largest `capacity_of − load`, strictly positive; ties go to the first in sorted order.

### `place(self, uid, workers=None) -> Placement | None`
Place one unit. An existing placement is returned untouched — adding a worker moves nothing. A missing unit is `None`. Otherwise pick `_best` among the eligible pool; `None` if nothing has free capacity ("the system is full" — or nothing that can reach it; never "w-1 is full"). The reason names the free capacity, the pool size, the labels reached (under `labels-subset`) and the server. Then the row first (CAS decides who won: if another instance placed it meanwhile, the mutator returns `None` and the other's row is used), then `assign_add` on the winner's worker. `test_two_controllers_agree_by_cas`: two threads placing 40 cameras with opposite preferences end with every camera exactly once across `w-1`/`w-2`.

### `ensure_placed(self, workers=None) -> list[Placement]`
The pass: `unplace_deleted`, then `place` every unit; returns what is placed. `test_placement_is_stored_with_a_reason_and_adding_a_worker_moves_nothing`: six cameras split 3/3 by capacity 3; the seventh waits; a third worker arriving takes only the seventh.

### `unplaceable(self) -> list[dict]`
Units with no placement that no live worker's labels can serve — the console's honest answer, with the labels named and the live worker count.

### `where(self, uid) -> str | None`
The placed worker.

### `move(self, uid, to, reason) -> Placement`
The one two-writer operation: remove the unit from every assignment that lists it other than `to` (wherever it is listed, not only where the row says), rewrite the placement row, `assign_add` on `to`. The destination takes the next epoch when it starts; the source's lease fences on renewal and it stops. Explicit, never automatic.

### `redistribute(self, workers=None) -> list[(uid, from, to)]`
The controller's one unasked move: for each released slot (scale-in, or `retire`) that still lists units, move each to the live worker with the most free capacity; stop when the system is full (the unit waits, listed where it was). A merely lapsed slot is not touched: that is a crash, and its process returns under the same name. `test_scale_in_releases_a_slot_and_the_controller_redistributes`: a silent `w-3` moves nothing; after `release_slot()` its two cameras go to `w-1`/`w-2` with reason `slot w-3 released; …`.

### `rebalance(self, budget, dead_band=None, workers=None) -> list[(uid, from, to)]`
Up to `budget` moves: each step takes the most and least loaded workers by `load/capacity`, stops if their spread is under the dead band or the low one is full, and moves the lowest-numbered unit of the high one. Only when asked. `test_rebalance_is_explicit_budgeted…`: budget 0 moves nothing; budget 3 moves three from `w-1` to `w-2`; a further budget of 5 moves one more and then stops inside the 10 % band.

### `read_model(self, lost_after=45.0) -> list[dict]`
What the console lists: every `status` entry from every worker's latest heartbeat (any age), tagged with `worker`, `server`, `age` and `worker_state` (`live` or `stale`), sorted by unit id. `test_the_failure_arithmetic`: with the controller gone the read model still answers from heartbeats; with the worker gone 100 s the rows say `stale`, age 100.

### `snapshot(self) -> dict`
Units and placement as one object for the layer above: `{cluster, ts, <rows>: [{id, <snapshot fields>, revision, worker, server}]}`. A copy with an age — never the rows themselves, which do not leave raft.

### `publish_snapshot(self) -> None`
Writes the snapshot JSON to the object store at `<name>/snapshot`.

### `failover_seconds(self) -> dict[str, float]`
Per worker: `started − previous_hb` from the heartbeat's own fields — the gap between the last heartbeat of the previous instance and this instance's start, measured from what the workers wrote, not by the controller.

## Notes
- Every write is `Controller.write` (CAS loop) or a create-only `put(cas=0)`; nothing is cached, so the process can be killed anywhere.
- The order inside `place` — placement row, then assignment — is what makes two instances agree: the row is the lock.
- `capacity_of`/`labels_of`/`server_of` call `workers_seen(max_age=1e12)` each time, i.e. one object-store listing per call; correctness over speed, fine on one box.
