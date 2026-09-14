# signer-policy.hcl — the ACL policy for job `domain-signer`'s workload identity: the only writer of its keys, its users, and what it publishes to agents

**Role.** Lesson 4. Bound to the `domain-signer` job in the domain cluster (north). The header comment: the signer is the only writer of its keys, its users, and what it publishes to agents. Every path here is one the Python writes by CAS: `Signer._persist`, `IdentityStore._put`/`publish`, `DomainPublisher.publish_keys`/`publish_revoked`, `EntitlementCache.install`, `ClusterPlacer._store`/`_rev`.

## Stanza by stanza

### `namespace "default"` → `variables`
- `path "domain/signer" { capabilities = ["read", "write"] }` — the two private keys and the root certificate (`Signer._persist`, `Signer.restore`).
- `path "identity/*" { capabilities = ["read", "write"] }` — `identity/users/<id>` records and `identity/pointer` (`IdentityStore`). No `list` capability, though `IdentityStore.users()` lists `identity/users/` — see Notes.
- `path "domain/keys" { capabilities = ["read", "write"] }` — the public key set for agents (`DomainPublisher.publish_keys`).
- `path "domain/revoked" { capabilities = ["read", "write"] }` — the revocation list (`publish_revoked`).
- `path "domain/licence" { capabilities = ["read", "write"] }` — the cached licence (`EntitlementCache`, Lesson 5): the entitlement cache is expected to live in the signer process.
- `path "domain/placement" { capabilities = ["read", "write"] }` — the placement revision counter (`ClusterPlacer._rev`).
- `path "domain/placement/*" { capabilities = ["read", "write"] }` — the per-camera placements (`ClusterPlacer._store`): the placement service is expected to live in the signer process too.

## Notes
- `domain/grants/*` is absent, so `DomainPublisher.publish_grants` — which writes `domain/grants/<cluster>` in the domain cluster — would be refused under this policy; no production process calls it either. Grants have no writer in the deployed system. See the report.
- `signer_service.py` uses `domain/signer`, `identity/*`, `domain/keys`, `domain/revoked`; the licence and placement grants anticipate code the service does not yet run.
- Nomad's Variables ACL distinguishes `list` from `read`; `IdentityStore.users()` and `login_federated` call `vars.list("identity/users/")`, which this policy does not grant.
