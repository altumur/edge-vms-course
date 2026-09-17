"""vmscontroller — the only writer of vms/*. It is the platform's
SpecController run from vms.subsystem.yaml; this module is the VMS's
vocabulary over it (camera, not unit) and nothing else.

    cameras       CRUD by CAS; revision bumps on every operator edit; refuses controller-owned fields
    placement     which worker runs a camera — by the workers' own capacity, under the label constraint,
                  stored with a reason naming the server; adding a worker moves nothing; rebalance only when asked
    assignment    vms/workers/<worker> — what each worker reads
    the snapshot  vms/snapshot — cameras and placement as one object for the layer above (М12)

It holds nothing. Two instances are harmless. It is never on the recovery
path. On one box it is a cluster of one: the snapshot still says which
server, and it is the hostname.
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # controller.py — vmscontroller: the platform's SpecController run from `vms.subsystem.yaml`, in the VMS's
# words
#
# **Role in the module.** Lesson 6. The VMS controller is not written here; it is
# `w2cplatform.spec.SpecController` (see `w2cplatform/spec.py`) instantiated with the VMS's spec. This
# module contributes only vocabulary: the class `VmsController` whose constructor bakes in `SPEC`, and five
# aliases so the tests and the console can say *camera* where the platform says *unit*. The docstring is the
# controller's contract restated for the VMS: cameras are CRUD by CAS with `revision` bumped on every
# operator edit and controller-owned fields refused; placement is by the workers' own reported capacity
# under the `labels-subset` constraint, stored with a reason naming the server, adding a worker moves
# nothing, rebalance only when asked; the assignment `vms/workers/<worker>` is what each worker reads; the
# snapshot `vms/snapshot` is cameras and placement as one object for the layer above (М12). It holds
# nothing, two instances are harmless, and it is never on the recovery path. Used by `__main__.controller`
# (with the controller's token), `__main__.console` and `vms/console.py` (with the console's token), and
# every Lesson 4/5 test.
#
# ## Module-level names
# - `VMS` — `SPEC.sub`, the `Subsystem("vms")` key layout (`vms/workers/<w>`, `vms/epoch/<id>`,
#   `vms/slots/<w>`, …).
# - `Placement`, `Refused` — re-exported from `w2cplatform.spec` (the `noqa: F401` says so) so callers can
#   write `from vms.controller import Refused`; `test_crud_by_cas_and_what_it_refuses` catches this
#   `Refused`.
#
# ### Aliases
# - `create_camera = SpecController.create` — `refuse`, take the next numeric id from `vms/next_id`, write
#   `vms/cameras/<id>` with `cas=0`, write the derived `vms/retention/<id>`; not placed here.
# - `update_camera = SpecController.update` — `refuse`, read-modify-write, `revision + 1` (what makes the
#   worker restart the pipeline), derived rows refreshed if their source changed.
# - `delete_camera = SpecController.delete` — marks the row `deleted: "true"` and sets `vms/retention/<id>`
#   to `{days: 0}`; the placement is taken back by the controller's next `unplace_deleted`.
# - `camera = SpecController.unit` — the row, or `None`.
# - `cameras = SpecController.units` — every live row, sorted by id.
#
# ## Notes
# - What the tests prove through this class: refusals of `worker/revision/phase/epoch/placement` and of a
#   create without `source` (`test_crud_by_cas…`); placement stored with a reason and untouched when a
#   worker arrives; capacity as the worker's word; forty cameras placed by two racing controllers, each
#   exactly once; explicit budgeted rebalance; the failure arithmetic; scale-in redistribution versus a
#   crash left alone; the console's token refused at `place` (`Forbidden`).
# - Nothing in this file mentions how a camera records: that is `worker.py`. "The worker is the subsystem."
# ================================================================================================
from __future__ import annotations

import time

from w2cplatform.objects import ObjectStore
from w2cplatform.spec import Placement, Refused, SpecController  # noqa: F401  — the VMS's names for the platform's things
from w2cplatform.variables import Variables

from .config import SPEC

VMS = SPEC.sub


# `SpecController` with the VMS's spec fixed. Every behaviour — `create`, `update`, `delete`,
# `unplace_deleted`, `place`, `ensure_placed`, `redistribute`, `rebalance`, `move`, `read_model`,
# `snapshot`, `publish_snapshot`, `headroom`, `capacity_of`, `failover_seconds`, … — is the platform's and
# is documented in `w2cplatform/spec.py`. Which of them a given process may actually complete is decided by
# the token its `vars_` carries, not by this class: the same class holds the controller's token in
# `vmscontroller` and the console's token in `console`.
class VmsController(SpecController):
    # `super().__init__(SPEC, vars_, objects, capacity, wall, cluster)`. `capacity` is only the fallback for
    # a worker whose heartbeat has not said its own number (the spec's `placement.capacity.fallback` is also
    # 50). `cluster` names the snapshot's cluster; on one box it is a cluster of one and the snapshot still
    # says which server, the hostname.
    def __init__(self, vars_: Variables, objects: ObjectStore, capacity: int = 50, wall=time.time, cluster: str | None = None):
        super().__init__(SPEC, vars_, objects, capacity, wall, cluster)

    # the VMS's word is "camera"
    create_camera = SpecController.create
    update_camera = SpecController.update
    delete_camera = SpecController.delete
    camera = SpecController.unit
    cameras = SpecController.units
