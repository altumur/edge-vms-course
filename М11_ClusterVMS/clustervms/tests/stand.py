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

    def resources_up(self, peers=None) -> dict:
        """Each server's resource job, heartbeating — what makes a server a place footage can go (lesson 6)."""
        from cluster.resource import cluster_resource
        self.resources = {}
        for name, srv in self.servers.items():
            v, o = self.as_process(f"resource ({name})", f"resource-{name}", ["objects/platform/resources/*"])
            r = cluster_resource(srv.resource, name, f"http://{name}:8090", v, o, wall=self.wall, peers=peers)
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


def who_may_write_what() -> str:
    """Lesson 5: every process of the cluster tries one write that is its own and one that is not. The
    grants are the ones the code derives from the spec (`acl_worker`, `acl_controller`, `acl_console`) —
    the same the policy files in deploy/ are checked against."""
    s = Stand()
    tries = [
        ("vmsworker w-0", WORKER_GRANTS, [("vms/epoch/7", {"epoch": "1"}), ("vms/placement/7", {"worker": "w-0"})]),
        ("recworker r-0", RECORDER_GRANTS, [("rec/holds/disks-a", {"holder": "alloc-0002"}), ("rec/recordings/7", {"cam": "7"})]),
        ("vmscontroller", SPEC.acl_controller() + ["objects/vms/snapshot/*"],
         [("vms/placement/7", {"worker": "w-0", "reason": "…"}), ("vms/cameras/7", {"name": "moved"})]),
        ("console", SPEC.acl_console(), [("vms/cameras/7", {"name": "north-gate"}), ("vms/workers/w-0", {"units": "7"})]),
        ("resource srv-a", ["objects/platform/resources/*"],
         [("objects/platform/resources/srv-a/heartbeat", {"data": "{}"}), ("objects/vms/heartbeats/w-0", {"data": "{}"})]),
    ]
    for who, grants, writes in tries:
        v, _ = s.as_process(who, who.split()[0] + "-" + who.split()[-1], grants)
        for key, items in writes:
            try:
                v.put(key, items)
            except Exception:                                            # noqa: BLE001 — the 403 is the point
                pass
    return s.log.render()


def _host(url: str) -> str:
    return url.split("//", 1)[1].split("/", 1)[0].split(":", 1)[0]


class _Manifests:
    """The console's manifest reader over the servers' directories instead of HTTP — `GET <resource>/manifest/<unit>`."""

    def __init__(self, s):
        self.s = s

    def read(self, url, cam):
        from vms.archive import Manifest
        srv = self.s.servers[_host(url)]
        if getattr(srv, "down", False):
            raise ConnectionError(srv.name)
        return Manifest(srv.archive, cam).read()


def _peers(s):
    """The resources' peer client over directories instead of HTTP — the three calls `PeerClient` makes."""
    from tests.test_lesson3_events import DirReader

    class Peers(DirReader):
        def _srv(self, url):
            srv = self.c.servers[_host(url)]
            if getattr(srv, "down", False):
                raise ConnectionError(srv.name)
            return srv
    return Peers(s)


def _json(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, indent=2)


def an_edit_during_the_failover() -> str:
    """Lesson 6: srv-a dies under w-1; while nobody runs it, the operator renames the camera; the replacement
    on srv-b starts it with the new name. Nothing was published for this to work — the edit is in raft."""
    s = Stand()
    s.resources_up()
    con, ctl = s.console(), s.controller()
    con.create_camera({"name": "before", "source": "driverpack://file/1.mp4"})
    a = s.worker(1, "srv-a"); a.heartbeat_once(); ctl.ensure_placed(); a.reconcile_once()
    s.wall.advance(20)                                                 # srv-a is gone; w-1 is between instances
    mark = s.log.mark()
    con.update_camera(1, {"name": "edited during the failover"})
    b = s.worker(1, "srv-b", alloc="alloc-0077")
    b.reconcile_once()
    return s.log.render(since=mark)


