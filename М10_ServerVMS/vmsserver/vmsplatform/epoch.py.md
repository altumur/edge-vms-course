# epoch.py — the fencing-token issuer and the lease, generic to any writer that can have two instances

**Role in the module.** Lesson 1. A unit (a camera, a counter) may at any moment have two processes believing they run it — a zombie after a reschedule, or a reassignment window. The platform's answer is a fencing token, the *epoch*: a per-unit integer that comes from one issuer (the CAS on a Variables key) and only increases. Whoever holds the newest epoch is the writer; an older holder discovers it on its next renewal and stops. This file has the issuer (`next_epoch`), the reader (`current_epoch`) and the lease that a holder keeps on its epoch (`Lease`). The subsystem decides the key (`Subsystem.epoch_key(unit)` gives `<name>/epoch/<unit>`); the platform promises only that numbers come from one issuer, in order. `contract.Worker.take_epoch` and `renew_leases` are the callers; `vms.archive` puts the epoch in every segment and bucket path so a stale writer's output is identifiable afterwards.

## Module-level names
None.

## Functions
### `next_epoch(vars_, key, retries=200) -> (epoch, index)`
Issues the next epoch for `key` by check-and-set: read `{epoch}` and its ModifyIndex (missing key means epoch 0), try to `put` `epoch + 1` with `cas=idx`; on `Conflict` re-read and try again, up to `retries` times, then raise `RuntimeError`. Because the CAS is on the row's index, two callers racing get two different numbers in order and no number is ever reused — `test_epoch_issuer_never_reuses_a_number` runs four threads issuing 25 each and asserts the set is exactly 1..100. The returned index is the row's ModifyIndex after the write (unused by `Worker.take_epoch`, which keeps only the epoch).

### `current_epoch(vars_, key) -> int`
Reads the epoch stored under `key`, 0 if the key does not exist. Used by `Lease.renew` and by the console's `/events` route to compute the current epoch per unit for fencing in the index's reply.

## `class Lease`
What a worker holds per unit once it has taken the epoch. It answers one question — `may_write()` — from a monotonic clock, not from the store: writing is allowed while `now − last_renewal < ttl − margin` and the lease has not been fenced. Renewal is "read the key and find it still mine". The store being unreachable does not by itself stop writing; the TTL does. Created by `Worker.take_epoch`; `Worker.renew_leases` renews all of them and reports the ones lost.

### `__init__(self, vars_, key, epoch, ttl=30.0, margin=5.0, clock=time.monotonic)`
`key` is the epoch row, `epoch` the number this holder took, `ttl` and `margin` the window (writes stop `margin` seconds before the TTL to leave room for the fence to propagate), `clock` a monotonic clock (tests inject a fake). Sets `last_renewal = clock()` now, `fenced = False`, `conflicts = 0`.

### `renew(self) -> bool`
Once fenced, always `False`. Otherwise read `current_epoch(key)`: if the store raises (unreachable), do not fence — return `may_write()` and keep going until `ttl − margin` runs out; if the live epoch differs from mine, set `fenced = True`, count a conflict and return `False`; else stamp `last_renewal` and return `True`. `conflicts` is what the worker sums into its heartbeat (`conflicts=`) and the console exports as `<sub>_epoch_conflicts`.

### `may_write(self) -> bool`
`not fenced and (clock() − last_renewal) < ttl − margin`. The one line the actuator asks before a write.

### `seconds_left(self) -> float`
Time until `may_write` would become false, floored at 0.

## Notes
- `test_lease_on_a_monotonic_clock`: at 24.9 s the lease may write; at 25.1 s it may not and `seconds_left() == 0`; a successful `renew()` restores it; after someone else calls `next_epoch` on the same key, `renew()` returns False, `fenced` is set and `conflicts == 1`.
- The fence is discovered at renewal, never pushed: a zombie keeps writing for at most `ttl − margin` after the new holder took the epoch. That bounded window is the RPO the archive lesson accepts, and the epoch in the path is what lets the manifest mark that window as fenced afterwards.
