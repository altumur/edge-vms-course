# Lesson 10 — A Cluster of One

**Module:** DomainVMS — the smallest layer above a set of clusters (Module 12)
**You will build:** a camera that runs the platform as a cluster of its own — one store on its flash, one writer, a unit pinned to its hardware, no controller, its own epoch — publishing exactly what a cluster publishes, so that the directory, the read view, the forwarded write, the agent and Lesson 9's kept edits treat it like any server room. And the two things that are new because it is a device: flash that wears, and a boot whose order is a correctness question.
**Time:** ~90 minutes.

## Why this lesson exists

The vendor this part is written for makes cameras, and the code in them is the same platform that runs on the servers. Two deployments follow: cameras **with** a server room, and cameras **alone** — no server, no NAS, footage on SD cards. The design brief for them ([`КАМЕРЫ-НА-ПЛАТФОРМЕ.md`](../КАМЕРЫ-НА-ПЛАТФОРМЕ.md)) started by inventing mechanisms: a routing store, a change stream, per-unit ACLs, a "primary camera" with copies of everything on every other. Laid against this module, most of them turned out to be the domain under another name. What was left is the question this lesson answers: **what is a camera, to the domain?**

Not a worker in somebody's cluster. A cluster in М11's sense is servers close enough to share a raft, an orchestrator that restarts what dies, and units that move from one server to another. A set of cameras has none of the three: nothing moves off a camera, because the video comes from its own sensor. Treat all the cameras as **one** cluster and they need one consistent store among them — raft among cameras, on flash, over PoE that fails, which the brief refused on good grounds.

So each camera is **its own cluster**: a cluster of one. Its store is consistent for free — one flash, one writer. The domain's founding rule, *no raft spans clusters*, is exactly the brief's *no consensus between cameras*. And everything the domain already does — find a camera, forward an edit, carry grants, keep an edit for a member that is off — applies unchanged, provided the camera publishes what a cluster publishes. This lesson makes sure it does, and finds the two places where a device is not just a small server.

> **What you can verify without hardware.** Everything, in `tests/test_lesson10_cluster_of_one.py`: a server room and two cameras in one domain, found by serial; the epoch across reboots; a day of heartbeats against a flash-write counter; a camera's store after its agent has synced; an edit forwarded to a camera and refused for a subject with no grant; the boot order, by hand, in the wrong order and the right one; and Lesson 9 end to end on a real device.

## Prerequisites

- **Lesson 1** — the directory of directories, `ref` as the domain's name for a camera, and a complete answer versus an incomplete one.
- **Lesson 3** — the read view over heartbeats and snapshot shards, and the forwarded write.
- **Lesson 4** — the agent that writes `domain/*` and nothing else, and grants checked by the cluster's console.
- **Lesson 9** — an edit kept for a cluster that is off.
- **М10A** — the epoch (`w2cplatform/epoch.py`), and the heartbeat and snapshot shapes (`vms/heartbeats/<worker>`, `vms/snapshot/<worker>`).

## Learning objectives

1. Say why a camera is a cluster of its own, and why "all cameras, one cluster" is raft among cameras.
2. List what a device must publish for the domain to read it unchanged — and what it need not have.
3. Take an epoch with no one to fence, and say why it must still grow on every boot.
4. Put what changes every few seconds in RAM, and count what reaches flash.
5. Order a boot so that a camera never gives the domain a complete answer that is false.

---

## Step 1 — What the domain reads, and nothing more

The domain reads two things from a cluster, and has since Lesson 1: its workers' heartbeats under `vms/heartbeats/`, and its snapshot shards under `vms/snapshot/`. It never reads rows. So the whole contract between a camera and the domain is two objects:

```python
    def publish(self) -> None:
        now, row = self.wall(), self.row()
        rev = int(row.get("revision", 1))
        status = {"id": 1, "ref": self.serial, "name": row.get("name", ""), "enabled": True, "phase": "running",
                  "position": "converged", "revision": rev, "observed_revision": rev, "epoch": self.epoch}
        hb = {"worker": self.serial, "ts": now, "server": self.serial, ..., "status": [status],
              "live_url": f"rtsp://{self.address}/live", "playback_url": f"http://{self.address}/playback",
              "coverage": self.coverage}
        snap = {"cluster": self.name, "worker": self.serial, "ts": now,
                "cameras": [{**row, "worker": self.serial, "server": self.serial}]}
        self.ram.put(f"vms/heartbeats/{self.serial}", json.dumps(hb).encode())
        self.ram.put(f"vms/snapshot/{self.serial}", json.dumps(snap).encode())
```

The worker is the camera, and so is the server. That is not a trick: the heartbeat names the server so that the read view can group silence by failure domain (Lesson 3), and a camera's failure domain is the camera.

The first test puts a server room — М11's real controller and worker — and two cameras in one domain and asks for camera `SN4471`. The answer comes back from `cam-SN4471`, on worker `SN4471`, on server `SN4471`, complete. Nothing in `federation.py` or `readview.py` was written for devices.

## Step 2 — A pinned unit, and the keys nobody writes

In a server cluster a controller places a camera on a worker: it writes an assignment, the worker claims a slot, takes a lease. On a camera there is nothing to decide — the camera **is** the unit. So there is no controller, and the keys a placer would write simply do not exist. The test lists the camera's store after its agent has synced:

```
domain/grants    vms/cameras/1    vms/epoch/1
```

Its row, its epoch, and what the agent carried. No slot, no assignment — not forbidden by an ACL, just absent, because it is nobody's job to write them.

The row is made **once**, at the first boot, by the camera's own console, with `ref` set to its serial. The cluster-local id stays `1`, an integer, as in every cluster: the domain has always asked by `ref`, and making the serial the id would ripple through every integer camera id below, the event index's `cam INTEGER` among them.