def a_timeline_across_two_resources() -> str:
    """Lesson 6: camera 7 was recorded on srv-a under epoch 3 until the failure, then on srv-b under epoch 4.
    The console's timeline merges the two resources' manifests; srv-a goes silent and is NAMED; it comes back
    and nothing was rebuilt. Not a store trace: the heartbeat, then what `/timeline/7` answers, three times."""
    from cluster.resource import resources_seen
    from cluster.timeline import merged_timeline
    from tests.test_lesson3_resources import _segment
    s = Stand()
    t = s.wall()
    _segment(s.servers["srv-a"], 7, 3, t - 1200); _segment(s.servers["srv-a"], 7, 3, t - 600)
    _segment(s.servers["srv-b"], 7, 4, t - 300)
    rs = s.resources_up()
    out = ["# the heartbeat srv-a's resource publishes", "GET /v1/var/objects/platform/resources/srv-a/heartbeat → data:",
           _json(resources_seen(s.objects)["srv-a"])]
    ask = lambda: merged_timeline(resources_seen(s.objects), _Manifests(s), 7, t - 2000, t, current_epoch=4, now=s.wall())
    out += ["", "# GET /timeline/7 — both resources answer", _json(ask())]
    s.wall.advance(60); rs["srv-b"].heartbeat(); rs["srv-c"].heartbeat()
    out += ["", "# srv-a has been silent for 60 s", _json(ask())]
    rs["srv-a"].heartbeat()
    out += ["", "# srv-a is back — with its disks", _json(ask())]
    return s.log._clean("\n".join(out)) + "\n"


def _events_site(peers=None):
    from tests.test_lesson3_events import _observe
    s = Stand()
    t = s.wall() - 7200
    _observe(s, "srv-a", "vms", "7", 3, t + 12, "motion", zone="gate")          # the VMS worker, camera 7, before the failover
    _observe(s, "srv-a", "vms", "7", 3, t + 40, "silent")
    _observe(s, "srv-b", "vms", "7", 4, t + 1205, "motion")                      # after it: next epoch, other server
    _observe(s, "srv-c", "det", "d-12", 1, t + 30, "person", cam=7, score=0.9)  # a detector ABOUT camera 7, on a GPU server
    _observe(s, "srv-a", "vms", "7", 3, t + 6800, "motion")                      # in srv-a's OPEN bucket
    return s, t


def _merged(s):
    from w2cplatform.eventdatabase import MergedIndex

    def fetch(url, p):
        name = _host(url)
        if getattr(s.servers[name], "down", False):
            raise ConnectionError(name)
        return s.resources[name].database.query(float(p["from"]), float(p["to"]), int(p["cam"]) if "cam" in p else None,
                                                p.get("kind"), p.get("subsystem"), p.get("unit"), limit=int(p.get("limit", 1000)))
    return MergedIndex(s.objects, fetch=fetch, wall=s.wall)


def _short(q) -> dict:
    keep = ("t", "subsystem", "unit", "kind", "server", "epoch", "fenced", "cam")
    return {"state": q["state"], "events": [{k: e[k] for k in keep if k in e} for e in q["events"]]}


def events_merged() -> str:
    """Lesson 7: each resource keeps a database over its OWN tree; the console holds none and merges theirs,
    fencing by the epochs only the cluster's rows know. A silent resource is named, not guessed."""
    s, t = _events_site()
    rs = s.resources_up()
    out = ["# each resource rebuilds its own database, from its own buckets"]
    for name in rs:
        out.append(f"{name}: {rs[name].database.rebuild()}")
    m = _merged(s)
    out += ["", "# GET /events?cam=7 on the console — merged from every live resource",
            _json(_short(m.query(t, t + 7200, cam=7, current_epochs={("vms", "7"): 4})))]
    s.wall.advance(60); rs["srv-b"].heartbeat(); rs["srv-c"].heartbeat()
    out += ["", "# srv-a has been silent for 60 s", _json(_short(m.query(t, t + 7200, cam=7, current_epochs={("vms", "7"): 4})))]
    return "\n".join(out) + "\n"


