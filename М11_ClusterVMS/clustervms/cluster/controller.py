"""The controller as a job — and it is М10's, unchanged. What the first
ClusterVMS design added here (label constraints, the server in the reason,
the snapshot for М12, the measured failover) turned out to be the general
behaviour with N = 1: on one box the labels are empty, the server is the
hostname, and the snapshot is what a single-cluster customer's console
reads. So it lives in the platform's SpecController, the VMS's spec names
it, and this module keeps only the name М11's lessons used.

Still no Nomad client: it never places a process, never sets `count`,
never retires a slot from a silence.
"""
from __future__ import annotations

from vms.controller import VmsController as ClusterController  # noqa: F401
from w2cplatform.contract import Heartbeat


def heartbeats(objects, sub: str = "vms") -> dict[str, Heartbeat]:
    """Every worker's last heartbeat, whatever its age — the console's read model.

    One prefix and no filter: `<sub>/heartbeats/` holds heartbeats and nothing else
    (М10A Lesson 27)."""
    out = {}
    for key in objects.list(sub.rstrip("/") + "/heartbeats/"):
        raw = objects.get(key)
        if raw:
            hb = Heartbeat.from_bytes(raw)
            out[hb.worker] = hb
    return out
