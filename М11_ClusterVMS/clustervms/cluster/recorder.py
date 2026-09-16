"""The recorder as an allocation — and it is М10's `RecWorker`, unchanged.
The runtime hands it what it hands a worker (SLOT_INDEX → slot r-<i>,
SERVER_NAME → the server whose archive it writes into, LABELS, INSTANCE_ID,
CAPACITY); it claims `rec/slots/r-<i>` by CAS, reads the
assignment the rec controller wrote, subscribes to each camera's fan-out from
whichever worker holds it (the VMS heartbeat's `live_url`), and writes
footage into rec/<cam>/e<epoch>/ on ITS server's archive.

The jobspec is the one with the archive constraint (`meta.archive is_set`)
and `spread`; whether two recorders on one server both carry recordings is
`rec/policy {servers}` — `distinct` by default, the one subsystem where a
second process on the same disks is no second place to record. When the
server dies, the rec controller moves its recordings to a server whose
resource answers; the footage written before the move stays on the old
disks under the old epoch, and the timeline names it unavailable — not lost.
"""
from __future__ import annotations

from vms.recorder import REC, RecWorker  # noqa: F401
from vms.worker import FakeActuator


class ClusterRecorder(RecWorker):
    def __init__(self, vars_, objects, actuator=None, env: dict | None = None, **kw):
        super().__init__(None, vars_, objects, actuator or FakeActuator(), env=env, **kw)
