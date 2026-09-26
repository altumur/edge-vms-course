"""Lesson 1 — what a cluster cannot know.

A domain is N clusters, each with its own raft (Nomad region), its own
Variables and its own object store. Nothing replicates between them. The
domain's directory is therefore an AGGREGATION over N cluster directories:
partial, stale by a bounded amount, sometimes incomplete — and the honest
response to "where is camera 7" when a cluster is unreachable is "not found
in the clusters I could reach", never a short list rendered as complete.

What a cluster publishes for the domain to read (М11 Lesson 10): one object
PER WORKER under `vms/snapshot/` — the controller's copy of the camera rows
placed on that worker, with the server, carrying a timestamp — plus
`vms/snapshot/unplaced` for the rows nobody holds, and the workers' own
heartbeats. Same shape as the heartbeats, and for the same reason: an object
store has a ceiling, and the one object the snapshot used to be was the only
thing in the platform that grew with the whole cluster. The domain never
reads a cluster's Variables: the rows stay in raft with one writer, and what
leaves is a copy with an age.

`Cluster` is the domain's handle on one region; `Federation` is the list;
`DomainDirectory` merges the snapshots with the incompleteness kept as a
first-class field of every answer.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from cluster.objectstore import ObjectStore
from cluster.variables import Variables

SNAPSHOT = "vms/snapshot/"           # a PREFIX: one object per worker, the shape the heartbeats already have
HEARTBEATS = "vms/heartbeats/"        # its sibling: the workers' own reports, one object each


class Unreachable(Exception):
    """The region did not answer. Raised by a cluster's Variables/objects
    when the link is down; the fakes raise it on demand."""


@dataclass
class Cluster:
    name: str
    vars: Variables
    objects: ObjectStore
    reaches: frozenset = frozenset()      # networks this cluster can see: {"vlan:cctv-a", ...}
    is_domain_cluster: bool = False       # the one that hosts the domain services — a stated decision

    def snapshot(self) -> dict | None:
        """The cluster's cameras and placement, merged from one object per worker.

        Read exactly the way `heartbeats()` below is read — a listing and a get per
        object — because the cluster now publishes it in exactly that shape. There is
        no single instant at which the whole cluster was in the state this returns:
        each shard carries its own `ts`, and the merge keeps the OLDEST as the age of
        the whole, because a directory is only as fresh as its stalest part.

        A camera can appear in two shards for the length of a move. The newer shard
        wins; reporting it twice would make `where()` raise on what is not a fault."""
        keys = self.objects.list(SNAPSHOT)
        if not keys:
            return None
        best: dict[str, tuple[float, dict]] = {}
        oldest = None
        for key in keys:
            raw = self.objects.get(key)
            if not raw:
                continue
            shard = json.loads(raw)
            ts = float(shard.get("ts", 0))
            oldest = ts if oldest is None else min(oldest, ts)
            for row in shard.get("cameras", []):
                uid = str(row.get("id"))
                if uid not in best or ts > best[uid][0]:
                    best[uid] = (ts, row)
        return {"cluster": self.name, "ts": oldest or 0, "cameras": [r for _, r in best.values()]}

    def heartbeats(self) -> dict[str, dict]:
        """worker -> its last heartbeat (М10's shape: status, server, epoch per camera).

        `vms/heartbeats/` holds heartbeats and nothing else, so this is a listing and a
        get — the same two calls `snapshot()` above makes, against a sibling directory.
        The filter this used to carry (`endswith("/heartbeat") and count("/") == 2`)
        was the price of a key layout that put every worker's name at the top."""
        out = {}
        for key in self.objects.list(HEARTBEATS):
            raw = self.objects.get(key)
            if raw:
                hb = json.loads(raw)
                out[hb["worker"]] = hb
        return out


@dataclass
class Answer:
    """Where a camera is, and how much of the domain that claim covers."""
    camera: int
    worker: str | None
    server: str | None
    cluster: str | None
    searched: list[str]
    unreachable: list[str]

    @property
    def complete(self) -> bool:
        return not self.unreachable

    @property
    def found(self) -> bool:
        return self.cluster is not None

    def sentence(self) -> str:
        if self.found:
            s = f"camera {self.camera} is on {self.worker or 'no worker yet'} ({self.server or '?'}) in {self.cluster}"
            return s if self.complete else s + f" (and {', '.join(self.unreachable)} could not be asked)"
        if self.complete:
            return f"camera {self.camera} is in no cluster of the domain ({len(self.searched)} clusters searched)"
        return (f"camera {self.camera} was not found in the {len(self.searched)} cluster(s) I could reach; "
                f"{', '.join(self.unreachable)} unreachable — not 'not anywhere'")


@dataclass
class Federation:
    clusters: dict[str, Cluster] = field(default_factory=dict)

    def add(self, c: Cluster) -> None:
        self.clusters[c.name] = c

    @property
    def domain_cluster(self) -> Cluster:
        hosts = [c for c in self.clusters.values() if c.is_domain_cluster]
        if len(hosts) != 1:
            raise RuntimeError(f"exactly one domain cluster must be designated; found {[c.name for c in hosts]}")
        return hosts[0]


class DomainDirectory:
    """A directory of directories. Reads each cluster's snapshot; never
    copies the rows; never claims more than it reached."""

    def __init__(self, fed: Federation, wall=time.time):
        self.fed, self.wall = fed, wall

    def scan(self) -> tuple[dict[str, dict], list[str]]:
        """{cluster: snapshot}, and the clusters that did not answer."""
        out, down = {}, []
        for name, c in self.fed.clusters.items():
            try:
                out[name] = c.snapshot() or {"cameras": [], "ts": 0}
            except Unreachable:
                down.append(name)
        return out, down

    def where(self, camera) -> Answer:
        """`camera` is the DOMAIN's name for it — the `ref` the domain gave the
        cluster when it forwarded the create. A cluster's own `id` is the
        cluster's: two clusters both have an id 7, and the domain never asks by it."""
        scan, down = self.scan()
        hits = [(cl, row) for cl, snap in scan.items() for row in snap.get("cameras", []) if str(row.get("ref", "")) == str(camera)]
        if len(hits) > 1:
            raise RuntimeError(f"camera {camera} claimed by {[h[0] for h in hits]}: a placement failure, not a tie")
        if hits:
            cl, row = hits[0]
            return Answer(camera, row.get("worker"), row.get("server"), cl, sorted(scan), sorted(down))
        return Answer(camera, None, None, None, sorted(scan), sorted(down))

    def holdings(self) -> tuple[dict[str, dict[str, list[int]]], list[str]]:
        """{cluster: {worker: [cameras]}} from the snapshots, and the silent clusters."""
        scan, down = self.scan()
        out: dict[str, dict[str, list[int]]] = {}
        for cl, snap in scan.items():
            out[cl] = {}
            for row in snap.get("cameras", []):
                out[cl].setdefault(row.get("worker") or "(unplaced)", []).append(row.get("ref") or f"{cl}/{row['id']}")
        return out, down

    def ages(self) -> dict[str, float]:
        """How old each cluster's snapshot is — the domain's RPO, shown, never hidden."""
        scan, _ = self.scan()
        now = self.wall()
        return {cl: max(0.0, now - float(snap.get("ts", 0))) for cl, snap in scan.items()}
