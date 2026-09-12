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
from __future__ import annotations

import os

from vmsplatform.spec import PLATFORM_FIELDS, SubsystemSpec

SPEC = SubsystemSpec.load(os.path.join(os.path.dirname(os.path.abspath(__file__)), "vms.subsystem.yaml"))
OPERATOR_FIELDS = tuple(SPEC.fields)
FORBIDDEN_FIELDS = PLATFORM_FIELDS


def row(items: dict) -> dict:
    return SPEC.row(items)


def items(row_: dict) -> dict:
    return SPEC.items(row_)
