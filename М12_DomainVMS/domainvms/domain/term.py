"""Lesson 15 — a domain cluster of one node.

A site of cameras and no server. The domain still has to run somewhere — the signer, the console, the
kept edits, the shared settings — and this module decided long ago where: in ONE designated cluster, with
nothing to fail over to, its key backed up beyond it (Lessons 4 and 7). On a server room that was a drill
done once a year. On a camera it is a Tuesday: cameras are unplugged, their flash wears out, they are
stolen. So re-hosting the domain has to become an ordinary operation, and three things follow.

    a term          the host holds a number, and every re-host takes a larger one. A host that comes back
                    after being replaced sees a larger term on any member and steps down: its writes are
                    refused, and it says to whom they go. The epoch of Lesson 1 of М10A, one level up —
                    and, as there, the loser finds out on its next read, not from a message
    beyond itself   what the domain holds that nobody else does is published to other members, signed, as
                    the shared settings are (Lesson 12). Much of its state already lives on members — each
                    cluster's grants in that cluster — but not all of it: the edit kept for a camera that is
                    OFF (Lesson 9) is, by definition, on no camera that could carry it home. A domain whose
                    host dies takes those with it unless it published them
    stranded        an old host that returns may have made changes after its last backup. They are not
                    silently lost and not silently applied: they are listed, for a person, as "not in term
                    N+1 — apply again?"

    domain/host                 in the host's Variables, and carried to every member: {term, host}, signed
    domain/backup/<member>      in the host's Variables: a pointer, for each member chosen to keep a copy
    backup/rev-<n>              in the host's durable objects: the signed state
    domain/backup               in a chosen member: its agent's copy of the pointer, and of the document

What is NOT in the backup: the signer's key. It is the one thing that must never sit beside the rest, and it
is restored from where Lesson 7 put it — offline, or in the recovery file the installer handed over. Without
it no backup verifies and no member follows the new host, which is the point.
"""
from __future__ import annotations

import json
import time

from .agent import DomainPublisher
from .federation import Unreachable
from .shared import NotTaken, sign, verify
from .signer import Signer

HOST, BACKUP = "domain/host", "domain/backup"
EXPORTED = ("domain/pending/", "domain/grants/", "domain/crossings", "domain/sources/", "domain/mirrors/", "domain/shared")


class Deposed(Exception):
    pass


class TwoHosts(Exception):
    pass


def read_host(vars_, keys, now: float) -> dict | None:
    items, _ = vars_.get(HOST)
    if not items:
        return None
    try:
        return verify(json.loads(items["doc"]), keys, now)
    except (NotTaken, ValueError, KeyError):
        return None


class DomainHost:
    """The domain's services on one member, holding a term."""

    def __init__(self, fed, name: str, signer: Signer, term: int, wall=time.time):
        self.fed, self.name, self.signer, self.term, self.wall = fed, name, signer, term, wall
        self.backup_rev = 0
        self.deposed_by: dict | None = None

    @property
    def vars(self):
        return self.fed.clusters[self.name].vars

    def claim(self) -> None:
        doc = sign({"term": self.term, "host": self.name, "at": self.wall()}, self.signer.tokens)
        _, idx = self.vars.get(HOST)
        self.vars.put(HOST, {"doc": json.dumps(doc, sort_keys=True)}, cas=idx)

    def export(self) -> dict[str, dict]:
        out = {}
        for prefix in EXPORTED:
            for path in self.vars.list(prefix):
                items, _ = self.vars.get(path)
                if items is not None:
                    out[path] = items
        return out

    # Publish the state beyond the host: one signed document in the host's durable store, and a pointer for
    # each member chosen to keep it, which that member's agent carries home as it carries the settings.
    def backup(self, targets: list[str], objects) -> int:
        self.guard()
        self.backup_rev += 1
        doc = sign({"term": self.term, "rev": self.backup_rev, "host": self.name, "at": self.wall(),
                    "state": self.export()}, self.signer.tokens)
        raw = json.dumps(doc, sort_keys=True, ensure_ascii=False).encode()
        key = f"backup/rev-{self.backup_rev}"
        objects.put(key, raw)
        import hashlib
        for t in targets:
            path = f"{BACKUP}/{t}"
            _, idx = self.vars.get(path)
            self.vars.put(path, {"object": key, "rev": self.backup_rev, "term": self.term,
                                 "sha256": hashlib.sha256(raw).hexdigest()}, cas=idx)
        return self.backup_rev

    # Is this host still the host? Any member that carries a larger term says no. Read, not told: the
    # loser finds out on its next look, exactly as a fenced worker does.
    def check(self) -> bool:
        keys = self.signer.tokens.keyset()
        for name, c in self.fed.clusters.items():
            if name == self.name:
                continue
            try:
                rec = read_host(c.vars, keys, self.wall())
            except Unreachable:
                continue
            if rec and int(rec["term"]) > self.term:
                self.deposed_by = rec
                return False
        return True

    def guard(self) -> None:
        if self.deposed_by:
            raise Deposed(f"{self.name} held the domain at term {self.term}; {self.deposed_by['host']} holds it "
                          f"at term {self.deposed_by['term']} — edits go there")


