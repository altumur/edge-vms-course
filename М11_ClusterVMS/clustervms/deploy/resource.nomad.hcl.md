# resource.nomad.hcl — the resource: a PLATFORM system job, one allocation on every server that declares `meta.archive`

**Role.** Lesson 2/3. The jobspec for `python3 -m cluster resource` (see `../cluster/__main__.py.md`, `../cluster/resource.py.md`). A `system` job pinned to servers with disks for as long as the server exists: it serves every subsystem's buckets, takes mirrors from its peers, retains buckets by each subsystem's own policy, and runs the passes subsystems register on it (the VMS: manifests, media retention). The header's "No controller" — nothing places a resource; the disk is where it is. Its heartbeat is `platform/resources/<server>`; its token is `resource-policy.hcl`.

## Stanza by stanza

### `job "resource"`
- `datacenters = ["room-a"]` — from `client.hcl`.
- `type = "system"` — one allocation per eligible node.
- `constraint { attribute = "${meta.archive}"  operator = "is_set" }` — at job level (the console job puts the same constraint in the group; both scope every group). Only nodes whose `client.hcl` declares `meta.archive`.

### `group "resource"`
- `network { mode = "host"  port "manifests" { static = 8090 } }` — host network, fixed port 8090: the port `RESOURCE_URL` below advertises and `__main__` defaults `RESOURCE_PORT` to.

### `task "resource"`
- `driver = "podman"` — the Podman driver.
- `identity { env = true }` — `NOMAD_TOKEN` scoped by the policy bound to job `resource`.
- `config.image = "localhost/clustervms:latest"` — the local image.
- `config.network_mode = "host"` — shares the host network.
- `config.args = ["python3", "-m", "cluster", "resource"]` — the `resource` verb.
- `config.volumes = ["/data/spool:/data/spool", "/data/archive:/data/archive"]` — the same disks the worker on this server writes: the spool (so `ArchiveResource` can see closed segments) and the archive (buckets, manifests, mirrors under `.mirror/<server>/`), both read-write.
- `env.OBJECTS = "variables://objects"` — the comment: its heartbeat as a Variable; no MinIO on this cluster.
- `env.RESOURCE_URL = "http://${attr.unique.network.ip-address}:8090"` — the node's IP interpolated by Nomad: the URL in the heartbeat, where peers PUT mirrors, the index reads buckets, and the console fetches `/manifest` and `/segment`. An IP rather than a name so no DNS is needed between servers.
- `env.NOMAD_NODE_NAME = "${node.unique.name}"` — the server's name for the heartbeat and the mirror directory; Nomad already exports `NOMAD_NODE_NAME` to every task, so this makes it explicit and lets a bench override it in the jobspec.
- `service { name = "resource"  port = "manifests" }` — the comment: peers find each other here, and `verify-bench.sh` item 5a uses `nomad service info -json resource` to pick a resource to PUT a mirror probe against. (In the code, peers actually find each other through `resources_seen(objects)` — the heartbeats — not the service catalog.)
- `resources { cpu = 200  memory = 256 }` — 200 MHz, 256 MB: an HTTP file server plus a ten-minute policy pass.

## Notes
- No `SPOOL`/`ARCHIVE` env: `__main__` defaults them to `/data/spool` and `/data/archive`, which the volumes bind.
- The `service` block has no `provider = "nomad"`; see `console.nomad.hcl.md`.
- Because this job is `system` and the worker job carries the same `meta.archive` constraint, a worker's `ARCHIVE` is always served by a resource on the same server — "a worker records into the resource on its own server".
