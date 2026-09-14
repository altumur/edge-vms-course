"""The VMS's schema — as a spec the platform's controller runs from
(vms.subsystem.yaml, beside this file). What this module keeps is the
Python view of the same thing, for the worker and the tests:

    vms/cameras/<id>      the row: the spec's fields, plus revision       (the controller writes)
    vms/workers/<worker>  units, rev                                      (the controller writes)
    vms/placement/<id>    worker, reason, at, rev                         (the controller writes)
    vms/retention/<id>    days — derived from events_retention_days       (the controller writes; the resource reads)
    vms/epoch/<id>        epoch                                           (a worker takes, by CAS)
    vms/next_id           n                                               (the controller)

A camera row is small, rare and must be consistent: raft's shape. Nothing
here is controller-derived status — that is in the worker's heartbeat.
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # config.py — the VMS's schema as the Python view over `vms.subsystem.yaml`: `SPEC`, `row()`, `items()`
#
# **Role in the module.** The schema itself is the YAML beside this file (see `vms.subsystem.yaml`), and the
# platform's `SubsystemSpec` parses it. This module loads that spec once at import and exposes the two
# conversions the worker and the tests need — a Variables row of strings to a typed camera dict, and back.
# The docstring is the VMS's key map, worth keeping in view:
#
# - `vms/cameras/<id>` — the row: the spec's fields plus `revision` (the controller writes; the console's
#   token may too).
# - `vms/workers/<worker>` — `units, rev` (the controller writes; the worker reads).
# - `vms/placement/<id>` — `worker, reason, at, rev` (the controller writes).
# - `vms/retention/<id>` — `days`, derived from `events_retention_days` (the controller writes; the
#   platform's resource job reads).
# - `vms/epoch/<id>` — `epoch` (a worker takes, by CAS).
# - `vms/next_id` — `n` (the controller).
#
# "A camera row is small, rare and must be consistent: raft's shape. Nothing here is controller-derived
# status — that is in the worker's heartbeat." Used by `worker.py` (`row` on every refresh), `controller.py`
# (`SPEC`), `console.py` via the controller, `__main__.py` (`SPEC.acl_*`, `row` in `retain`) and the tests
# (`SPEC.acl_console()` / `acl_controller()` to build the two tokens).
#
# ## Module-level names
# - `SPEC` — `SubsystemSpec.load(<this directory>/vms.subsystem.yaml)`, parsed once at import. PyYAML is
#   imported lazily inside `load`, so this is the one import in the VMS that needs it.
# - `OPERATOR_FIELDS` — `tuple(SPEC.fields)`: the field names the operator owns (`name, source, enabled,
#   retention_days, events_retention_days, priority, labels, ref`), in YAML order.
# - `FORBIDDEN_FIELDS` — an alias of `psimplatform.spec.PLATFORM_FIELDS` (`worker, placement, epoch,
#   revision, observed_revision, phase, id`): what `SubsystemSpec.refuse` rejects in a create/update body.
#   Kept under the VMS's old name for readers of earlier lessons.
#
# ## Notes
# - Neither function filters the `deleted` marker: a row the controller marked `deleted: "true"` converts
#   like any other. `VmsWorker.refresh` checks the marker itself before calling `row`; `__main__.retain`
#   does not.
# - Changing a field's type or default is a YAML edit, not a Python one; this module has nothing to change.
# ================================================================================================
from __future__ import annotations

import os

from psimplatform.spec import PLATFORM_FIELDS, SubsystemSpec

SPEC = SubsystemSpec.load(os.path.join(os.path.dirname(os.path.abspath(__file__)), "vms.subsystem.yaml"))
LIVE_SPEC = SubsystemSpec.load(os.path.join(os.path.dirname(os.path.abspath(__file__)), "live.subsystem.yaml"))   # the third subsystem: live fan-outs
LIVE_PORT_BASE = 20000       # a camera's RTP port on its worker's server: LIVE_PORT_BASE + camera id (the heartbeat carries it)
OPERATOR_FIELDS = tuple(SPEC.fields)
FORBIDDEN_FIELDS = PLATFORM_FIELDS


# `SPEC.row(items)`: Variables items (all strings) to a typed dict with `id`, every spec field (its default
# if absent) and `revision` (default 1). The worker calls it on each camera row named by its assignment;
# `retain` calls it on every row under `vms/cameras/`.
def row(items: dict) -> dict:
    return SPEC.row(items)


# `SPEC.items(row_)`: the inverse, everything as strings (`bool` → `"true"/"false"`, lists comma-joined).
# The controller uses the spec's method directly; this wrapper exists for symmetry and for the tests.
def items(row_: dict) -> dict:
    return SPEC.items(row_)
