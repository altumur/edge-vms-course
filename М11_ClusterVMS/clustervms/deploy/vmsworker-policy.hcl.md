# vmsworker-policy.hcl — the ACL policy bound to job `vmsworker`'s workload identity: its epochs, its slot, its heartbeat, and nothing else

**Role.** Lesson 2. Applied with `nomad acl policy apply -namespace default -job vmsworker vmsworker deploy/vmsworker-policy.hcl` (`verify-bench.sh` item 5), so every `vmsworker` allocation's `NOMAD_TOKEN` carries exactly this. The header comment: a worker writes its epochs (by CAS, when it starts a camera) and its slot (by CAS, when it claims a name) and nothing else; `verify-bench.sh` proves the "nothing else" from inside an allocation (`own=200 other=403`). It matches what `tests/conftest.py` grants the fake worker — `["vms/epoch/*", "vms/slots/*"]` — plus the heartbeat object.

## Stanza by stanza

### `namespace "default"` → `variables`
- `path "vms/epoch/*" { capabilities = ["write", "read", "list"] }` — `vms/epoch/<camera>`: `next_epoch` (CAS increment when a worker starts a camera) and the lease renewals; the epoch is what fences an old instance.
- `path "vms/slots/*" { capabilities = ["write", "read", "list"] }` — `vms/slots/w-<n>`: `claim_slot` (CAS: holder = this allocation, gen + 1), `renew_slot`, `release_slot` on SIGTERM. `list` so a worker without an index preference can find a free or lapsed slot.
- `path "vms/holds/*" { capabilities = ["write", "read", "list"] }` — the place a worker took (`Subsystem.hold_key`), a claim about this process like its slot. Named by `acl_worker()` and missing here until `test_policies` was run again: a worker taking a place on a real cluster would have been refused.
- `path "objects/vms/heartbeats/*" { capabilities = ["write", "read", "list"] }` — its heartbeat, as an object-as-Variable (`objects/vms/heartbeats/w-<n>`, written without CAS — the last heartbeat wins). `read` so a fresh instance can read the previous instance's heartbeat for `previous_hb` and measure its own failover.
- `path "objects/vms/*" { capabilities = ["read", "list"] }` — the snapshot shards and the blobs: read, never written by a worker. Until М10A Lesson 27 this line said `write` and the one above did not exist, because the heartbeat key put the worker's name FIRST (`objects/vms/w-1/heartbeat`) and there was no narrower prefix to name — so a worker could write М12's directory and poison a blob. The fix was the key layout, not a new ACL mechanism.
- `path "vms/*" { capabilities = ["read", "list"] }` — the assignment `vms/workers/<w>` and the camera rows `vms/cameras/<id>` it reconciles against; never written.

## Notes
- `objects/vms/*` also covers `objects/vms/snapshot`, so a worker token *could* overwrite the controller's snapshot; the code never does, but the grant is wider than "its heartbeat". A per-allocation path would need the slot name, which is not known when the policy is written.
- No `objects/platform/*` read: a worker never looks at resource heartbeats — it records into the resource on its own server by path, not by lookup.
- The bench's item 4 uses a client token with only this policy to write `vms/epoch/verify`, `vms/slots/w-verify` and `objects/vms/w-verify/heartbeat`, and to be refused on `vms/cameras/verify`.
