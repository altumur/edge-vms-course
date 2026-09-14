# verify-bench.sh — the checks that need a real cluster, in one run: PASS/FAIL per item

**Role.** What the 29 tests cannot prove — that Nomad keeps the promises the fakes implement — is checked here against the real bench. Run with a management `NOMAD_ADDR`/`NOMAD_TOKEN` from any box with the `nomad` CLI: `deploy/verify-bench.sh`. Six numbered checks (the header lists them): the Nomad version, the cluster's shape, the jobspecs validating, the ACL with a token carrying only the worker's policy, the same from *inside* a `vmsworker` allocation with the task's own workload-identity token, and a scale drill. Two sub-checks were added: 4b (the console's token) and 5a (the mirror is resource to resource). Needs `nomad`, `python3`, `curl`, `awk`, `sed`, `sort -V`. `set -u`; each check records PASS or FAIL and the script exits 0 only if nothing failed.

## Environment
- `NOMAD_ADDR`, `NOMAD_TOKEN` — the management token, read by the `nomad` CLI.
- `HERE` — the script's directory, so the policy and jobspec paths resolve wherever it is run from.

## Step by step

### `ok()` / `bad()`
Print `  PASS  …` or `  FAIL  …` and count.

### 1 — Nomad ≥ 1.8.0
Parses `nomad version`, compares with `sort -V`. The `disconnect` block that `vmsworker.nomad.hcl` relies on (Lesson 4's `lost_after`/`replace`/`reconcile`) does not exist before 1.8.

### 2 — three servers, one leader; Podman healthy; `meta.archive` somewhere
`nomad server members`: at least three rows, exactly one with `Leader = true`. For every node id from `nomad node status -quiet`, the verbose status must show the `podman` driver healthy (`^podman +true`), and a node whose verbose output has an `archive` meta row is counted; at least one must declare `meta.archive` or the `resource` job has nowhere to be.

### 3 — the jobspecs validate
`nomad job validate` on `vmsworker`, `vmscontroller`, `console`, `resource`, `autoscaler` (five, though the header says "the four jobspecs"); a failure prints the validator's last line.

### 4 — policy semantics with a token carrying ONLY `vmsworker`
Creates the four policies (unbound), then a 10-minute client token with policy `vmsworker`. With it: `nomad var put -force vms/epoch/verify` must succeed, `vms/slots/w-verify` must succeed, `objects/vms/w-verify/heartbeat data='{}'` must succeed (the heartbeat as an object-as-Variable), and `vms/cameras/verify` must be **refused** — "one writer per key is enforced rather than promised". Probes are purged afterwards. If no token could be created, the likely causes are printed (ACLs not bootstrapped, `NOMAD_TOKEN` unset).

### 4b — the console's token
A token with policy `console`: writes `vms/cameras/verify` and `vms/idem/verify` (a retry answered by any instance), is refused on `vms/placement/verify` ("a console that can place is a second controller") and on `vms/workers/w-verify`.

### 5 — the binding to the job's workload identity
`nomad acl policy apply -namespace default -job <job> …` binds `vmsworker`, `vmscontroller` and `console` policies to their jobs — what the product relies on, since no job ever holds a static token. Then finds a running `vmsworker` allocation and, with `nomad alloc exec -task vmsworker`, runs a shell **inside** it that `curl`s `PUT /v1/var/vms/epoch/verify` (own prefix) and `PUT /v1/var/vms/cameras/verify` (another's) with the allocation's own `NOMAD_TOKEN`; expects `own=200 other=403`. No running allocation → FAIL with "run the job first".

### 5a — the mirror is resource to resource
`nomad service info -json resource` gives one resource's address; `PUT /mirror/srv-verify/vms/0/e1/19700101T000000Z.events.jsonl` with a one-event body must return 204 and `GET /mirrored/srv-verify` must list a `path` — a peer took a copy and lists it, and nothing went through a store. No registered service → FAIL.

### 6 — scale out, then in
Reads the current `Count` of `vmsworker`'s group. `nomad job scale vmsworker N+1`, wait 20 s: `vms/slots/w-N` must show `released = false` — the new allocation with index N claimed the slot by CAS. `nomad job scale vmsworker N`, wait 30 s: the same slot must show `released = true` — the allocation got SIGTERM inside `kill_timeout` and `release_slot()` ran (an unreleased slot means the timeout was too short or the stop was not orderly). The controller was asked for neither.

### summary
`<pass> passed, <fail> failed`; exit status is `[ "$fail" -eq 0 ]`.

## Notes
- Item 5 relies on `curl` inside the `clustervms` image (present, from М10's Containerfile) and on `NOMAD_ADDR` being unset or pointing at the local agent inside the allocation (`network_mode = "host"` makes `127.0.0.1:4646` right).
- The `resource` policy is applied in item 4 but never bound with `-job resource` in item 5; see `resource-policy.hcl.md`.
- Item 5a depends on the `service` block registering, which without Consul needs `provider = "nomad"` in `resource.nomad.hcl`.
- Item 6 leaves the scaled-in slot row `released = true` behind, which is what the controller's `redistribute()` then acts on — the same sequence `test_nomad_job_scale_out_then_in` runs on the fake.
