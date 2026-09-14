# agent-policy.hcl — the ACL policy for job `domain-agent`'s workload identity: write `domain/keys`, `domain/revoked`, `domain/grants`; read the rest of `domain/*`; `vms/*` denied

**Role.** Lesson 4. Bound to the `domain-agent` job in every cluster (`agent.nomad.hcl`'s `identity { env = true }`), applied as М11's policies are (`nomad acl policy apply -namespace default -job domain-agent …`, see `../../../М11_ClusterVMS/clustervms/deploy/vmsworker-policy.hcl.md`). The header comment restates the one-writer-per-prefix pattern: a worker may write `vms/<w>/*` and its slot, the controller `vms/*`, the agent `domain/*`; nobody else. `verify-bench.sh` item 3 proves it against real Nomad with a client token carrying only this policy; `test_nothing_about_a_user_reaches_a_worker_only_trust_does` proves the same shape against the fake ACL (`acl = {"agent": ["domain/*"]}`).

## Stanza by stanza

### `namespace "default"` → `variables`
- `path "domain/keys" { capabilities = ["read", "write"] }` — the signer's public key set (`tokens.KeySet.to_items`), written by `DomainAgent.sync` by CAS; `read` so the agent can compare before writing (it writes only on change).
- `path "domain/revoked" { capabilities = ["read", "write"] }` — the revocation list (`RevocationList.to_items`), same pattern.
- `path "domain/grants" { capabilities = ["read", "write"] }` — this cluster's grants (`grants_to_items`), copied from the domain cluster's `domain/grants/<cluster>` to this exact path here. The exact path, not a glob: the agent writes one grants Variable.
- `path "domain/*" { capabilities = ["read"] }` — read anything else under `domain/` (e.g. `domain/signer` or `domain/licence` if the agent runs in the domain cluster, `domain/placement/*`), never write it.
- `path "vms/*" { capabilities = ["deny"] }` — the controller's rows, the workers' epochs and slots, the assignments: explicitly denied, so even a later, wider grant elsewhere cannot leak in. The bench's `vms/cameras/999` write must be 403.

## Notes
- No `list` capability anywhere: `DomainAgent` only `get`s and `put`s known paths, which is all it needs.
- No `objects/*`: the agent never touches heartbeats or the snapshot.
- The policy governs the agent's writes into *its own* region. Its reads from the domain region (`DOMAIN_NOMAD_ADDR`) go through federation forwarding with the same token; that the token is honoured there is bench item 2, not this file.
