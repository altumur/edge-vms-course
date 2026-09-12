"""Lesson 3 — a recorder's state outlives its server: the rehydration sequence,
against fakes, and the RPO measured rather than promised.

    Server A dies
      └─ Nomad reschedules recorder 3's allocation → Server B
           1. empty Postgres; migrations run
           2. read its own Nomad Variable — "I am recorder 3; my configuration
              is object node-3/rev-812, and these are my camera ids"
           3. fetch that object from the CLUSTER's object store
           4. restore it; check the revision against the Variable
           5. request a new epoch
           6. begin recording into epoch-N+1

Publication order matters: object store FIRST, then the Variable — so a
Variable never points at an object that is not there.
"""
from __future__ import annotations

import json
import random

from variables import Variables, next_epoch


class ObjectStore:
    """Cluster-scoped, durable, never queried: put/get by key."""
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.puts = 0

    def put(self, key: str, data: bytes):
        self.objects[key] = data; self.puts += 1

    def get(self, key: str) -> bytes | None:
        return self.objects.get(key)


class Postgres:
    """The recorder's own database: configuration rows with a revision."""
    def __init__(self):
        self.cameras: dict[int, dict] = {}
        self.revision = 0

    def edit(self, cam_id: int, **fields):
        self.cameras.setdefault(cam_id, {"id": cam_id})
        self.cameras[cam_id].update(fields)
        self.revision += 1
        return self.revision

    def dump(self) -> bytes:
        return json.dumps({"revision": self.revision, "cameras": self.cameras}, sort_keys=True).encode()

    def restore(self, blob: bytes):
        d = json.loads(blob)
        self.revision, self.cameras = d["revision"], {int(k): v for k, v in d["cameras"].items()}


class NodeInstance:
    def __init__(self, node_id: str, vars_: Variables, store: ObjectStore):
        self.node_id, self.vars, self.store = node_id, vars_, store
        self.db = Postgres()                        # step 1: empty
        self.epoch = 0
        self.published_rev = 0
        self.acked: list[tuple[int, str]] = []      # (revision, what the operator was told)

    # -- normal operation ------------------------------------------------
    def save_camera(self, cam_id: int, **fields) -> str:
        rev = self.db.edit(cam_id, **fields)          # acknowledge on local commit...
        msg = "saved · not yet replicated"           # ...and SHOW durability
        self.acked.append((rev, msg))
        return msg

    def publish(self):
        """The one-way publication upward. Object first, Variable second."""
        key = f"{self.node_id}/rev-{self.db.revision}"
        self.store.put(key, self.db.dump())
        items, idx = self.vars.get(f"nodes/{self.node_id}")
        self.vars.put(f"nodes/{self.node_id}", {
            "node": self.node_id, "config": key, "revision": str(self.db.revision),
            "cameras": ",".join(str(c) for c in sorted(self.db.cameras))}, cas=idx)
        self.published_rev = self.db.revision
        self.acked = [(r, "saved · replicated" if r <= self.published_rev else m) for r, m in self.acked]

    # -- failover ----------------------------------------------------------
    def rehydrate(self) -> dict:
        items, _ = self.vars.get(f"nodes/{self.node_id}")     # step 2
        if items is None:
            return {"state": "unconfigured"}                 # never invent a configuration
        blob = self.store.get(items["config"])                # step 3
        assert blob is not None, "a Variable must never point at a missing object"
        self.db.restore(blob)                                 # step 4
        assert self.db.revision == int(items["revision"]), "revision disagrees with the Variable"
        self.epoch, _ = next_epoch(self.vars, self.node_id)   # step 5
        return {"state": "restored", "revision": self.db.revision, "epoch": self.epoch,
                "cameras": sorted(self.db.cameras)}           # step 6 follows


def measure_rpo(publish_interval: float, edits_per_hour: float, trials: int, seed: int = 1):
    """Operator edits arrive at random; recorder publishes every publish_interval
    seconds; the server dies at a random moment. What did the operator see
    acknowledged, and what came back?"""
    r = random.Random(seed)
    lost_edits, lost_seconds, worst = 0, 0.0, 0.0
    for _ in range(trials):
        vars_, store = Variables(), ObjectStore()
        n = NodeInstance("node-3", vars_, store)
        n.save_camera(1, name="lobby"); n.publish()           # a seen recorder
        t, next_pub = 0.0, publish_interval
        death = r.uniform(0, 3600)
        last_edit_t = None
        while t < death:
            t += r.expovariate(edits_per_hour / 3600)
            if t >= death:
                break
            while next_pub <= t:
                n.publish(); next_pub += publish_interval
            n.save_camera(r.randint(1, 50), retention_days=r.choice([7, 14, 30]))
            last_edit_t = t
        while next_pub <= death:
            n.publish(); next_pub += publish_interval
        told_saved = len(n.acked)
        n2 = NodeInstance("node-3", vars_, store)
        r2 = n2.rehydrate()
        survived = r2["revision"]
        lost = n.db.revision - survived
        lost_edits += lost
        if lost:
            window = death - (next_pub - publish_interval)
            lost_seconds += window; worst = max(worst, window)
    return lost_edits, worst


if __name__ == "__main__":
    vars_, store = Variables(), ObjectStore()
    n = NodeInstance("node-3", vars_, store)
    print(n.save_camera(7, name="lobby", rtsp_url="rtsp://10.0.0.41/s"))
    n.publish()
    print(f"published rev {n.published_rev}: object {list(store.objects)[-1]}, Variable -> {vars_.get('nodes/node-3')[0]['config']}")
    print(n.save_camera(7, retention_days=14), "   <- rev 2, not yet published")
    print("\n-- Server A dies. recorder 3 is rescheduled to Server B --")
    n2 = NodeInstance("node-3", vars_, store)
    print(json.dumps(n2.rehydrate()))
    print(f"camera 7 came back with retention_days={n2.db.cameras[7].get('retention_days', 30)} "
          f"— the rev-2 edit the operator saw as 'not yet replicated' is gone. That is the RPO.")
    print("\n-- a recorder the directory has never seen --")
    print(json.dumps(NodeInstance("node-9", vars_, store).rehydrate()))
    print("\n-- RPO measured: 1000 random deaths, 20 edits/hour --")
    for interval in (300, 60, 10, 2):
        lost, worst = measure_rpo(interval, 20, 1000)
        print(f"publish every {interval:>3}s: {lost:4d} edits lost across 1000 failovers; "
              f"worst window {worst:5.1f}s")
