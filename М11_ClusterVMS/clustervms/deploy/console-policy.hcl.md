# console-policy.hcl — the ACL policy bound to job `console`'s workload identity: the operator's rows and nothing else

**Role.** Lesson 2, "one writer per prefix". Applied with `nomad acl policy apply -namespace default -job console console deploy/console-policy.hcl` (`verify-bench.sh` item 5), which binds it to every allocation of job `console` — the `NOMAD_TOKEN` that `identity { env = true }` injects carries exactly these rights. The header comment is the design line: a console writes a camera's row, the id counter and the retention row derived from it; not an assignment, not a placement, not a slot — a console that could place would be a second controller with a browser in front of it. It is `SPEC.acl_console()` from `vmsplatform/spec.py` written out as Nomad HCL, plus the read grants a console needs. `verify-bench.sh` item 4b proves the "nothing else" with a token carrying only this policy.

## Stanza by stanza

### `namespace "default"` → `variables`
- `path "vms/cameras/*" { capabilities = ["write", "read", "list"] }` — the camera rows: `POST /cameras` (create with `cas=0`), `PUT` (read-modify-write with `revision + 1`), `DELETE` (marks `deleted`). `list` for `GET /cameras`.
- `path "vms/next_id" { capabilities = ["write", "read"] }` — the numeric id counter the console increments by CAS on create.
- `path "vms/retention/*" { capabilities = ["write", "read", "list", "destroy"] }` — the derived row `vms/retention/<id>` (`days`, from `events_retention_days`), written beside the camera row on create/update and set to `{days: 0}` on delete; `destroy` so a console may remove it. The `resource` job reads it (`resource-policy.hcl`).
- `path "vms/idem/*" { capabilities = ["write", "read", "list", "destroy"] }` — the Idempotency-Key store (`SpecConsole.IdempotencyKeys`, prefix `vms/idem/`): a retried POST is answered the same by *any* console instance because the key is in raft, which is what lets the `system` job have no leader. `destroy` for expiry.
- `path "vms/*" { capabilities = ["read", "list"] }` — everything else the read model needs: `vms/workers/*` (the directory, `/where`), `vms/placement/*` (the reason), `vms/slots/*`, `vms/epoch/*` (the current epoch for `fenced` in `/timeline`). No write, so `con.place()` is a 403 from raft (`test_the_acl_from_inside_an_allocation`; bench 4b tries `vms/placement/*` and `vms/workers/*`).
- `path "objects/*" { capabilities = ["read", "list"] }` — the heartbeats and the snapshot as Variables (`OBJECTS=variables://objects`): `workers_seen`, `resources_seen`, `/resources`, `/metrics`.
- `path "platform/*" { capabilities = ["read", "list"] }` — `platform/mirror` and any platform-level row the page shows.

## Notes
- The console writes its **marks** (`POST /marks`) into a bucket file on this server's archive, not into Variables — no policy line is needed for that; the jobspec's `/data/archive` volume is.
- Nomad's variables ACL evaluates the most specific matching path, so the `vms/*` read-only line does not take the write away from `vms/cameras/*`.
