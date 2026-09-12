"""The same shape in Python: clustervms' ClusterAppHost with every task
running (reconcile, pump_buses, report, retention, publish, lease,
heartbeat, reindex) against the same fakes, plus an asyncio HTTP listener
standing in for the console (uvicorn is not on the measuring machine), at
idle with fifty cameras. Run with RECORDER_PATH and CLUSTERVMS_PATH set."""
import asyncio, gc, logging, os, sys, tempfile
logging.disable(logging.CRITICAL)                 # idle means idle: no log lines either
sys.path.insert(0, os.environ.get("CLUSTERVMS_PATH", "../clustervms"))
sys.path.insert(0, os.path.join(os.environ.get("CLUSTERVMS_PATH", "../clustervms"), "tests"))
import cluster                                    # noqa: E402  (puts recorder on sys.path)
from cluster.apphost import ClusterAppHost        # noqa: E402
from cluster.identity import Identity             # noqa: E402
from cluster.objectstore import FsObjectStore     # noqa: E402
from cluster.publish import Publisher             # noqa: E402
from cluster.variables import FakeVariables       # noqa: E402
from cluster import metrics                       # noqa: E402
from apphost.config import Settings               # noqa: E402
from apphost.pipeline import FakeActuator         # noqa: E402
from conftest import FakeClusterStore as _Fake, cam  # noqa: E402


class FakeClusterStore(_Fake):
    """М9's retention task also runs here; give it the two reads it makes."""
    async def partitions(self, parent): return []
    async def retention_days(self): return {}



def pss_kb():
    with open("/proc/self/smaps_rollup") as f:
        for line in f:
            if line.startswith("Pss:"):
                return int(line.split()[1])
    return -1


async def main():
    os.environ["ARCHIVE_DIR"] = os.path.join(tempfile.gettempdir(), "clustervms-baseline-archive")
    os.environ["DATABASE_URL"] = "postgresql://none"
    v, objs = FakeVariables(), FsObjectStore(tempfile.mkdtemp(prefix="restore-"))
    await Publisher("node-3", FakeClusterStore([cam(i) for i in range(1, 51)]), v, objs, 0).publish_once()
    items, _ = v.get("nodes/node-3")
    ident = Identity("node-3", items["config"], 1, list(range(1, 51)), None)
    host = ClusterAppHost(Settings(), FakeClusterStore(), v, objs, ident, actuator=FakeActuator())

    async def handle(reader, writer):
        writer.write(b"HTTP/1.0 200 OK\r\n\r\n" + metrics.render(host).encode()); await writer.drain(); writer.close()
    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    task = asyncio.create_task(host.run(serve_console=False))
    await asyncio.sleep(1.5)
    gc.collect()
    print(f"python node idle: PSS {pss_kb()} kB, {len(asyncio.all_tasks())} tasks, epoch {host.settings.epoch}, "
          f"{host.restore.state}")
    host.stopping.set()
    await task
    srv.close()

asyncio.run(main())
