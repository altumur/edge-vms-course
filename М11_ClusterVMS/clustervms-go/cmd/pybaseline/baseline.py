"""The same shape in Python: one worker and one controller at idle with
fifty cameras against the in-memory raft fake, PSS from smaps_rollup."""
import os, sys, tempfile, threading, time
here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get("CLUSTERVMS_PATH", os.path.join(here, "..", "..", "..", "clustervms")))
import cluster  # noqa: E402  — puts vmsserver on sys.path
from cluster.controller import ClusterController  # noqa: E402
from cluster.objectstore import FsObjectStore  # noqa: E402
from cluster.variables import FakeVariables  # noqa: E402
from cluster.worker import ClusterWorker  # noqa: E402
from vms.worker import FakeActuator  # noqa: E402


def pss_kb():
    with open("/proc/self/smaps_rollup") as f:
        for line in f:
            if line.startswith("Pss:"):
                return int(line.split()[1])
    return -1


vars_ = FakeVariables(); d = tempfile.mkdtemp(prefix="baseline-")
objects = FsObjectStore(os.path.join(d, "objects"))
ctl = ClusterController(vars_, objects, capacity=50, cluster="bench")
for i in range(1, 51):
    ctl.create_camera({"source": f"driverpack://file/{i}.mp4"})
w = ClusterWorker(vars_, objects, FakeActuator(), env={"NOMAD_ALLOC_INDEX": "0", "NOMAD_NODE_NAME": "srv-a"},
                  archive_root=os.path.join(d, "archive"), capacity=50)
w.heartbeat_once(); ctl.ensure_placed()
stop = threading.Event()
threading.Thread(target=w.run, kwargs={"poll": 0.5, "stop": stop}, daemon=True).start()
def ctl_loop():
    while not stop.is_set():
        ctl.ensure_placed(); ctl.redistribute(); ctl.publish_snapshot(); stop.wait(1)
threading.Thread(target=ctl_loop, daemon=True).start()
time.sleep(1.5)
print(f"python server idle: PSS {pss_kb()} kB, {threading.active_count()} threads, {len(w.reconciler.actual)} cameras running")
stop.set()
