"""М9 Lesson 6 — the reconcile loop, with nothing in it. Copied here
unchanged, because this is the contract: the same `>=`, the same stop loop
over what is RUNNING, the same jitter. М9's seven tests run against it in
tests/test_worker.py without a change of meaning.

    Desired state is persisted. Actual state is derived.
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # reconciler.py — М9 Lesson 6's reconcile loop, copied unchanged: desired persisted, actual derived
#
# **Role in the module.** Lesson 4's contract with М9. The loop that turns a list of desired camera rows into
# `start`/`restart`/`stop` calls on an actuator, with backoff on failure and a `converged | lagging | stalled`
# status per camera. It is copied here *unchanged* on purpose: the same `>=` on revision, the same stop loop
# over what is RUNNING, the same jitter, so М9's seven tests (`tests/test_lesson4_worker.py`, tests 1–7) run
# against it without a change of meaning. It knows nothing about epochs, leases, slots or the store —
# `worker.py` wraps it: `VmsWorker` is its `Store` (`desired()` returns the rows of the assignment) and
# `VmsWorker._actuate` is its `Actuator` (the gate that takes an epoch and checks the lease before the real
# actuator). "Desired state is persisted. Actual state is derived."
#
# ## Module-level names
# - `CONVERGED, LAGGING, STALLED = "converged", "lagging", "stalled"` — the three positions `status()`
#   reports; the worker copies them into its heartbeat as `position`.
# - `Store` (Protocol) — anything with `desired() -> list[dict]`; the rows must carry `id`, `enabled`,
#   `revision`.
# - `Actuator` — `Callable[[verb, cam], bool]`: `start` / `restart` / `stop` with the row (`stop` gets only
#   `{id}`); True on success.
#
# ## `class Reconciler`
# One loop over one store and one actuator. State, all in memory and rebuilt from nothing on restart: `actual`
# (`{camera_id: {"revision": n}}` for pipelines it believes are running — the comment says IN MEMORY ONLY, and
# test 5 shows why: a persisted `actual` is a cache that lies), `failures` (`{camera_id: {n, retry_at,
# delay}}`), `max_backoff` (60 s), `stall_failures` (3).
#
# ### `__init__(self, store, actuator, max_backoff=60.0, stall_failures=3)`
# Stores the collaborators and the two knobs; `actual` and `failures` start empty.
#
# ### `reconcile(self, now=0.0) -> list[(verb, id)]`
# One pass. `desired` is the store's rows with `enabled` true, by id. For each: skip if `actual[id].revision
# >= desired revision` (already there — the `>=` is the contract) or if the camera is in backoff (`now <
# retry_at`); otherwise `start` (not in `actual`) or `restart` (in `actual`, revision behind), and on success
# record the revision and clear the failure, on failure `_fail` and record `("failed", id)`. Then the stop
# loop walks `actual` — what is RUNNING — and stops anything no longer desired (disabled or deleted), removing
# it from `actual`. Returns the actions in order. Tests 1–4: converge then idle; a revision bump restarts;
# disable and delete stop; a fresh instance re-derives (`actual == {}` and starts everything again).
#
# ### `_fail(self, cid, now)`
# Exponential backoff with jitter: `n` failures → base `min(2**n, max_backoff)`, delay `base × (0.5 + random ×
# 0.5)`, `retry_at = now + delay`. Test 6: 200 cameras all failing at `now=0` get retries spread over (1.0,
# 2.0] with a spread over 0.5 s; nothing retries at 0.5; all 200 at 2.0.
#
# ### `lost(self, cid, now)`
# The pipeline died underneath the loop: drop it from `actual` and count a failure so it is restarted after a
# backoff rather than instantly. The worker calls it from `pump_once` for every dead camera the actuator
# reports.
#
# ### `clear(self)`
# "What a fence does: the pipelines were stopped underneath the loop." Empties `actual` without touching the
# actuator; `VmsWorker.fence` calls `actuator.stop_all()` and then this.
#
# ### `status(self) -> dict[id, (position, lag)]`
# For each enabled desired row: `lag = max(revision − actual revision, 0)`; `CONVERGED` at 0; else `STALLED`
# once the camera has failed `stall_failures` times, else `LAGGING`. Test 7: a camera failing three times
# becomes stalled; a `lost` one goes back to lagging.
#
# ## Notes
# - The loop never sleeps or schedules itself: "nothing supervises the loop, because the loop is the process"
#   — `VmsWorker.run` calls `reconcile` on its poll.
# - A `restart` keeps the camera in `actual` only if the actuator succeeds; `VmsWorker._actuate` reuses the
#   held epoch on a restart so an edit does not fence anything
#   (`test_worker_runs_its_assignment_and_takes_an_epoch_per_camera`: a restart keeps epoch 1).
# ================================================================================================
from __future__ import annotations

import random
from typing import Callable, Protocol

CONVERGED, LAGGING, STALLED = "converged", "lagging", "stalled"


class Store(Protocol):
    def desired(self) -> list[dict]: ...


Actuator = Callable[[str, dict], bool]


class Reconciler:
    def __init__(self, store: Store, actuator: Actuator, max_backoff: float = 60.0, stall_failures: int = 3):
        self.store, self.actuator = store, actuator
        self.actual: dict[int, dict] = {}       # camera_id -> {"revision": n}   IN MEMORY ONLY
        self.failures: dict[int, dict] = {}
        self.max_backoff, self.stall_failures = max_backoff, stall_failures

    def reconcile(self, now: float = 0.0) -> list[tuple[str, int]]:
        desired = {c["id"]: c for c in self.store.desired() if c["enabled"]}
        actions: list[tuple[str, int]] = []
        for cid, cam in desired.items():
            have = self.actual.get(cid)
            if have and have["revision"] >= cam["revision"]:
                continue
            if self.failures.get(cid) and now < self.failures[cid]["retry_at"]:
                continue
            verb = "start" if not have else "restart"
            if self.actuator(verb, cam):
                self.actual[cid] = {"revision": cam["revision"]}
                self.failures.pop(cid, None)
                actions.append((verb, cid))
            else:
                self._fail(cid, now)
                actions.append(("failed", cid))
        for cid in list(self.actual):                 # the stop loop walks what is RUNNING
            if cid not in desired:
                self.actuator("stop", {"id": cid})
                del self.actual[cid]
                actions.append(("stop", cid))
        return actions

    def _fail(self, cid: int, now: float) -> None:
        n = self.failures.get(cid, {}).get("n", 0) + 1
        base = min(2 ** n, self.max_backoff)
        delay = base * (0.5 + random.random() * 0.5)
        self.failures[cid] = {"n": n, "retry_at": now + delay, "delay": delay}

    def lost(self, cid: int, now: float) -> None:
        self.actual.pop(cid, None)
        self._fail(cid, now)

    def clear(self) -> None:
        """What a fence does: the pipelines were stopped underneath the loop."""
        self.actual.clear()

    def status(self) -> dict[int, tuple[str, int]]:
        out: dict[int, tuple[str, int]] = {}
        for cam in self.store.desired():
            if not cam["enabled"]:
                continue
            cid = cam["id"]
            have = self.actual.get(cid, {}).get("revision", 0)
            lag = max(cam["revision"] - have, 0)
            if lag == 0:
                out[cid] = (CONVERGED, 0)
            elif self.failures.get(cid, {}).get("n", 0) >= self.stall_failures:
                out[cid] = (STALLED, lag)
            else:
                out[cid] = (LAGGING, lag)
        return out
