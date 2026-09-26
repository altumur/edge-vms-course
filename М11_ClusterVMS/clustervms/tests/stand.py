"""The module's stand, traced: three servers, the real controller, workers and console, and every call they
make to the cluster's store written down in Nomad's own words (`tests/trace.py`).

Each SCENE is one moment of the module's story, run from nothing, and returns its trace. The lessons quote
these traces; the full ones live in `М11_ClusterVMS/traces/<scene>.txt`, and `test_stand.py` fails the day
the code and a stored trace disagree — so an example in a lesson is a record of a run, never a guess.

    python3 tests/stand.py              # print every scene
    python3 tests/stand.py --write      # regenerate М11_ClusterVMS/traces/
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cluster  # noqa: E402,F401  — puts М10's vmsserver on sys.path

from cluster.controller import ClusterController  # noqa: E402
from cluster.objectstore import VariablesObjectStore  # noqa: E402
from cluster.variables import FakeVariables  # noqa: E402
from cluster.recworker import ClusterRecorder  # noqa: E402
from cluster.worker import ClusterWorker  # noqa: E402
from vms import volumes  # noqa: E402
from vms.config import REC_SPEC, SPEC  # noqa: E402
from vms.worker import FakeActuator  # noqa: E402
from tests.conftest import Cluster  # noqa: E402
from tests.trace import TraceLog, TracedVariables  # noqa: E402

TRACES = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "traces")
WORKER_GRANTS = SPEC.sub.acl_worker() + ["objects/vms/heartbeats/*"]       # what vmsworker-policy.hcl grants
RECORDER_GRANTS = REC_SPEC.sub.acl_worker() + ["objects/rec/heartbeats/*"]  # what recworker-policy.hcl grants


class Stand(Cluster):
    """The conftest cluster, with every process's store traced under its own name and token."""

    def __init__(self):
        super().__init__()
        self.base = FakeVariables()
        self.log = TraceLog(roots={self.root: "/data"})
        self.vars = TracedVariables(self.base, self.log, "console")
        self.objects = VariablesObjectStore(self.vars)

    def as_process(self, who: str, writer: str | None = None, grants: list[str] | None = None):
        inner = self.base.as_writer(writer, grants) if writer else self.base
        v = TracedVariables(inner, self.log, who)
        return v, VariablesObjectStore(v)

    def worker(self, index: int, server: str, capacity: int = 50, actuator=None, alloc=None) -> ClusterWorker:
        v, o = self.as_process(f"vmsworker (allocation {index} on {server})", f"vmsworker-{index}", WORKER_GRANTS)
        return ClusterWorker(v, o, actuator or FakeActuator(), env=self.env(index, server, alloc),
                             clock=self.clock, wall=self.wall, capacity=capacity)

    def recorder(self, index: int, server: str, capacity: int = 50, actuator=None, alloc=None) -> ClusterRecorder:
        v, o = self.as_process(f"recworker (allocation {index} on {server})", f"recworker-{index}", RECORDER_GRANTS)
        return ClusterRecorder(v, o, actuator or FakeActuator(), env=self.env(index, server, alloc),
                               archive=self.servers[server].resource, clock=self.clock, wall=self.wall, capacity=capacity)

    def resources_up(self) -> dict:
        """Each server's resource job, heartbeating — what makes a server a place footage can go (lesson 6)."""
        from cluster.resource import cluster_resource
        self.resources = {}
        for name, srv in self.servers.items():
            v, o = self.as_process(f"resource ({name})", f"resource-{name}", ["objects/platform/resources/*"])
            r = cluster_resource(srv.resource, name, f"http://{name}:8090", v, o, wall=self.wall)
            r.space_probe = lambda path: (4 * 10**12, 3 * 10**12)     # a 4 TB disk, 1 TB used — the same on every run
            r.heartbeat()
            self.resources[name] = r
        return self.resources

    def controller(self, who: str = "vmscontroller") -> ClusterController:
        v, o = self.as_process(who, "vmscontroller", SPEC.acl_controller() + ["objects/vms/snapshot/*"])
        return ClusterController(v, o, wall=self.wall)

    def console(self, who: str = "console") -> ClusterController:
        v, o = self.as_process(who, "console", SPEC.acl_console())
        return ClusterController(v, o, wall=self.wall)


