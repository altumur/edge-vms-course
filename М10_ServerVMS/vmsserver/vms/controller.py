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
from __future__ import annotations

import time

from vmsplatform.objects import ObjectStore
from vmsplatform.spec import Placement, Refused, SpecController  # noqa: F401  — the VMS's names for the platform's things
from vmsplatform.variables import Variables

from .config import SPEC

VMS = SPEC.sub


class VmsController(SpecController):
    def __init__(self, vars_: Variables, objects: ObjectStore, capacity: int = 50, wall=time.time, cluster: str | None = None):
        super().__init__(SPEC, vars_, objects, capacity, wall, cluster)

    # the VMS's word is "camera"
    create_camera = SpecController.create
    update_camera = SpecController.update
    delete_camera = SpecController.delete
    camera = SpecController.unit
    cameras = SpecController.units
