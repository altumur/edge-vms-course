# contract.py — the subsystem contract: Subsystem, Assignment, Heartbeat, Slot, and the Controller and Worker base classes

**Role in the module.** Lesson 1. This file is the whole of what the platform knows about any subsystem, and it is deliberately small: a config prefix `<name>/*` writable by the controller only; assignment rows `<name>/workers/<worker>`; a heartbeat object `<name>/<worker>/heartbeat`; an epoch prefix `<name>/epoch/<unit>` the workers take by CAS; an event log on the resource (`events.py`); and a slot prefix `<name>/slots/<worker>` — identity by claim. It depends on `variables.py`, `objects.py` and `epoch.py` and nothing else. `spec.SpecController` extends `Controller`; `vms.worker.VmsWorker` and the counter worker in `tests/test_second_subsystem.py` extend `Worker`. The file's own docstring settles who decides how many workers there are: not the controller. The scheduler runs `count` of them; the platform's part is to give those interchangeable processes stable names — the slots — so assignments survive a reschedule. A slot released on an orderly stop is redistributed by the controller; a slot that merely lapses is a crash, left alone for the scheduler.

## Module-level names
None (dataclasses and classes only).

## `class Subsystem` (dataclass)
A name, and the key layout derived from it. Every path the platform touches for a subsystem is produced here, so the layout is in one place.
- `name` — the prefix (`vms`, `counter`).

### `config(self, *parts) -> str`
`<name>/<part>/<part>…` — the generic path builder used by `SpecController` for rows, `next_id`, `placement/<id>`, derived rows and the snapshot key.

### `assignment(self, worker) -> str`
`<name>/workers/<worker>`.

### `heartbeat_key(self, worker) -> str`
`<name>/<worker>/heartbeat` — an object-store key, not a Variable.

### `epoch_key(self, unit) -> str`
`<name>/epoch/<unit>`.

### `slot_key(self, worker) -> str`
`<name>/slots/<worker>`.

### `acl_controller(self) -> list[str]`
`[<name>/*]` — the whole prefix. This is Lesson 1's coarse ACL; `SubsystemSpec.acl_controller` narrows it in Lesson 5 once the console gets its own token.

### `acl_worker(self) -> list[str]`
`[<name>/epoch/*, <name>/slots/*]` — a worker writes only epochs and its slot, never configuration.

## `class Assignment` (dataclass)
What one worker should run: the row at `<name>/workers/<worker>`.
- `worker` — the slot name.
- `units` — the subsystem's unit ids as strings; the platform does not know what they are.
- `rev` — bumped on every change, so a worker can tell a new assignment from the one it already applied (`assignment_rev` in the VMS heartbeat).

### `to_items(self) -> dict`
`{"units": "1,2,3", "rev": n}` — the Variables row form (strings only).

### `from_items(cls, worker, items) -> Assignment`
The inverse; a missing row is an empty assignment with `rev 0`. Empty strings in the list are dropped.

## `class Heartbeat` (dataclass)
A worker's own report, written as one JSON object.
- `worker` — the name; `ts` — wall-clock time of the write; `status` — a list of per-unit dicts (the read model: `{id, phase, epoch, …}` in the VMS); `extra` — every other top-level key (`server`, `labels`, `capacity`, `headroom`, `conflicts`, `started`, `previous_hb`, …). The platform reads `extra` by name in `SpecController` and the console's `/metrics`; it never defines the keys.

### `to_bytes(self) -> bytes`
`{"worker", "ts", "status", **extra}` as JSON.

### `from_bytes(cls, raw) -> Heartbeat`
The inverse; everything that is not `worker`/`ts`/`status` lands in `extra`.

## `class Slot` (dataclass)
A worker's name as a row: who holds it, until when (wall clock), and whether the last holder let go on purpose. This is the mechanism behind identity by claim.
- `name` — `w-1`; `holder` — the claiming process's `instance` string; `until` — wall-clock expiry of the claim; `released` — True when the holder stopped on purpose (default True for a row that does not exist yet); `gen` — a generation counter bumped on every claim.

### `to_items` / `from_items(cls, name, items)`
Row conversion; `released` is stored as `"true"`/`"false"`. A missing row is `Slot(name)` — released, no holder.

### `lapsed(self, now) -> bool`
Held, not released, and past `until`: the holder went silent — a crash.

### `claimable(self, now) -> bool`
Released, or never held, or past `until`. (A lapsed slot is claimable; a released one is too.)

## Functions
### `slot_number(name) -> int`
The integer after the last `-` in a slot name (`w-3` → 3), 0 if not numeric. Used to order free slots and to pick the next unused number.

## `class Controller`
The only writer of `<name>/*`. It holds nothing: every method reads the store, decides, and writes by CAS, so two instances are harmless — this is the property `spec.SpecController` and the VMS controller inherit, and the reason the controller is never on the recovery path.

### `__init__(self, sub, vars_, objects, wall=time.time)`
Keeps the `Subsystem`, the two stores and a wall clock (tests inject a fake).

### `write(self, path, mutate, retries=10) -> dict`
The one write primitive: read `(items, idx)`, call `mutate(dict(items or {}))`; if it returns `None` nothing is written and the current items are returned; otherwise `put(cas=idx)`; on `Conflict` re-read and repeat, up to `retries`, then `RuntimeError`. Every mutation in `SpecController` goes through this, which is what makes "two controllers agree by CAS" true.

