"""Lesson 11 — hundreds of small members, measured.

The module promised a directory and a read view for "low hundreds of clusters' worth". With server rooms
that meant a handful of clusters and hundreds of cameras; with cameras as members it means hundreds of
CLUSTERS, each with one camera, and a good share of them off at any moment. The code did not change shape
when the members did. Its cost did, in three places nobody had reason to look at with three clusters:

    per pass        what one pass of the read view asks of every member — counted, not estimated
    per edit        what "where is camera X" costs — the directory scans every member, every call
    per silence     what a member that does not answer costs — on a real link, the whole timeout

`Meter` wraps each member's stores, counts the calls a pass makes, and charges each one what it would cost
on a link: `latency` for an answer, `timeout` for finding out there is none. `pass_time(lanes)` is the
wall time of that work spread over `lanes` concurrent readers. The numbers in Lesson 11 come out of it.
"""
from __future__ import annotations

from collections import defaultdict

from .federation import Cluster, Federation, Unreachable


class _Metered:
    def __init__(self, meter: "Meter", name: str, inner):
        self.meter, self.name, self.inner = meter, name, inner

    def _call(self, fn, *a, **kw):
        m = self.meter
        m.calls[self.name] += 1
        try:
            out = fn(*a, **kw)
        except Unreachable:
            m.cost[self.name] += m.timeout               # the price of learning that nobody is there
            raise
        m.cost[self.name] += m.latency
        if isinstance(out, (bytes, bytearray)):
            m.bytes[self.name] += len(out)
        return out

    def get(self, *a):
        return self._call(self.inner.get, *a)

    def list(self, *a):
        return self._call(self.inner.list, *a)

    def put(self, *a, **kw):
        return self._call(self.inner.put, *a, **kw)


class Meter:
    def __init__(self, latency: float = 0.02, timeout: float = 2.0):
        self.latency, self.timeout = latency, timeout
        self.reset()

    def reset(self) -> None:
        self.calls: dict[str, int] = defaultdict(int)
        self.cost: dict[str, float] = defaultdict(float)
        self.bytes: dict[str, int] = defaultdict(int)

    def wrap(self, c: Cluster) -> Cluster:
        return Cluster(c.name, _Metered(self, c.name, c.vars), _Metered(self, c.name, c.objects), c.reaches,
                       c.is_domain_cluster)

    def wrap_all(self, fed: Federation) -> Federation:
        out = Federation()
        for c in fed.clusters.values():
            out.add(self.wrap(c))
        return out

    @property
    def total_calls(self) -> int:
        return sum(self.calls.values())

    @property
    def total_bytes(self) -> int:
        return sum(self.bytes.values())

    # The members' costs spread over `lanes` readers, longest first onto the least loaded — how a pool of
    # threads spends them. One lane is the sum: the pass as Lesson 3 wrote it.
    def pass_time(self, lanes: int = 1) -> float:
        load = [0.0] * max(1, lanes)
        for c in sorted(self.cost.values(), reverse=True):
            load[load.index(min(load))] += c
        return max(load)
