"""The fencing token and the lease — generic to any writer that can have
two instances. The subsystem decides what key the epoch goes in; the
platform only promises that it comes from one issuer and increases.

    next_epoch(vars, key)   issue the next epoch for `key` by check-and-set: two callers
                            racing get two different numbers, in order
    Lease                   may_write while now − last_renewal < TTL − margin, on a
                            monotonic clock; renew = read the key and find it still mine
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # epoch.py — the fencing-token issuer and the lease, generic to any writer that can have two instances
#
# **Role in the module.** Lesson 1. A unit (a camera, a fan-out, a model) may at any moment have two processes
# believing they run it — a zombie after a reschedule, or a reassignment window. The platform's answer is a
# fencing token, the *epoch*: a per-unit integer that comes from one issuer (the CAS on a Variables key) and
# only increases. Whoever holds the newest epoch is the writer; an older holder discovers it on its next
# renewal and stops. This file has the issuer (`next_epoch`), the reader (`current_epoch`) and the lease
# that a holder keeps on its epoch (`Lease`). The subsystem decides the key (`Subsystem.epoch_key(unit)`
# gives `<name>/epoch/<unit>`); the platform promises only that numbers come from one issuer, in order.
# `contract.Worker.take_epoch` and `renew_leases` are the callers; `vms.archive` puts the epoch in every
# segment and bucket path so a stale writer's output is identifiable afterwards.
#
# ## Module-level names
# None.
#
# ## Notes
# - `test_lease_on_a_monotonic_clock`: at 24.9 s the lease may write; at 25.1 s it may not and
#   `seconds_left() == 0`; a successful `renew()` restores it; after someone else calls `next_epoch` on the
#   same key, `renew()` returns False, `fenced` is set and `conflicts == 1`.
# - The fence is discovered at renewal, never pushed: a zombie keeps writing for at most `ttl − margin`
#   after the new holder took the epoch. That bounded window is the RPO the archive lesson accepts, and the
#   epoch in the path is what lets the manifest mark that window as fenced afterwards.
# ================================================================================================
from __future__ import annotations

import time

from .variables import Conflict, Variables


# Issues the next epoch for `key` by check-and-set: read `{epoch}` and its ModifyIndex (missing key means
# epoch 0), try to `put` `epoch + 1` with `cas=idx`; on `Conflict` re-read and try again, up to `retries`
# times, then raise `RuntimeError`. Because the CAS is on the row's index, two callers racing get two
# different numbers in order and no number is ever reused — `test_epoch_issuer_never_reuses_a_number` runs
# four threads issuing 25 each and asserts the set is exactly 1..100. The returned index is the row's
# ModifyIndex after the write (unused by `Worker.take_epoch`, which keeps only the epoch).
def next_epoch(vars_: Variables, key: str, retries: int = 200) -> tuple[int, int]:
    for _ in range(retries):
        items, idx = vars_.get(key)
        current = int(items["epoch"]) if items else 0
        try:
            new_idx = vars_.put(key, {"epoch": current + 1}, cas=idx)
            return current + 1, new_idx
        except Conflict:
            continue
    raise RuntimeError(f"could not issue an epoch for {key} after {retries} conflicts")


# Reads the epoch stored under `key`, 0 if the key does not exist. Used by `Lease.renew` and by the
# console's `/events` route to compute the current epoch per unit for fencing in the index's reply.
def current_epoch(vars_: Variables, key: str) -> int:
    items, _ = vars_.get(key)
    return int(items["epoch"]) if items else 0


# What a worker holds per unit once it has taken the epoch. It answers one question — `may_write()` — from a
# monotonic clock, not from the store: writing is allowed while `now − last_renewal < ttl − margin` and the
# lease has not been fenced. Renewal is "read the key and find it still mine". The store being unreachable
# does not by itself stop writing; the TTL does. Created by `Worker.take_epoch`; `Worker.renew_leases`
# renews all of them and reports the ones lost.
class Lease:
    # `key` is the epoch row, `epoch` the number this holder took, `ttl` and `margin` the window (writes
    # stop `margin` seconds before the TTL to leave room for the fence to propagate), `clock` a monotonic
    # clock (tests inject a fake). Sets `last_renewal = clock()` now, `fenced = False`, `conflicts = 0`.
    def __init__(self, vars_: Variables, key: str, epoch: int, ttl: float = 30.0, margin: float = 5.0,
                 clock=time.monotonic):
        self.vars, self.key, self.epoch = vars_, key, epoch
        self.ttl, self.margin, self.clock = ttl, margin, clock
        self.last_renewal = clock()
        self.fenced = False
        self.conflicts = 0

    # Once fenced, always `False`. Otherwise read `current_epoch(key)`: if the store raises (unreachable),
    # do not fence — return `may_write()` and keep going until `ttl − margin` runs out; if the live epoch
    # differs from mine, set `fenced = True`, count a conflict and return `False`; else stamp `last_renewal`
    # and return `True`. `conflicts` is what the worker sums into its heartbeat (`conflicts=`) and the
    # console exports as `<sub>_epoch_conflicts`.
    def renew(self) -> bool:
        if self.fenced:
            return False
        try:
            live = current_epoch(self.vars, self.key)
        except Exception:                      # noqa: BLE001 — the store is unreachable; keep going until TTL − margin
            return self.may_write()
        if live != self.epoch:
            self.fenced, self.conflicts = True, self.conflicts + 1
            return False
        self.last_renewal = self.clock()
        return True

    # `not fenced and (clock() − last_renewal) < ttl − margin`. The one line the actuator asks before a
    # write.
    def may_write(self) -> bool:
        return not self.fenced and (self.clock() - self.last_renewal) < (self.ttl - self.margin)

    # Time until `may_write` would become false, floored at 0.
    def seconds_left(self) -> float:
        return max(0.0, (self.ttl - self.margin) - (self.clock() - self.last_renewal))
