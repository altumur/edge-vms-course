# device.py — Lesson 10: a camera that runs the platform as a cluster of its own, publishing exactly what a cluster publishes

**Role in the module.** Part two's member. A camera is not a worker in somebody's cluster (nothing on it can move) and "all cameras, one cluster" would be raft among cameras; so each camera is a cluster of one — one store on its flash, one writer. The domain reads a cluster's heartbeats and snapshot shards and nothing else, so a camera that publishes one of each, named by its serial, is read like a server room. What is new is physical: flash wears (heartbeats go to RAM), and a camera is rebooted whole (the door opens after the first publish). Used by the tests of Lessons 10–15 as the member; `alarms.EventDoor` and `term` use its door and durable store.

## Module-level names
- `ROW = "vms/cameras/1"`, `EPOCH = "vms/epoch/1"` — the camera's row (cluster-local id 1, `ref` = serial) and its epoch.

## `class Flash`
The camera's Variables, counting `writes` — reads are free, every put is an erase somewhere. `get`, `list`, `put` delegate.

## `class Ram`
A thread-safe dict object store (`put/get/list/delete`) for what changes every pass; replaced at every boot, because a heartbeat from before the reboot is a lie about now.

## `class _Door`
What the domain and neighbours reach a camera's store through: raises `Unreachable` while `door_open` is false (off, or booting).

## `class DeviceCluster`
### `__init__(self, serial, flash_vars, wall=time.time, name=None, reaches=(), address=None, disk=None)`
`name` defaults to `cam-<serial>`; `disk` is the durable object store (the card) — a `Ram` that is NOT reset at boot, standing in for one; `coverage` is what the card holds (Lesson 13).
### `boot(self, first_name=None) -> int`
The order is the lesson: door shut; fresh RAM; `next_epoch` from flash by CAS (grows every boot, a crash included); the row if this is the first boot ever; publish; THEN the door. Returns the epoch.
### `power_off(self)` — the door shuts.
### `row(self)`, `rec_prefix(self)` — the row (JSON in one item), and `rec/<serial>/e<epoch>/`.
### `publish(self)`
One heartbeat (`worker` = `server` = serial, the status entry with `ref` and `epoch`, `live_url`, `playback_url`, `coverage`) and one snapshot shard, both into RAM.
### `cluster(self, domain=False) -> Cluster`, `disk_door(self)` — the domain's handle (stores through the door; `domain=True` for a camera hosting the domain, Lesson 15), and the durable store through the door.
### `may(self, subject, capability)`, `update_camera(self, camera, fields, subject)`, `create_camera(...)`, `current(self, ref)`
The camera's console: accepts `1` or the serial, raises `Unreachable` when the door is shut (the domain reaches it over the network), checks a named subject against the grants its agent carried (403 otherwise; `None` is its own page), CAS-writes the row with `revision + 1`, publishes. `create_camera` is 409 — a device is one camera. `current` serves Lesson 9's agent.

## Notes
- `test_a_day_of_heartbeats_costs_the_flash_nothing`: 8 640 publishes and 2 880 agent passes; flash takes 2 writes at boot and 1 when grants arrive.
- `test_the_door_opens_after_the_first_publish_not_before` shows the wrong order by hand (a false complete answer, 404) and checks `boot` keeps the door shut during its publish.
