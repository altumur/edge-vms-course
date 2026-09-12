"""The VMS's schema, as items in the platform's config store.

    vms/cameras/<id>      id, name, source, enabled, retention_days, priority, labels, revision   (the controller writes)
    vms/workers/<worker>  units, rev                                             (the controller writes)
    vms/placement/<id>    worker, reason, at, rev                                (the controller writes)
    vms/epoch/<id>        epoch                                                  (a worker takes, by CAS)
    vms/next_id           n                                                      (the controller)

A camera row is small, rare and must be consistent: raft's shape. The
operator-owned columns are name, source, enabled, retention_days; the
controller owns revision; nothing here is controller-derived status —
that is in the worker's heartbeat, and only there.
"""
from __future__ import annotations

OPERATOR_FIELDS = ("name", "source", "enabled", "retention_days", "events_retention_days", "priority", "labels", "ref")
# ref: the name a layer above knows this camera by — the domain's id (М12), a customer's asset tag. The cluster's
# `id` is the cluster's; two clusters both have a camera 7. The domain looks a camera up by ref, never by id.
# labels: where the camera is reachable from — "vlan:cctv-a" — matched against the labels a
# worker reports from its server. Empty on one box; М11's controller places by it.
FORBIDDEN_FIELDS = ("worker", "placement", "epoch", "revision", "observed_revision", "phase", "id")


def row(items: dict) -> dict:
    return {"id": int(items["id"]), "name": items.get("name", ""), "source": items.get("source", ""),
            "enabled": items.get("enabled", "true") == "true", "retention_days": int(items.get("retention_days", 30)),
            "events_retention_days": int(items.get("events_retention_days", 365)),
            "priority": int(items.get("priority", 100)), "revision": int(items.get("revision", 1)),
            "labels": [l for l in items.get("labels", "").split(",") if l], "ref": items.get("ref", "")}


def items(row_: dict) -> dict:
    return {"id": row_["id"], "name": row_["name"], "source": row_["source"],
            "enabled": "true" if row_["enabled"] else "false", "retention_days": row_["retention_days"],
            "events_retention_days": row_.get("events_retention_days", 365),
            "priority": row_.get("priority", 100), "revision": row_["revision"],
            "labels": ",".join(row_.get("labels", []) if isinstance(row_.get("labels", []), list) else str(row_["labels"]).split(",")),
            "ref": str(row_.get("ref", "") or "")}
