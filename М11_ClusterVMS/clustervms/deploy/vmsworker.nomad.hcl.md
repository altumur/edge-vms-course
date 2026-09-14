# vmsworker.nomad.hcl — the worker as a service job: `count = N`, the autoscaler moves N, each allocation claims its slot by CAS

**Role.** Lesson 2 (and 4 for `disconnect`). The jobspec for `python3 -m cluster worker` (see `../cluster/__main__.py.md`, `../cluster/worker.py.md`) — DriverPack as a service job, the cluster's replacement for М10's `vmsworker@.container` template (`../../../М10_ServerVMS/vmsserver/deploy/vmsworker@.container.md`). The header's rule: `count = N` and *nothing in the VMS decides N* — the operator sets the bounds, the Nomad Autoscaler moves `count` from the workers' own load, the controller never does. Each allocation claims slot `w-<NOMAD_ALLOC_INDEX>` by CAS on `vms/slots/w-<n>`: the index is the preference, the Variable is the proof. Its token is `vmsworker-policy.hcl`.

## Stanza by stanza

### `job "vmsworker"`
- `datacenters = ["room-a"]` — from `client.hcl`.
- `type = "service"` — long-running, rescheduled on node loss (the `disconnect` block below says how).

### `group "vmsworker"`
- `count = 2` — the starting number of workers; the autoscaler owns it afterwards. Allocation indexes 0..count-1 become slots `w-0`, `w-1`.

### `scaling`
- `enabled = true` — the group may be scaled by the API (`nomad job scale`, and the autoscaler).
- `min = 1` — never fewer than one worker.
- `max = 12` — the comment: the servers' budget, B + n·I from М9 Lesson 7 — how many workers of `resources` below the three servers can carry.
- `policy.cooldown = "5m"` — the comment: longer than a failover, so a reschedule (a replacement worker briefly reporting full load or none) is not read as demand.
- `policy.evaluation_interval = "1m"` — how often the autoscaler evaluates.
- `check "load" { source = "prometheus"  query = "avg(vms_worker_load)" }` — the metric is assigned ÷ capacity across live workers, from the heartbeats via the console's `/metrics` (the comment: never CPU — a worker's CPU says nothing about how many cameras it still has room for). `source` names the `apm "prometheus"` plugin in `autoscaler.nomad.hcl`.
- `strategy "target-value" { target = 0.9 }` — keep average load at 0.9: scale out when the fleet is fuller than 90 %, in when emptier; matches the `strategy "target-value"` plugin in the autoscaler config.

### `constraint`
- `attribute = "${meta.archive}"  operator = "is_set"` — the comment: a worker records into the resource on its own server, so only servers that have one.

### `disconnect`
Lesson 4 — the comment: the defaults are wrong for a worker.
- `lost_after = "45s"` — how long a client may be unreachable before its allocations are considered lost and replaced. 45 s is the design's number: the worker's lease TTL is 30 s with a 5 s margin, so the holder has already stopped writing at 25 s on its own clock before Nomad starts a replacement (`test_the_lease_stops_writing_before_the_replacement_may_start`); the same 45 s is `lost_after` in `merged_timeline` and the platform's `Resource`.
- `replace = true` — start a replacement allocation elsewhere once lost; that replacement, with the same index, claims `w-<n>` and reads the assignment — the failover of `test_the_power_pull` (48 s = 45 + a schedule).
- `stop_on_client_after = "25s"` — a disconnected client stops the allocation itself after 25 s; the comment: the holder stops at TTL − margin on its own clock anyway, so this only makes Nomad agree with the worker.
- `reconcile = "best_score"` — when the disconnected client returns and both the original and the replacement exist, keep the better-placed one and stop the other. Either way the loser is already fenced at the slot and the epochs (`test_the_old_instance_wakes_up…`), so the choice is about economy.

### `task "vmsworker"`
- `driver = "podman"` — the Podman driver.
- `kill_timeout = "20s"` — the comment: room to release the slot — `release_slot()` on SIGTERM is what makes a scale-in "redistribute" rather than "a crash". Same 20 s as М10's `StopTimeout=20`, which also lets `splitmuxsink` finalize its open segment. `verify-bench.sh` item 6 checks `released = true` after a scale-in.
- `identity { env = true }` — the comment: `NOMAD_TOKEN`, the task's own workload identity, scoped by the policy.
- `config.image = "localhost/clustervms:latest"` — the local image.
- `config.network_mode = "host"` — the local Nomad agent at `127.0.0.1:4646`, and camera RTSP on the server's VLANs.
- `config.args = ["python3", "-m", "cluster", "worker"]` — the `worker` verb.
- `config.volumes = ["/data/spool:/data/spool", "/data/archive:/data/archive", "/data/media:/data/media"]` — the spool `archivesink` writes, the archive it promotes into (the resource's tree on this server), and the media files `driverpack://file/<name>` plays. Unlike М10's unit, `/data/media` is not `:ro` here.
- `env.OBJECTS = "variables://objects"` — heartbeats as Variables; the comment says no MinIO on this cluster.
- `env.CAPACITY = "50"` — the comment: this server's number (cameras it can carry), per node class in a product. The worker reports it in its heartbeat and the controller places by that report, not by this file.
- `resources { cpu = 2000  memory = 2048 }` — the comment: B + n·I, rounded up — 2 GHz and 2 GB for 50 cameras' pipelines.

## Notes
- `NOMAD_ALLOC_INDEX`, `NOMAD_NODE_NAME`, `NOMAD_META_labels`, `NOMAD_ALLOC_ID` are not set here; Nomad exports them to every task, and `client.hcl`'s `meta.labels` is what `NOMAD_META_labels` carries.
- `SPOOL`/`ARCHIVE`/`SEGMENT_SECONDS` are left to `__main__`'s defaults (`/data/spool`, `/data/archive`, 600).
- Nomad's duplicate-index case (issue #10727) is harmless by design: two allocations with one index resolve at the CAS on the slot row (`test_two_allocations_with_one_index_resolve_at_the_cas`).
- The one-line `resources` block: see `autoscaler.nomad.hcl.md`.
