"""The same five operations in Python, timed the way `go test -bench` times them."""
import json, os, sys, tempfile, time
here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get("CLUSTERVMS_PATH", os.path.join(here, "..", "..", "..", "clustervms")))
import cluster  # noqa: E402
from cluster.controller import ClusterController  # noqa: E402
from cluster.directory import Directory  # noqa: E402
from cluster.objectstore import FsObjectStore  # noqa: E402
from cluster.variables import FakeVariables  # noqa: E402
from psimplatform.contract import Assignment, Heartbeat  # noqa: E402
from psimplatform.epoch import next_epoch  # noqa: E402
from psimplatform.events import parse_bucket  # noqa: E402


def bench(name, fn, setup=lambda: None, min_seconds=1.0):
    n, total = 0, 0.0
    while total < min_seconds:
        arg = setup()
        t = time.perf_counter(); fn(arg); total += time.perf_counter() - t; n += 1
    per = total / n
    unit = "ns" if per < 1e-6 else ("µs" if per < 1e-3 else "ms")
    val = per * {"ns": 1e9, "µs": 1e6, "ms": 1e3}[unit]
    print(f"{name:<48} {n:>8} {val:12.1f} {unit}/op")


v = FakeVariables()
bench("IssueAnEpochByCAS", lambda _: next_epoch(v, "vms/epoch/7"))

v2 = FakeVariables()
for w in range(1000):
    v2.put(f"vms/workers/w-{w}", Assignment(f"w-{w}", [str(w * 20 + c + 1) for c in range(20)], 1).to_items())
def scan(_):
    d = Directory(v2, ttl=5.0)
    assert d.where(7007) == "w-350"
bench("DirectoryScan1000Workers", scan)

def setup_place():
    vars_ = FakeVariables(); d = tempfile.mkdtemp(prefix="bench-")
    objects = FsObjectStore(os.path.join(d, "objects"))
    ctl = ClusterController(vars_, objects, capacity=40)
    for w in ("w-0", "w-1", "w-2", "w-3"):
        objects.put(f"vms/{w}/heartbeat", Heartbeat(w, time.time(), [], {"capacity": 40, "headroom": 40, "server": "srv-b", "labels": "vlan:cctv-a,vlan:cctv-b"}).to_bytes())
    for n in range(120):
        ctl.create_camera({"source": f"driverpack://file/{n}.mp4", "labels": ["vlan:cctv-b"] if n % 2 == 0 else []})
    return ctl
bench("Place120CamerasOn4WorkersWithLabels", lambda ctl: ctl.ensure_placed(), setup_place)

bench("ParseOneBucketPath", lambda _: parse_bucket("/a/vms/7/e5/20260912T101000Z.events.jsonl", "/a"))

status = [{"id": i, "ref": "", "name": "cam", "enabled": True, "phase": "running", "position": "converged", "revision": 1, "observed_revision": 1, "epoch": 1} for i in range(1, 51)]
hb = Heartbeat("w-1", 1.0, status, {"server": "srv-a", "capacity": 50, "headroom": 0})
bench("EncodeDecodeA50CameraHeartbeat", lambda _: Heartbeat.from_bytes(hb.to_bytes()))
