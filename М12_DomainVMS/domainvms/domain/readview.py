"""Lesson 3 — the camera list, and where the console gets it.

Not a fan-out to N consoles (waits for the slowest, breaks on the first
dead one). Not status in Variables (raft is not for frequent, large data).
The read model reads what each cluster's WORKERS already publish beside
their heartbeat — `vms/<worker>/heartbeat`, carrying the worker's status
per camera, its server, its epochs — plus each cluster's `vms/snapshot/*`
(one object per worker, the same shape) for the rows a worker is not yet
running. It holds them in memory and
serves the list, search and pagination from there. No call to any worker
or controller on any request. It is a cache that admits to being one: a
restart is one pass over the objects.

Staleness is shown, never hidden: every row carries the age of the
heartbeat it came from; a worker older than `lost_after` is *stale — last
known state*, its cameras still listed. A cluster that did not answer is
reported as such, with its rows from the last successful pass — never as
an empty cluster.

Grouped by failure domain: the heartbeat carries the server the worker runs
on, so when a server dies its workers go silent together and the console
shows ONE cause, not thirty greyed cameras.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .federation import Federation, Unreachable


@dataclass
class Row:
    camera: int
    name: str
    worker: str
    cluster: str
    server: str
    phase: str
    position: str
    revision: int
    observed_revision: int
    epoch: int
    age: float                  # seconds since the heartbeat this row came from
    worker_state: str           # "live" | "stale" | "unreachable" (cluster did not answer)

    def to_json(self) -> dict:
        d = self.__dict__.copy()
        d["as_of"] = f"as of {self.age:.0f} s ago" if self.worker_state == "live" else f"{self.worker_state} — last known state, {self.age:.0f} s old"
        return d


@dataclass
class Cause:
    scope: str                  # "cluster" | "server" | "worker"
    name: str
    silent_for: float
    workers: list[str]
    cameras: int

    def sentence(self) -> str:
        what = {"cluster": "cluster unreachable", "server": "server silent", "worker": "worker silent"}[self.scope]
        return f"{what}: {self.name} for {self.silent_for:.0f} s — {len(self.workers)} worker(s), {self.cameras} camera(s)"


@dataclass
class Snapshot:
    worker: str
    cluster: str
    ts: float
    server: str
    status: list[dict]


class ReadView:
    def __init__(self, fed: Federation, lost_after: float = 45.0, wall=time.time):
        self.fed, self.lost_after, self.wall = fed, lost_after, wall
        self.snapshots: dict[tuple[str, str], Snapshot] = {}     # (cluster, worker) -> last heartbeat seen
        self.cluster_ok: dict[str, float] = {}
        self.cluster_down_since: dict[str, float] = {}
        self.passes = 0

    # -- the one pass ----------------------------------------------------------
    def refresh(self) -> None:
        now = self.wall()
        for name, c in self.fed.clusters.items():
            try:
                for w, hb in c.heartbeats().items():
                    self.snapshots[(name, w)] = Snapshot(w, name, float(hb.get("ts", 0)), str(hb.get("server", "?")),
                                                         list(hb.get("status", [])))
            except Unreachable:
                self.cluster_down_since.setdefault(name, now)
                continue
            self.cluster_ok[name] = now
            self.cluster_down_since.pop(name, None)
        self.passes += 1

    # -- reads, from memory ----------------------------------------------------
    def rows(self) -> list[Row]:
        now = self.wall()
        out = []
        for s in self.snapshots.values():
            age = max(0.0, now - s.ts)
            state = "unreachable" if s.cluster in self.cluster_down_since else ("stale" if age > self.lost_after else "live")
            for st in s.status:
                out.append(Row(int(st["id"]), st.get("name", ""), s.worker, s.cluster, s.server, st.get("phase", "?"),
                               st.get("position", "?"), int(st.get("revision", 0)), int(st.get("observed_revision", 0)),
                               int(st.get("epoch", 0)), age, state))
        out.sort(key=lambda r: (r.cluster, r.worker, r.camera))
        return out

    def list(self, q: str = "", page: int = 1, size: int = 50, cluster: str | None = None) -> dict:
        rows = [r for r in self.rows() if (not cluster or r.cluster == cluster)
                and (not q or q.lower() in r.name.lower() or q == str(r.camera))]
        total = len(rows)
        page_rows = rows[(page - 1) * size: page * size]
        return {"total": total, "page": page, "size": size, "rows": [r.to_json() for r in page_rows],
                "clusters": {n: ("unreachable" if n in self.cluster_down_since else "ok") for n in self.fed.clusters},
                "complete": not self.cluster_down_since}

    def causes(self) -> list[Cause]:
        """Silence grouped by the largest failure domain that explains it."""
        now = self.wall()
        causes: list[Cause] = []
        for cl, since in self.cluster_down_since.items():
            ws = [s for s in self.snapshots.values() if s.cluster == cl]
            causes.append(Cause("cluster", cl, now - since, sorted(s.worker for s in ws), sum(len(s.status) for s in ws)))
        silent = [s for s in self.snapshots.values() if s.cluster not in self.cluster_down_since and now - s.ts > self.lost_after]
        by_server: dict[tuple[str, str], list[Snapshot]] = {}
        for s in silent:
            by_server.setdefault((s.cluster, s.server), []).append(s)
        for (cl, server), group in by_server.items():
            all_on_server = [s for s in self.snapshots.values() if s.cluster == cl and s.server == server]
            silent_for = now - max(s.ts for s in group)
            if len(group) == len(all_on_server) and len(group) > 1:
                causes.append(Cause("server", f"{cl}/{server}", silent_for, sorted(s.worker for s in group), sum(len(s.status) for s in group)))
            else:
                for s in group:
                    causes.append(Cause("worker", f"{cl}/{s.worker}", now - s.ts, [s.worker], len(s.status)))
        return sorted(causes, key=lambda c: (-c.cameras, c.name))
