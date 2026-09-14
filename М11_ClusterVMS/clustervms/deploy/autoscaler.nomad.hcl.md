# autoscaler.nomad.hcl — the Nomad Autoscaler as a job: the only thing that changes `vmsworker`'s `count`

**Role.** Lesson 2. The fifth job on the cluster (the header comment says "fourth cluster-level job", counting differently from the README's "fifth"): HashiCorp's Nomad Autoscaler (MPL-2.0) run as an ordinary service job. It reads `vms_worker_load` — assigned ÷ capacity, from the workers' own heartbeats, exposed by the console's `/metrics` and scraped by Prometheus — and moves `vmsworker`'s `count` inside the bounds `vmsworker.nomad.hcl`'s `scaling` block sets. Nobody in the VMS talks to it; it talks to Nomad. This is how the design keeps "the controller never decides how many workers" true: the decision is the operator's bounds plus this job. Submitted with `nomad job run deploy/autoscaler.nomad.hcl`; validated by `verify-bench.sh` item 3.

## Stanza by stanza

### `job "autoscaler"`
- `datacenters = ["room-a"]` — the one datacenter `server.hcl`/`client.hcl` declare; every jobspec here names it.
- `type = "service"` — long-running, rescheduled if its node dies; one instance is enough and two would race on the same policy, so `count = 1` below.

### `group "autoscaler"`
- `count = 1` — one agent.

### `task "autoscaler"`
- `driver = "podman"` — the `nomad-driver-podman` plugin from `client.hcl`'s `plugin_dir`.
- `identity { env = true }` — Nomad injects the task's workload-identity token as `NOMAD_TOKEN`, which the template below hands to the autoscaler's `nomad` block. The token's rights come from whatever policy is bound to job `autoscaler` (see Notes).

### `config` (podman)
- `image = "docker.io/hashicorp/nomad-autoscaler:latest"` — the upstream image, pulled from Docker Hub; the only non-`localhost/` image in the deploy.
- `network_mode = "host"` — so `127.0.0.1:4646` (Nomad) and `127.0.0.1:9090` (Prometheus) in the config resolve to the host's services.
- `args = ["agent", "-config", "/local/config.hcl"]` — run the agent with the rendered template; `/local` is Nomad's per-task directory mounted into the container.

### `template`
- `data = <<EOT … EOT` — the autoscaler's own configuration, rendered by Nomad's template engine before the task starts:
  - `nomad { address = "http://127.0.0.1:4646"  token = "{{ env "NOMAD_TOKEN" }}" }` — the local agent, and the workload-identity token read from the task's environment at render time, so no token is written into the jobspec.
  - `apm "prometheus" { driver = "prometheus"  config = { address = "http://127.0.0.1:9090" } }` — the APM plugin named `prometheus`, which the `scaling` block's `check "load" { source = "prometheus" }` refers to by this name; the comment says what Prometheus scrapes: `vms-console:8080/metrics` (the `vms-console` service with tag `metrics`, see `console.nomad.hcl.md`).
  - `strategy "target-value" { driver = "target-value" }` — the strategy plugin the check's `strategy "target-value" { target = 0.9 }` uses: raise or lower count to bring `avg(vms_worker_load)` to 0.9.
- `destination = "local/config.hcl"` — where the rendered file lands, matching `args`.

### `resources`
- `cpu = 100  memory = 128` — a small agent: 100 MHz, 128 MB.

## Notes
- No `autoscaler-policy.hcl` exists in `deploy/`; the workload-identity token this job uses needs `namespace "default" { policy = "scale" }` (or `capabilities = ["scale-job", "read-job"]`) to call `nomad job scale`, and `verify-bench.sh` binds policies only to `vmsworker`, `vmscontroller` and `console`. As shipped, the agent's scaling calls would be refused by Nomad's ACL.
- Prometheus itself is not a job in this directory; the config assumes one on every server at `:9090` (host network) that scrapes the `vms-console` service.
- `resources { cpu = 100  memory = 128 }` puts two attributes on one line; HCL2 requires a newline (or separate lines) between attributes, so `nomad job validate` is expected to reject this line — see the report in the summary. The same form appears in every jobspec here.
