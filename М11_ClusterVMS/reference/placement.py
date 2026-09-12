"""Lesson 5 — placement onto recorders: capacity measured, constraints as
labels, the placement STORED, and one rule with a property test:

    adding a recorder moves nothing.

Not consistent hashing: cameras are not uniform (a 4K stream is eight 720p
streams), constraints break a ring, and at 3am "why is camera 812 on
recorder 3" must be a row with a reason and a timestamp, not a hash to recompute.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Camera:
    id: int
    load: float                       # in units of the probe's per-pipeline increment I (a 720p stream = 1.0)
    labels: frozenset = frozenset()   # e.g. {"vlan:cctv-b"} — where it is reachable from


@dataclass
class recorder:
    id: str
    capacity: float                   # cameras' worth of load this shard holds: (budget - B) / I from the probe
    labels: frozenset = frozenset()   # which VLANs this recorder's server can reach
    epoch: int = 0


@dataclass
class Placement:
    node: str
    reason: str
    at: int                           # a revision, not a wall clock


class Placer:
    def __init__(self, nodes: dict[str, recorder]):
        self.nodes = nodes
        self.placed: dict[int, Placement] = {}     # STORED, never derived
        self.rev = 0

    # -- helpers -----------------------------------------------------------
    def load_of(self, node_id: str, cameras: dict[int, Camera]) -> float:
        return sum(cameras[c].load for c, p in self.placed.items() if p.node == node_id and c in cameras)

    def eligible(self, cam: Camera) -> list[recorder]:
        return [n for n in self.nodes.values() if cam.labels <= n.labels]

    # -- placement ---------------------------------------------------------
    def place(self, cam: Camera, cameras: dict[int, Camera]) -> Placement | None:
        """Place ONE new camera. Existing placements are never touched."""
        if cam.id in self.placed:
            return self.placed[cam.id]
        self.rev += 1
        best, best_free = None, 0.0
        for n in self.eligible(cam):
            free = n.capacity - self.load_of(n.id, cameras)
            if free >= cam.load and free > best_free:
                best, best_free = n, free
        if best is None:
            return None                           # "the system is full" — never "recorder 3 is full"
        why = f"most free capacity ({best_free:.1f}) among {len(self.eligible(cam))} eligible"
        self.placed[cam.id] = Placement(best.id, why, self.rev)
        return self.placed[cam.id]

    def remove(self, cam_id: int) -> None:
        self.placed.pop(cam_id, None)

    def add_node(self, node: recorder) -> None:
        self.nodes[node.id] = node                # nothing moves. That is the rule.

    def remove_node(self, node_id: str, cameras: dict[int, Camera]) -> list[int]:
        """A recorder retired ON PURPOSE (not a failover — failover moves the recorder).
        Its cameras are re-placed; returns those that found no home."""
        del self.nodes[node_id]
        orphans = [c for c, p in self.placed.items() if p.node == node_id]
        homeless = []
        for c in orphans:
            del self.placed[c]
            if self.place(cameras[c], cameras) is None:
                homeless.append(c)
        return homeless

    # -- rebalance: explicit, budgeted, observable, interruptible -----------
    def rebalance(self, cameras: dict[int, Camera], budget: int) -> list[tuple[int, str, str]]:
        """Move at most `budget` cameras from the most loaded recorder to the least,
        only while that reduces the spread. Returns the moves as (cam, from, to).
        Every move is the ONE two-writer operation in this module — the old recorder
        must stop and the new one start — which is why it needs the epoch."""
        moves = []
        for _ in range(budget):
            loads = {n: self.load_of(n, cameras) / self.nodes[n].capacity for n in self.nodes}
            hi = max(loads, key=loads.get); lo = min(loads, key=loads.get)
            if loads[hi] - loads[lo] < 0.10:
                break                             # within 10 %: leave it alone
            candidates = [c for c, p in self.placed.items() if p.node == hi
                          and cameras[c].labels <= self.nodes[lo].labels]
            if not candidates:
                break
            c = min(candidates, key=lambda c: cameras[c].load)
            if self.load_of(lo, cameras) + cameras[c].load > self.nodes[lo].capacity:
                break
            self.rev += 1
            self.placed[c] = Placement(lo, f"rebalance from {hi} (spread {loads[hi]-loads[lo]:.0%})", self.rev)
            moves.append((c, hi, lo))
        return moves


# -- invariants ---------------------------------------------------------------

def check_invariants(p: Placer, cameras: dict[int, Camera]) -> None:
    for cid, pl in p.placed.items():
        assert pl.node in p.nodes, f"camera {cid} placed on a recorder that does not exist"
        assert cameras[cid].labels <= p.nodes[pl.node].labels, f"camera {cid} violates its constraint"
    for n in p.nodes.values():
        assert p.load_of(n.id, cameras) <= n.capacity + 1e-9, f"{n.id} over capacity"
    assert len(p.placed) == len(set(p.placed)), "a camera on two recorders"
