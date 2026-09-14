"""The same shape in Python: the reconciler, asyncio, json and an HTTP
listener at idle — recorder without GStreamer and without a database, so the
two numbers compare like for like. Run from the recorder directory."""
import asyncio, json, os, sys, time
sys.path.insert(0, os.environ.get("RECORDER_PATH", "."))
from worker.reconciler import Reconciler          # noqa: E402
from worker.pipeline import FakeActuator            # noqa: E402


def pss_kb():
    with open("/proc/self/smaps_rollup") as f:
        for line in f:
            if line.startswith("Pss:"):
                return int(line.split()[1])
    return -1


class Store:
    def __init__(self):
        self.rows = [{"id": i, "enabled": True, "revision": 1} for i in range(1, 51)]
    def desired(self):
        return self.rows


async def main():
    r = Reconciler(Store(), FakeActuator())
    r.reconcile(0)
    async def handle(reader, writer):
        writer.write(b"HTTP/1.0 200 OK\r\n\r\n" + json.dumps(r.status()).encode()); await writer.drain(); writer.close()
    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    await asyncio.sleep(0.5)
    import gc; gc.collect()
    print(f"python controller idle: PSS {pss_kb()} kB")
    srv.close()

asyncio.run(main())