# The agent's side: the host record is carried like the keys, with one rule the keys never needed — it
# never goes BACKWARDS. An agent still pointed at an old host that came back would otherwise carry that
# host's smaller term over the larger one, and undo the re-host on every member it reached.
def carry_host(domain_vars, member_vars, keys, now: float) -> str:
    items, _ = domain_vars.get(HOST)
    if not items:
        return "no host record"
    try:
        incoming = verify(json.loads(items["doc"]), keys, now)
    except (NotTaken, ValueError, KeyError) as e:
        return f"refused: {e}"
    have = read_host(member_vars, keys, now)
    if have and int(have["term"]) >= int(incoming["term"]):
        return "holding" if int(have["term"]) == int(incoming["term"]) else "holding a larger term"
    _, idx = member_vars.get(HOST)
    member_vars.put(HOST, dict(items), cas=idx)
    return f"took term {incoming['term']}"


# Which member hosts the domain, as a member works it out: the largest term it can verify — its own carried
# record, or a claim a reachable member makes about ITSELF. A host that is off is still the host if nobody
# holds a larger term; the agent then waits, it does not wander to a smaller one.
def find_host(fed, member_vars, keys, now: float) -> str | None:
    best = read_host(member_vars, keys, now)
    for name, c in fed.clusters.items():
        try:
            rec = read_host(c.vars, keys, now)
        except Unreachable:
            continue
        if not rec or rec["host"] != name:
            continue                                     # a member's CARRIED record is hearsay; only the host's own claim counts here
        if best is None or int(rec["term"]) > int(best["term"]):
            best = rec
        elif int(rec["term"]) == int(best["term"]) and rec["host"] != best["host"]:
            raise TwoHosts(f"{rec['host']} and {best['host']} both claim term {rec['term']}: a re-host was done twice")
    return best["host"] if best else None


def rehost(fed, new: str, signer_backup: bytes, domain_id: str, objects_of, wall=time.time) -> tuple[DomainHost, dict]:
    """Re-host the domain on `new` from the signer's backup and the newest verified state any reachable
    member holds. `objects_of(member)` is that member's durable store."""
    now = wall()
    new_vars = fed.clusters[new].vars
    signer = Signer.restore(domain_id, new_vars, signer_backup, now=wall)
    keys = signer.tokens.keyset()
    top_term, best, ignored = 0, None, []
    for name, c in fed.clusters.items():
        try:
            rec = read_host(c.vars, keys, now)
            ptr, _ = c.vars.get(BACKUP)
            raw = objects_of(name).get(BACKUP) if ptr else None
        except Unreachable:
            continue
        if rec:
            top_term = max(top_term, int(rec["term"]))
        if raw is None:
            continue
        try:
            doc = verify(json.loads(raw), keys, now)
        except (NotTaken, ValueError) as e:
            ignored.append((name, str(e)))
            continue
        if best is None or (int(doc["term"]), int(doc["rev"])) > (int(best[1]["term"]), int(best[1]["rev"])):
            best = (name, doc)
    if best:
        for path, items in best[1]["state"].items():
            _, idx = new_vars.get(path)
            new_vars.put(path, items, cas=idx)
    DomainPublisher(new_vars).publish_keys(keys)
    for name, c in fed.clusters.items():
        c.is_domain_cluster = name == new
    host = DomainHost(fed, new, signer, top_term + 1, wall)
    host.backup_rev = int(best[1]["rev"]) if best else 0
    host.claim()
    rev = host.backup_rev
    report = {"term": host.term, "restored_from": best[0] if best else None, "rev": rev, "ignored": ignored,
              "state": best[1]["state"] if best else {},
              "sentence": (f"term {host.term} on {new}: the domain's state from backup rev {rev}, held by {best[0]}; "
                           f"anything the old host changed after rev {rev} is not here" if best else
                           f"term {host.term} on {new}: no backup could be reached — the domain starts empty but for its keys")}
    return host, report


# What an old host that came back holds and the new term does not: every exported item that differs from
# the state the new host was restored from. For a person to look at — never applied by anyone on its own.
def stranded(old_vars, restored_state: dict) -> list[tuple[str, str, str]]:
    out = []
    for prefix in EXPORTED:
        for path in old_vars.list(prefix):
            items, _ = old_vars.get(path)
            theirs = restored_state.get(path, {})
            for k, v in (items or {}).items():
                if theirs.get(k) != v:
                    out.append((path, k, v))
    return out
