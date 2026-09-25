"""Lesson 10 — a cluster of one.

A camera that runs the platform is a cluster of its own. Not a worker in somebody else's cluster, because a
set of cameras has none of what makes a cluster М11's — no raft among them, no orchestrator, no unit that
could move to another camera — and not "all the cameras, together", because that would need one consistent
store across them, which is raft among cameras. Each camera alone has a store that is consistent for free:
its flash, and one writer.

So this is what a device has to be for the domain to treat it like any other member, and it is little:

    its row         `vms/cameras/1`, made at its first boot by its own console, `ref` = its serial — the name
                    the domain knows it by. The cluster-local id stays an integer, as in every cluster
    its unit        pinned: the camera IS the unit. Nothing places it, so there is no controller, no slot, no
                    assignment — the keys a placer would write are simply not there
    its epoch       taken by the camera itself, once per boot, from its flash by CAS. It fences nothing — there
                    is no second instance to fence — but the archive path and the event buckets carry it, so
                    it must grow on every boot, a crash included
    what it shows   ONE heartbeat and ONE snapshot shard, in the shapes a cluster publishes (`vms/heartbeats/`,
                    `vms/snapshot/`), so the domain's directory and read view read it unchanged. In RAM: they
                    change every few seconds, and flash does not survive being written every few seconds
    its link up     the domain agent, as in every cluster, writing `domain/*` and nothing else

And one ordering rule that a server cluster never had to state, because a server cluster is never wholly
rebooted: the door opens AFTER the first publish. A camera that answers before it has published answers
"I have no cameras" — and that is a COMPLETE answer, which the domain must believe (Lesson 1). An edit
sent at that moment is a 404, not a kept edit.
"""
from __future__ import annotations

import json
import threading
import time

from cluster.variables import Conflict, Variables
from w2cplatform.epoch import next_epoch

from .agent import ClusterTrust
from .api import ApiError
from .federation import Cluster, Unreachable

ROW, EPOCH = "vms/cameras/1", "vms/epoch/1"


class Flash:
    """The camera's Variables, counting what it costs the flash. Reads are free; every put is an erase
    somewhere, and a camera's flash is rated in erases."""

    def __init__(self, inner: Variables):
        self.inner, self.writes = inner, 0

    def get(self, path):
        return self.inner.get(path)

    def list(self, prefix):
        return self.inner.list(prefix)

    def put(self, path, items, cas=None):
        idx = self.inner.put(path, items, cas)
        self.writes += 1
        return idx


class Ram:
    """The camera's object store for what changes every few seconds. A dict: it does not survive a reboot,
    and it should not — a heartbeat from before the reboot is a lie about now."""

    def __init__(self):
        self._d: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put(self, key: str, data: bytes) -> None:
        with self._lock:
            self._d[key] = bytes(data)

    def get(self, key: str) -> bytes | None:
        with self._lock:
            return self._d.get(key)

    def list(self, prefix: str) -> list[str]:
        with self._lock:
            return sorted(k for k in self._d if k.startswith(prefix))

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._d.pop(key, None) is not None


class _Door:
    """What the domain reaches a camera through. Closed while it is off, and while it is booting."""

    def __init__(self, device: "DeviceCluster", store_of):
        self.device, self.store_of = device, store_of

    def _open(self):
        if not self.device.door_open:
            raise Unreachable(f"{self.device.name} did not answer")
        return self.store_of()

    def get(self, *a):
        return self._open().get(*a)

    def put(self, *a, **kw):
        return self._open().put(*a, **kw)

    def list(self, *a):
        return self._open().list(*a)