def the_events_mirror() -> str:
    """Lesson 7: the storage knob's events row — one Variable. Each resource copies its CLOSED buckets to the
    next live resource; srv-a goes silent and its events come from srv-b's copy, saying so; srv-a returns with
    an empty disk and pulls its buckets home."""
    import os
    import shutil
    from w2cplatform.resource import MIRROR_KEY
    s, t = _events_site()
    rs = s.resources_up(peers=_peers(s))
    out = [f"# knob off: srv-a pass -> mirrored {rs['srv-a'].pass_()['mirrored']}"]
    s.vars.put(MIRROR_KEY, {"enabled": "true", "copies": "1"})
    out.append("# the knob: PUT /v1/var/platform/mirror {\"Items\": {\"enabled\": \"true\", \"copies\": \"1\"}}")
    for name in rs:
        r = rs[name].pass_()
        out.append(f"{name} pass -> mirrored {r['mirrored']} to {r['peers']}")
    for r in rs.values():
        r.heartbeat()
    for name in rs:
        out.append(f"{name} database rebuild -> {rs[name].database.rebuild()}")
    m = _merged(s)
    out += ["", "# srv-a answers: its own events, the open bucket included", _json(_short(m.query(t, t + 7200, cam=7)))]
    s.wall.advance(60); rs["srv-b"].heartbeat(); rs["srv-c"].heartbeat()
    out += ["", "# srv-a silent: its closed buckets, from srv-b's copy", _json(_short(m.query(t, t + 7200, cam=7)))]
    shutil.rmtree(s.servers["srv-a"].archive); os.makedirs(s.servers["srv-a"].archive)
    rs["srv-a"].heartbeat()
    out += ["", f"# srv-a back with an EMPTY disk: restore -> {rs['srv-a'].restore()}"]
    rs["srv-a"].heartbeat(); rs["srv-a"].database.rebuild()
    out += [_json(_short(m.query(t, t + 7200, cam=7)))]
    return "\n".join(out) + "\n"


def _recording(s, n=3):
    """Lesson 8's starting point: w-1 on srv-a holds cameras 1..n under epoch 1, the servers' resources alive."""
    s.resources_up()
    con, ctl = s.console(), s.controller()
    for i in range(n):
        con.create_camera({"name": f"cam-{i + 1}", "source": f"driverpack://file/{i + 1}.mp4"})
    a = s.worker(1, "srv-a", alloc="alloc-A")
    a.heartbeat_once(); ctl.ensure_placed(); a.reconcile_once(); a.heartbeat_once()
    return ctl, a


def pull_the_power() -> str:
    """Lesson 8: srv-a dies at t=0. Nomad's `disconnect { lost_after = 45s }` starts the replacement on srv-b
    at t=48 (+ a placement). It claims w-1, finds its predecessor's heartbeat, reads its assignment, takes the
    next epoch for every camera — asking nobody — and reports the failover it measured."""
    s = Stand()
    ctl, a = _recording(s)
    s.wall.advance(45 + 3)
    mark = s.log.mark()
    b = s.worker(1, "srv-b", alloc="alloc-B")
    b.reconcile_once(); b.heartbeat_once()
    return s.log.render(since=mark) + f"\n# the controller reads the heartbeats: failover_seconds() = {ctl.failover_seconds()}\n"


