# vmscontroller-policy.hcl — the ACL policy bound to job `vmscontroller`'s workload identity: the only writer of placement

**Role.** Lesson 2. Applied with `nomad acl policy apply -namespace default -job vmscontroller vmscontroller deploy/vmscontroller-policy.hcl` (`verify-bench.sh` item 5). It is `SPEC.acl_controller()` from `w2cplatform/spec.py` (`vms/workers/*`, `vms/placement/*`, `vms/slots/*`) as Nomad HCL, plus the snapshot and the reads. The header comment lists what placement means: which worker runs which camera, a released slot's redistribution, an operator's `retire`, and the snapshot that leaves the cluster. It never writes a camera row — those are the console's (`console-policy.hcl`). Two processes hold the same `VmsController` class; this file is the only thing that makes one of them "the controller".

## Stanza by stanza

### `namespace "default"` → `variables`
- `path "vms/workers/*" { capabilities = ["write", "read", "list"] }` — the assignments `vms/workers/<worker>` (`Assignment` rows) that every worker reads and the directory scans; written by CAS in `place`, `move`, `redistribute`, `unplace_deleted`.
- `path "vms/placement/*" { capabilities = ["write", "read", "list"] }` — the placement record per camera with its reason ("on srv-b, reaching vlan:cctv-a,vlan:cctv-b"); what `/where` shows.
- `path "vms/slots/*" { capabilities = ["write", "read", "list"] }` — the slot rows: the controller reads them for `slots()`/`redistribute` and writes them for an operator's `retire`; workers claim and release them with their own token.
- `path "objects/vms/snapshot/*" { capabilities = ["write", "read", "list"] }` — `publish_snapshot()` puts one object per worker through `VariablesObjectStore`, i.e. the Variables `objects/vms/snapshot/<worker>` and `objects/vms/snapshot/unplaced`: cameras and placement for М12's read model, a copy with an age. The `*` is not decoration: this line named the exact path `objects/vms/snapshot` until М10A Lesson 27, and after the snapshot was sharded (Lesson 25) every shard was a 403 — logged as "placement pass failed", which is the one thing that had not failed. `tests/test_policies.py` now checks the file against `acl_objects_controller()`.
- `path "objects/*" { capabilities = ["read", "list"] }` — every heartbeat (`workers_seen`, `resources_seen`) for placement by reported capacity and labels.
- `path "*" { capabilities = ["read", "list"] }` — everything else read-only: the camera rows it places, `vms/next_id`, `vms/epoch/*`, `platform/*`.

## Notes
- No write on `vms/cameras/*` or `vms/retention/*`, so a controller job could not run `delete_camera` even though the class has it; in the deployed loop (`__main__.controller`) it only calls `ensure_placed`, `redistribute` and `publish_snapshot`, all inside these grants.
- `test_the_acl_from_inside_an_allocation` gives its controller `vms/*` — broader than this file; the bench's item 4b is the check that the narrower policy still lets the controller place what the console created.
