"""ClusterVMS — М11. М10's platform shape across several servers.

Built ON М10's `vmsserver/` (imported, not copied): the same `vmsplatform`
contract and the same `vms/` controller, worker and archive resource. This
package supplies what a cluster adds and nothing else:

    variables.py    Nomad Variables over HTTP (ModifyIndex, cas) — and the fake with the promised semantics
    objectstore.py  MinIO / S3 (SigV4 in s3.py) — the heartbeats and the domain's snapshots
    worker.py       the worker as an allocation: a slot from NOMAD_ALLOC_INDEX, labels from the server
    controller.py   the controller as a job: placement under label constraints; the snapshot for М12
    resource.py     the archive resource as a system job: its heartbeat, its manifests served
    timeline.py     one camera across two resources; *unavailable*, never *lost*
    directory.py    where is camera 7 — one scan of vms/workers/*
    console.py      the cluster console, standard library
"""
import os
import sys

_here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for cand in (os.environ.get("VMSSERVER_PATH", ""),
             os.path.join(_here, "vmsserver"),
             os.path.join(os.path.dirname(os.path.dirname(_here)), "М10_ServerVMS", "vmsserver")):
    if cand and os.path.isdir(cand) and cand not in sys.path:
        sys.path.append(cand)          # append, not insert: our own tests/ must win
        break
