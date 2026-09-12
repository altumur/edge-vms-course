"""The worker as an allocation — and it is М10's `VmsWorker`, unchanged.
What Nomad hands a process (NOMAD_ALLOC_INDEX, NOMAD_NODE_NAME,
NOMAD_META_labels, NOMAD_ALLOC_ID, CAPACITY) the worker reads from its
environment on a box exactly as in an allocation; this module keeps the
name М11's lessons used and the `env=` calling convention.

Nothing here is new behaviour. A worker on a cluster is a worker on a box
whose stores happen to be raft.
"""
from __future__ import annotations

from vms.worker import FakeActuator, VmsWorker, labels_from_environment, slot_from_environment  # noqa: F401


class ClusterWorker(VmsWorker):
    def __init__(self, vars_, objects, actuator=None, env: dict | None = None, **kw):
        super().__init__(None, vars_, objects, actuator or FakeActuator(), env=env, **kw)
