"""The controller as data. A subsystem gives the platform a spec — one YAML
file — and the platform runs the controller from it:

    name          the prefix: <name>/*
    unit          the rows: where they live (<name>/<rows>/<id>), how an id is made (numeric | <field>),
                  the operator's fields with types and defaults, and derived rows (a second row the
                  platform keeps beside the unit — the VMS's <name>/retention/<id> that the resource reads)
    placement     capacity and headroom as heartbeat fields; a constraint and a tie-break BY NAME from
                  the catalogue below — never an expression; the rebalance dead band
    snapshot      the fields that leave the cluster, as one object for the layer above

The catalogue is deliberately short. `labels-subset`: a unit's `labels` must
be a subset of what the worker's server reports. `most-free-capacity`: the
worker with the most capacity − load wins. A subsystem that needs another
rule registers a function under a name (`register_constraint`), which is the
same door the resource opens for its hooks — code, named, not YAML pretending
to be code.

What is NOT in a spec: anything about what a unit does. That is the worker,
and the worker is the subsystem.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from .contract import Controller, Subsystem
from .objects import ObjectStore
from .variables import Variables

PLATFORM_FIELDS = ("worker", "placement", "epoch", "revision", "observed_revision", "phase", "id")   # never the operator's


class Refused(Exception):
    pass


@dataclass
class Placement:
    unit: object                  # the unit's id: an int for numeric ids, a str otherwise
    worker: str
    reason: str
    at: float
    rev: int


@dataclass
class Field:
    name: str
    type: str = "string"          # string | int | float | bool | list
    default: object = None
    required: bool = False

    def parse(self, v):
        if v is None:
            return self.default_value()
        if self.type == "int":
            return int(v)
        if self.type == "float":
            return float(v)
        if self.type == "bool":
            return v if isinstance(v, bool) else str(v).lower() == "true"
        if self.type == "list":
            return list(v) if isinstance(v, (list, tuple)) else [x for x in str(v).split(",") if x]
        return str(v)

    def default_value(self):
        if self.default is not None:
            return self.default
        return {"int": 0, "float": 0.0, "bool": False, "list": []}.get(self.type, "")

    def to_item(self, v) -> str:
        if self.type == "bool":
            return "true" if v else "false"
        if self.type == "list":
            return ",".join(v)
        return str(v)


@dataclass
class Derived:
    row: str                      # under the subsystem's prefix, with {id}
    items: dict                   # item -> field
    on_delete: dict | None = None # what the row becomes when the unit is deleted (None: left alone)


@dataclass
class SubsystemSpec:
    name: str
    rows: str = "units"
    id: str = "numeric"           # numeric, or the field whose value is the id
    fields: dict[str, Field] = field(default_factory=dict)
    derived: list[Derived] = field(default_factory=list)
    capacity_from: str = "capacity"
    capacity_fallback: int = 50
    headroom_from: str = "headroom"
    constraint: str = "none"
    tie_break: str = "most-free-capacity"
    dead_band: float = 0.10
    snapshot: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "SubsystemSpec":
        unit, pl = d.get("unit", {}), d.get("placement", {})
        fields = {n: Field(n, f.get("type", "string"), f.get("default"), bool(f.get("required", False)))
                  for n, f in (unit.get("fields") or {}).items()}
        for f in fields.values():
            if f.default is not None:
                f.default = f.parse(f.default) if f.type != "string" else str(f.default)
        derived = [Derived(x["row"], dict(x.get("items", {})), x.get("on_delete")) for x in unit.get("derived", [])]
        cap = pl.get("capacity", {}) or {}
        return cls(name=d["name"], rows=unit.get("rows", "units"), id=str(unit.get("id", "numeric")), fields=fields,
                   derived=derived, capacity_from=cap.get("from", "capacity"), capacity_fallback=int(cap.get("fallback", 50)),
                   headroom_from=(pl.get("headroom", {}) or {}).get("from", "headroom"),
                   constraint=pl.get("constraint", "none"), tie_break=pl.get("tie_break", "most-free-capacity"),
                   dead_band=float((pl.get("rebalance", {}) or {}).get("dead_band", 0.10)),
                   snapshot=list(d.get("snapshot", []) or list(fields)))

    @classmethod
    def load(cls, path: str) -> "SubsystemSpec":
        import yaml
        with open(path) as f:
            return cls.from_dict(yaml.safe_load(f))

    @property
    def sub(self) -> Subsystem:
        return Subsystem(self.name)

    # -- who writes what: two tokens, one prefix each ------------------------------------
    def acl_console(self) -> list[str]:
        """The operator's rows: what a console (count ≥ 2, anywhere) may write — never placement."""
        out = [f"{self.name}/{self.rows}/*", f"{self.name}/next_id"]
        for d in self.derived:
            out.append(f"{self.name}/{d.row.split('/')[0]}/*")
        return out

    def acl_controller(self) -> list[str]:
        """Placement: what the controller (count = 1) may write — never a unit's row."""
        return [f"{self.name}/workers/*", f"{self.name}/placement/*", f"{self.name}/slots/*"]

    @property
    def numeric(self) -> bool:
        return self.id == "numeric"

    def parse_id(self, v):
        return int(v) if self.numeric else str(v)

    # -- rows <-> items --------------------------------------------------------------
    def row(self, items: dict) -> dict:
        r = {"id": self.parse_id(items["id"])}
        for n, f in self.fields.items():
            r[n] = f.parse(items.get(n)) if n in items else f.default_value()
        r["revision"] = int(items.get("revision", 1))
        return r

    def items(self, row: dict) -> dict:
        out = {"id": str(row["id"]), "revision": str(row.get("revision", 1))}
        for n, f in self.fields.items():
            out[n] = f.to_item(row.get(n, f.default_value()))
        return out

    def refuse(self, fields: dict) -> None:
        bad = [k for k in fields if k in PLATFORM_FIELDS]
        if bad:
            raise Refused(f"a client may not set {bad}: placement is decided and stored by the controller with a "
                          f"reason; revision, epoch and phase are not the operator's")
        unknown = [k for k in fields if k not in self.fields]
        if unknown:
            raise Refused(f"unknown field(s) {unknown}")

    def new_row(self, uid, fields: dict) -> dict:
        r = {"id": uid}
        for n, f in self.fields.items():
            if f.required and not fields.get(n):
                raise Refused(f"a {self.name} unit needs a {n}")
            v = fields.get(n)
            r[n] = f.parse(v) if v is not None else f.default_value()
            if isinstance(r[n], str) and "{id}" in r[n]:
                r[n] = r[n].replace("{id}", str(uid))
        r["revision"] = 1
        return r