# -- the scenes ------------------------------------------------------------------------------------

def worker_starts() -> str:
    """Lesson 1: a worker allocation starts on srv-a, claims its slot and says what it is."""
    s = Stand()
    s.worker(0, "srv-a", capacity=50).heartbeat_once()
    return s.log.render()


def console_creates_a_camera() -> str:
    """Lesson 2: the console writes a camera row — a number from `next_id`, then the row, both by CAS."""
    s = Stand()
    s.console().create_camera({"name": "north-gate", "source": "driverpack://acme/10.2.0.11", "labels": ["vlan:cctv-a"]})
    return s.log.render()


def two_editors_one_row() -> str:
    """Lesson 2: two consoles edit camera 1 at once. The second write carries an index that is no longer
    current, gets 409, reads again and writes on top of the first — nobody's edit is lost."""
    s = Stand()
    s.console("console A").create_camera({"name": "north-gate", "source": "driverpack://acme/10.2.0.11"})
    a, b = s.console("console A"), s.console("console B")
    mark = s.log.mark()
    key = SPEC.sub.config("cameras", "1")
    first = {"done": False}

    def mutate(items):
        if not first["done"]:                   # between A's read and A's write, B writes
            first["done"] = True
            b.update(1, {"events_retention_days": 30})
        items["name"] = "north-gate-2"
        return items

    a.write(key, mutate)
    return s.log.render(since=mark)


def a_worker_may_not_write_a_camera() -> str:
    """Lesson 2: the worker's token writes its epochs, its slot, its hold and its heartbeat — and a camera
    row is a 403 from the store, not a rule in the worker's code."""
    s = Stand()
    w = s.worker(0, "srv-a")
    mark = s.log.mark()
    try:
        w.vars.put("vms/cameras/1", {"name": "mine now"})
    except Exception:                                                    # noqa: BLE001 — the point
        pass
    return s.log.render(since=mark)


def a_recorder_starts() -> str:
    """Lesson 3: a recorder allocation starts on srv-a, where the administrator declared the disks as a
    volume. It claims a slot like any worker — and then takes the VOLUME, by CAS, under `rec/holds/`: the
    one write a recorder makes that a VMS worker never does."""
    s = Stand()
    volumes.write(s.base, {"name": "disks-a", "kind": "local", "server": "srv-a", "url": s.servers["srv-a"].archive, "quota_bytes": 4 * 10**12})
    r = s.recorder(0, "srv-a")
    r.lease_pass(); r.heartbeat_once()
    return s.log.render()


def two_allocations_one_index() -> str:
    """Lesson 4: two allocations come up with one index — a reschedule whose old instance is still alive, or
    Nomad's own duplicate-index bug. The second takes the slot outright; the first finds out when it renews,
    and stops. One of them records, never both."""
    s = Stand()
    s.resources_up()
    con, ctl = s.console(), s.controller()
    for i in (1, 2):
        con.create_camera({"name": f"cam-{i}", "source": f"driverpack://file/{i}.mp4"})
    a = s.worker(0, "srv-a", alloc="alloc-A")
    ctl.assign("w-0", ["1", "2"])
    a.reconcile_once()
    mark = s.log.mark()
    b = s.worker(0, "srv-b", alloc="alloc-B")                         # the same index, a second allocation
    a.lease_pass()                                                     # the old one renews — and learns
    b.reconcile_once()
    return s.log.render(since=mark)


