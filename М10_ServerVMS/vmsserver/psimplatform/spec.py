"""The controller as data. A subsystem gives the platform a spec — one YAML
file — and the platform runs the controller from it:

    name          the prefix: <name>/*
    unit          the rows: where they live (<name>/<rows>/<id>), how an id is made (numeric | <field>),
                  the operator's fields with types and defaults, and derived rows (a second row the
                  platform keeps beside the unit — the VMS's <name>/retention/<id> that the resource reads)
    placement     capacity and headroom as heartbeat fields; a constraint and a tie-break BY NAME from
                  the catalogue below — never an expression; `requires: resource` when a worker must
                  run where a resource answers (not placed on, moved off, while it is silent); the
                  rebalance dead band
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
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # spec.py — the controller as data: SubsystemSpec from `<sub>.subsystem.yaml`, and SpecController, the one
# controller every subsystem runs
#
# **Role in the module.** Lesson 5. A subsystem gives the platform one YAML file and the platform runs its
# controller from it. The spec says: `name` (the prefix `<name>/*`); `unit` (where rows live —
# `<name>/<rows>/<id>` — how an id is made — `numeric` or a field name — the operator's fields with types
# and defaults, and derived rows kept beside the unit, such as the VMS's `vms/retention/<id>` that the
# resource reads); `placement` (which heartbeat fields carry capacity and headroom, a constraint and a
# tie-break chosen *by name* from a short catalogue — never an expression — `requires: resource` when the
# worker's server must have a resource that answers, and the rebalance dead band);
# `snapshot` (the fields that leave the cluster); `console` (the name of the running gauge). What is not in
# a spec: anything about what a unit does — that is the worker, and the worker is the subsystem.
# `SpecController` extends `contract.Controller` and adds units, placement, redistribution, rebalance, the
# read model and the snapshot. `vms/controller.py` is this class with the VMS's spec and VMS names for the
# methods; `vms/config.py` exposes `row()`/`items()`; `console.py` runs over the same spec.
# `tests/test_lesson7_live.py` and `tests/test_lesson8_det.py` run two more subsystems through it from their own YAML.
#
# ## Module-level names
# - `PLATFORM_FIELDS = ("worker", "placement", "epoch", "revision", "observed_revision", "phase", "id")` —
#   never the operator's. `SubsystemSpec.refuse` rejects any of them in a create/update body: placement is
#   decided and stored by the controller with a reason; revision, epoch and phase are not the operator's.
# - `SpecController.POLICY_DEFAULTS` / `POLICY_CHOICES` — the administrator's knobs, one row `<name>/policy`
#   written by the console (`acl_console` includes it) and read by the controller on every pass: `servers`
#   is `shared` (default: every worker carries units, two on one server included — a box is that; a dead
#   server's slot is the scheduler's to reschedule onto a neighbour) or `distinct` (one worker per server
#   carries units, `idle_by_policy` names the rest; a server whose worker and resource are both silent is
#   gone, `gone_servers`, and its units move). The jobspec says `spread`, so both are possible without
#   touching Nomad.
# - `CONSTRAINTS` — the catalogue: `"none"` (always eligible) and `"labels-subset"` (`_labels_subset`).
#   `requires: resource` is not a constraint on the unit but on the worker's server: `resource_state` reads
#   `platform/resources/<server>/heartbeat` — `live`, `silent`, or `unknown` (never seen) — and `_pool`
#   drops workers whose resource is silent; `redistribute` moves their units off with the reason
#   `resource on <server> silent`. Nomad's `meta.archive` puts a worker where disks are declared; this is
#   whether the resource there still answers. `unknown` passes: silent is a fact, unknown is not one.
#   Extended only by `register_constraint`.
#
# ## Notes
# - Every write is `Controller.write` (CAS loop) or a create-only `put(cas=0)`; nothing is cached, so the
#   process can be killed anywhere.
# - The order inside `place` — placement row, then assignment — is what makes two instances agree: the row
#   is the lock.
# - `capacity_of`/`labels_of`/`server_of` call `workers_seen(max_age=1e12)` each time, i.e. one object-store
#   listing per call; correctness over speed, fine on one box.
# ================================================================================================
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from .contract import Controller, Subsystem, slot_number
from .objects import ObjectStore
from .variables import Variables

PLATFORM_FIELDS = ("worker", "placement", "epoch", "revision", "observed_revision", "phase", "id")   # never the operator's


# A write the spec does not allow: a platform field, an unknown field, a missing required field, an id for a
# numeric-id subsystem, a duplicate id. The console turns it into HTTP 400.
class Refused(Exception):
    pass


# A stored placement decision: `unit` (int for numeric ids, str otherwise), `worker`, `reason` (a sentence
# naming the free capacity, the labels reached and the server), `at` (wall time), `rev`. Lives at
# `<name>/placement/<id>`.
@dataclass
class Placement:
    unit: object                  # the unit's id: an int for numeric ids, a str otherwise
    worker: str
    reason: str
    at: float
    rev: int


# One operator field from the spec: `name`, `type` (`string | int | float | bool | list`), `default`,
# `required`.
@dataclass
class Field:
    name: str
    type: str = "string"          # string | int | float | bool | list
    default: object = None
    required: bool = False

    # Convert an item string or JSON value to the typed value; `None` gives the default. Bools accept a real
    # bool or the string `"true"`; lists accept a list or a comma-separated string.
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

    # The declared default, else the type's zero (`0`, `0.0`, `False`, `[]`, `""`).
    def default_value(self):
        if self.default is not None:
            return self.default
        return {"int": 0, "float": 0.0, "bool": False, "list": []}.get(self.type, "")

    # The Variables form: bools as `"true"/"false"`, lists comma-joined, else `str`.
    def to_item(self, v) -> str:
        if self.type == "bool":
            return "true" if v else "false"
        if self.type == "list":
            return ",".join(v)
        return str(v)


# A second row the platform keeps beside the unit: `row` (a path template under the prefix with `{id}`, e.g.
# `retention/{id}`), `items` (`{item name: field name}` — which unit field feeds each item), `on_delete`
# (what the row becomes when the unit is deleted; `None` leaves it alone). The VMS's derived row makes
# `events_retention_days` visible to the resource as `vms/retention/<id> {days}` and sets `{days: 0}` on
# delete so the buckets go at the next pass.
@dataclass
class Derived:
    row: str                      # under the subsystem's prefix, with {id}
    items: dict                   # item -> field
    on_delete: dict | None = None # what the row becomes when the unit is deleted (None: left alone)


# The parsed YAML. Fields: `name`; `rows` (`"units"`; the VMS says `cameras`); `id` (`"numeric"` or a field
# name); `fields`; `derived`; `capacity_from` / `capacity_fallback` (heartbeat key for a worker's capacity,
# and the number for a worker that said nothing); `headroom_from`; `constraint`; `tie_break` (only
# `most-free-capacity` exists); `dead_band`; `snapshot` (field names); `running_gauge` (`units_running` by
# default; `cameras_recording` for the VMS).
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
    requires: str = "none"        # "resource": a worker is eligible only while its server's resource is not silent
    tie_break: str = "most-free-capacity"
    dead_band: float = 0.10
    snapshot: list[str] = field(default_factory=list)
    running_gauge: str = "units_running"     # the console's gauge for units in phase "running" (console: {running: …})

    # Builds the spec from the YAML dict, tolerating absent sections. Field defaults are parsed to their
    # type once here (strings kept as strings so `"cam{id}"` survives). `snapshot` defaults to every field.
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
                   constraint=pl.get("constraint", "none"), requires=str(pl.get("requires", "none")), tie_break=pl.get("tie_break", "most-free-capacity"),
                   dead_band=float((pl.get("rebalance", {}) or {}).get("dead_band", 0.10)),
                   snapshot=list(d.get("snapshot", []) or list(fields)),
                   running_gauge=str((d.get("console", {}) or {}).get("running", "units_running")))

    # `yaml.safe_load` then `from_dict`. PyYAML is imported lazily so the rest of the platform has no
    # dependency on it.
    @classmethod
    def load(cls, path: str) -> "SubsystemSpec":
        import yaml
        with open(path) as f:
            return cls.from_dict(yaml.safe_load(f))

    # `Subsystem(name)` — the key layout from `contract.py`.
    @property
    def sub(self) -> Subsystem:
        return Subsystem(self.name)

    # -- who writes what: two tokens, one prefix each ------------------------------------
    # The operator's rows: `<name>/<rows>/*`, `<name>/next_id`, `<name>/idem/*` (a retried POST answered the
    # same by any instance), `<name>/policy` (the administrator's knobs) and the first segment of every
    # derived row (`<name>/retention/*`). Never placement. This is the console process's token.
    def acl_console(self) -> list[str]:
        """The operator's rows: what a console (one per server, any of them) may write — never placement."""
        out = [f"{self.name}/{self.rows}/*", f"{self.name}/next_id", f"{self.name}/idem/*",   # idem: a retried POST answered the same by ANY instance
               f"{self.name}/policy"]                                                         # the administrator's knobs: servers distinct | shared
        for d in self.derived:
            out.append(f"{self.name}/{d.row.split('/')[0]}/*")
        return out

    # Placement: `<name>/workers/*`, `<name>/placement/*`, `<name>/slots/*` — never a unit's row. The
    # controller process's token (count = 1). Together the two ACLs split the old `<name>/*` so that the
    # console cannot place and the controller cannot edit; `test_the_console_over_http` proves
    # `con.place(1)` raises `Forbidden`.
    def acl_controller(self) -> list[str]:
        """Placement: what the controller (count = 1) may write — never a unit's row."""
        return [f"{self.name}/workers/*", f"{self.name}/placement/*", f"{self.name}/slots/*"]

    # Whether ids are numbers; convert a string id accordingly.
    @property
    def numeric(self) -> bool:
        return self.id == "numeric"

    def parse_id(self, v):
        return int(v) if self.numeric else str(v)

    # -- rows <-> items --------------------------------------------------------------
    # Variables items to a typed row: `id`, every spec field (default if absent), `revision` (default 1).
    def row(self, items: dict) -> dict:
        r = {"id": self.parse_id(items["id"])}
        for n, f in self.fields.items():
            r[n] = f.parse(items.get(n)) if n in items else f.default_value()
        r["revision"] = int(items.get("revision", 1))
        return r

    # The inverse, all strings.
    def items(self, row: dict) -> dict:
        out = {"id": str(row["id"]), "revision": str(row.get("revision", 1))}
        for n, f in self.fields.items():
            out[n] = f.to_item(row.get(n, f.default_value()))
        return out

    # Raise `Refused` for any `PLATFORM_FIELDS` key or any key not in the spec. Called first by `create` and
    # `update`.
    def refuse(self, fields: dict) -> None:
        bad = [k for k in fields if k in PLATFORM_FIELDS]
        if bad:
            raise Refused(f"a client may not set {bad}: placement is decided and stored by the controller with a "
                          f"reason; revision, epoch and phase are not the operator's")
        unknown = [k for k in fields if k not in self.fields]
        if unknown:
            raise Refused(f"unknown field(s) {unknown}")

    # A fresh row: each required field must be present and truthy (`"a vms unit needs a source"`), others
    # get their default; a string value containing `{id}` has it substituted (the VMS's `name: "cam{id}"`);
    # `revision` is 1.
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
# The unit's `labels` must be a subset of what the worker's server reports.
def _labels_subset(row: dict, worker_labels: set[str]) -> bool:
    return set(row.get("labels") or []) <= worker_labels


