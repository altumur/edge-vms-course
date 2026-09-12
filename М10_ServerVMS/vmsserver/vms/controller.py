"""vmscontroller — the only writer of vms/*.

    cameras       CRUD by CAS; revision bumps on every operator edit; refuses controller-owned fields
    placement     which worker runs a camera — by capacity, stored with a reason, never derived;
                  adding a worker moves nothing; rebalance only when asked, budgeted
    assignment    vms/workers/<worker> — what each worker reads

It holds nothing. Two instances are harmless: every write is
read-modify-write by CAS, and the loser re-reads. It is never on the
recovery path: a worker restarts from its assignment without asking.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from vmsplatform.contract import Controller, Subsystem
from vmsplatform.objects import ObjectStore
from vmsplatform.variables import Variables

from .config import FORBIDDEN_FIELDS, OPERATOR_FIELDS, items, row

VMS = Subsystem("vms")


class Refused(Exception):
    pass


@dataclass
class Placement:
    camera: int
    worker: str
    reason: str
    at: float
    rev: int


class VmsController(Controller):
    def __init__(self, vars_: Variables, objects: ObjectStore, capacity: int = 50, wall=time.time):
        super().__init__(VMS, vars_, objects, wall)
        self.capacity = capacity          # the FALLBACK for a worker whose heartbeat carries no capacity;
                                          # the number itself is the worker's: B + n·I measured on ITS server

    def capacity_of(self, worker: str) -> int:
        """What the worker said it can carry, from its last heartbeat. The
        controller does not know the servers; it reads what the workers report."""
        hb = self.workers_seen(max_age=1e12).get(worker)
        return int(hb.extra["capacity"]) if hb and "capacity" in hb.extra else self.capacity

    # -- cameras ----------------------------------------------------------------
    def _refuse(self, fields: dict) -> None:
        bad = [k for k in fields if k in FORBIDDEN_FIELDS]
        if bad:
            raise Refused(f"a client may not set {bad}: placement is decided and stored by the controller with a "
                          f"reason; revision, epoch and phase are not the operator's")
        unknown = [k for k in fields if k not in OPERATOR_FIELDS]
        if unknown:
            raise Refused(f"unknown field(s) {unknown}")

    def _next_id(self) -> int:
        new = self.write(VMS.config("next_id"), lambda it: {"n": int(it.get("n", 0)) + 1})
        return int(new["n"])

    def create_camera(self, fields: dict) -> dict:
        self._refuse(fields)
        if not fields.get("source"):
            raise Refused("a camera needs a source (driverpack://file/<name> or driverpack://<vendor>/<host>)")
        cid = self._next_id()
        r = {"id": cid, "name": fields.get("name", f"cam{cid}"), "source": fields["source"],
             "enabled": bool(fields.get("enabled", True)), "retention_days": int(fields.get("retention_days", 30)),
             "events_retention_days": int(fields.get("events_retention_days", 365)),
             "priority": int(fields.get("priority", 100)), "revision": 1,
             "labels": list(fields.get("labels", [])), "ref": str(fields.get("ref", "") or "")}
        self.vars.put(VMS.config("cameras", str(cid)), items(r), cas=0)
        self._retention(cid, r["events_retention_days"])
        return r

    def _retention(self, cid: int, days: int | None) -> None:
        """The platform retains buckets by <sub>/retention/<unit>; the VMS's
        policy for a camera's events is one more row it writes."""
        if days is None:
            self.write(VMS.config("retention", str(cid)), lambda it: {"days": 0} if it else None)
        else:
            self.write(VMS.config("retention", str(cid)), lambda it: None if it.get("days") == str(days) else {"days": days})

    def update_camera(self, cid: int, fields: dict) -> dict:
        self._refuse(fields)
        def mutate(it):
            if not it or it.get("deleted") == "true":
                raise KeyError(cid)
            r = row(it)
            for k, v in fields.items():
                r[k] = v
            r["revision"] += 1                       # the trigger from М9 Lesson 5, in the controller
            return items(r)
        r = row(self.write(VMS.config("cameras", str(cid)), mutate))
        if "events_retention_days" in fields:
            self._retention(cid, r["events_retention_days"])
        return r

    def delete_camera(self, cid: int) -> None:
        self.write(VMS.config("cameras", str(cid)), lambda it: {**it, "deleted": "true"} if it else None)
        self._retention(cid, None)                   # a deleted camera's buckets go at the next pass
        pl = self.placement(cid)
        if pl:
            self.assign_remove(pl.worker, str(cid))
            self.write(VMS.config("placement", str(cid)), lambda it: {"worker": "", "reason": "deleted", "at": self.wall(), "rev": int(it.get("rev", 0)) + 1})

    def camera(self, cid: int) -> dict | None:
        it, _ = self.vars.get(VMS.config("cameras", str(cid)))
        return row(it) if it and it.get("deleted") != "true" else None

    def cameras(self) -> list[dict]:
        out = []
        for p in self.vars.list(VMS.config("cameras") + "/"):
            it, _ = self.vars.get(p)
            if it and it.get("deleted") != "true":
                out.append(row(it))
        return sorted(out, key=lambda r: r["id"])

    # -- placement -----------------------------------------------------------------
    def placement(self, cid: int) -> Placement | None:
        it, _ = self.vars.get(VMS.config("placement", str(cid)))
        if not it or not it.get("worker"):
            return None
        return Placement(cid, it["worker"], it["reason"], float(it["at"]), int(it["rev"]))

    def load(self, worker: str) -> int:
        return len(self.assignment(worker).units)

    def place(self, cid: int, workers: list[str] | None = None) -> Placement | None:
        """Place ONE camera on the worker with the most free capacity among
        those seen heartbeating (or given). An existing placement is returned
        untouched: adding a worker moves nothing."""
        have = self.placement(cid)
        if have:
            return have
        workers = sorted(workers if workers is not None else self.workers_seen())
        best, free = None, 0
        for w in workers:
            f = self.capacity_of(w) - self.load(w)
            if f > free:
                best, free = w, f
        if best is None:
            return None                                 # "the system is full" — never "w-1 is full"
        pl = Placement(cid, best, f"most free capacity ({free}) among {len(workers)} worker(s)", self.wall(), 0)
        # the row first (CAS decides who won), then the assignment
        def mutate(it):
            if it and it.get("worker"):
                return None                             # the other instance placed it while we thought
            return {"worker": pl.worker, "reason": pl.reason, "at": pl.at, "rev": int(it.get("rev", 0)) + 1 if it else 1}
        written = self.write(VMS.config("placement", str(cid)), mutate)
        pl = Placement(cid, written["worker"], written["reason"], float(written["at"]), int(written["rev"]))
        self.assign_add(pl.worker, str(cid))
        return pl

    def ensure_placed(self, workers: list[str] | None = None) -> list[Placement]:
        out = []
        for cam in self.cameras():
            pl = self.place(cam["id"], workers)
            if pl:
                out.append(pl)
        return out

    def where(self, cid: int) -> str | None:
        pl = self.placement(cid)
        return pl.worker if pl else None

    def move(self, cid: int, to: str, reason: str) -> Placement:
        """The one two-writer operation: the destination takes the next epoch when
        it starts; the source's lease fences on renewal and it stops. Explicit,
        never automatic."""
        for w, a in self.assignments().items():                     # wherever it is listed, and not only where the row says
            if str(cid) in a.units and w != to:
                self.assign_remove(w, str(cid))
        new = self.write(VMS.config("placement", str(cid)),
                         lambda it: {"worker": to, "reason": reason, "at": self.wall(), "rev": int(it.get("rev", 0)) + 1 if it else 1})
        self.assign_add(to, str(cid))
        return Placement(cid, to, reason, float(new["at"]), int(new["rev"]))

    def redistribute(self, workers: list[str] | None = None) -> list[tuple[int, str, str]]:
        """The controller's one unasked move: a slot that was RELEASED — the
        scheduler scaled in, or an operator retired it — still lists cameras.
        Move them to the workers that are here. A slot that merely lapsed is
        not touched: that is a crash, and its process returns under the same
        name with its assignment intact."""
        moves = []
        for gone in self.released_slots():
            live = sorted(w for w in (workers if workers is not None else self.workers_seen()) if w != gone)
            for unit in sorted(self.assignment(gone).units, key=int):
                cid = int(unit)
                best = max(live, key=lambda w: self.capacity_of(w) - self.load(w), default=None)
                if best is None or self.load(best) >= self.capacity_of(best):
                    break                                   # the system is full; the camera waits, listed where it was
                self.move(cid, best, f"slot {gone} released; most free capacity ({self.capacity_of(best) - self.load(best)})")
                moves.append((cid, gone, best))
        return moves

    def headroom(self) -> int:
        """The cluster's number for the autoscaler: cameras the live workers
        could still take, from their heartbeats. The controller has no number
        of its own to add: capacity is measured where the pipelines run."""
        return sum(int(hb.extra.get("headroom", 0)) for hb in self.workers_seen().values())

    def rebalance(self, budget: int, dead_band: float = 0.10, workers: list[str] | None = None) -> list[tuple[int, str, str]]:
        workers = sorted(workers if workers is not None else self.workers_seen())
        moves = []
        for _ in range(budget):
            if len(workers) < 2:
                break
            loads = {w: self.load(w) / self.capacity_of(w) for w in workers}
            hi, lo = max(workers, key=loads.get), min(workers, key=loads.get)
            if loads[hi] - loads[lo] < dead_band:
                break
            cands = sorted(int(u) for u in self.assignment(hi).units)
            if not cands or self.load(lo) + 1 > self.capacity_of(lo):
                break
            cid = cands[0]
            self.move(cid, lo, f"rebalance from {hi} (spread {(loads[hi] - loads[lo]) * 100:.0f}%)")
            moves.append((cid, hi, lo))
        return moves

    # -- the read model, from heartbeats -----------------------------------------------
    def read_model(self, lost_after: float = 45.0) -> list[dict]:
        now = self.wall()
        rows = []
        for w, hb in self.workers_seen(max_age=1e12).items():
            age = now - hb.ts
            state = "live" if age <= lost_after else "stale"
            for s in hb.status:
                rows.append({**s, "worker": w, "server": hb.extra.get("server", "?"), "age": round(age, 1), "worker_state": state})
        return sorted(rows, key=lambda r: r["id"])
