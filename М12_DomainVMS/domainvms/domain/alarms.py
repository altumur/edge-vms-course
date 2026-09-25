"""Lesson 14 — alarms from every member.

The operator of a site of cameras wants ONE list of alarms: the door forced at gate 3, the stream gone at
the loading bay, newest first, whichever camera saw it. Inside a cluster the platform has that list already
— every resource's buckets, merged by `MergedIndex`, with `state` and `truncated` said per source (М10A,
М11). Across members there is no cluster to merge in: each camera's events are on its own card.

So the domain merges, the way the directory merges snapshots — from each member, with what it could not
reach said as part of the answer — and adds the one thing a card does not have: a second copy. A camera
that is off, or whose card has died, is exactly the camera whose last alarms matter most.

    one list        each member's alarms, fetched from its door, merged newest first, each line naming its
                    member. A member asked for at most `per_member` lines, and one that had more says so —
                    a storm on one camera must not push every other camera off the page
    a mirror        a neighbouring member keeps a copy of another's CLOSED alarm buckets. Closed, because a
                    closed bucket never changes: copying it needs no coordination, and a copy is either all
                    there or not there. Alarm lines only: a neighbour's flash is not a second card
    pulled          the copy is made by the one who keeps it, reading the other's door — the platform's rule
                    that the one who lacks takes the initiative. Nothing is written into the source
    chosen          which neighbour keeps whose copy is the domain's decision, by reachability, stored per
                    member and carried by its agent (`domain/mirrors`) — stable, so that one camera added to
                    a site of three hundred moves a handful of copies, not all of them
    honest          a member answered from its mirror says so, and says up to WHEN: the alarms of its open
                    bucket were in no copy, and "none since 14:20" must never read as "none"
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time

from w2cplatform.events import ALARM, EventLog, buckets_under, read_bucket

from .agent import MIRRORS_PATH
from .federation import Unreachable

BUCKET = 600


class Card:
    """A member's events, on its card: М10's buckets, `vms/<unit>/e<epoch>/<start>Z.events.jsonl`, and a
    `mirror/<member>/…` tree beside them for the copies this member keeps of others."""

    def __init__(self, root: str | None = None, unit: str = "1", bucket: int = BUCKET):
        self.root, self.unit, self.bucket = root or tempfile.mkdtemp(prefix="card-"), unit, bucket

    def observe(self, epoch: int, t: float, kind: str, alarm: bool = False, **fields) -> None:
        EventLog(self.root, "vms", self.unit, epoch, self.bucket).append(t, kind, cls=ALARM if alarm else "observation", **fields)

    def _alarms_in(self, root: str, since: float, until: float) -> list[dict]:
        out = []
        for b in buckets_under(root, "vms", self.unit, self.bucket):
            if b.end <= since or b.start >= until:
                continue
            out += [e for e in read_bucket(os.path.join(root, b.path))
                    if e.get("class") == ALARM and since <= float(e["t"]) < until]
        return out

    def alarms(self, since: float, until: float, limit: int) -> dict:
        evs = sorted(self._alarms_in(self.root, since, until), key=lambda e: -float(e["t"]))
        return {"events": evs[:limit], "truncated": len(evs) > limit}

    def closed_alarm_buckets(self, now: float) -> dict[str, list[dict]]:
        """{relative path: its alarm lines} for every bucket that has closed and holds an alarm."""
        out = {}
        for b in buckets_under(self.root, "vms", self.unit, self.bucket):
            if b.end > now:
                continue                                 # still being written: in no copy, by design
            lines = [e for e in read_bucket(os.path.join(self.root, b.path)) if e.get("class") == ALARM]
            if lines:
                out[b.path] = lines
        return out

    # -- the copies this member keeps of others --------------------------------------------------------
    def _mirror_root(self, of: str) -> str:
        return os.path.join(self.root, "mirror", of)

    def keep_copy(self, of: str, path: str, lines: list[dict]) -> bool:
        dst = os.path.join(self._mirror_root(of), path)
        if os.path.exists(dst):
            return False                                 # closed means immutable: what is here is all of it
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".part"
        with open(tmp, "w") as f:
            f.write("".join(json.dumps(e) + "\n" for e in lines))
        os.replace(tmp, dst)                             # all there or not there
        return True

    def mirrored(self, of: str, since: float, until: float, limit: int) -> dict:
        root = self._mirror_root(of)
        evs = sorted(self._alarms_in(root, since, until), key=lambda e: -float(e["t"])) if os.path.isdir(root) else []
        ends = [b.end for b in buckets_under(root, "vms", self.unit, self.bucket)] if os.path.isdir(root) else []
        return {"events": evs[:limit], "truncated": len(evs) > limit, "known_until": max(ends) if ends else None}


class EventDoor:
    """A member's events as the domain and its neighbours reach them: through its door, closed when it is
    off."""

    def __init__(self, member, card: Card):
        self.member, self.card = member, card

    def _open(self) -> Card:
        if not self.member.door_open:
            raise Unreachable(f"{self.member.name} did not answer")
        return self.card

    def alarms(self, since, until, limit):
        return self._open().alarms(since, until, limit)

    def closed_alarm_buckets(self, now):
        return self._open().closed_alarm_buckets(now)

    def mirrored(self, of, since, until, limit):
        return self._open().mirrored(of, since, until, limit)


def _score(a: str, b: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{a}|{b}".encode()).digest()[:8], "big")


class MirrorPlan:
    """Who keeps a copy of whom. Rendezvous hashing: each member ranks the others by a hash of the pair and
    takes the top `copies`, among the members that share a network with it when there are enough of them.
    Adding a member changes a pair only where the newcomer outranks it — about `copies` in N of them."""

    def __init__(self, domain_vars, copies: int = 1):
        self.vars, self.copies = domain_vars, copies

    def choose(self, members: dict[str, frozenset]) -> dict[str, list[str]]:
        """{member: [the members that keep its copies]}"""
        out = {}
        for a, nets in members.items():
            others = [b for b in members if b != a]
            near = [b for b in others if nets & members[b]]
            pool = near if len(near) >= self.copies else others
            out[a] = sorted(pool, key=lambda b: -_score(a, b))[: self.copies]
        return out

    def publish(self, fed) -> dict[str, list[str]]:
        members = {n: c.reaches for n, c in fed.clusters.items() if not c.is_domain_cluster}
        holders = self.choose(members)
        keeps: dict[str, dict] = {b: {} for b in members}
        for a, bs in holders.items():
            for b in bs:
                keeps[b][a] = "1"
        for b, items in keeps.items():
            path = f"{MIRRORS_PATH}/{b}"
            have, idx = self.vars.get(path)
            if have != items:
                self.vars.put(path, items, cas=idx)
        return holders

    def holders(self, of: str) -> list[str]:
        out = []
        for path in self.vars.list(f"{MIRRORS_PATH}/"):
            items, _ = self.vars.get(path)
            if items and of in items:
                out.append(path.rsplit("/", 1)[1])
        return sorted(out)


# One pass of a member's mirror job: for each member its agent says it keeps a copy of, pull the closed
# alarm buckets it does not have yet. A source that does not answer is simply not copied this pass.
def mirror_once(me: Card, my_vars, doors, now: float) -> int:
    keeps, _ = my_vars.get(MIRRORS_PATH)
    copied = 0
    for of in sorted(keeps or {}):
        try:
            closed = doors(of).closed_alarm_buckets(now)
        except Unreachable:
            continue
        for path, lines in closed.items():
            copied += me.keep_copy(of, path, lines)
    return copied


class DomainAlarms:
    """The one list. `doors(member)` is that member's `EventDoor`."""

    def __init__(self, fed, doors, plan: MirrorPlan, wall=time.time, per_member: int = 100):
        self.fed, self.doors, self.plan, self.wall, self.per_member = fed, doors, plan, wall, per_member

    def list(self, since: float, until: float | None = None) -> dict:
        until = self.wall() if until is None else until
        events, members = [], {}
        for name, c in self.fed.clusters.items():
            if c.is_domain_cluster:
                continue
            try:
                got = self.doors(name).alarms(since, until, self.per_member)
                members[name] = {"state": "ok", "truncated": got["truncated"]}
                events += [{**e, "member": name} for e in got["events"]]
                continue
            except Unreachable:
                pass
            for b in self.plan.holders(name):
                try:
                    got = self.doors(b).mirrored(name, since, until, self.per_member)
                except Unreachable:
                    continue
                members[name] = {"state": "mirror", "via": b, "known_until": got["known_until"], "truncated": got["truncated"]}
                events += [{**e, "member": name, "from_mirror_on": b} for e in got["events"]]
                break
            else:
                members[name] = {"state": "unreachable", "truncated": False}
        events.sort(key=lambda e: -float(e["t"]))
        return {"events": events, "members": members, "complete": all(m["state"] == "ok" for m in members.values()),
                "sentence": self._sentence(members)}

    @staticmethod
    def _sentence(members: dict) -> str:
        parts = []
        for name, m in sorted(members.items()):
            if m["state"] == "mirror":
                when = time.strftime("%H:%M", time.gmtime(m["known_until"])) if m["known_until"] else "never"
                parts.append(f"{name} off — its alarms from the copy on {m['via']}, known up to {when} UTC; none known since")
            elif m["state"] == "unreachable":
                parts.append(f"{name} off, and no copy of its alarms could be reached")
            if m.get("truncated"):
                parts.append(f"{name} had more alarms than one page holds; showing its newest")
        return "; ".join(parts) if parts else "every member answered"
