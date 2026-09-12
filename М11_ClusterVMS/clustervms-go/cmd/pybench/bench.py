"""The same six operations as cluster/bench_test.go, timed in Python. Each
is run for at least 0.5 s and the per-operation time is printed in the
units `go test -bench` uses (ns/op) so measure.sh can show them side by side."""
import datetime as dt, os, random, sys, time
sys.path.insert(0, os.environ.get("CLUSTERVMS_PATH", "../clustervms"))
import cluster                                            # noqa: E402
from cluster.configio import decode, encode               # noqa: E402
from cluster.directory import Directory                   # noqa: E402
from cluster.epoch import next_epoch                      # noqa: E402
from cluster.placement import Camera, recorder, Placer        # noqa: E402
from cluster.reindex import parse                         # noqa: E402
from cluster.s3 import sign                               # noqa: E402
from cluster.variables import FakeVariables               # noqa: E402

ACCESS, SECRET = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
MAY24 = dt.datetime(2013, 5, 24, tzinfo=dt.timezone.utc)
VLANS = ["vlan:a", "vlan:b", "vlan:c"]


def bench(name, fn, setup=lambda: None, min_seconds=0.5):
    n, total = 0, 0.0
    while total < min_seconds:
        arg = setup()
        t0 = time.perf_counter(); fn(arg); total += time.perf_counter() - t0; n += 1
    print(f"{name:<32} {n:>8} {total / n * 1e9:>14.0f} ns/op")


def pworld(seed):
    r = random.Random(seed)
    nodes = {f"node-{i}": recorder(f"node-{i}", r.choice([48, 64, 80]), frozenset(r.sample(VLANS, r.randint(1, 3))))
             for i in range(1, 5)}
    cams = {c: Camera(c, r.choice([1.0, 1.0, 1.0, 2.0, 8.0]), frozenset([r.choice(VLANS)]) if r.random() < 0.5 else frozenset())
            for c in range(1, 121)}
    return nodes, cams


payload = bytes(64 << 10)
bench("SigV4Sign", lambda _: sign("PUT", "minio.cluster:9000", "/restore/node-3/rev-812", "", {}, payload,
                                  ACCESS, SECRET, "us-east-1", MAY24))
v = FakeVariables()
bench("NextEpochCAS", lambda _: next_epoch(v, "node-3"))

v2 = FakeVariables()
for n in range(1, 1001):
    v2.put(f"nodes/node-{n}", {"node": f"node-{n}", "config": "x", "revision": 1,
                               "cameras": ",".join(str(n * 100 + c) for c in range(20))})
    v2.put(f"nodes/node-{n}/epoch", {"epoch": 1})
d = Directory(v2, ttl=0)
bench("DirectoryScan1000recorders", lambda _: (d.scan(force=True), d.where(70007)))

seed = [0]
def place_world(arg):
    nodes, cams = arg
    p = Placer(nodes, FakeVariables())
    for c in sorted(cams):
        p.place(cams[c], cams)
def next_world():
    seed[0] += 1; return pworld(seed[0])
bench("Place120Cameras", place_world, setup=next_world)

bench("ParseSegmentPath", lambda _: parse("/data/archive/7/e5/20260908T091000Z.mp4", "/data/archive"))

rows = [{"id": i, "site_id": "hq", "name": f"cam{i}", "rtsp_url": f"rtsp://10.0.0.{i}/s", "cred_username": None,
         "cred_secret": None, "enabled": True, "retention_days": 30, "priority": 100, "revision": i} for i in range(1, 201)]
bench("EncodeDecode200Cameras", lambda _: decode(encode([{"id": "hq", "name": "hq"}], rows, [], [])))
