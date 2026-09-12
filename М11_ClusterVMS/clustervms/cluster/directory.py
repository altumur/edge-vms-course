"""Where is camera 7 — answered from the cluster in one scan.

`vms/workers/*` is the assignment, written by the controller into one raft
and read by every worker from the same raft; scanning it is the cluster
directory, and it is *consistent* because it is one store. М12 aggregates
several of these and cannot be — which is why the question is answered here.
"""
from __future__ import annotations

import time

from vmsplatform.contract import Assignment


class Directory:
    def __init__(self, vars_, ttl: float = 5.0, clock=time.monotonic, prefix: str = "vms/workers/"):
        self.vars, self.ttl, self.clock, self.prefix = vars_, ttl, clock, prefix
        self._cache: dict[str, list[str]] = {}
        self._at = -1e9
        self.scans = 0

    def scan(self, force: bool = False) -> dict[str, list[str]]:
        if force or self.clock() - self._at >= self.ttl:
            out = {}
            for path in self.vars.list(self.prefix):
                worker = path[len(self.prefix):]
                items, _ = self.vars.get(path)
                out[worker] = Assignment.from_items(worker, items).units
            self._cache, self._at, self.scans = out, self.clock(), self.scans + 1
        return self._cache

    def where(self, camera_id: int) -> str | None:
        hits = [w for w, units in self.scan().items() if str(camera_id) in units]
        return hits[0] if len(hits) == 1 else (None if not hits else "+".join(sorted(hits)))   # a reassignment window shows as both

    def holdings(self, worker: str) -> list[int]:
        return sorted(int(u) for u in self.scan().get(worker, []))

