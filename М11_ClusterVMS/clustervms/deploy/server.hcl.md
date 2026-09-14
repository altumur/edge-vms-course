# server.hcl — Lesson 1: one of three Nomad servers, ACLs on

**Role.** The agent configuration for the server (raft) side of Nomad on each of the three appliance servers; on the bench each box loads this beside `client.hcl` (`nomad agent -config server.hcl -config client.hcl`). Three servers form the raft that holds Variables — the cluster's config store, the one that makes `vms/cameras/*`, `vms/workers/*`, `vms/epoch/*`, `vms/slots/*` and the `objects/…` heartbeats consistent. The header comment is the quorum rule: three tolerates one loss, five tolerates two; never two, never four (an even count adds a failure point without adding tolerance).

## Stanza by stanza

### top level
- `datacenter = "room-a"` — the datacenter name jobspecs select; the same as `client.hcl`.
- `data_dir = "/data/nomad"` — the raft log and snapshots, on the data partition.

### `server`
- `enabled = true` — this agent is a server (votes, holds raft).
- `bootstrap_expect = 3` — wait for three servers before electing a leader; with three boxes this is the whole cluster, and it prevents a lone first server from bootstrapping a one-node raft that later splits.
- `server_join { retry_join = ["10.0.0.11", "10.0.0.12", "10.0.0.13"] }` — the three servers' addresses (default serf port 4648); each keeps retrying until it has joined, so boot order does not matter. `client.hcl`'s `servers` lists the same hosts on the RPC port.

### `acl`
- `enabled = true` — Lesson 2: Variables are ACL'd, one writer per key. With this on, every `nomad var put` needs a token, and the four `*-policy.hcl` files bound to jobs are what give each job exactly its prefixes; `verify-bench.sh` items 4 and 5 prove the enforcement. The management token for bootstrapping (`nomad acl bootstrap`) is the operator's, never a job's.

## Notes
- No TLS, no gossip encryption: the bench is a private room network; a product would add `tls {}` and `encrypt`.
- `verify-bench.sh` item 1 requires Nomad ≥ 1.8.0 for the `disconnect` block in `vmsworker.nomad.hcl`; nothing here pins a version.