def scale_out_and_in() -> str:
    """Lesson 4: the workers are full; a camera waits; a third worker appears and the camera lands on it.
    Then the third is stopped in order: it releases its slot, and the controller moves its camera."""
    s = Stand()
    s.resources_up()
    con, ctl = s.console(), s.controller()
    ctl.capacity = 4
    for i in range(8):
        con.create_camera({"name": f"cam-{i + 1}", "source": f"driverpack://file/{i + 1}.mp4"})
    ws = [s.worker(0, "srv-a", capacity=4), s.worker(1, "srv-b", capacity=4)]
    for w in ws:
        w.heartbeat_once()
    ctl.ensure_placed()
    for w in ws:
        w.reconcile_once(); w.heartbeat_once()
    con.create_camera({"name": "cam-9", "source": "driverpack://file/9.mp4"})
    mark = s.log.mark()
    ctl.ensure_placed()                                                # full: the ninth waits
    w2 = s.worker(2, "srv-c", capacity=4); w2.heartbeat_once()         # `nomad job scale vmsworker 3`
    ctl.ensure_placed()
    w2.reconcile_once()
    w2.release_slot()                                                  # `… 2`: SIGTERM inside kill_timeout
    con.delete_camera(1); con.delete_camera(2)                         # room to move into
    ctl.redistribute()
    # The controller re-reads every placement and camera row on every pass — thousands of GETs that say
    # nothing new. The writes are the story; the reads are counted.
    reads = sum(1 for c in s.log.calls[mark:] if c.method == "GET")
    return s.log.render(since=mark, methods=("PUT", "DELETE")) + f"\n# … and {reads} GET requests, omitted\n"


def a_crash_releases_nothing() -> str:
    """Lesson 4: w-2 dies without a word. Its slot lapses; the controller moves nothing; the replacement
    Nomad starts claims w-2 and finds its assignment where it left it."""
    s = Stand()
    s.resources_up()
    con, ctl = s.console(), s.controller()
    con.create_camera({"name": "cam-1", "source": "driverpack://file/1.mp4"})
    w = s.worker(2, "srv-c"); w.heartbeat_once()
    ctl.ensure_placed(); w.reconcile_once()
    s.wall.advance(60)                                                 # it died; nobody said so
    mark = s.log.mark()
    ctl.redistribute()                                                 # nothing released: nothing to move
    again = s.worker(2, "srv-a", alloc="alloc-0099")                   # the disconnect block brings index 2 back
    again.refresh()
    return s.log.render(since=mark)


def what_the_autoscaler_reads() -> str:
    """Lesson 4: two workers of capacity 4 carry eight cameras between them. What the console's `/metrics`
    says — the page Prometheus scrapes and the Autoscaler's `avg(vms_worker_load)` is computed from."""
    from cluster.console import metrics_text
    s = Stand()
    s.resources_up()
    con, ctl = s.console(), s.controller()
    ctl.capacity = 4
    for i in range(8):
        con.create_camera({"name": f"cam-{i + 1}", "source": f"driverpack://file/{i + 1}.mp4"})
    ws = [s.worker(0, "srv-a", capacity=4), s.worker(1, "srv-b", capacity=4)]
    for w in ws:
        w.heartbeat_once()
    ctl.ensure_placed()
    for w in ws:
        w.reconcile_once(); w.heartbeat_once()
    text = metrics_text(s.console(), 0.0)
    keep = [l for l in text.splitlines() if "worker_load" in l or "headroom" in l or "workers_live" in l or "spare" in l]
    return "GET /metrics\n" + "\n".join(keep) + "\n"


SCENES = {"01-worker-starts": worker_starts,
          "02-console-creates-a-camera": console_creates_a_camera,
          "02-two-editors-one-row": two_editors_one_row,
          "02-a-worker-may-not-write-a-camera": a_worker_may_not_write_a_camera,
          "03-a-recorder-starts": a_recorder_starts,
          "04-two-allocations-one-index": two_allocations_one_index,
          "04-scale-out-and-in": scale_out_and_in,
          "04-a-crash-releases-nothing": a_crash_releases_nothing,
          "04-what-the-autoscaler-reads": what_the_autoscaler_reads}


def main() -> None:
    write = "--write" in sys.argv
    if write:
        os.makedirs(TRACES, exist_ok=True)
    for name, scene in SCENES.items():
        text = scene()
        if write:
            with open(os.path.join(TRACES, name + ".txt"), "w", encoding="utf-8") as f:
                f.write(text)
        else:
            print(f"==== {name}\n{text}")


if __name__ == "__main__":
    main()
