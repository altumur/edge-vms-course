"""Lesson 1 — placement at the level above the one М11 built.

    Nomad     picks the SERVER   on resources        because it knows the servers — and how many workers, and where
    Cluster   picks the WORKER   on capacity         because only its controller sees its workers' headroom (М11 Lesson 10)
    Domain    picks the CLUSTER  on REACHABILITY     because only it knows which clusters exist and what each can see

Reachability, not capacity: a camera on the warehouse VLAN can be reached
from the warehouse cluster and from nowhere else. Capacity only breaks ties
among clusters that can see the camera at all.

Only place when you must: the camera is new, or an operator asked. A dead
server is not a trigger (Nomad moves the worker; its cameras follow its
name). A dead cluster is not a trigger either — its cameras cannot be
reached from anywhere else.

The placement is STORED, with a reason and a time, in the domain cluster's
Variables under domain/placement/<camera> — by check-and-set, which is why
two placers racing is harmless: the second gets a conflict and re-reads.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from cluster.variables import Conflict, Variables

from .federation import Cluster, Federation


@dataclass(frozen=True)
class CameraSite:
    camera: int
    network: str                      # "vlan:cctv-b" — where the camera's packets can be seen from
    load: float = 1.0


@dataclass
class ClusterPlacement:
    cluster: str
    reason: str
    at: float
    rev: int


class Refused(Exception):
    """No cluster can reach that network — 'the domain cannot place it',
    never 'cluster X is full'."""


class ClusterPlacer:
    def __init__(self, fed: Federation, vars_: Variables | None = None, headroom=None, clock=time.time):
        """`headroom(cluster) -> float` is the cluster's spare capacity in units
        of I, from its own placement service; the domain does not measure it."""
        self.fed = fed
        self.vars = vars_ or fed.domain_cluster.vars
        self.headroom = headroom or (lambda c: 1.0)
        self.clock = clock

    def path(self, camera: int) -> str:
        return f"domain/placement/{camera}"

    def current(self, camera: int) -> ClusterPlacement | None:
        items, _ = self.vars.get(self.path(camera))
        if not items or not items.get("cluster"):
            return None
        return ClusterPlacement(items["cluster"], items["reason"], float(items["at"]), int(items["rev"]))

    def candidates(self, site: CameraSite, unreachable: set[str] = frozenset()) -> list[Cluster]:
        return [c for c in self.fed.clusters.values() if site.network in c.reaches and c.name not in unreachable]

    def place(self, site: CameraSite, unreachable: set[str] = frozenset()) -> ClusterPlacement:
        """Place ONE new camera. An existing placement is returned untouched."""
        have = self.current(site.camera)
        if have is not None:
            return have
        cands = self.candidates(site, unreachable)
        if not cands:
            seen = sorted(c.name for c in self.fed.clusters.values() if site.network in c.reaches)
            if seen:
                raise Refused(f"camera {site.camera}: the only cluster(s) reaching {site.network} "
                              f"({', '.join(seen)}) are unreachable; not placing elsewhere — nothing else can see it")
            raise Refused(f"camera {site.camera}: no cluster in the domain reaches {site.network}")
        best = max(sorted(cands, key=lambda c: c.name), key=lambda c: self.headroom(c.name))
        why = (f"only cluster reaching {site.network}" if len(cands) == 1
               else f"most headroom ({self.headroom(best.name):.1f}) among {len(cands)} reaching {site.network}")
        return self._store(site.camera, best.name, why)

    def _store(self, camera: int, cluster: str, reason: str, retries: int = 5) -> ClusterPlacement:
        for _ in range(retries):
            items, idx = self.vars.get(self.path(camera))
            if items and items.get("cluster"):           # somebody placed it while we thought
                return ClusterPlacement(items["cluster"], items["reason"], float(items["at"]), int(items["rev"]))
            rev = self._rev()
            pl = ClusterPlacement(cluster, reason, self.clock(), rev)
            try:
                self.vars.put(self.path(camera), {"cluster": cluster, "reason": reason, "at": pl.at, "rev": rev}, cas=idx)
                return pl
            except Conflict:
                continue                                 # the other placer won; re-read and agree with it
        raise RuntimeError(f"could not store placement for camera {camera} after {retries} conflicts")

    def _rev(self) -> int:
        items, idx = self.vars.get("domain/placement")
        rev = int(items["rev"]) + 1 if items else 1
        try:
            self.vars.put("domain/placement", {"rev": rev}, cas=idx)
        except Conflict:
            return self._rev()
        return rev

    def rebalance_across_clusters(self, *a, **k):
        raise NotImplementedError("a worker never crosses a cluster (М11); the domain moves a camera between "
                                  "clusters only when an operator asks, and then as delete-here/create-there")
