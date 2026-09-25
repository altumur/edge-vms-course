"""Lesson 13 — a stream from another cluster.

A camera that is its own cluster still wants a server to record it: the card in it holds days, the server's
volume holds months, and the card is in the camera a thief takes with him. The server's recorder does what
it always does — pulls RTSP, writes its volume, closes gaps from the device's own archive (М10B Lessons
15–16). What changes is only where it FINDS the camera. In one cluster it found the holder's `live_url`,
`playback_url` and `coverage` in its own cluster's heartbeats. This camera's heartbeat is in the camera's
cluster, which the recorder cannot read and must not depend on.

So the domain — which reads every member — publishes a SOURCE BOOK per recording cluster: for each camera
of another cluster that this one records, where it was last seen, and when. The cluster's agent carries it
home, as it carries grants. The recorder resolves `ref:<serial>` against its own cluster's copy, so a
recording goes on with the domain switched off, from the address last carried.

    data crosses    footage and coverage go from the camera's cluster to the server's
    work does not   the recorder stays in the server cluster; the camera learns of no server; no row, no
                    epoch and no request is written into the camera's cluster by anyone but itself
    one consumer    a camera serves one live session and one backfill (М10B Lesson 15). Which cluster
                    records it is therefore a DOMAIN decision, stored, one per camera — the same shape as
                    Lesson 1's placement, and refused the same way when a second cluster asks
    a hint          what the book says about the card is as old as the book. The device is asked again
                    before a range is fetched, and a range the card no longer holds is dropped, not retried

    domain/crossings            in the domain cluster: {ref: the cluster that records it}
    domain/sources/<cluster>    in the domain cluster: that cluster's source book
    domain/sources              in the recording cluster: its agent's copy
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from cluster.variables import Conflict
from vms.archive import subtract

from .agent import SOURCES_PATH
from .api import ApiError

CROSSINGS = "domain/crossings"


class Crossings:
    """The domain's side: which cluster records which camera of another cluster, and the books."""

    def __init__(self, domain_vars, view, wall=time.time):
        self.vars, self.view, self.wall = domain_vars, view, wall

    def all(self) -> dict[str, str]:
        items, _ = self.vars.get(CROSSINGS)
        return dict(items or {})

    def record(self, ref: str, on: str) -> dict:
        ref = str(ref)
        known = self.view.last_known(ref)
        if known is None:
            raise ApiError(404, f"camera {ref} has never been seen by the domain; nothing to record")
        if known[0] == on:
            raise ApiError(400, f"camera {ref} is in {on} already: a cluster records its own cameras as it always did")
        for _ in range(10):
            items, idx = self.vars.get(CROSSINGS)
            items = dict(items or {})
            if items.get(ref) == on:
                return {"camera": ref, "recorded_by": on, "from": known[0]}
            if ref in items:
                raise ApiError(409, f"camera {ref} is recorded by {items[ref]} already: a device serves one live "
                                    f"session and one backfill, and a second recorder would take them from the first")
            items[ref] = on
            try:
                self.vars.put(CROSSINGS, items, cas=idx)
                return {"camera": ref, "recorded_by": on, "from": known[0]}
            except Conflict:
                continue
        raise ApiError(409, f"could not record {ref} on {on}: the crossings kept changing")

    # Each recording cluster's book, from the read view's memory: the doors the camera's worker last
    # published, with the time they were published. A camera whose cluster is silent keeps its last entry
    # and says so — the address it had is the best there is, and it is usually still right.
    def publish(self) -> dict[str, dict]:
        books: dict[str, dict] = {}
        for ref, on in self.all().items():
            books.setdefault(on, {})
            found = self._doors(ref)
            if found is not None:
                books[on][ref] = json.dumps(found, sort_keys=True)
        for on, book in books.items():
            path = f"{SOURCES_PATH}/{on}"
            have, idx = self.vars.get(path)
            if have != book:
                self.vars.put(path, book, cas=idx)
        return books

    def _doors(self, ref: str) -> dict | None:
        for (cluster, worker), s in self.view.snapshots.items():
            if any(str(st.get("ref", "")) == ref for st in s.status) and s.doors:
                return {"cluster": cluster, "worker": worker, **s.doors, "as_of": s.ts,
                        "reachable": cluster not in self.view.cluster_down_since}
        return None


class NotResolvable(Exception):
    pass


@dataclass
class Source:
    ref: str
    cluster: str
    live_url: str
    playback_url: str
    coverage: dict | None
    age: float
    reachable: bool


# The recorder's side, in the recording cluster: `ref:<serial>` against this cluster's own copy of the book.
# Any other source is this cluster's own and is found as it always was — in its own heartbeats.
def resolve(cluster_vars, source: str, now: float) -> Source | None:
    if not str(source).startswith("ref:"):
        return None
    ref = source[4:]
    items, _ = cluster_vars.get(SOURCES_PATH)
    if not items or ref not in items:
        raise NotResolvable(f"{ref} is not in this cluster's source book: the domain has not asked this cluster "
                            f"to record it, or has never seen it publish a door")
    e = json.loads(items[ref])
    return Source(ref, e["cluster"], e["live_url"], e["playback_url"], e.get("coverage"),
                  max(0.0, now - float(e["as_of"])), bool(e.get("reachable", True)))


# What to fetch from the card: what the book says it holds, minus what we hold, bounded as М10B's recorder
# bounds it (not older than our own volume keeps, not fresher than `settle`) — and then checked against
# what the device says NOW. The book is as old as its last carry; a card is a ring, and the oldest hour it
# listed may have been overwritten since. A range the card no longer has is dropped with its reason — a
# retry would ask the same card the same question.
def plan_backfill(src: Source, ours: list[tuple[float, float]], now: float, keep_days: float, settle: float,
                  ask_device) -> tuple[list[tuple[float, float]], list[tuple[tuple[float, float], str]]]:
    if not src.coverage:
        return [], []
    want = (max(float(src.coverage["from"]), now - keep_days * 86400), min(float(src.coverage["to"]), now - settle))
    if want[1] <= want[0]:
        return [], []
    holes = subtract(want, ours)
    if not holes:
        return [], []
    card = ask_device()                                  # {"from", "to"} — the card, now
    fetch, dropped = [], []
    for h in holes:
        lo, hi = max(h[0], float(card["from"])), min(h[1], float(card["to"]))
        if hi > lo:
            fetch.append((lo, hi))
        for gone in subtract(h, [(lo, hi)] if hi > lo else []):
            dropped.append((gone, "no longer on the card"))
    return fetch, dropped