CONSTRAINTS = {"none": lambda row, labels: True, "labels-subset": _labels_subset}


# A subsystem's own rule, as code under a name — never as YAML. The same door the resource opens for its
# hooks.
def register_constraint(name: str, fn) -> None:
    """A subsystem's own rule, as code under a name — never as YAML."""
    CONSTRAINTS[name] = fn


# Sort key: numeric ids before others, numbers by value.
def _unit_key(u: str):
    return (0, int(u)) if u.isdigit() else (1, u)


# The only writer of `<name>/*`, from a spec. Holds nothing; two instances are harmless; never on the
# recovery path. The VMS is one spec; live and det are others — same code.
class SpecController(Controller):
    """The only writer of <name>/*, from a spec. Holds nothing; two instances
    are harmless; never on the recovery path. The VMS is one spec; live and
    det are others — same code."""

    # `capacity` is only the fallback for a worker whose heartbeat says nothing (defaults to the spec's).
    # `cluster` is the name the snapshot carries (`$CLUSTER`, else `cluster-a`); one box is a cluster of
    # one.
    def __init__(self, spec: SubsystemSpec, vars_: Variables, objects: ObjectStore, capacity: int | None = None,
                 wall=time.time, cluster: str | None = None):
        super().__init__(spec.sub, vars_, objects, wall)
        self.spec = spec
        self.capacity = capacity if capacity is not None else spec.capacity_fallback   # the FALLBACK for a worker whose heartbeat says nothing
        self.cluster = cluster or os.environ.get("CLUSTER", "cluster-a")               # the name the snapshot carries; one box is a cluster of one

    # `<name>/<rows>/<id>`.
    def _row_key(self, uid) -> str:
        return self.sub.config(self.spec.rows, str(uid))

    # -- what the workers say --------------------------------------------------------
    # The worker's own number from its latest heartbeat (`extra[capacity_from]`, any age), else the
    # fallback. `test_capacity_is_the_workers_word_not_the_controllers`: workers saying 2 and 6 are placed
    # by 2 and 6; an unknown worker gets 50.
    def capacity_of(self, worker: str) -> int:
        hb = self.workers_seen(max_age=1e12).get(worker)
        return int(hb.extra[self.spec.capacity_from]) if hb and self.spec.capacity_from in hb.extra else self.capacity

    # The `labels` string of its heartbeat, split on commas.
    def labels_of(self, worker: str) -> set[str]:
        hb = self.workers_seen(max_age=1e12).get(worker)
        return set(l for l in hb.extra.get("labels", "").split(",") if l) if hb else set()

    # The heartbeat's `server`, `"?"` if unknown. Goes into placement reasons and the snapshot.
    def server_of(self, worker: str) -> str:
        hb = self.workers_seen(max_age=1e12).get(worker)
        return hb.extra.get("server", "?") if hb else "?"

    # Sum of `extra[headroom_from]` over workers seen in the last 45 s — what the autoscaler reads via
    # `/metrics`. Stale until the workers heartbeat again after a placement.
    def headroom(self) -> int:
        return sum(int(hb.extra.get(self.spec.headroom_from, 0)) for hb in self.workers_seen().values())

    # -- units ------------------------------------------------------------------------
    # For numeric ids, bump `<name>/next_id {n}` by CAS and return it; otherwise `Refused` (the unit is
    # named by its field).
    def _next_id(self):
        if self.spec.numeric:
            new = self.write(self.sub.config("next_id"), lambda it: {"n": int(it.get("n", 0)) + 1})
            return int(new["n"])
        raise Refused(f"a {self.spec.name} unit is named by its {self.spec.id}")

    # Keep every derived row in step: on create/update write `{item: to_item(row[field])}` only if it
    # differs; on delete write `on_delete` if set and the row exists.
    def _derived(self, row: dict | None, uid, deleted: bool = False) -> None:
        for d in self.spec.derived:
            path = self.sub.config(*d.row.replace("{id}", str(uid)).split("/"))
            if deleted:
                if d.on_delete is not None:
                    self.write(path, lambda it, v=d.on_delete: {k: str(x) for k, x in v.items()} if it else None)
                continue
            want = {k: self.spec.fields[f].to_item(row[f]) for k, f in d.items.items()}
            self.write(path, lambda it, want=want: None if it == want else want)

    # `refuse`, choose the id (numeric: `_next_id`; else the field's value, which must be present and not
    # already exist), build the row with `new_row`, write it with `cas=0` (create-only), write derived rows,
    # return the row. Placement is not done here — the controller's pass does it; the console reports
    # `worker: None`.
    def create(self, fields: dict) -> dict:
        self.spec.refuse(fields)
        if self.spec.numeric:
            uid = self._next_id()
        else:
            uid = str(fields.get(self.spec.id) or "")
            if not uid:
                raise Refused(f"a {self.spec.name} unit needs a {self.spec.id}")
            old, idx = self.vars.get(self._row_key(uid))
            if old and old.get("deleted") != "true":
                raise Refused(f"{self.spec.name} unit {uid} exists")
            if old:                                                 # a named unit deleted earlier comes back under its name:
                r = self.spec.new_row(uid, fields)                  # a fresh row, one revision on from the old one, by CAS on it
                r["revision"] = int(old.get("revision", 0)) + 1
                self.vars.put(self._row_key(uid), self.spec.items(r), cas=idx)
                self._derived(r, uid)
                return r
        r = self.spec.new_row(uid, fields)
        self.vars.put(self._row_key(uid), self.spec.items(r), cas=0)
        self._derived(r, uid)
        return r

    # `refuse`, then read-modify-write the row: `KeyError` if missing or marked deleted; parse each field
    # into the row; bump `revision` (the trigger from М9 Lesson 5, now in the controller — the worker
    # restarts what it runs on a new revision); write. Derived rows are refreshed only if one of their
    # source fields changed.
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

    # The operator's half: the row is marked `deleted: "true"` (not removed) and derived rows get their
    # `on_delete`. Its placement is the controller's half, taken back on the next pass by `unplace_deleted`
    # — a console's token cannot touch an assignment, and does not need to.
    def delete(self, uid) -> None:
        """The operator's half: the row is marked. Its placement is the controller's
        half, taken back on the next pass (`unplace_deleted`) — a console's token
        cannot touch an assignment, and does not need to."""
        self.write(self._row_key(uid), lambda it: {**it, "deleted": "true"} if it else None)
        self._derived(None, uid, deleted=True)

    # The controller's half of a delete: for every `placement/<id>` row with a worker whose unit no longer
    # exists, remove the unit from that worker's assignment and rewrite the placement as `{worker: "",
    # reason: "deleted", at, rev+1}`. Runs first in `ensure_placed` and `redistribute`. The console test:
    # after `DELETE /cameras/1` the placement still says `w-1` until `unplace_deleted()` returns `[1]`.
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

    # The row, or `None` if absent or deleted.
    def unit(self, uid) -> dict | None:
        it, _ = self.vars.get(self._row_key(uid))
        return self.spec.row(it) if it and it.get("deleted") != "true" else None

    # Every live row under `<name>/<rows>/`, sorted by `_unit_key`.
    def units(self) -> list[dict]:
        out = []
        for p in self.vars.list(self.sub.config(self.spec.rows) + "/"):
            it, _ = self.vars.get(p)
            if it and it.get("deleted") != "true":
                out.append(self.spec.row(it))
        return sorted(out, key=lambda r: _unit_key(str(r["id"])))

    # -- placement ---------------------------------------------------------------------
    # The stored decision, `None` if no row or the worker is empty (unplaced).
    def placement(self, uid) -> Placement | None:
        it, _ = self.vars.get(self.sub.config("placement", str(uid)))
        if not it or not it.get("worker"):
            return None
        return Placement(self.spec.parse_id(uid), it["worker"], it["reason"], float(it["at"]), int(it["rev"]))

    # Assigned units on that worker — from the assignment row, not from the heartbeat.
    def load(self, worker: str) -> int:
        return len(self.assignment(worker).units)

    # Workers passing the spec's constraint against their labels.
    def eligible(self, row: dict, workers: list[str]) -> list[str]:
        rule = CONSTRAINTS[self.spec.constraint]
        return [w for w in workers if rule(row, self.labels_of(w))]

    # -- the administrator's knobs: one row, `<name>/policy`, written by the console ---------------
    # `servers`: `shared` (default) — every worker is a place to put units, two on one server included (a
    # box IS several workers on one server); a dead server's slot is Nomad's to reschedule onto a neighbour
    # (Lesson 4's power pull), and the controller waits for it. `distinct` — one worker per server carries
    # units; a second worker Nomad put on the same server idles by policy, and a server whose worker and
    # resource both fall silent is gone (`gone_servers`) — its units move. The jobspec says `spread`, so
    # both are possible without touching Nomad; the administrator chooses on the console.
    POLICY_DEFAULTS = {"servers": "shared"}
    POLICY_CHOICES = {"servers": ("distinct", "shared")}

    def policy(self) -> dict:
        items, _ = self.vars.get(self.sub.config("policy"))
        out = dict(self.POLICY_DEFAULTS)
        for k, v in (items or {}).items():
            if k in self.POLICY_CHOICES and v in self.POLICY_CHOICES[k]:
                out[k] = v
        return out

    def set_policy(self, changes: dict) -> dict:
        for k, v in changes.items():
            if k not in self.POLICY_CHOICES or v not in self.POLICY_CHOICES[k]:
                raise Refused(f"policy {k} must be one of {', '.join(self.POLICY_CHOICES.get(k, ()))}")
        self.write(self.sub.config("policy"), lambda it: {**(it or {}), **{k: str(v) for k, v in changes.items()}})
        return self.policy()

    # Under `servers: distinct`, the workers that a server's OTHER workers must yield to: for each server,
    # the worker with units (the most, ties to the first name), else the first by name; the rest idle by
    # policy. Under `shared`, nobody.
    def idle_by_policy(self, workers) -> list[str]:
        if self.policy()["servers"] != "distinct":
            return []
        by_server: dict[str, list[str]] = {}
        for w in sorted(workers, key=slot_number):
            by_server.setdefault(self.server_of(w), []).append(w)
        idle = []
        for server, ws in by_server.items():
            if server == "?" or len(ws) < 2:
                continue
            keep = max(ws, key=lambda w: (self.load(w), -slot_number(w)))
            idle += [w for w in ws if w != keep]
        return idle

    # -- what the worker's server must have: a resource, when the spec says so ---------------------
    # The state of the resource on a server, from `platform/resources/<server>/heartbeat`: `"live"` (younger
    # than `lost_after`), `"silent"` (older), `"unknown"` (never heartbeaten — a box before its resource
    # process starts, a bench). Nomad's `meta.archive` constraint puts a worker where disks are declared;
    # this is the live fact: whether the resource there still answers.
    def resource_state(self, server: str, lost_after: float = 45.0) -> str:
        from .resource import resources_seen                       # the platform's own reader of the resource heartbeats
        hb = resources_seen(self.objects).get(server)
        if hb is None:
            return "unknown"
        return "live" if self.wall() - float(hb["ts"]) <= lost_after else "silent"

    # Workers whose server's resource is silent, when the spec requires one: not placed on, and (in
    # `redistribute`) moved off. A worker on a server whose resource was never seen passes — "silent" is
    # a fact, "unknown" is not one.
    def without_resource(self, workers) -> list[str]:
        if self.spec.requires != "resource":
            return []
        return [w for w in workers if self.resource_state(self.server_of(w)) == "silent"]

    # A server that is gone, not a process that crashed: the slot has lapsed and stayed lapsed for another
    # `lost_after` — Nomad's chance to reschedule it onto a spare server, in which case the replacement
    # claims the name and inherits the assignment (Lesson 4) — AND the resource on the slot's last known
    # server is silent. One silence is a crash and is left alone; two independent silences from the same
    # server are a fact about the server. Only when the spec requires a resource.
    def gone_servers(self, lost_after: float = 45.0) -> dict[str, str]:
        """Lapsed slots whose server's resource is silent too: {slot: server}."""
        if self.spec.requires != "resource" or self.policy()["servers"] != "distinct":
            return {}                                                    # shared: a dead server's slot is Nomad's to reschedule onto a neighbour
        now = self.wall(); out = {}
        for name, slot in self.slots().items():
            if slot.lapsed(now) and now > slot.until + lost_after and self.assignment(name).units:
                server = self.server_of(name)
                if server != "?" and self.resource_state(server, lost_after) == "silent":
                    out[name] = server
        return out

    # The given list, or the workers seen heartbeating in the last 45 s; minus those whose resource is
    # silent when the spec requires one; sorted.
    def _pool(self, workers):
        pool = sorted(workers if workers is not None else self.workers_seen())
        gone = set(self.without_resource(pool)) | set(self.idle_by_policy(pool))
        return [w for w in pool if w not in gone]

    # `most-free-capacity`: the worker with the largest `capacity_of − load`, strictly positive; ties go to
    # the first in sorted order.
    def _best(self, pool: list[str]) -> tuple[str | None, int]:
        best, free = None, 0
        for w in pool:                                        # most-free-capacity: the one tie-break in the catalogue
            f = self.capacity_of(w) - self.load(w)
            if f > free:
                best, free = w, f
        return best, free

    # Place one unit. An existing placement is returned untouched — adding a worker moves nothing. A missing
    # unit is `None`. Otherwise pick `_best` among the eligible pool; `None` if nothing has free capacity
    # ("the system is full" — or nothing that can reach it; never "w-1 is full"). The reason names the free
    # capacity, the pool size, the labels reached (under `labels-subset`) and the server. Then the row first
    # (CAS decides who won: if another instance placed it meanwhile, the mutator returns `None` and the
    # other's row is used), then `assign_add` on the winner's worker. `test_two_controllers_agree_by_cas`:
    # two threads placing 40 cameras with opposite preferences end with every camera exactly once across
    # `w-1`/`w-2`.
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
        if self.spec.requires == "resource":
            reason += f", whose resource is {self.resource_state(self.server_of(best))}"
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

    # The pass: `unplace_deleted`, then `place` every unit; returns what is placed.
    # `test_placement_is_stored_with_a_reason_and_adding_a_worker_moves_nothing`: six cameras split 3/3 by
    # capacity 3; the seventh waits; a third worker arriving takes only the seventh.
    def ensure_placed(self, workers: list[str] | None = None) -> list[Placement]:
        self.unplace_deleted()
        out = []
        for r in self.units():
            pl = self.place(r["id"], workers)
            if pl:
                out.append(pl)
        return out

    # Units with no placement that no live worker's labels can serve — the console's honest answer, with the
    # labels named and the live worker count.
    def unplaceable(self) -> list[dict]:
        """Units nothing live can serve — the console's honest answer, with the labels named."""
        live = self._pool(None)
        return [{"id": r["id"], "labels": r.get("labels", []), "workers_live": len(live)}
                for r in self.units() if self.placement(r["id"]) is None and not self.eligible(r, live)]

    # The placed worker.
    def where(self, uid) -> str | None:
        pl = self.placement(uid)
        return pl.worker if pl else None

    # The one two-writer operation: remove the unit from every assignment that lists it other than `to`
    # (wherever it is listed, not only where the row says), rewrite the placement row, `assign_add` on `to`.
    # The destination takes the next epoch when it starts; the source's lease fences on renewal and it
    # stops. Explicit, never automatic.
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

    # The controller's one unasked move: for each released slot (scale-in, or `retire`) that still lists
    # units, move each to the live worker with the most free capacity; stop when the system is full (the
    # unit waits, listed where it was). A merely lapsed slot is not touched: that is a crash, and its
    # process returns under the same name. Two more cases when the spec requires a resource: a live worker
    # whose server's resource went silent (it has nowhere to write), and a slot that lapsed AND whose
    # server's resource is silent — the server is gone, and with one worker per server (`distinct_hosts`)
    # nobody will claim that slot until the server returns; its units go to the workers that are here. `test_scale_in_releases_a_slot_and_the_controller_redistributes`:
    # a silent `w-3` moves nothing; after `release_slot()` its two cameras go to `w-1`/`w-2` with reason
    # `slot w-3 released; …`.
    def redistribute(self, workers: list[str] | None = None) -> list[tuple]:
        """The controller's one unasked move: a slot that was RELEASED — the
        scheduler scaled in, or an operator retired it — still lists units. Move
        them to the workers that are here. A slot that merely lapsed is not
        touched: that is a crash, and its process returns under the same name."""
        self.unplace_deleted()
        moves = []
        seen = sorted(workers if workers is not None else self.workers_seen())
        # a released slot — and, when the spec requires a resource, a live worker whose server's resource
        # went silent: it heartbeats, but it has nowhere to write; its units go to workers that do
        gone_for = {g: f"slot {g} released" for g in self.released_slots()}
        for w in self.without_resource(seen):
            if self.assignment(w).units:
                gone_for.setdefault(w, f"resource on {self.server_of(w)} silent")
        for w, server in self.gone_servers().items():                  # the server is gone: its slot lapsed and its resource silent
            gone_for.setdefault(w, f"server {server} gone: slot {w} lapsed and its resource silent")
        for gone, why in gone_for.items():
            live = [w for w in self._pool(workers) if w != gone]
            for unit in sorted(self.assignment(gone).units, key=_unit_key):
                uid = self.spec.parse_id(unit)
                row = self.unit(uid)
                pool = self.eligible(row, live) if row else live
                best = max(pool, key=lambda w: self.capacity_of(w) - self.load(w), default=None)
                if best is None or self.load(best) >= self.capacity_of(best):
                    break                                   # the system is full; the unit waits, listed where it was
                self.move(uid, best, f"{why}; most free capacity ({self.capacity_of(best) - self.load(best)}); on {self.server_of(best)}")
                moves.append((uid, gone, best))
        return moves

    # Up to `budget` moves: each step takes the most and least loaded workers by `load/capacity`, stops if
    # their spread is under the dead band or the low one is full, and moves the lowest-numbered unit of the
    # high one. Only when asked. `test_rebalance_is_explicit_budgeted…`: budget 0 moves nothing; budget 3
    # moves three from `w-1` to `w-2`; a further budget of 5 moves one more and then stops inside the 10 %
    # band.
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
    # What the console lists: every `status` entry from every worker's latest heartbeat (any age), tagged
    # with `worker`, `server`, `age` and `worker_state` (`live` or `stale`), sorted by unit id.
    # `test_the_failure_arithmetic`: with the controller gone the read model still answers from heartbeats;
    # with the worker gone 100 s the rows say `stale`, age 100.
    def read_model(self, lost_after: float = 45.0) -> list[dict]:
        now = self.wall()
        rows = []
        for w, hb in self.workers_seen(max_age=1e12).items():
            age = now - hb.ts
            state = "live" if age <= lost_after else "stale"
            for s in hb.status:
                rows.append({**s, "worker": w, "server": hb.extra.get("server", "?"), "age": round(age, 1), "worker_state": state})
        return sorted(rows, key=lambda r: _unit_key(str(r["id"])))

    # Units and placement as one object for the layer above: `{cluster, ts, <rows>: [{id, <snapshot fields>,
    # revision, worker, server}]}`. A copy with an age — never the rows themselves, which do not leave raft.
    def snapshot(self) -> dict:
        """Units and placement as one object: what the layer above reads. A copy
        with an age — never the rows themselves, which do not leave raft."""
        keep = ["id"] + [f for f in self.spec.snapshot if f != "id"] + ["revision"]
        units = []
        for r in self.units():
            w = self.where(r["id"])
            units.append({**{k: r[k] for k in keep if k in r}, "worker": w, "server": self.server_of(w or "")})
        return {"cluster": self.cluster, "ts": self.wall(), self.spec.rows: units}

    # Writes the snapshot JSON to the object store at `<name>/snapshot`.
    def publish_snapshot(self) -> None:
        import json
        self.objects.put(self.sub.config("snapshot"), json.dumps(self.snapshot()).encode())

    # Per worker: `started − previous_hb` from the heartbeat's own fields — the gap between the last
    # heartbeat of the previous instance and this instance's start, measured from what the workers wrote,
    # not by the controller.
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
