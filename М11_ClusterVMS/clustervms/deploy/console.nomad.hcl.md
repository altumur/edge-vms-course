# console.nomad.hcl — the console job: the page and the API, one instance on every server that runs a resource; no index of its own

**Role.** Lesson 2/5. The jobspec for `python3 -m cluster console` (see `../cluster/__main__.py.md` and `../cluster/console.py.md`). A `system` job: one allocation on every eligible server, so any server's `:8080` *is* the console and nothing sits in front of it — no load balancer, no ingress; a person types any server's name or a DNS name resolving to all of them. The header comment explains why that is safe: the console is stateless — every instance reads the same raft and the same heartbeats, rebuilds its event index from the resources on start, and a retried POST is answered the same by whichever instance gets it because the Idempotency-Key lives in `vms/idem/*`. It is placed on servers with a resource so an operator's marks have a bucket to go into; drop the constraint and `/marks` answers 503 there. Its token is `console-policy.hcl`.

## Stanza by stanza

### `job "console"`
- `datacenters = ["room-a"]` — the datacenter from `client.hcl`.
- `type = "system"` — one allocation per eligible node, added automatically when a node joins; no `count`.

### `group "console"`
- `constraint { attribute = "${meta.archive}"  operator = "is_set" }` — only servers whose `client.hcl` declares `meta.archive`, i.e. servers with a resource; the same constraint the `resource` and `vmsworker` jobs carry, so console, resource and workers are always co-located. This is why `__main__.console` can pass `archive_root=archive` for marks.
- `network { mode = "host"  port "console" { static = 8080 } }` — host networking and a fixed port 8080 on every server, so the address is predictable; `static` reserves it with the scheduler.

### `task "console"`
- `driver = "podman"` — the Podman driver from `client.hcl`.
- `identity { env = true }` — `NOMAD_TOKEN` in the environment, scoped by the policy bound to job `console`.
- `config.image = "localhost/clustervms:latest"` — the image from `Containerfile`, built on every server.
- `config.network_mode = "host"` — the container shares the host network (the port above, and `127.0.0.1:4646` for Variables).
- `config.args = ["python3", "-m", "cluster", "console"]` — the `console` verb.
- `config.volumes = ["/data/archive:/data/archive"]` — this server's archive, read-write, so the console's `console/<instance>/…` marks bucket is written into the resource's tree (and the resource job then serves and mirrors it).
- `env.OBJECTS = "variables://objects"` — heartbeats and the snapshot as Variables (`objectstore.open_store`).
- `env.ARCHIVE = "/data/archive"` — the `archive` path `__main__` checks with `os.path.isdir` before passing it as `marks_root`.
- `env.CONSOLE_PORT = "8080"` — must match the `port` block; `__main__` reads it (host defaults to `0.0.0.0`).
- `env.CLUSTER = "room-a"` — the cluster name the console's `ClusterController` carries (the snapshot's `cluster` field, though the console does not publish it).
- `service { name = "vms-console"  port = "console"  tags = ["metrics"] }` — registers each instance so that, as the comment says, the autoscaler's Prometheus scrapes it (the `metrics` tag is what a Prometheus service-discovery job would select), М12's read model finds it, and a browser resolves it.
- `resources { cpu = 300  memory = 128 }` — 300 MHz, 128 MB: the page and the API; the event database moved into the resource job.

## Notes
- No `EVENTDB` here any more: the console holds no index. `/events` asks every live resource's `GET /events` (each resource's own `EventDatabase`) and merges — see `resource.nomad.hcl.md`.
- `CAPACITY` is not set; the console's controller uses the fallback 50 only for a worker whose heartbeat carries no capacity.
- The `service` block has no `provider`; Nomad's default provider is Consul, and nothing in `server.hcl`/`client.hcl` configures Consul. Without `provider = "nomad"` the job will not register (and `nomad job run` reports a missing Consul), which also affects `verify-bench.sh` item 5a's `nomad service info` for the `resource` service.
- The two-attributes-on-one-line `resources` block is discussed in `autoscaler.nomad.hcl.md`.
