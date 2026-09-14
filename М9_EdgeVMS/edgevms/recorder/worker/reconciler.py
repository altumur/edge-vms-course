"""Lesson 6 — the reconcile loop, with nothing in it.

Pure logic over an injected store and actuator. No database, no GStreamer,
no network. This is the part of the product that survives the rewrite
(Lesson 9, Step 5): only the actuator changes.

    Desired state is persisted. Actual state is derived.
"""
from __future__ import annotations

import random
from typing import Callable, Protocol

CONVERGED, LAGGING, STALLED = "converged", "lagging", "stalled"


class Store(Protocol):
    def desired(self) -> list[dict]: ...


Actuator = Callable[[str, dict], bool]


class Reconciler:
    def __init__(self, store: Store, actuator: Actuator,
                 max_backoff: float = 60.0, stall_failures: int = 3):
        self.store = store
        self.actuator = actuator
        self.actual: dict[int, dict] = {}     # camera_id -> {"revision": n}   IN MEMORY ONLY
        self.failures: dict[int, dict] = {}   # camera_id -> {"n":, "retry_at":, "delay":}
        self.max_backoff = max_backoff
        self.stall_failures = stall_failures

    def reconcile(self, now: float = 0.0) -> list[tuple[str, int]]:
        desired = {c["id"]: c for c in self.store.desired() if c["enabled"]}
        actions: list[tuple[str, int]] = []

        for cid, cam in desired.items():
            have = self.actual.get(cid)
            if have and have["revision"] >= cam["revision"]:
                continue                                   # already applied
            if self.failures.get(cid) and now < self.failures[cid]["retry_at"]:
                continue                                   # in backoff, not yet
            verb = "start" if not have else "restart"
            if self.actuator(verb, cam):
                self.actual[cid] = {"revision": cam["revision"]}
                self.failures.pop(cid, None)
                actions.append((verb, cid))
            else:
                self._fail(cid, now)
                actions.append(("failed", cid))

        # The stop loop walks what we are RUNNING, not what is desired: you
        # cannot learn about a deletion by looking at rows that exist.
        for cid in list(self.actual):
            if cid not in desired:
                self.actuator("stop", {"id": cid})
                del self.actual[cid]
                actions.append(("stop", cid))
        return actions

    def _fail(self, cid: int, now: float) -> None:
        n = self.failures.get(cid, {}).get("n", 0) + 1
        base = min(2 ** n, self.max_backoff)
        delay = base * (0.5 + random.random() * 0.5)      # jitter: 50-100% of base
        self.failures[cid] = {"n": n, "retry_at": now + delay, "delay": delay}

    def lost(self, cid: int, now: float) -> None:
        """A running pipeline died (bus error, watchdog). Forget it so the next
        pass restarts it, and count the failure so backoff applies."""
        self.actual.pop(cid, None)
        self._fail(cid, now)

    def status(self) -> dict[int, tuple[str, int]]:
        """camera_id -> (position, lag). The vocabulary from Lesson 6, Step 7.
        Positions only — reasons live on the conditions axis (Lesson 9)."""
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
