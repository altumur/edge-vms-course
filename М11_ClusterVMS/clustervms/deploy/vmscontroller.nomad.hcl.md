# vmscontroller.nomad.hcl — the controller job: one instance, safe at two, no port

**Role.** Lesson 2/5. The jobspec for `python3 -m cluster controller` (see `../cluster/__main__.py.md`, `../cluster/controller.py.md`). The header comment carries the design: `count = 1` is economy, not correctness — correctness is CAS on raft, and a second instance would only repeat the same five-second pass (`test_two_controllers_agree_under_constraints`). It has no HTTP because nothing asks it anything (the console is a separate job with its own token), and no Nomad client: it needs no more of the API than any task gets — Variables under its own prefixes, `vmscontroller-policy.hcl`. It is the only job here with no `constraint`: a controller can run on any server, disks or not.

## Stanza by stanza

### `job "vmscontroller"`
- `datacenters = ["room-a"]` — from `client.hcl`.
- `type = "service"` — long-running; if its node dies Nomad reschedules it elsewhere and the new instance simply runs the next pass from raft — it holds nothing.

### `group "vmscontroller"`
- `count = 1` — one pass every five seconds is enough; see the header.
- No `network` block — no port; no `constraint` — any node.

### `task "vmscontroller"`
- `driver = "podman"` — the Podman driver.
- `identity { env = true }` — `NOMAD_TOKEN`, scoped by the policy bound to job `vmscontroller`: write on `vms/workers/*`, `vms/placement/*`, `vms/slots/*`, `objects/vms/snapshot`; read everything.
- `config.image = "localhost/clustervms:latest"` — the local image.
- `config.network_mode = "host"` — only so `127.0.0.1:4646` reaches the local Nomad agent for Variables.
- `config.args = ["python3", "-m", "cluster", "controller"]` — the `controller` verb.
- No `volumes` — the controller touches no disk; its state is raft.
- `env.OBJECTS = "variables://objects"` — the comment: heartbeats and the snapshot as Variables, no MinIO on this cluster.
- `env.CLUSTER = "room-a"` — the name the snapshot carries (`snap["cluster"]`), what М12 keys its aggregate by. `__main__` defaults to `cluster-a` if unset.
- `resources { cpu = 200  memory = 128 }` — a five-second loop over a few hundred Variables.

## Notes
- `CAPACITY` is not set (fallback 50 in `__main__`), and it only matters for a worker whose heartbeat has not reported its own capacity.
- Failover of the controller itself is not measured anywhere: while it is being rescheduled, workers keep recording from their last assignment and the console keeps writing rows; only new placements wait.
- The one-line `resources` block: see `autoscaler.nomad.hcl.md`.