def a_server_gone_under_distinct() -> str:
    """Lesson 8: the administrator chose `servers: distinct`. A crash (one silence: the slot) moves nothing; a
    dead server (two silences: the slot, and the resource on its server) is a fact the controller acts on."""
    s = Stand()
    ctl, a = _recording(s)
    s.console().set_policy({"servers": "distinct"})                    # the administrator, on the console: the controller may not
    b = s.worker(2, "srv-b", alloc="alloc-C"); b.heartbeat_once()
    rs = s.resources
    s.wall.advance(2 * 45 + 3); b.heartbeat_once(); rs["srv-a"].heartbeat(); rs["srv-b"].heartbeat(); rs["srv-c"].heartbeat()
    crash = s.log.mark()
    one = (ctl.gone_servers(), ctl.redistribute())
    after_crash = s.log.mark()
    s.wall.advance(2 * 45 + 3); b.heartbeat_once(); rs["srv-b"].heartbeat(); rs["srv-c"].heartbeat()
    gone = ctl.gone_servers()
    mark = s.log.mark()
    ctl.redistribute()
    b.reconcile_once()
    writes_in_crash = sum(1 for c in s.log.calls[crash:after_crash] if c.method == "PUT")
    return (f"# a crash — w-1 silent, srv-a's resource alive: gone_servers() = {one[0]}, redistribute() = {one[1]}, "
            f"PUT requests: {writes_in_crash}\n"
            f"# the power pull — w-1 AND srv-a's resource silent: gone_servers() = {gone}\n\n"
            + s.log.render(since=mark, methods=("PUT",)))


def the_old_instance_wakes_up() -> str:
    """Lesson 9: srv-a was not dead — partitioned, or paused. It comes back with w-1 still holding three cameras
    under epoch 1. Its next renewal finds the slot held by another; and every epoch says the same."""
    s = Stand()
    ctl, a = _recording(s)
    s.wall.advance(45 + 3)
    b = s.worker(1, "srv-b", alloc="alloc-B"); b.reconcile_once(); b.heartbeat_once()
    mark = s.log.mark()
    a.lease_pass()                                                     # kill -CONT
    a.renew_leases()
    a.heartbeat_once()
    return s.log.render(since=mark) + f"\n# a.conflicts() = {a.conflicts()}, a.recording_allowed = {a.recording_allowed}\n"


def a_reassignment_is_not_a_zombie() -> str:
    """Lesson 9: the controller moves camera 2 from w-1 to w-2. w-1 loses the lease on 2 exactly as a zombie
    would — and one read of its assignment tells it this is a move: it lets 2 go and keeps 1 and 3."""
    s = Stand()
    ctl, a = _recording(s)
    b = s.worker(2, "srv-b", alloc="alloc-C"); b.heartbeat_once()
    mark = s.log.mark()
    ctl.move(2, "w-2", "operator: srv-b sees that VLAN")
    b.reconcile_once()
    lost = a.lease_pass()
    a.reconcile_once()
    return s.log.render(since=mark) + f"\n# a.lease_pass() lost {lost}; a.recording_allowed = {a.recording_allowed}; running {sorted(a.actuator.running)}\n"


SCENES = {"01-worker-starts": worker_starts,
          "02-console-creates-a-camera": console_creates_a_camera,
          "02-two-editors-one-row": two_editors_one_row,
          "02-a-worker-may-not-write-a-camera": a_worker_may_not_write_a_camera,
          "03-a-recorder-starts": a_recorder_starts,
          "04-two-allocations-one-index": two_allocations_one_index,
          "04-scale-out-and-in": scale_out_and_in,
          "04-a-crash-releases-nothing": a_crash_releases_nothing,
          "04-what-the-autoscaler-reads": what_the_autoscaler_reads,
          "05-who-may-write-what": who_may_write_what,
          "06-an-edit-during-the-failover": an_edit_during_the_failover,
          "06-a-timeline-across-two-resources": a_timeline_across_two_resources,
          "07-events-merged": events_merged,
          "07-the-events-mirror": the_events_mirror,
          "08-pull-the-power": pull_the_power,
          "08-a-server-gone-under-distinct": a_server_gone_under_distinct,
          "09-the-old-instance-wakes-up": the_old_instance_wakes_up,
          "09-a-reassignment-is-not-a-zombie": a_reassignment_is_not_a_zombie}


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
