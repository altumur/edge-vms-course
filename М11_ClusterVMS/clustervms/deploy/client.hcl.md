# client.hcl — Lesson 1: a Nomad client agent on an appliance server

**Role.** The agent configuration for the client side of Nomad on each of the three appliance servers (`nomad agent -config client.hcl`; on the bench each box runs `server.hcl` and this one together — the comment says so). It declares the datacenter, where Nomad keeps state and plugins, which servers to join, the two `meta` keys the VMS jobs depend on, and the Podman driver. The `meta` block is the piece of this file the Python code reads: `labels` is what a worker reports in its heartbeat and the controller places by; `archive` is what pins the `resource` job (and the `console` and `vmsworker` jobs) to servers with disks.

## Stanza by stanza

### top level
- `datacenter = "room-a"` — the datacenter every jobspec's `datacenters = ["room-a"]` selects; must match `server.hcl`.
- `data_dir = "/data/nomad"` — allocation directories, the client state DB; on the data partition, not the rootfs, as in М9.
- `plugin_dir = "/data/nomad/plugins"` — where external task drivers are found; the comment names the two that live there, `nomad-driver-podman` and `exec2`, because neither is built into the Nomad binary.

### `client`
- `enabled = true` — this agent runs allocations.
- `servers = ["10.0.0.11:4647", "10.0.0.12:4647", "10.0.0.13:4647"]` — the three servers' RPC addresses (port 4647), the same hosts `server.hcl`'s `retry_join` names; a client needs only one reachable to join.
- `meta { labels = "vlan:cctv-a,vlan:cctv-b" }` — a comma-separated list of what this server's NICs can reach. It is exported to every task on this node as `NOMAD_META_labels`; `vms.worker.labels_from_environment` splits it, the worker puts it in its heartbeat, and the controller's `labels-subset` constraint (`vms.subsystem.yaml`) places a camera only on a worker whose server's labels cover the camera's. Per server: the tests model `srv-a` = `cctv-a`, `srv-b` = both, `srv-c` = `cctv-b`.
- `meta { archive = "/data/archive" }` — declares that this server carries a resource with disks. Its *presence* is what the jobspecs test (`constraint { attribute = "${meta.archive}" operator = "is_set" }`); its value is the archive root the `resource` and `console` jobs mount. `verify-bench.sh` item 2 counts servers that declare it.

### `plugin "nomad-driver-podman"`
- `config { socket_path = "unix:///run/podman/podman.sock" }` — the rootful Podman API socket the driver talks to; the comment gives the enabling command, `systemctl enable --now podman.socket`. Every task here uses `driver = "podman"`, so `verify-bench.sh` item 2 checks the driver is healthy on every node.

## Notes
- `NOMAD_NODE_NAME` (the worker's and resource's `server`) comes from the agent's `name`, which is not set here, so it defaults to the hostname — `srv-a`, `srv-b`, `srv-c` in the tests' vocabulary.
- Nothing here mentions Consul; the jobspecs' `service` blocks therefore need `provider = "nomad"` to register without one (see the jobspec notes).
