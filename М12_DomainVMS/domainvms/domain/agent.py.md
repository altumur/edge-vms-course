# agent.py — Lesson 4: the domain agent that copies the key set, the revocation list and THIS cluster's grants into the cluster's `domain/*` and nothing else; `python3 -m domain.agent`

**Role in the module.** Lesson 4. One small Nomad job per cluster (`deploy/agent.nomad.hcl`) whose only right is to write `domain/*` in that cluster's Variables (`deploy/agent-policy.hcl`) — the way a worker's only right is its epochs and its slot, and the controller's is `vms/*` (М11's policies). It carries the three things a cluster needs from the domain and nothing else: the signer's public key set, the revocation list, and the grants for THIS cluster. When the domain is unreachable it stops updating; the cluster's console and gateway keep verifying with the keys they have, issued tokens run to expiry, grants run to theirs, nobody new logs in — "the bounded outage the services table promises, with the mechanism named". Workers are not involved. Three classes: the signer's side (`DomainPublisher`), the agent (`DomainAgent`), and the cluster's reader (`ClusterTrust`). Depends on `tokens.KeySet`/`RevocationList`, `grants` (lazily, to avoid a cycle), and М11's `Variables`. Used by `signer_service` (publisher), `console.main` (trust) and the Lesson 4 tests.

## Module-level names
- `KEYS_PATH = "domain/keys"`, `REVOKED_PATH = "domain/revoked"`, `GRANTS_PATH = "domain/grants"` — the three Variables. In the domain cluster, grants are per cluster at `domain/grants/<cluster>`; in a member cluster they land at plain `domain/grants`.

## `class DomainPublisher`
"The signer's side: writes the key set and the revocation list into the DOMAIN cluster's Variables, where agents read them."
### `__init__(self, domain_vars)`.
### `publish_keys(self, ks)` — `domain/keys` ← `ks.to_items()`, by CAS on the current index.
### `publish_revoked(self, rl)` — `domain/revoked` ← `rl.to_items()`, by CAS.
### `publish_grants(self, cluster, grants)` — `domain/grants/<cluster>` ← `grants_to_items(grants)`, by CAS; "the cluster's agent copies them home". Called only by tests (see Notes).

## `class DomainAgent`
### `__init__(self, cluster, domain_vars, cluster_vars, now=time.time)`
`cluster` is this cluster's name (selects its grants); `domain_vars` the domain cluster's Variables (read); `cluster_vars` this cluster's Variables through the agent's own token (written). `last_synced`, `syncs` for the log line and the tests.

### `sync(self) -> bool`
One pass. Reads the three Variables from the domain (`domain/keys`, `domain/revoked`, `domain/grants/<cluster>`); `Unreachable` → `False` and nothing written (`test_domain_down_clusters_keep_verifying_nobody_new_logs_in` asserts `last_synced` unchanged). For each of the three that exists on the domain side, compare with what the cluster has and `put` by CAS only if different — an unchanged set costs no raft write. Stamps `last_synced`, counts, returns `True`. `test_nothing_about_a_user_reaches_a_worker_only_trust_does`: after a sync south's Variables under `domain/` are exactly `["domain/keys"]` and under `identity/` nothing; and the agent's writer handle gets `Forbidden` on `vms/cameras/7`, `vms/epoch/7`, `vms/slots/w-0`.

## `class ClusterTrust`
"What a cluster's console and gateway read from THEIR OWN cluster's Variables — never from the domain — to verify tokens and decide grants offline."
### `__init__(self, cluster_vars)`.
### `keyset(self) -> KeySet | None` — `domain/keys`, or `None` before the first sync.
### `revoked(self) -> set[str]` — the jtis from `domain/revoked` (empty if absent).
### `grants(self) -> list[Grant]` — `grants_from_items` of `domain/grants`; the console loads them with `ClusterGrants.renew_from_domain`.

## Functions

### `main()`
`python3 -m domain.agent`, one per cluster. `CLUSTER` (default `NOMAD_REGION`, default `local`); `DOMAIN_NOMAD_ADDR` (required — the domain cluster, read through federation forwarding); `NOMAD_ADDR` (this cluster, written); `SYNC_INTERVAL` (default 30 s). Loops `sync()` until SIGTERM/SIGINT; any exception counts as "domain unreachable" (the comment: keep the last set) and prints `<cluster>: domain unreachable; keeping the key set from <last_synced>`.

## Notes
- Both `NomadVariables` handles use the same `NOMAD_TOKEN` — the agent's workload identity — for the read from the domain region and the write to the local one; whether that token is honoured through forwarding is `verify-bench.sh` item 2/3, not a test.
- In production, `sync()` sees `URLError`/`OSError` from `NomadVariables` on a dead link, not `Unreachable`; `main()`'s broad `except` is what makes it behave as designed. Callers of `sync()` that catch only `Unreachable` (the tests) rely on the fakes.
- No production process calls `publish_grants`: `signer_service` publishes keys and the revocation list only, and `deploy/signer-policy.hcl` does not grant `domain/grants/*` to the signer. Grants reach a cluster only in the tests. See the report.
- The domain cluster's own agent (the jobspec runs one per region, including north) reads `domain/keys` from north and writes it to north — the same Variable, always equal, so a no-op.