### `workers_seen(self, max_age=45.0) -> dict[str, Heartbeat]`
Which workers exist: those whose heartbeat object under `<name>/` is at most `max_age` old. Never a list the controller keeps — a fact it reads. `test_controller_and_worker_bases_speak_only_the_contract`: after the wall clock advances 100 s a silent worker is not a worker.

### `assignment(self, worker) -> Assignment`
Reads one worker's row.

### `assign(self, worker, units) -> Assignment`
Replaces the worker's assignment with the sorted, de-duplicated list and bumps `rev`.

### `assign_add(self, worker, unit) -> Assignment`
Read-modify-write adding one unit; returns `None` from the mutator (no write) if already present. Two controllers adding different units to one worker at once both land.

### `assign_remove(self, worker, unit) -> Assignment`
The mirror of `assign_add`.

### `assignments(self) -> dict[str, Assignment]`
Every row under `<name>/workers/`.

### `slots(self) -> dict[str, Slot]`
Every row under `<name>/slots/`, read only — the controller never hands slots out.

### `released_slots(self) -> list[str]`
Slots whose holder let go on purpose (scale-in, or `retire`) and that still have units assigned: what a subsystem redistributes. A slot that merely lapsed is not here — that is a crash, and the scheduler brings the process back under the same name. Sorted by slot number.

### `retire(self, worker) -> Slot`
An operator's statement that a slot is gone for good: marks it `released` by CAS (no-op if already released). The controller never decides this on its own from a silence.

## `class Worker`
Runs its assignment and reports. Reads `<name>/workers/<me>` and the units it names; writes its heartbeat object and, when it starts a unit, that unit's epoch by CAS. Never writes configuration. A fresh worker rediscovers everything from the store. Subsystems subclass it and implement `reconcile_once`.

### `__init__(self, sub, name, vars_, objects, lease_ttl=30.0, lease_margin=5.0, clock=time.monotonic, wall=time.time, instance=None, slot_ttl=45.0)`
`name` is the slot name, or `None` until `claim_slot()`. `instance` identifies the process (`hostname:pid:6hex` by default) and is what a slot row records as `holder`. Keeps two clocks: monotonic for leases, wall for slot expiry. `epochs` and `leases` are per-unit dicts, empty at start.

### `claim_slot(self, prefer=None, retries=50) -> str`
Become somebody. Lists the slot rows; with `prefer` (Nomad's `NOMAD_ALLOC_INDEX`, systemd's `%i`) the candidate list is just that name and it is taken by CAS even from a holder that has not lapsed — the scheduler is the authority on which process is the current one, and the old holder finds out on its next `renew_slot`. Without `prefer`, candidates are: lapsed slots first (oldest `until` first — their assignment is waiting), then free (released or never held) slots by number, then a fresh `w-<max+1>`. For each candidate, re-read, skip if not claimable (only in the no-`prefer` case), and `put` a new `Slot(cand, instance, now + slot_ttl, released=False, gen+1)` with `cas=idx`; a `Conflict` means someone took it between read and write, move on. Sets `self.slot` and `self.name`. `test_identity_by_claim_is_a_platform_piece`: two nameless workers get `w-1` and `w-2`; after `w-1` lapses a third gets `w-1` back and its assignment with it; `prefer="w-7"` creates and takes `w-7`.

### `renew_slot(self) -> bool`
Still me? Read the slot; if `holder` is another instance, return False — the instance is fenced as a whole (the VMS worker stops recording on this). Otherwise extend `until` by CAS; a `Conflict` is also False. A worker with no slot (fixed name without claim) returns True.

### `release_slot(self) -> None`
An orderly stop (SIGTERM from the scheduler: scale-in, or a drain). Writes the row with `released=True` and `until=now`, if this instance still holds it; a `Conflict` is ignored. This flag is what tells scale-in from a crash: a crash says nothing and the slot merely lapses.

### `assignment(self) -> Assignment`
Reads my row.

### `take_epoch(self, unit) -> int`
Called when the worker starts a unit: `next_epoch` on `<name>/epoch/<unit>`, record it in `epochs`, and open a `Lease` on it. A second worker starting the same unit gets the next number, and the first one's lease fences on renewal.

### `release(self, unit) -> None`
Forget the unit's epoch and lease (the worker stopped it).

### `may_write(self, unit) -> bool`
The unit's lease says so, and there is one.

### `renew_leases(self) -> list[str]`
Renews every lease; returns the units whose lease was lost — fenced or expired — for the subsystem to stop.

### `conflicts(self) -> int`
Sum of `conflicts` over all leases; goes into the heartbeat.

### `heartbeat(self, status, **extra) -> None`
Writes `Heartbeat(name, wall(), status, extra)` to `<name>/<name>/heartbeat` in the object store. The VMS passes `server`, `labels`, `capacity`, `headroom`, `conflicts`, `started`, `previous_hb`, etc. as `extra`.

### `reconcile_once(self, now) -> list`
Abstract: what a subsystem implements (the VMS's is М9 Lesson 6's loop).

## Notes
- The tests enforce the boundary: `test_the_platform_knows_nothing_about_video` asserts no import from `vms/` and not the word "camera" in this file; `test_second_subsystem.py` runs a counter through the same `Controller`/`Worker`.
- Ordering that matters: a worker claims its slot before reading its assignment (the name is the row key); it takes an epoch before writing anything for a unit; it renews slot and leases on a shorter period than `slot_ttl` / `lease_ttl − margin`.
- `until` is wall-clock while leases are monotonic: slots are compared across processes and boxes, leases only within one process.
