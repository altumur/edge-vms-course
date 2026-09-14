# variables.py — a file-backed config store with the semantics of Nomad Variables: ModifyIndex, check-and-set, one writer per prefix

**Role in the module.** Lesson 1's config store. It gives one box exactly what a raft-backed Nomad Variables store gives a cluster: every path has a `ModifyIndex`, a `put(cas=<index>)` succeeds only if the index still matches and raises `Conflict` otherwise, and a writer identity may be confined to a set of prefixes (the ACL policy). Everything in the platform that must be consistent — assignments, placement rows, epochs, slots, idempotency keys, unit rows — lives here; bulk or frequent data (heartbeats, snapshots, media) goes to `objects.py` instead. The store is a directory: one JSON file per path under `<root>/vars/`, one counter file for the index, one lock file. Every write is write-then-rename and serialised by an `fcntl` lock, so two processes on the same box see exactly what two clients of one raft would: one of them wins the CAS. М11 swaps this class for real Nomad Variables behind the same `Variables` Protocol.

## Module-level names
- `Conflict` — exception: the `cas` index passed to `put`/`delete` did not equal the path's current ModifyIndex. Every CAS loop in the platform (`epoch.next_epoch`, `Controller.write`, `Worker.claim_slot`, `IdempotencyKeys.claim`) catches it and re-reads.
- `Forbidden` — exception: this writer identity is not allowed to write that path. Raised by `put` when an ACL is set; it is how "one writer per prefix" is enforced mechanically rather than by convention. The console test proves a console token cannot write placement by expecting exactly this.
- `Variables` — `typing.Protocol` with `get`, `put`, `list`: the interface every consumer types against. `FileVariables` implements it here; М11's Nomad client will too. (Note `delete` is not in the Protocol even though `FileVariables` has it; `IdempotencyKeys.prune` uses it.)

## Functions
### `_safe(path)`
Refuses a path containing `..` or starting with `/` (raises `ValueError`) and returns it unchanged. Called on every path before it is turned into a filename, so a caller cannot escape `<root>/vars/`.

## `class FileVariables`
The store. Holds only paths (`root`, `dir`, `index_file`, `lock_file`) plus an optional writer identity and ACL map; it keeps no cache, so any number of instances over the same directory — in one process or many — are equivalent.

### `__init__(self, root, writer=None, acl=None)`
`root` is the store directory; `<root>/vars/` is created. `writer` is this handle's identity (None means unrestricted). `acl` is `{writer: [allowed prefixes]}`; when both `writer` and a non-empty `acl` are present, `put` checks them. Nothing is read at construction; the index counter file is created lazily on the first write.

### `as_writer(self, writer, allowed) -> FileVariables`
Returns a new handle on the same directory seen through another identity, allowed only the given prefixes (`'vms/*'`, `'vms/epoch/*'` style: a trailing `*` means prefix match, otherwise exact path). This is what a Nomad ACL policy does for a task's token. The tests build the controller with `as_writer("vmscontroller", SPEC.acl_controller())` and the console with `as_writer("vmsconsole", SPEC.acl_console())`.

### `_file(self, path) -> str`
Maps a variable path to its file: `<root>/vars/<path with / encoded as %2F>.json`. One flat directory, so `list` is a single `listdir`.

### `_locked(self)`
Opens the lock file and takes an exclusive `fcntl.flock` on it; the returned file object is used as a context manager, and closing it releases the lock. This serialises every `put`/`delete` across all processes on the box.

### `_next_index(self) -> int`
Reads the counter file (missing or empty means 1000), increments it, writes it back atomically (tmp + `os.replace`) and returns the new value. Always called under the lock. The starting value 1000 is why the very first write in the tests returns index 1001 — indices never restart from zero after a restart, so a stale CAS from before a restart still conflicts.

### `get(self, path) -> (dict | None, int)`
Reads the path's file and returns `(items, index)`; a missing path is `(None, 0)`. Not locked: a reader sees either the old file or the new one, never a half-written one, because writes rename into place. Items come back as a fresh dict of strings.

### `put(self, path, items, cas=None) -> int`
The write. First the ACL: if this handle has a writer and an ACL, the path must match one of the writer's allowed patterns or `Forbidden` is raised — before taking the lock. Then, under the lock: read the current index; if `cas` was given and differs, raise `Conflict`; otherwise take the next index, dump `{"items": {k: str(v)}, "index": idx}` to a tmp file and rename it over the path's file; return the new index. Two rules to remember: `cas=0` means "create only" (the path must not exist — `SpecController.create` and `IdempotencyKeys.claim` rely on it); and every value is stringified on the way in, which is why callers such as `Assignment.from_items` and `Slot.from_items` parse ints, floats and `"true"`/`"false"` back out.

### `delete(self, path, cas=None) -> None`
Under the lock: the same CAS check as `put`, then remove the file (a missing file is not an error) and burn an index. Used by `IdempotencyKeys.prune`. No ACL check is applied here.

### `list(self, prefix) -> list[str]`
Every stored path that starts with `prefix`, decoded from the filenames and sorted. Callers pass prefixes ending in `/` (`"vms/workers/"`, `"vms/slots/"`, `"vms/idem/"`) to enumerate a row family.

## Notes
- Atomicity model: rename is atomic on one filesystem; the lock serialises read-check-write; therefore `put(cas=idx)` is a true compare-and-set. `test_two_processes_one_cas_winner` runs four threads, each with its own `FileVariables`, incrementing a counter by CAS and asserts the final value equals the number of successful writes.
- `test_the_config_store_survives_a_restart_and_refuses_a_stale_cas` shows a second handle on the same directory reads the same items and index, a stale `cas` raises `Conflict`, and `get("nope")` is `(None, 0)`.
- The ACL check is on the handle, not the file: an unrestricted handle over the same directory can still write anything. The confinement is real only when each process gets only its own token — which is what the Quadlet units under `deploy/` arrange.