# -- the catalogue --------------------------------------------------------------------
def _labels_subset(row: dict, worker_labels: set[str]) -> bool:
    return set(row.get("labels") or []) <= worker_labels


CONSTRAINTS = {"none": lambda row, labels: True, "labels-subset": _labels_subset}


def register_constraint(name: str, fn) -> None:
    """A subsystem's own rule, as code under a name — never as YAML."""
    CONSTRAINTS[name] = fn


def _unit_key(u: str):
    return (0, int(u)) if u.isdigit() else (1, u)


class SpecController(Controller):
    """The only writer of <name>/*, from a spec. Holds nothing; two instances
    are harmless; never on the recovery path. The VMS is one spec; a counter,
    a detector, are others — same code."""

    def __init__(self, spec: SubsystemSpec, vars_: Variables, objects: ObjectStore, capacity: int | None = None,
                 wall=time.time, cluster: str | None = None):
        super().__init__(spec.sub, vars_, objects, wall)
        self.spec = spec
        self.capacity = capacity if capacity is not None else spec.capacity_fallback   # the FALLBACK for a worker whose heartbeat says nothing
        self.cluster = cluster or os.environ.get("CLUSTER", "cluster-a")               # the name the snapshot carries; one box is a cluster of one

    def _row_key(self, uid) -> str:
        return self.sub.config(self.spec.rows, str(uid))

    # -- what the workers say --------------------------------------------------------
    def capacity_of(self, worker: str) -> int:
        hb = self.workers_seen(max_age=1e12).get(worker)
        return int(hb.extra[self.spec.capacity_from]) if hb and self.spec.capacity_from in hb.extra else self.capacity

    def labels_of(self, worker: str) -> set[str]:
        hb = self.workers_seen(max_age=1e12).get(worker)
        return set(l for l in hb.extra.get("labels", "").split(",") if l) if hb else set()

    def server_of(self, worker: str) -> str:
        hb = self.workers_seen(max_age=1e12).get(worker)
        return hb.extra.get("server", "?") if hb else "?"

    def headroom(self) -> int:
        return sum(int(hb.extra.get(self.spec.headroom_from, 0)) for hb in self.workers_seen().values())

    # -- units ------------------------------------------------------------------------
    def _next_id(self):
        if self.spec.numeric:
            new = self.write(self.sub.config("next_id"), lambda it: {"n": int(it.get("n", 0)) + 1})
            return int(new["n"])
        raise Refused(f"a {self.spec.name} unit is named by its {self.spec.id}")

    def _derived(self, row: dict | None, uid, deleted: bool = False) -> None:
        for d in self.spec.derived:
            path = self.sub.config(*d.row.replace("{id}", str(uid)).split("/"))
            if deleted:
                if d.on_delete is not None:
                    self.write(path, lambda it, v=d.on_delete: {k: str(x) for k, x in v.items()} if it else None)
                continue
            want = {k: self.spec.fields[f].to_item(row[f]) for k, f in d.items.items()}
            self.write(path, lambda it, want=want: None if it == want else want)

    def create(self, fields: dict) -> dict:
        self.spec.refuse(fields)
        if self.spec.numeric:
            uid = self._next_id()
        else:
            uid = str(fields.get(self.spec.id) or "")
            if not uid:
                raise Refused(f"a {self.spec.name} unit needs a {self.spec.id}")
            if self.vars.get(self._row_key(uid))[0]:
                raise Refused(f"{self.spec.name} unit {uid} exists")
        r = self.spec.new_row(uid, fields)
        self.vars.put(self._row_key(uid), self.spec.items(r), cas=0)
        self._derived(r, uid)
        return r

    def update(self, uid, fields: dict) -> dict:
        self.spec.refuse(fields)
        def mutate(it):
            if not it or it.get("deleted") == "true":
                raise KeyError(uid)
            r = self.spec.row(it)
            for k, v in fields.items():
                r[k] = self.spec.fields[k].parse(v)
            r["revision"] += 1                       # the trigger from М9 Lesson 5, in the controller
            return self.spec.items(r)
        r = self.spec.row(self.write(self._row_key(uid), mutate))
        if any(f in fields for d in self.spec.derived for f in d.items.values()):
            self._derived(r, uid)
        return r

    def delete(self, uid) -> None:
        """The operator's half: the row is marked. Its placement is the controller's
        half, taken back on the next pass (`unplace_deleted`) — a console's token
        cannot touch an assignment, and does not need to."""
        self.write(self._row_key(uid), lambda it: {**it, "deleted": "true"} if it else None)
        self._derived(None, uid, deleted=True)

    def unplace_deleted(self) -> list:
        """The controller's half of a delete: every placement whose unit is gone
        loses its assignment and its row says so. Runs first in every pass."""
        gone = []
        for p in self.vars.list(self.sub.config("placement") + "/"):
            uid = self.spec.parse_id(p.rsplit("/", 1)[1])
            it, _ = self.vars.get(p)
            if not it or not it.get("worker") or self.unit(uid) is not None:
                continue
            self.assign_remove(it["worker"], str(uid))
            self.write(p, lambda it: {"worker": "", "reason": "deleted", "at": self.wall(), "rev": int(it.get("rev", 0)) + 1})
            gone.append(uid)
        return gone

    def unit(self, uid) -> dict | None:
        it, _ = self.vars.get(self._row_key(uid))
        return self.spec.row(it) if it and it.get("deleted") != "true" else None

    def units(self) -> list[dict]:
        out = []
        for p in self.vars.list(self.sub.config(self.spec.rows) + "/"):
            it, _ = self.vars.get(p)
            if it and it.get("deleted") != "true":
                out.append(self.spec.row(it))
        return sorted(out, key=lambda r: _unit_key(str(r["id"])))

    # -- placement ---------------------------------------------------------------------
    def placement(self, uid) -> Placement | None:
        it, _ = self.vars.get(self.sub.config("placement", str(uid)))
        if not it or not it.get("worker"):
            return None
        return Placement(self.spec.parse_id(uid), it["worker"], it["reason"], float(it["at"]), int(it["rev"]))

    def load(self, worker: str) -> int:
        return len(self.assignment(worker).units)

    def eligible(self, row: dict, workers: list[str]) -> list[str]:
        rule = CONSTRAINTS[self.spec.constraint]
        return [w for w in workers if rule(row, self.labels_of(w))]

    def _pool(self, workers):
        return sorted(workers if workers is not None else self.workers_seen())

    def _best(self, pool: list[str]) -> tuple[str | None, int]:
        best, free = None, 0
        for w in pool:                                        # most-free-capacity: the one tie-break in the catalogue
            f = self.capacity_of(w) - self.load(w)
            if f > free:
                best, free = w, f
        return best, free

    def place(self, uid, workers: list[str] | None = None) -> Placement | None:
        """Place ONE unit on the worker with the most free capacity among those
        seen heartbeating (or given) that satisfy the constraint. An existing
        placement is returned untouched: adding a worker moves nothing."""
        have = self.placement(uid)
        if have:
            return have
        row = self.unit(uid)
        if row is None:
            return None
        pool = self.eligible(row, self._pool(workers))
        best, free = self._best(pool)
        if best is None:
            return None                                 # "the system is full" — or nothing that can reach it; never "w-1 is full"
        reason = f"most free capacity ({free}) among {len(pool)} worker(s)"
        if self.spec.constraint == "labels-subset" and row.get("labels"):
            reason += f" reaching {','.join(sorted(row['labels']))}"
        reason += f"; on {self.server_of(best)}"
        pl = Placement(self.spec.parse_id(uid), best, reason, self.wall(), 0)
        # the row first (CAS decides who won), then the assignment
        def mutate(it):
            if it and it.get("worker"):
                return None                             # the other instance placed it while we thought
            return {"worker": pl.worker, "reason": pl.reason, "at": pl.at, "rev": int(it.get("rev", 0)) + 1 if it else 1}
        written = self.write(self.sub.config("placement", str(uid)), mutate)
        pl = Placement(pl.unit, written["worker"], written["reason"], float(written["at"]), int(written["rev"]))
        self.assign_add(pl.worker, str(uid))
        return pl

    def ensure_placed(self, workers: list[str] | None = None) -> list[Placement]:
        self.unplace_deleted()
        out = []
        for r in self.units():
            pl = self.place(r["id"], workers)
            if pl:
                out.append(pl)
        return out

    def unplaceable(self) -> list[dict]:
        """Units nothing live can serve — the console's honest answer, with the labels named."""
        live = self._pool(None)
        return [{"id": r["id"], "labels": r.get("labels", []), "workers_live": len(live)}
                for r in self.units() if self.placement(r["id"]) is None and not self.eligible(r, live)]

    def where(self, uid) -> str | None:
        pl = self.placement(uid)
        return pl.worker if pl else None

    def move(self, uid, to: str, reason: str) -> Placement:
        """The one two-writer operation: the destination takes the next epoch when
        it starts; the source's lease fences on renewal and it stops. Explicit,
        never automatic."""
        for w, a in self.assignments().items():                     # wherever it is listed, and not only where the row says
            if str(uid) in a.units and w != to:
                self.assign_remove(w, str(uid))
        new = self.write(self.sub.config("placement", str(uid)),
                         lambda it: {"worker": to, "reason": reason, "at": self.wall(), "rev": int(it.get("rev", 0)) + 1 if it else 1})
        self.assign_add(to, str(uid))
        return Placement(self.spec.parse_id(uid), to, reason, float(new["at"]), int(new["rev"]))

    def redistribute(self, workers: list[str] | None = None) -> list[tuple]:
        """The controller's one unasked move: a slot that was RELEASED — the
        scheduler scaled in, or an operator retired it — still lists units. Move
        them to the workers that are here. A slot that merely lapsed is not
        touched: that is a crash, and its process returns under the same name."""
        self.unplace_deleted()
        moves = []
        for gone in self.released_slots():
            live = [w for w in self._pool(workers) if w != gone]
            for unit in sorted(self.assignment(gone).units, key=_unit_key):
                uid = self.spec.parse_id(unit)
                best = max(live, key=lambda w: self.capacity_of(w) - self.load(w), default=None)
                if best is None or self.load(best) >= self.capacity_of(best):
                    break                                   # the system is full; the unit waits, listed where it was
                self.move(uid, best, f"slot {gone} released; most free capacity ({self.capacity_of(best) - self.load(best)})")
                moves.append((uid, gone, best))
        return moves

    def rebalance(self, budget: int, dead_band: float | None = None, workers: list[str] | None = None) -> list[tuple]:
        dead_band = self.spec.dead_band if dead_band is None else dead_band
        workers = self._pool(workers)
        moves = []
        for _ in range(budget):
            if len(workers) < 2:
                break
            loads = {w: self.load(w) / self.capacity_of(w) for w in workers}
            hi, lo = max(workers, key=loads.get), min(workers, key=loads.get)
            if loads[hi] - loads[lo] < dead_band:
                break
            cands = sorted(self.assignment(hi).units, key=_unit_key)
            if not cands or self.load(lo) + 1 > self.capacity_of(lo):
                break
            uid = self.spec.parse_id(cands[0])
            self.move(uid, lo, f"rebalance from {hi} (spread {(loads[hi] - loads[lo]) * 100:.0f}%)")
            moves.append((uid, hi, lo))
        return moves

    # -- what the console and the layer above read -------------------------------------------
    def read_model(self, lost_after: float = 45.0) -> list[dict]:
        now = self.wall()
        rows = []
        for w, hb in self.workers_seen(max_age=1e12).items():
            age = now - hb.ts
            state = "live" if age <= lost_after else "stale"
            for s in hb.status:
                rows.append({**s, "worker": w, "server": hb.extra.get("server", "?"), "age": round(age, 1), "worker_state": state})
        return sorted(rows, key=lambda r: _unit_key(str(r["id"])))

    def snapshot(self) -> dict:
        """Units and placement as one object: what the layer above reads. A copy
        with an age — never the rows themselves, which do not leave raft."""
        keep = ["id"] + [f for f in self.spec.snapshot if f != "id"] + ["revision"]
        units = []
        for r in self.units():
            w = self.where(r["id"])
            units.append({**{k: r[k] for k in keep if k in r}, "worker": w, "server": self.server_of(w or "")})
        return {"cluster": self.cluster, "ts": self.wall(), self.spec.rows: units}

    def publish_snapshot(self) -> None:
        import json
        self.objects.put(self.sub.config("snapshot"), json.dumps(self.snapshot()).encode())

    def failover_seconds(self) -> dict[str, float]:
        """Per worker: the gap between the heartbeat before its current instance
        started and that instance's first — measured from what the workers wrote."""
        out = {}
        for w, hb in self.workers_seen(max_age=1e12).items():
            started = float(hb.extra.get("started", hb.ts))
            prev = float(hb.extra.get("previous_hb", 0) or 0)
            if prev:
                out[w] = round(started - prev, 1)
        return out