class DeviceCluster:
    def __init__(self, serial: str, flash_vars: Variables, wall=time.time, name: str | None = None,
                 reaches=(), address: str | None = None, disk=None):
        self.serial = str(serial)
        self.name = name or f"cam-{self.serial}"
        self.flash, self.ram, self.wall = Flash(flash_vars), Ram(), wall
        self.disk = disk if disk is not None else Ram()  # the durable object store — the card; survives a reboot (Lesson 12)
        self.reaches = frozenset(reaches)
        self.address = address or f"{self.name}.local"
        self.epoch, self.boots, self.door_open = 0, 0, False
        self.coverage: dict | None = None               # what the card holds, when there is a card (Lesson 13)

    # -- power ----------------------------------------------------------------------------------------
    # The order is the lesson. The epoch first, because everything the camera writes after it carries it.
    # The row, if this is the first boot ever. The publish. And only then the door.
    def boot(self, first_name: str | None = None) -> int:
        self.door_open = False
        self.ram = Ram()                                 # what lived in RAM did not survive, and should not have
        self.epoch, _ = next_epoch(self.flash, EPOCH)
        if self.flash.get(ROW)[0] is None:
            self._put_row({"id": 1, "ref": self.serial, "name": first_name or self.serial, "revision": 1}, cas=0)
        self.boots += 1
        self.publish()
        self.door_open = True
        return self.epoch

    def power_off(self) -> None:
        self.door_open = False

    # -- the row: one writer, this camera's console ------------------------------------------------------
    def row(self) -> dict:
        items, _ = self.flash.get(ROW)
        return json.loads(items["row"]) if items else {}

    def _put_row(self, row: dict, cas) -> int:
        return self.flash.put(ROW, {"row": json.dumps(row, ensure_ascii=False, sort_keys=True)}, cas=cas)

    def rec_prefix(self) -> str:
        return f"rec/{self.serial}/e{self.epoch}/"

    # -- what the domain reads ---------------------------------------------------------------------------
    # In the shapes a cluster publishes, and the only thing that makes them a cluster's is the shape: one
    # heartbeat named by its writer, one snapshot shard named by its worker. The camera is its own server
    # — the failure domain a heartbeat names is the box it runs on, and here that box is the camera.
    def publish(self) -> None:
        now, row = self.wall(), self.row()
        rev = int(row.get("revision", 1))
        status = {"id": 1, "ref": self.serial, "name": row.get("name", ""), "enabled": True, "phase": "running",
                  "position": "converged", "revision": rev, "observed_revision": rev, "epoch": self.epoch}
        hb = {"worker": self.serial, "ts": now, "server": self.serial, "instance": f"{self.name}-boot-{self.boots}",
              "capacity": 1, "headroom": 0, "status": [status],
              "live_url": f"rtsp://{self.address}/live", "playback_url": f"http://{self.address}/playback",
              "coverage": self.coverage}
        snap = {"cluster": self.name, "worker": self.serial, "ts": now,
                "cameras": [{**row, "worker": self.serial, "server": self.serial}]}
        self.ram.put(f"vms/heartbeats/{self.serial}", json.dumps(hb).encode())
        self.ram.put(f"vms/snapshot/{self.serial}", json.dumps(snap).encode())

    def cluster(self, domain: bool = False) -> Cluster:
        """The domain's handle on this camera: its stores, through its door. `domain=True` is a camera that
        hosts the domain's services (Lesson 15)."""
        return Cluster(self.name, _Door(self, lambda: self.flash), _Door(self, lambda: self.ram), self.reaches, domain)

    def disk_door(self) -> _Door:
        """Its durable object store, through its door — where a camera that hosts the domain publishes the
        domain's documents, and where a member keeps the copies its agent verified."""
        return _Door(self, lambda: self.disk)

    # -- its console: the camera's own page and the domain's forwarded edits alike ------------------------
    # The grant is checked HERE, against the grants this camera's agent carried home — the same check a
    # server cluster's console makes (Lesson 4). A subject of None is the camera's own page, logged in
    # locally; the domain always names one.
    def may(self, subject: str, capability: str) -> bool:
        now = self.wall()
        return any(g.subject == subject and g.capability in (capability, "admin") and now < g.valid_until
                   for g in ClusterTrust(self.flash).grants())

    def update_camera(self, camera, fields: dict, subject: str | None) -> dict:
        if not self.door_open:
            raise Unreachable(f"{self.name} did not answer")          # the domain reaches it over the network
        if str(camera) not in ("1", self.serial):
            raise ApiError(404, f"{self.name} is one camera, {self.serial}; it has no camera {camera}")
        if subject is not None and not self.may(subject, "edit"):
            raise ApiError(403, f"{subject} has no grant to edit on {self.name}")
        for _ in range(10):
            items, idx = self.flash.get(ROW)
            row = json.loads(items["row"])
            row.update(fields)
            row["revision"] = int(row.get("revision", 1)) + 1
            try:
                self._put_row(row, cas=idx)
                break
            except Conflict:
                continue
        else:
            raise ApiError(409, f"{self.name}: the row kept changing underneath the edit")
        self.publish()                                   # the domain sees it on its next pass, not on a timer here
        return {"revision": row["revision"]}

    def create_camera(self, fields: dict, subject: str | None) -> dict:
        raise ApiError(409, f"{self.name} is a device: it is one camera, and its row was made at its first boot")

    def current(self, ref):
        """For the agent applying kept edits (Lesson 9): `(id, row)` for the camera the domain calls `ref`."""
        return (1, self.row()) if str(ref) == self.serial else None
