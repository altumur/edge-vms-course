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
# **Role in the module.** Lesson 6. A subsystem gives the platform one YAML file and the platform runs its
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
# `tests/test_lesson8_live.py` and `tests/test_lesson9_det.py` run two more subsystems through it from their own YAML.
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

from urllib.parse import urlsplit

from .secrets import is_secret_field
from .blobs import digest as blob_digest, is_digest, verify
from .contract import DRAIN_KEY, UNPLACED, Controller, Subsystem, slot_number
from .limits import TooLarge
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


# One operator field from the spec: `name`, `type` (`string | int | float | bool | list | url | blob`),
# `default`, `required`. A `blob` holds a DIGEST (`sha256-<hex>`); the bytes live in the object store under
# `<name>/blobs/<digest>` and the platform never looks inside them — see `blobs.py`.
@dataclass
class Field:
    name: str
    type: str = "string"          # string | int | float | bool | list | url | blob
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
# default; `cameras_running` for the VMS).
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
    servers: str = "shared"       # the default of the `servers` policy knob: shared | distinct (the console may change it)
    tie_break: str = "most-free-capacity"
    near: str = "none"            # a subsystem whose worker holding the same unit id this one prefers to be beside (an affinity, never a filter)
    # WHICH unit of that subsystem. `id` — the same unit id, which is what `near: <sub>` means and all the
    # VMS needed while the camera and its recording were the same string. A subsystem whose units are named
    # for something else has to say so: a detector's unit is `7-linecross`, no recorder ever reports that id,
    # and `near: rec` on it would match nothing — an affinity that reads as followed and is not. So
    # `near: {sub: rec, by: cam}` — follow the recorder holding the recording my `cam` field names.
    near_by: str = "id"
    # `near: {sub: rec, of: cam}` — WHAT OF THEIRS the value is matched against. `by` says which value of
    # MINE to look for (my id, or a field of my row); `of` says where in THEIR status to look for it (their
    # unit id by default, or a field they report). The two are independent, and the second arrived when
    # recordings stopped being named by their camera: "the recorder running a recording whose `cam` is
    # camera 7" holds whether the operator called it `7`, `7-cloud` or `gate-lobby`, while "the recorder
    # running unit `7`" was only ever true by coincidence — and silently false when the coincidence ended.
    #
    #   vms:    near: {sub: rec, of: cam}            my id (7)        vs their `cam`     — whoever records me
    #   det:    near: {sub: rec, by: cam, of: cam}   my field `cam`   vs their `cam`     — likewise, for a pair
    #   detjob: near: {sub: rec, by: rec}            my field `rec`   vs their id        — I name the recording
    #
    # The field is read from the heartbeat STATUS (`status_extra` puts it there), not from the other
    # subsystem's rows, which this controller has no business reading. Several of their units may answer —
    # two recordings of one camera — and then the affinity takes the smallest of their ids, so two passes
    # over the same heartbeats reach the same server. Being beside one of them is the point; the other
    # reads the same fan-out over the network.
    near_of: str = ""
    # `home: <field>` — the server named in that field of the unit's own row is where it prefers to run;
    # `home: near` — wherever the subsystem this one follows is. A PREFERENCE and not a label: a label is a
    # filter, and a unit whose home is down would become unplaceable — the one thing it must not be,
    # because the home being down is exactly when the work has to continue somewhere else. It is topology,
    # not taste: the recording's home is the disk it is written to. Coming home is then not a procedure but
    # a consequence, bounded by `ensure_home`.
    home: str = ""
    # `spread_by: <field>` — units sharing a value of that field go on DIFFERENT servers. Unlike `near` this
    # is a FILTER, not a preference: the whole point of a second copy is that it is not where the first one
    # is, and a second copy on the same server is not a second copy. Unplaceable while no other server
    # qualifies, and that is the honest answer — `/unplaceable` says so rather than quietly co-locating.
    spread_by: str = ""
    # `place_by: <field>` — WHAT the policy and the home are counted in: the heartbeat field that names the
    # place a worker occupies. `server` by default, and for everything whose unit of storage is a server
    # that is the truth. A recorder's is not: a box with three disks runs three recorders, one per volume,
    # and `servers: distinct` has to mean one per DISK — two recorders on one volume are no second place to
    # record, while two on one server with different disks are exactly that.
    #
    # Only the policy and `home` follow this field. Reachability (`requires: resource`), draining and
    # `spread_by` stay on the server, because those are about a machine: a volume has no address, cannot be
    # drained on its own, and two copies on two disks of one server survive nothing the operator was buying
    # insurance against.
    place_by: str = "server"
    # `retire_when: {field: state, in: [done, failed]}` — a unit whose row says one of those values is
    # FINISHED, and finished work is not placed. The first subsystem to need it is `detjob`, whose unit
    # ends; everything before it ran until an operator said stop.
    #
    # Not `enabled`. That field is read by workers, never here: a disabled camera keeps its assignment and
    # its line in the console's list, and a unit that has to STAY VISIBLE while doing nothing is a
    # different thing from one that is over. Saying which field and which values, per subsystem, is the
    # difference between the two — and it is the row that says it, not a heartbeat: un-placing a finished
    # unit makes its worker drop it, which makes the heartbeat stop mentioning it, which would erase the
    # only evidence it was ever finished. It would then be placed again, and start over, for ever.
    retire_field: str = ""
    retire_values: tuple = ()
    dead_band: float = 0.10
    snapshot: list[str] = field(default_factory=list)
    # `tables: [volumes]` — row families this subsystem's CONSOLE owns besides its units. Not units: nothing
    # is placed on them, they have no epoch and no worker; they are the administrator's lists, like `policy`
    # but plural and named. `rec` declares `volumes`, the archives an operator may write recordings into.
    #
    # The platform learns the NAME and nothing else: the ACL gains `<name>/<table>/*`, and what a row of
    # that table means, which fields it has and which routes serve it are the subsystem's, in its own
    # console code. That is the line: a generic grant is a platform matter, a volume is not.
    tables: tuple[str, ...] = ()
    running_gauge: str = "units_running"     # the console's gauge for units in phase "running" (console: {running: …})
    # `events: {older_epochs: fenced | earlier-run}` — what it MEANS that a unit's events were written
    # under an epoch that is not the current one.
    #
    # `fenced` (the default, and right for everything that runs until stopped): a writer that lost the
    # race and kept writing. The page strikes those events through, because they are a zombie's.
    #
    # `earlier-run`: the same mechanism, the opposite meaning. A unit whose work ENDS takes a new epoch
    # every time the operator runs it again, so an older epoch is a finished earlier result — a run to
    # compare against, not a loser to strike through. Marking it `fenced` would tell an operator that the
    # search they ran last week was never valid.
    older_epochs: str = "fenced"

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
        # `snapshot:` LEFT OUT means "every field that may go" — a convenience, not a decision.
        # `snapshot: []` means "no field of the row leaves the cluster", which is a decision. The two were
        # one thing here for as long as this read `list(d.get("snapshot", [])) or [every field]`: an empty
        # list is falsy, so a spec declaring "publish nothing" published everything, and its author would
        # have learned that from М12 rather than from the file they wrote.
        #
        # An empty list is not an empty object. `id`, `revision`, `worker` and `server` are structural and
        # go either way, because "which unit is where" is the snapshot's other job and the layer above is
        # built on it. What `[]` buys is that nothing an OPERATOR typed leaves the cluster.
        #
        # `snapshot:` with nothing after it is neither, so it is refused rather than guessed: the author
        # meant one of the two and the file does not say which.
        if "snapshot" in d and d["snapshot"] is None:
            raise ValueError(f"spec {d['name']}: `snapshot:` with nothing after it says neither — write "
                             f"`snapshot: []` for no fields, or leave the key out for every field")
        declared = d.get("snapshot")
        cap = pl.get("capacity", {}) or {}
        spec = cls(name=d["name"], rows=unit.get("rows", "units"), id=str(unit.get("id", "numeric")), fields=fields,
                   derived=derived, capacity_from=cap.get("from", "capacity"), capacity_fallback=int(cap.get("fallback", 50)),
                   headroom_from=(pl.get("headroom", {}) or {}).get("from", "headroom"),
                   constraint=pl.get("constraint", "none"), requires=str(pl.get("requires", "none")), servers=str(pl.get("servers", "shared")), tie_break=pl.get("tie_break", "most-free-capacity"),
                   near=str((pl.get("near") or {}).get("sub", "none") if isinstance(pl.get("near"), dict) else pl.get("near", "none")),
                   near_by=str((pl.get("near") or {}).get("by", "id") if isinstance(pl.get("near"), dict) else "id"),
                   near_of=str((pl.get("near") or {}).get("of", "") if isinstance(pl.get("near"), dict) else ""),
                   spread_by=str(pl.get("spread_by", "") or ""),
                   place_by=str(pl.get("place_by", "server") or "server"),
                   home=str(pl.get("home", "") or ""),
                   retire_field=str((pl.get("retire_when") or {}).get("field", "") or ""),
                   retire_values=tuple(str(v) for v in ((pl.get("retire_when") or {}).get("in") or [])),
                   dead_band=float((pl.get("rebalance", {}) or {}).get("dead_band", 0.10)),
                   snapshot=(list(declared) if declared is not None else
                             [n for n, f in fields.items() if not is_secret_field(n) and f.type != "blob"]),
                   tables=tuple(str(t) for t in (d.get("tables") or [])),
                   running_gauge=str((d.get("console", {}) or {}).get("running", "units_running")),
                   older_epochs=str((d.get("events", {}) or {}).get("older_epochs", "fenced")))
        # A secret in the snapshot is a secret leaving the cluster: `vms/snapshot/*` is what М12's directory
        # reads. Refused at LOAD time, not watched for at review time — and only when it is named, because
        # the default ("every field") is a convenience and not a decision.
        # A table's name becomes a key family and an ACL prefix, so it is a name and not a path, and it may
        # not be the unit rows under another spelling — two writers on one family with different rules.
        for t in spec.tables:
            if not t or "/" in t or t in (spec.rows, "policy", "slots", "holds", "epoch", "idem", "requests"):
                raise ValueError(f"spec {spec.name}: `tables:` takes a fresh row family name, not {t!r}")
        leaks = [n for n in spec.snapshot if is_secret_field(n)]
        if leaks:
            raise ValueError(f"spec {spec.name}: a secret may not be in the snapshot: {leaks} — "
                             f"the snapshot is what leaves the cluster")
        # A blob is the one field that is certainly too big for the snapshot, and the snapshot is one
        # object per worker with a ceiling over it. Refused at LOAD time for the same reason a secret is:
        # by the time someone notices the snapshot stopped publishing, М12 has been stale for a while.
        heavy = [n for n, f in fields.items() if f.type == "blob" and n in spec.snapshot]
        if heavy:
            raise ValueError(f"spec {spec.name}: a blob may not be in the snapshot: {heavy} — the snapshot "
                             f"is one object per worker under a ceiling, and a blob is what does not fit "
                             f"in a row in the first place")
        unknown_snap = [n for n in spec.snapshot if n not in fields]
        if unknown_snap:
            raise ValueError(f"spec {spec.name}: snapshot names no field: {unknown_snap}")
        if spec.older_epochs not in ("fenced", "earlier-run"):
            raise ValueError(f"spec {spec.name}: events.older_epochs is fenced or earlier-run, "
                             f"not {spec.older_epochs!r} — the page draws one of the two")
        if spec.retire_field and spec.retire_field not in fields:
            raise ValueError(f"spec {spec.name}: retire_when names no field: {spec.retire_field!r}")
        if bool(spec.retire_field) != bool(spec.retire_values):
            raise ValueError(f"spec {spec.name}: retire_when needs both a field and a non-empty `in` — "
                             f"a predicate that matches nothing retires nothing, silently")
        if spec.near_by != "id" and spec.near_by not in fields:
            raise ValueError(f"spec {spec.name}: near.by names no field: {spec.near_by!r}")
        if spec.near_by != "id" and spec.near == "none":
            raise ValueError(f"spec {spec.name}: near.by needs a near to follow")
        if spec.near_of and spec.near == "none":
            raise ValueError(f"spec {spec.name}: near.of needs a near to follow")
        # `of` names a field of the OTHER subsystem's status, which this loader cannot see — nothing to
        # check here. A name that matches nothing behaves like an affinity that finds no holder: units are
        # placed by the filters alone, `_pick` says so in its reason, and nobody is refused.
        if spec.home == "near" and spec.near == "none":
            raise ValueError(f"spec {spec.name}: home: near needs a near to follow")
        if spec.home and spec.home != "near" and spec.home not in fields:
            raise ValueError(f"spec {spec.name}: home names no field: {spec.home!r}")
        return spec

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
               f"{self.name}/policy",                                                         # the administrator's knobs: servers distinct | shared
               f"{self.name}/sweep",                                                          # what the blob sweep marked, and when
               f"{self.name}/requests/*",                                                     # bounded work an operator asked a worker for, outside its ordinary pass
               DRAIN_KEY]                                                                     # "this machine is about to stop": the operator's, and the same row for every subsystem
        for d in self.derived:
            out.append(f"{self.name}/{d.row.split('/')[0]}/*")
        out += [f"{self.name}/{t}/*" for t in self.tables]              # the administrator's lists: `rec/volumes/*`
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
        # A `url` field may not carry a userinfo. `rtsp://root:hunter2@10.0.0.5/…` is how a password
        # reaches a row that is in the SNAPSHOT — out of the cluster, into М12's directory, and onto the
        # screen of every console, past a mask that only looks at `*_secret`. The credential fields are
        # where it goes instead, and saying so is better than moving it quietly: an operator who pasted a
        # URL from a browser learns that this system keeps the two apart.
        # A `blob` field holds the digest of the bytes, never the bytes. Without this, the obvious thing
        # for a client to do — paste the lump into the row — is also the thing that puts a row over the
        # store's ceiling, and the refusal it gets says "too big" rather than what to do instead.
        for name, f in self.fields.items():
            if f.type == "blob" and fields.get(name) and not is_digest(fields[name]):
                raise Refused(f"{name} takes a digest, not the bytes ({len(str(fields[name]))} of them): "
                              f"PUT the bytes to /{self.rows}/<id>/{name} and the row gets the digest back")
        for name, f in self.fields.items():
            if f.type == "url" and fields.get(name):
                u = urlsplit(str(fields[name]))
                if u.username or u.password or "@" in u.netloc:
                    raise Refused(f"{name} may not carry a login: put it in cred_username / cred_secret — "
                                  f"a url field is in the snapshot, and the snapshot leaves the cluster")

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

    # The place this worker occupies, in the units the spec counts in: its server, or its volume when the
    # subsystem says `place_by: volume`. `"?"` when the worker has not said — and an unknown place never
    # matches a home, so a unit with a home waits rather than landing somewhere at random.
    def place_of(self, worker: str) -> str:
        if self.spec.place_by == "server":
            return self.server_of(worker)
        hb = self.workers_seen(max_age=1e12).get(worker)
        if hb is None:
            return "?"
        # Three cases, and the difference between the last two is the point.
        #
        # The field NAMES a place — the ordinary one: that is where this worker is.
        #
        # The field is ABSENT: one volume named after the server. That is the truth for every box with one
        # disk, it is what a worker written before the field existed means, and where it is NOT the truth
        # it errs the safe way — three recorders on three disks that nobody told apart read as three on
        # one place, and `distinct` idles two of them rather than letting two think they own the same disk.
        #
        # The field is PRESENT AND EMPTY: the worker says it is on NO place — a spare, running and holding
        # nothing, waiting for a place to become free. Reading that as its server would be the worst
        # answer available: the spare would share a place with the recorder that actually owns the disk,
        # and `distinct` would idle one of the two at random.
        if self.spec.place_by in hb.extra:
            return str(hb.extra[self.spec.place_by])
        return str(hb.extra.get("server", "?"))

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
            # A named unit's id comes VERBATIM from the operator's body, and from here it becomes three
            # things: the key `<sub>/<rows>/<id>`, the prefix an ACL is matched against, and a directory on
            # a resource's disk (`events.unit_dir`). So it is a name, not a path: no separators, and not a
            # relative one. `variables.safe_path` refuses the same shapes one layer down — this one is a
            # 400 to the person who typed it rather than a 500 from the store.
            if "/" in uid or uid in (".", ".."):
                raise Refused(f"a {self.spec.name} {self.spec.id} is a name, not a path: {uid!r}")
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

    # Whether this row says the work is over — `retire_when` in the spec, and nothing at all for the
    # subsystems that never end.
    def retired(self, row: dict | None) -> bool:
        if not self.spec.retire_field or row is None:
            return False
        return str(row.get(self.spec.retire_field, "")) in self.spec.retire_values

    # The other half of `retire_when`: a unit that FINISHED while placed gives its assignment back, so the
    # worker drops it and the budget it was holding is free again. Without this the predicate below only
    # stops the next placement, and a cluster's whole capacity ends up held by work that is over.
    #
    # The reason says which value did it, because "done" and "failed" are a very different message to the
    # person reading `/where/<id>`.
    def unplace_retired(self) -> list:
        """Every placement whose unit's row says the work is over loses its assignment."""
        done = []
        for p in self.vars.list(self.sub.config("placement") + "/"):
            uid = self.spec.parse_id(p.rsplit("/", 1)[1])
            it, _ = self.vars.get(p)
            if not it or not it.get("worker") or not self.retired(self.unit(uid)):
                continue
            state = str(self.unit(uid).get(self.spec.retire_field, ""))
            self.assign_remove(it["worker"], str(uid))
            self.write(p, lambda i: {"worker": "", "reason": state, "at": self.wall(), "rev": int(i.get("rev", 0)) + 1})
            done.append(uid)
        return done

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
        out = [w for w in workers if rule(row, self.labels_of(w))]
        return [w for w in out if self.server_of(w) not in self.servers_taken(row)]

    # The servers already carrying a unit that shares this row's `spread_by` value — where this one may
    # therefore NOT go. Empty when the subsystem does not ask to spread, which is every subsystem today.
    #
    # Read the whole rule in one sentence: two recordings of one camera exist to survive one server, so
    # putting them on one server is not a compromise, it is the failure the operator was buying insurance
    # against. `near` pulls a recorder towards the camera's holder and would otherwise pull BOTH copies to
    # the same place — the preference loses to the filter, and the reason says which.
    def servers_taken(self, row: dict) -> set[str]:
        field = self.spec.spread_by
        if not field:
            return set()
        value = row.get(field)
        if value in (None, ""):
            return set()
        mine, taken = str(row["id"]), set()
        for other in self.units():
            if str(other["id"]) == mine or str(other.get(field)) != str(value):
                continue
            pl = self.placement(other["id"])
            if pl is not None:
                taken.add(self.server_of(pl.worker))
        return taken

    # -- the administrator's knobs: one row, `<name>/policy`, written by the console ---------------
    # `servers`: `shared` (default) — every worker is a place to put units, two on one server included (a
    # box IS several workers on one server); a dead server's slot is Nomad's to reschedule onto a neighbour
    # (Lesson 4's power pull), and the controller waits for it. `distinct` — one worker per server carries
    # units; a second worker Nomad put on the same server idles by policy, and a server whose worker and
    # resource both fall silent is gone (`gone_servers`) — its units move. The jobspec says `spread`, so
    # both are possible without touching Nomad; the administrator chooses on the console.
    POLICY_CHOICES = {"servers": ("distinct", "shared")}

    @property
    def POLICY_DEFAULTS(self) -> dict:                                   # the spec's `placement.servers` is the default; the row overrides
        return {"servers": self.spec.servers}

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
        by_place: dict[str, list[str]] = {}
        for w in sorted(workers, key=slot_number):
            by_place.setdefault(self.place_of(w), []).append(w)
        idle = []
        for server, ws in by_place.items():
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
    # Who may receive units now. Three exclusions, and the third is the one a catalogue teaches: a worker
    # that RELEASED its slot said it is leaving, and a service catalogue's answer to that is deregistration —
    # gone from the list at once, not in `lost_after` seconds when its heartbeat finally ages out. We have
    # the fact (`Slot.released`) and used it only to move units OFF such a worker (`redistribute`); without
    # this line the very next `place()` could put a new one back ON it, and the pass after that would move
    # it off again. A departure that still collects work is churn at every scale-in and every update.
    # Workers on the server being drained: not placed on, and (in `redistribute`) moved off. The third
    # kind of "not here" beside a released slot and a silent resource — and the only one the operator says
    # BEFORE it is true, which is the whole point of an upgrade.
    def on_draining(self, workers) -> list[str]:
        server = self.draining()
        return [w for w in workers if server and self.server_of(w) == server] if server else []

    def _pool(self, workers):
        pool = sorted(workers if workers is not None else self.workers_seen())
        leaving = {n for n, s in self.slots().items() if s.released}
        gone = set(self.without_resource(pool)) | set(self.idle_by_policy(pool)) | leaving | set(self.on_draining(pool))
        return [w for w in pool if w not in gone]

    # The dry run. Which units nothing else could serve if this server went away — asked BEFORE it does,
    # with the machinery that will answer for real afterwards (`eligible` over the pool minus that server).
    # Fifty cameras leaving a machine have to land somewhere, and "somewhere" is a fact about headroom and
    # labels, not a hope. An upgrade script reads this and stops; the alternative is reading `/unplaceable`
    # after the reboot.
    def would_strand(self, server: str, workers: list[str] | None = None) -> list[str]:
        """Unit ids that nothing left could serve if `server` stopped now."""
        pool = [w for w in self._pool(workers) if self.server_of(w) != server]
        out = []
        for row in self.units():
            uid = row["id"]
            pl = self.placement(uid)
            if pl is not None and self.server_of(pl.worker) != server:
                continue                                   # it is not on that server: not its business
            if not self.eligible(row, pool):
                out.append(str(uid))
        return out

    # `near: <sub>`: the worker of that subsystem whose heartbeat status lists this unit's id in phase
    # `running` — `(worker, server)` — or None. The recorder says `near: vms`: the camera's holder.
    #
    # `near.of` names the field of THEIR status entry the value is matched against, instead of their unit
    # id: how a camera finds the recorder running a recording OF it without knowing what the operator named
    # that recording. Ties (two recordings of one camera) go to the smallest of their ids, so two passes
    # over the same heartbeats reach the same server.
    def holder_near(self, uid) -> tuple[str, str] | None:
        if self.spec.near == "none":
            return None
        field = self.spec.near_of
        want = self.near_id(uid)                                   # `by`: which value of mine to look for
        if not want:
            return None
        from .console import heartbeats                            # the read model's scan, without the age filter
        found: list[tuple[str, str, str]] = []                     # (their unit id, worker, server)
        for w, hb in heartbeats(self.objects, self.spec.near + "/").items():
            if self.wall() - hb.ts > 45.0:
                continue
            for st in hb.status:
                key = st.get(field) if field else st.get("id")
                if str(key) == want and st.get("phase") == "running":
                    if not field:
                        return w, hb.extra.get("server", "?")
                    found.append((str(st.get("id")), w, hb.extra.get("server", "?")))
        if found:
            _, w, server = sorted(found)[0]
            return w, server
        return None

    # Whose unit of the followed subsystem this one wants to be beside: its own id by default, or the
    # string in the field `near.by` names. The row is read for the second form only — a subsystem whose
    # units share the other's naming pays nothing for the ones that do not.
    def near_id(self, uid) -> str:
        if self.spec.near_by == "id":
            return str(uid)
        row = self.unit(uid)
        return str(row.get(self.spec.near_by, "") or "") if row else ""

    # Where this unit belongs, for `ensure_home`. `home` is either the name of a field on the row — the
    # server an operator named — or the literal `near`, meaning "wherever the thing I follow is".
    #
    # The second form is what makes one subsystem come home BEHIND another. `near` alone is applied once,
    # when a unit is placed: a camera whose worker was moved while a server was down keeps being held
    # there for ever, because reading its fan-out over RTSP works and nothing is broken.
    #
    # Exactly one of a following pair may say `home: near`, and that is not a detail. Two subsystems that
    # each follow the other have no anchor: every pass moves each towards where the other was, and they
    # swap places instead of meeting. The anchor is the one with a real home — for the VMS, the recording,
    # because it writes to a disk and a disk does not move.
    def home_for(self, row: dict) -> str:
        if self.spec.home == "near":
            near = self.holder_near(row["id"]) if self.spec.near != "none" else None
            return near[1] if near and near[1] != "?" else ""
        return str(row.get(self.spec.home, "") or "") if self.spec.home else ""

    # The same, addressed by id — what `_pick` needs before a unit is placed anywhere.
    def home_of(self, uid) -> str:
        if not self.spec.home or self.spec.home == "near":
            return ""                                     # the `near` form is resolved by `_pick`, which has the holder
        row = self.unit(uid)
        return str(row.get(self.spec.home, "") or "") if row else ""

    # The pick, with the two affinities in order — home first, then `near` — over a pool the FILTERS have
    # already cut (`eligible`: the constraint and `spread_by`). `(worker, free, note)`, and the note says
    # which it was: "at home on srv-a", "beside w-1 holding it", "away from home srv-a" — so the reason
    # tells the operator both where the recording reads its source from and whether it is where it belongs.
    #
    # Home before near, because they disagree exactly when a server is down: `near` would pin a recorder to
    # whichever server picked up the camera, and nothing would ever come back.
    def _pick(self, pool: list[str], uid) -> tuple[str | None, int, str]:
        near = self.holder_near(uid)
        home = near[1] if self.spec.home == "near" and near else self.home_of(uid)
        follows = self.spec.home == "near"
        if home:
            best, free = self._best([w for w in pool if self.place_of(w) == home])
            if best is not None:
                return best, free, (f", beside {near[0]} holding it" if follows else f", at home on {home}")
        if near is not None and not follows:
            beside = [w for w in pool if self.server_of(w) == near[1]]
            best, free = self._best(beside)
            if best is not None:
                return best, free, f", beside {near[0]} holding it" + (f" (home {home} has no room)" if home else "")
        best, free = self._best(pool)
        note = ""
        if best is not None and near is not None and self.server_of(best) != near[1]:
            note = f", away from {near[0]} on {near[1]} (no room there)"
        if best is not None and home and not follows and self.place_of(best) != home:
            note += f"; away from home {home}"
        return best, free, note

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
        if row is None or self.retired(row):
            return None                                 # finished work is not placed, and not "unplaceable" either
        pool = self.eligible(row, self._pool(workers))
        best, free, near = self._pick(pool, uid)
        if best is None:
            return None                                 # "the system is full" — or nothing that can reach it; never "w-1 is full"
        reason = f"most free capacity ({free}) among {len(pool)} worker(s)"
        if self.spec.constraint == "labels-subset" and row.get("labels"):
            reason += f" reaching {','.join(sorted(row['labels']))}"
        reason += f"; on {self.server_of(best)}"
        if self.spec.requires == "resource":
            reason += f", whose resource is {self.resource_state(self.server_of(best))}"
        reason += near
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
        self.unplace_retired()
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
                for r in self.units()
                if self.placement(r["id"]) is None and not self.retired(r) and not self.eligible(r, live)]

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
        for w in self.on_draining(seen):                               # an operator said this machine is about to stop
            if self.assignment(w).units:
                gone_for.setdefault(w, f"server {self.server_of(w)} draining")
        for w, server in self.gone_servers().items():                  # the server is gone: its slot lapsed and its resource silent
            gone_for.setdefault(w, f"server {server} gone: slot {w} lapsed and its resource silent")
        for gone, why in gone_for.items():
            live = [w for w in self._pool(workers) if w != gone]
            for unit in sorted(self.assignment(gone).units, key=_unit_key):
                uid = self.spec.parse_id(unit)
                row = self.unit(uid)
                pool = self.eligible(row, live) if row else live
                best, free, near = self._pick(pool, uid)
                if best is None:
                    break                                   # the system is full; the unit waits, listed where it was
                self.move(uid, best, f"{why}; most free capacity ({free}); on {self.server_of(best)}{near}")
                moves.append((uid, gone, best))
        return moves

    # Units placed away from the home their row names, moved back — at most `budget` a pass, because every
    # move is a new epoch and a seam in the recording. It is the other half of `home`: the preference in
    # `_pick` decides where a unit goes when it is placed, and this is what happens to one already placed
    # somewhere else when its home comes back.
    #
    # `eligible` runs first, so the filters still beat the preference: a unit whose home is barred by
    # `spread_by` or by its labels stays where it is. A home with no live worker, or no room, is not an
    # error and says nothing — the unit is where it can be, which is the point of a preference.
    def ensure_home(self, budget: int = 1, workers: list[str] | None = None) -> list[tuple]:
        """Units away from the home their row names — or, with `near` and no `home`, away
        from the server holding what they follow — moved back, `budget` a pass."""
        if budget <= 0 or not self.spec.home:
            return []
        moves, pool = [], self._pool(workers)
        for row in self.units():
            if len(moves) >= budget:
                break
            uid, home = row["id"], self.home_for(row)
            pl = self.placement(uid)
            if not home or pl is None or self.place_of(pl.worker) == home:
                continue
            best, free = self._best([w for w in self.eligible(row, pool) if self.place_of(w) == home])
            if best is None:
                continue                                  # home is not back, or has no room: stay put, quietly
            why = f"it follows {self.spec.near} onto" if self.spec.home == "near" else "home is"
            self.move(uid, best, f"{why} {home}; most free capacity ({free}); on {home}")
            moves.append((uid, pl.worker, best))
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

    # -- blobs: a field too big for a row ------------------------------------------------------------
    # The bytes of one `blob` field. Written FIRST, before the row that names them: a crash between the
    # two leaves an object nobody points at (harmless, collectable), where the other order would leave a
    # row pointing at nothing — a unit that cannot start. The same order М12's identity store publishes in.
    #
    # The key is the digest, so this is idempotent by construction: writing the same bytes twice writes
    # the same object twice, and two units with the same mask share one object.
    def put_blob(self, data: bytes) -> str:
        """Store the bytes; return the digest to put in the row."""
        import json
        d = blob_digest(data)
        # These exact bytes may be sitting on the sweep's list right now — the same mask uploaded again
        # for a second unit, while the copy the first unit stopped naming is marked for collection.
        # Taking it off the list makes the sweep's own CAS fail, and a sweep that loses that CAS deletes
        # nothing at all. The alternative is a lock, for a window two store calls wide.
        key = self.sub.sweep_key()
        items, idx = self.vars.get(key)
        marked = json.loads((items or {}).get("digests", "[]"))
        if d in marked:
            self.vars.put(key, {**items, "digests": json.dumps([x for x in marked if x != d])}, cas=idx)
        self.objects.put(self.sub.blob_key(d), data)
        return d

    # What a worker calls with the digest it read from its row. `None` when the object is not there, which
    # is a real state — the row travelled and the object did not — and the caller must not start on it.
    #
    # The bytes are CHECKED against the digest before they are handed over. Not belt-and-braces: the key
    # says what the bytes are, and nothing but this makes that true. An ACL says who may write the key,
    # which is a different claim and a weaker one — it cannot survive a store shared more widely than
    # intended, an object copied between stores, or a bad disk. `verify` survives all three.
    def blob(self, d: str, check_digest: bool = True) -> bytes | None:
        data = self.objects.get(self.sub.blob_key(d))
        if data is None or not check_digest:
            return data
        return verify(d, data)

    # Every digest any row currently names: what a sweep would keep. There is no sweep — nothing in the
    # platform deletes an object — and this is the half of it that can be written honestly today.
    def blobs_referenced(self) -> set[str]:
        names = [n for n, f in self.spec.fields.items() if f.type == "blob"]
        return {r[n] for r in self.units() for n in names if is_digest(r.get(n) or "")}

    # -- the sweep: collecting blobs nothing names any more ------------------------------------------
    # Nothing else in the platform deletes an object, and this is the one thing that has to. A blob key is
    # the digest of its bytes, so every edit of a `blob` field makes a NEW permanent object: unlike a
    # heartbeat, whose key is reused by the next instance of the slot, blobs grow with the number of edits
    # over the system's lifetime and nothing ever reclaims them.
    #
    # The obvious implementation is wrong, and it is worth being precise about why. "Delete every blob no
    # row names" races with `put_blob`: the object is written BEFORE the row that names it (Lesson 26), so
    # a sweep landing between those two writes sees an unreferenced blob and deletes the bytes a row is
    # about to point at. The unit then cannot start, and the operator's upload silently did nothing.
    #
    # So noticing and deleting are put in DIFFERENT PASSES:
    #
    #   mark   nothing is deleted. The digests that no row names are written to `<name>/sweep` with the
    #          time. A blob created after this moment is not on the list, which is where the grace period
    #          comes from — no timestamps on objects required, and `variables://` has none to offer.
    #   sweep  one pass later, and only after `grace`: the marked digests are checked AGAIN, the row is
    #          cleared by CAS on the index just read, and only then are the objects removed.
    #
    # The order of those last two is the whole safety argument. Clearing first means a lost CAS — anyone
    # touched the row since the mark — costs nothing: not one object has been deleted yet. Deleting first
    # would mean acting on a decision that something has already contradicted.
    #
    # `limit` is not a nicety either: `<name>/sweep` is a row, and a row has the store's ceiling over it
    # (Lesson 26). The sweep is subject to the rule it was written under.
    SWEEP_LIMIT = 64
    SWEEP_GRACE = 300.0

    def sweep_blobs(self, limit: int = SWEEP_LIMIT, grace: float = SWEEP_GRACE) -> dict:
        """One pass: marks, or sweeps, or waits. `{marked, deleted, waiting}`."""
        import json
        names = [n for n, f in self.spec.fields.items() if f.type == "blob"]
        if not names:
            return {"marked": 0, "deleted": 0, "waiting": 0}
        key, now = self.sub.sweep_key(), self.wall()
        items, idx = self.vars.get(key)
        marked = json.loads((items or {}).get("digests", "[]"))

        if not marked:                                   # -- mark: notice, write it down, delete nothing
            referenced = self.blobs_referenced()
            prefix = self.sub.blobs_prefix()
            orphans = sorted(k[len(prefix):] for k in self.objects.list(prefix)
                             if k[len(prefix):] not in referenced)[:limit]
            if orphans:
                self.vars.put(key, {"at": str(now), "digests": json.dumps(orphans)}, cas=idx)
            return {"marked": len(orphans), "deleted": 0, "waiting": 0}

        if now - float((items or {}).get("at", 0)) < grace:
            return {"marked": 0, "deleted": 0, "waiting": len(marked)}

        # -- sweep: check again, clear the decision, and only then remove the bytes
        referenced = self.blobs_referenced()
        doomed = [d for d in marked if d not in referenced]
        self.vars.put(key, {"at": str(now), "digests": "[]"}, cas=idx)   # Conflict here deletes nothing
        deleted = sum(1 for d in doomed if self.objects.delete(self.sub.blob_key(d)))
        return {"marked": 0, "deleted": deleted, "waiting": 0}

    # Units and placement for the layer above, ONE OBJECT PER WORKER: `<name>/snapshot/<worker>` holding
    # `{cluster, worker, ts, <rows>: [{id, <snapshot fields>, revision, worker, server}]}`, plus
    # `<name>/snapshot/unplaced` for the units nobody holds. A copy with an age — never the rows
    # themselves, which do not leave raft.
    #
    # The shape is the heartbeat's, and that is the point. Every other object in the platform is already
    # sharded by its writer — one heartbeat per worker, one resource heartbeat per server — and stays small
    # whatever the cluster does. The snapshot was the exception: one object for every unit in the cluster,
    # under a store with a ceiling. See `Subsystem.snapshot_key` for the arithmetic that made this a defect
    # rather than a preference.
    def snapshot_shards(self) -> dict[str, dict]:
        """The snapshot as one object per worker, keyed by shard name."""
        keep = ["id"] + [f for f in self.spec.snapshot if f != "id"] + ["revision"]
        now, out = self.wall(), {}
        for r in self.units():
            w = self.where(r["id"])
            self.sub.snapshot_key(w)              # refuses a worker named `unplaced` before it shadows the shard
            sh = out.setdefault(w or UNPLACED, {"cluster": self.cluster, "worker": w, "ts": now, self.spec.rows: []})
            sh[self.spec.rows].append({**{k: r[k] for k in keep if k in r}, "worker": w,
                                       "server": self.server_of(w or "")})
        return out

    # The shards merged back, for a reader inside this process. What М12 does over the wire is the same
    # merge, out of `objects.list(snapshot_prefix())` — see `Cluster.snapshot` there.
    def snapshot(self) -> dict:
        """Every shard merged: what the layer above ends up seeing."""
        shards = self.snapshot_shards()
        units = [u for sh in shards.values() for u in sh[self.spec.rows]]
        return {"cluster": self.cluster, "ts": self.wall(), self.spec.rows: units}

    # How old the published snapshot is, in seconds — `None` when nothing has been published.
    #
    # Read from the STORE, not kept in this process: the controller has no port to serve it from, it is
    # restarted freely, and two of them may be running. Whoever can read the objects can answer this, which
    # is what makes it a number a console can put on `/metrics`.
    #
    # The age of the whole is the age of the STALEST shard, the same rule М12's reader uses: a directory is
    # only as fresh as its oldest part, and taking the newest would report an RPO better than the real one —
    # which is exactly the direction a number like this must never be wrong in.
    def snapshot_age(self, now: float | None = None) -> float | None:
        import json
        oldest = None
        prefix = self.sub.snapshot_prefix()
        for key in self.objects.list(prefix):
            raw = self.objects.get(key)
            if not raw:
                continue
            ts = float(json.loads(raw).get("ts", 0))
            oldest = ts if oldest is None else min(oldest, ts)
        if oldest is None:
            return None
        return max(0.0, (self.wall() if now is None else now) - oldest)

    # Writes one object per worker under `<name>/snapshot/`.
    def publish_snapshot(self) -> None:
        import json
        shards = self.snapshot_shards()
        prefix = self.sub.snapshot_prefix()
        # A worker that is GONE — scaled in, or its units moved away — keeps its last shard forever: nothing
        # in the platform deletes an object. Its units would go on being reported to М12 from a worker that
        # no longer exists. So every shard already in the store that this pass did not fill is written EMPTY.
        for key in self.objects.list(prefix):
            shards.setdefault(key[len(prefix):], {"cluster": self.cluster, "worker": None, "ts": self.wall(),
                                                  self.spec.rows: []})
        for name, shard in shards.items():
            try:
                self.objects.put(prefix + name, json.dumps(shard).encode())
            except TooLarge as e:
                # The store refuses with bytes; the caller knows what those bytes WERE. A shard is one
                # worker's assignment, so an oversized shard is not a shape problem any more — it is a
                # store too small to hold what a single worker carries, and `OBJECTS` is what names it.
                raise TooLarge(e.key, e.size, e.limit,
                               f"{len(shard[self.spec.rows])} units on {name}; the snapshot is already one "
                               f"object per worker, so the store is the thing to change (OBJECTS=…)") from e

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