## Step 3 — An epoch with no one to fence

The epoch exists to fence: when two instances of a worker think they hold a camera, the newer epoch wins (М10A Lesson 1). A camera has no second instance. Why take one?

Because the epoch is also in the **paths**. The archive is `rec/<cam>/e<epoch>/`, the event buckets are `vms/<unit>/e<epoch>/…`, and a reboot must never write into the directory of the life before it. So the camera takes its epoch itself, once per boot, from its flash, by the same CAS every worker uses:

```python
    def boot(self, first_name: str | None = None) -> int:
        self.door_open = False
        self.ram = Ram()                                 # what lived in RAM did not survive, and should not have
        self.epoch, _ = next_epoch(self.flash, EPOCH)
        if self.flash.get(ROW)[0] is None:
            self._put_row({"id": 1, "ref": self.serial, "name": first_name or self.serial, "revision": 1}, cas=0)
        ...
```

A crash straight after `next_epoch` costs a number and nothing else; the next boot takes the one after it.

## Step 4 — What flash can afford

A heartbeat every ten seconds is 8 640 writes a day. On a server's SSD that is noise. On a camera's flash, rated in erase cycles and expected to last the life of the camera, it is the thing that kills it.

So the camera has two stores, and the split is by **how often** a value changes. `Flash` holds the row, the epoch, and what the agent carries — values that change a few times a day at most. `Ram` holds the heartbeat and the snapshot shard, which change every pass and are worthless after a reboot anyway: a heartbeat from before the reboot is a lie about now. The domain reads both through the camera's door, so it cannot tell the difference and does not need to.

The test runs a day: 8 640 publishes and 2 880 agent passes. Flash takes two writes at boot (the epoch, the row) and one more when the grants first arrive. The agent's `_carry` compares before it writes, which is what keeps 2 879 unchanged passes free.

## Step 5 — The door opens after the first publish

A server cluster is never rebooted whole: some server is always up and publishing. A camera is rebooted whole every time. For the seconds between power and the first publish its RAM is empty — and if its door is open in those seconds, it answers the domain truthfully: *I hold no cameras.*

That is a **complete** answer. Lesson 1 made the domain believe complete answers: *in no cluster of the domain* is a 404, and an edit sent at that moment is refused as if the camera did not exist. The test does it by hand — power, an open door, nothing published — and gets exactly that 404.

With the door closed until the publish, the same seconds are a cluster that **did not answer**: an incomplete answer, and Lesson 9 keeps the edit. So the boot order is the lesson's one rule that a server room never needed:

```python
        self.boots += 1
        self.publish()
        self.door_open = True
```

The epoch first, because everything after it carries it. The row, if this is the first boot. The publish. And only then the door.

## Step 6 — Its console, and its only link up

A camera's own web page **is** its cluster's console: it writes the row. So is the domain's forwarded edit — it arrives at the same `update_camera`, with the subject's name, and the camera checks that subject against the grants **its** agent carried, exactly as a server cluster's console does (Lesson 4). A subject with no grant gets 403 from the camera, not from the domain.

The agent is the camera's only link upward, and it is Lesson 4's agent unchanged: keys, revocations, this cluster's grants, and Lesson 9's kept edits. The last test runs Lesson 9 end to end on a device: the camera goes off, the edit is kept beside its grants, the camera boots, its agent carries the edit home, its console applies it as the operator, and the domain clears what landed.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| An edit for a camera that is rebooting is refused with 404 | The door opened before the first publish; the domain got a complete, empty answer. Publish first. |
| Footage from after a reboot lands in the previous epoch's directory | The epoch is kept in RAM or taken once at manufacture. Take it from flash, by CAS, on every boot. |
| Cameras in the field die after two or three years | Heartbeats or snapshots are written to flash. Count flash writes per day in a test and put a number on it. |
| The domain lists a camera as *configured — no worker reports it* | The snapshot shard is published, the heartbeat is not (or under another name). Both, named by the serial. |
| A camera appears twice in the directory | Its `ref` changed — somebody made the id the serial on one path and not another. `ref` is the serial; the id is `1`. |

## Recap

- A camera is a cluster of its own; "all cameras, one cluster" is raft among cameras, which the brief refused.
- The domain reads a heartbeat and a snapshot shard. A device that publishes both, named by its serial, is read like a server room.
- The unit is pinned: no controller, no slot, no assignment — absent, not forbidden.
- The epoch fences nothing and is still taken on every boot, because the paths carry it.
- RAM for what changes every pass, flash for what changes a few times a day — counted.
- The door opens after the first publish, or the domain is told a complete answer that is false.
- The camera's console decides edits against its own grants; the agent is its only link up.

## Exercises

1. Make `boot` open the door first and run the test suite. Which test fails, and what exactly would the operator have seen?
2. A camera keeps its configuration on a 1 MiB flash partition of 4 KiB blocks, rated for 10 000 erase cycles each, and a small write costs about three block erases once the file system's journal is counted. Compute the partition's lifetime if every heartbeat reached it (about a hundred days), and with the design as built.
3. A sixteen-channel recorder that runs the platform is one device and sixteen cameras. Which parts of `DeviceCluster` change — the id, the snapshot, the epoch key — and which stay? (The brief's answer: rows by camera, `vms/devices/<device>` by device.)
4. The camera's agent syncs every 30 s. What does a camera that has been off for a week do in its first pass, in what order, and what is the worst case for flash writes?

## Where this is going

One camera is a member like any other. [**Lesson 11**](11-hundreds-of-small-members.md) takes three hundred of them, a tenth of them off, and measures what the domain's read view, directory and forwarded write cost at that shape — which the module promised and never measured.
