"""Fakes: a Variables raft in memory, an object store on disk, servers with
archive directories, a clock. No Nomad, no MinIO, no GStreamer."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cluster  # noqa: E402,F401  — puts М10's vmsserver on sys.path

from cluster.objectstore import FsObjectStore  # noqa: E402
from cluster.variables import FakeVariables  # noqa: E402
from cluster.recorder import ClusterRecorder  # noqa: E402
from cluster.worker import ClusterWorker  # noqa: E402
from vms.archive import ArchiveResource  # noqa: E402
from vms.worker import FakeActuator  # noqa: E402


class Clock:
    def __init__(self, t=1000.0): self.t = t
    def __call__(self): return self.t
    def advance(self, s): self.t += s


class Server:
    """A box in the cluster: a name, labels, an archive resource on its disks."""
    def __init__(self, root: str, name: str, labels: str = "", wall=None):
        self.name, self.labels = name, labels
        self.spool, self.archive = os.path.join(root, name, "spool"), os.path.join(root, name, "archive")
        self.resource = ArchiveResource(self.spool, self.archive, wall=wall)


class Cluster:
    """Three servers, one raft, one object store — and a clock the tests own."""
    def __init__(self, servers=(("srv-a", "vlan:cctv-a"), ("srv-b", "vlan:cctv-a,vlan:cctv-b"), ("srv-c", "vlan:cctv-b"))):
        self.root = tempfile.mkdtemp(prefix="clustervms-")
        self.vars = FakeVariables()
        self.objects = FsObjectStore(os.path.join(self.root, "objects"))
        self.clock, self.wall = Clock(), Clock(1_757_500_000.0)
        self.servers = {n: Server(self.root, n, l, self.wall) for n, l in servers}
        self.allocs = 0

    def env(self, index: int, server: str, alloc: str | None = None) -> dict:
        """What a RUNTIME puts in an allocation's environment — the neutral names of
        `w2cplatform.runtime`, which the jobspec fills from Nomad's own. The loop never
        sees a vendor's name; the file that already knows the orchestrator does the mapping."""
        self.allocs += 1
        return {"SLOT_INDEX": str(index), "SERVER_NAME": server,
                "LABELS": self.servers[server].labels,
                "INSTANCE_ID": alloc or f"alloc-{self.allocs:04d}"}

    def worker(self, index: int, server: str, capacity: int = 50, actuator=None, alloc=None) -> ClusterWorker:
        w = ClusterWorker(self.vars.as_writer(f"vmsworker", ["vms/epoch/*", "vms/slots/*"]), self.objects,
                          actuator or FakeActuator(), env=self.env(index, server, alloc),
                          clock=self.clock, wall=self.wall, capacity=capacity)
        return w

    def recorder(self, index: int, server: str, capacity: int = 50, actuator=None, alloc=None) -> ClusterRecorder:
        """A recorder allocation on `server`: writes into THAT server's archive resource."""
        return ClusterRecorder(self.vars.as_writer("vmsrecorder", ["rec/epoch/*", "rec/slots/*"]), self.objects,
                               actuator or FakeActuator(), env=self.env(index, server, alloc), archive=self.servers[server].resource,
                               clock=self.clock, wall=self.wall, capacity=capacity)


def world():
    return FakeVariables(), FsObjectStore(tempfile.mkdtemp(prefix="restore-"))


