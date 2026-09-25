"""Lesson 3 — the camera list, and where the console gets it.

Not a fan-out to N consoles (waits for the slowest, breaks on the first
dead one). Not status in Variables (raft is not for frequent, large data).
The read model reads what each cluster's WORKERS already publish —
`vms/heartbeats/<worker>`, carrying the worker's status per camera, its
server, its epochs — and, for the cameras no worker reports at all, each
cluster's `vms/snapshot/*`. It holds both in memory and serves the list,
search and pagination from there. No call to any worker or controller on
any request. It is a cache that admits to being one: a restart is one pass
over the objects.

The two sources answer different questions and the list keeps them apart.
A heartbeat is an OBSERVATION: this camera is running, at this revision,
on this worker. The snapshot is the CONFIGURATION: this camera is supposed
to exist. A camera that is configured and observed appears once, from the
observation. A camera that is configured and observed by nobody — nothing
placed it, or the worker that holds it has never reported — appears from
the snapshot, marked `configured`, and that is the whole of "set up but
not working" (М10A Lesson 13 shows the same pair inside one cluster, as
`rows` beside `configured`).

Until this was written the list simply did not contain those cameras: an
operator could add one, watch it fail to be placed, and see nothing at all
in the domain — the most confusing shape a fault can take.

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

from .federation import DomainDirectory, Federation, Unreachable


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
    worker_state: str           # "live" | "stale" | "unreachable" (cluster did not answer) | "configured" (no worker reports it)
    ref: str = ""               # the name the DOMAIN knows it by; the cluster's number is the cluster's

    def to_json(self) -> dict:
        d = self.__dict__.copy()
        if self.worker_state == "live":
            d["as_of"] = f"as of {self.age:.0f} s ago"
        elif self.worker_state == "configured":
            d["as_of"] = f"configured — no worker reports it; the cluster's copy is {self.age:.0f} s old"
        else:
            d["as_of"] = f"{self.worker_state} — last known state, {self.age:.0f} s old"
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
        self.configured: dict[str, list[dict]] = {}              # cluster -> the rows its snapshot carries
        self.configured_at: dict[str, float] = {}                # …and how old that copy is
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
                # …and the cluster's own copy of what SHOULD exist, for the cameras no worker reports.
                # Read in the same pass and from the same cluster, so a cluster that goes unreachable
                # loses both together rather than leaving one of them stale in a way nothing explains.
                self.configured[name] = (c.snapshot() or {}).get("cameras", [])
                self.configured_at[name] = float((c.snapshot() or {}).get("ts", 0))
            except Unreachable:
                self.cluster_down_since.setdefault(name, now)
                continue
            self.cluster_ok[name] = now
            self.cluster_down_since.pop(name, None)
        self.passes += 1

    # -- reads, from memory ----------------------------------------------------
    # The cluster that last reported the camera the domain calls `camera`, and the row it reported — kept when
    # that cluster stops answering, which is the point: an edit for a camera that is off is measured against
    # what the domain last SAW of it (Lesson 9). `refresh` leaves a silent cluster's copy where it was.
    def last_known(self, camera) -> tuple[str, dict] | None:
        for cluster, rows in self.configured.items():
            for row in rows:
                if str(row.get("ref", "")) == str(camera):
                    return cluster, row
        return None

    def rows(self) -> list[Row]:
        now = self.wall()
        out = []
        for s in self.snapshots.values():
            age = max(0.0, now - s.ts)
            state = "unreachable" if s.cluster in self.cluster_down_since else ("stale" if age > self.lost_after else "live")
            for st in s.status:
                out.append(Row(int(st["id"]), st.get("name", ""), s.worker, s.cluster, s.server, st.get("phase", "?"),
                               st.get("position", "?"), int(st.get("revision", 0)), int(st.get("observed_revision", 0)),
                               int(st.get("epoch", 0)), age, state, str(st.get("ref", ""))))
        # What the workers report, and then what the cluster says exists and nobody reports. The second
        # set is small by construction — it is the cameras that are NOT running — and it is the set an
        # operator is looking for when something has gone wrong.
        # Keyed by what the DOMAIN calls a camera — its `ref` — and not by the cluster's own id: two
        # clusters both have a camera 7 (М12 Lesson 1), and the same camera is identified one way in a
        # worker's status entry and another in the cluster's snapshot. Deduping by the cluster's number
        # would show a configured camera twice, once under each name.
        def ident(cluster: str, ref, cam) -> tuple:
            return (cluster, str(ref) if ref else str(cam))

        seen = {ident(r.cluster, r.ref, r.camera) for r in out}
        for cl, rows in self.configured.items():
            if cl in self.cluster_down_since:
                continue
            age = max(0.0, now - self.configured_at.get(cl, 0))
            for row in rows:
                try:
                    cam = int(row["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                ref = row.get("ref") or ""
                if ident(cl, ref, cam) in seen:
                    continue
                out.append(Row(cam, str(row.get("name", "")), str(row.get("worker") or ""), cl,
                               str(row.get("server") or "?"), "unobserved", "", int(row.get("revision", 0)),
                               0, 0, age, "configured", str(ref)))
        out.sort(key=lambda r: (r.cluster, r.worker, r.camera))
        return out

    def list(self, q: str = "", page: int = 1, size: int = 50, cluster: str | None = None) -> dict:
        rows = [r for r in self.rows() if (not cluster or r.cluster == cluster)
                and (not q or q.lower() in r.name.lower() or q == str(r.camera))]
        total = len(rows)
        page_rows = rows[(page - 1) * size: page * size]
        return {"total": total, "page": page, "size": size, "rows": [r.to_json() for r in page_rows],
                "clusters": {n: ("unreachable" if n in self.cluster_down_since else "ok") for n in self.fed.clusters},
                "rpo": self.rpo(),
                "complete": not self.cluster_down_since}

    # How far behind each cluster's published copy is, in seconds. `DomainDirectory.ages()` has computed
    # this since Lesson 1 and nothing outside a test ever called it — so the one number that says "this
    # cluster's controller stopped publishing" was computable and never computed.
    #
    # It is a different silence from the one `causes()` reports. A silent WORKER means cameras are not
    # running; a stale SNAPSHOT means the cluster is running fine and the domain's picture of it is not
    # moving. Shown apart, because the operator does different things about them.
    def rpo(self) -> dict[str, float | None]:
        """{cluster: seconds behind}, `None` for a cluster that did not answer."""
        try:
            ages = DomainDirectory(self.fed, wall=self.wall).ages()
        except Exception:                                        # noqa: BLE001 — a reader never fails a list
            ages = {}
        return {n: (None if n in self.cluster_down_since else round(ages.get(n), 1) if n in ages else None)
                for n in self.fed.clusters}

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
