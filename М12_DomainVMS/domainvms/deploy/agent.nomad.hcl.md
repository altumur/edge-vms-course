# agent.nomad.hcl — the domain agent job: one allocation per cluster that copies the signer's key set, the revocation list and the cluster's grants into this cluster's `domain/*`

**Role.** Lesson 4. The jobspec for `python3 -m domain.agent` (`../domain/agent.py.md`). Registered in every region; its token is `agent-policy.hcl`. The header comment: its only right is to write `domain/*` in THIS cluster's Variables — the signer's public key set and the revocation list, copied from the domain cluster — and when the domain is unreachable it stops updating; the cluster's console and gateway keep verifying with the keys they have.

## Stanza by stanza

### `job "domain-agent"`
- `datacenters = ["*"]` — any datacenter of the region it is registered in; no `region` attribute, so the job is registered per region (`nomad job run -region south …`) and each region gets its own agent.
- `type = "service"` — long-running, rescheduled on loss.

### `group "agent"`
- `count = 1` — one per cluster; a second would write the same items (compare-then-put), so it is economy, not correctness.

### `task "agent"`
- `driver = "podman"`.
- `config { image = "vms/domainvms:latest"; args = ["python3", "-m", "domain.agent"] }` — the domainvms image (no Containerfile ships in М12; the image name is a placeholder for one built from this package over М11's) and the `agent` entry point.
- `identity { env = true }` — `NOMAD_TOKEN` in the environment: the workload identity the policy is bound to. The comment: `domain/*` and nothing else.
- `template { data = <<-EOT … EOT  destination = "local/agent.env"  env = true }` — rendered into the task's environment:
  - `CLUSTER={{ env "NOMAD_REGION" }}` — this cluster's name from the allocation's environment, so the same jobspec serves every region and the agent asks for `domain/grants/<its own region>`.
  - `DOMAIN_NOMAD_ADDR=http://nomad.north:4646` — the domain cluster's API, read through federation forwarding (`federation.hcl`). The one hostname in the file, and it names a region's servers, not a box.
  - `NOMAD_ADDR=http://127.0.0.1:4646` — this cluster's local agent, written. (No `network_mode = "host"` is set in `config`, unlike М11's jobs; the loopback address assumes host networking.)
  - `SYNC_INTERVAL=30` — seconds between passes; how stale a cluster's copy of a revocation may be, and half of the agent's contribution to the revocation window.
- `resources { cpu = 50  memory = 64 }` — three small Variable reads and at most three writes every 30 s.

## Notes
- `config { …; … }` and `resources { cpu = 50  memory = 64 }` put two attributes in one block on one line; HCL2 expects a newline between attributes and does not use `;`. М11's notes flag the same form (`autoscaler.nomad.hcl.md`); `nomad job validate` would need to accept it for this file to run as written.
- The domain cluster (north) runs this job too and copies `domain/keys` onto itself — a no-op by the compare-before-write.
