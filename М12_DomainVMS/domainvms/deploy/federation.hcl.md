# federation.hcl — Lesson 1: a Nomad server agent configuration for the `south` region, federated with `north` (the domain cluster and authoritative ACL region)

**Role.** Lesson 1. The server-side agent configuration for one of the three servers of the south cluster, loaded beside a `client.hcl` as М11's `server.hcl` is (`../../../М11_ClusterVMS/clustervms/deploy/server.hcl.md`). The header comment is the federation model: several clusters, one domain — each cluster is a Nomad REGION; regions share no state and gossip-couple; `nomad server join` across regions federates them; a request to any region is forwarded to the right one. Nothing here replicates a worker, a Variable or an object between clusters. North is the domain cluster and the authoritative region for ACL policies. `verify-bench.sh` item 1 (`nomad server members` shows both regions) and item 2 (a `-region south` read from north) are its checks.

## Stanza by stanza

### top level
- `region = "south"` — this cluster's name as a region; it is what `NOMAD_REGION` carries into `agent.nomad.hcl`'s `CLUSTER`, the name in `CLUSTERS=…,south=…`, and `Cluster.name` in the federation.
- `datacenter = "room-b"` — the datacenter within the region (north's room is `room-a` in М11); jobspecs here use `datacenters = ["*"]`, so the name is informational for them.
- `data_dir = "/opt/nomad/data"` — raft log and snapshots (М11's `server.hcl` used `/data/nomad`).

### `server`
- `enabled = true` — this agent is a server (votes, holds raft).
- `bootstrap_expect = 3` — three servers per region before electing a leader: the same quorum rule as М11 (three tolerates one loss).
- `authoritative_region = "north"` — ACL policies and tokens are written in north and replicated to south; the domain cluster is the source of truth for who may write what, in every region. This is what lets one `domain-agent` policy apply everywhere, and what makes north "the domain cluster" at the Nomad level as well as in the code.
- `server_join { retry_join = ["nomad-1.north:4648", "nomad-1.south:4648"] }` — serf addresses (port 4648) of a server in *each* region: joining a south server to `nomad-1.north` is the WAN gossip that federates the two regions (the comment); `nomad-1.south` is the local LAN join. Each server keeps retrying, so boot order does not matter.

### `acl`
- `enabled = true` — Variables are ACL'd, one writer per key, in this region too; with `authoritative_region` set, the policies come from north.

## Notes
- Only south's file is given; north's would say `region = "north"`, `datacenter = "room-a"`, the same `authoritative_region = "north"`, and its own `retry_join`.
- No TLS or gossip encryption is configured; a WAN link between two rooms would want both.
- The domain code reads a remote region with `NomadVariables(addr=…)` pointed at that region's servers (no `region` query parameter — the address chosen *is* the region); forwarding is what makes `agent.nomad.hcl`'s `DOMAIN_NOMAD_ADDR=http://nomad.north:4646` answer from south with south's token, if north honours it (bench item 2).
