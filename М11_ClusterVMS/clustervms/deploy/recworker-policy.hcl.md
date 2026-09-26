# recworker-policy.hcl — bound to job recworker's workload identity

**Role.** Lesson 2. The recorder's version of `vmsworker-policy.hcl`: write `rec/epoch/*` (by CAS when a recording starts), `rec/slots/*` (by CAS when it claims `r-<i>`), `objects/rec/heartbeats/*` (its heartbeat, and nothing else under `objects/rec/` — М10A Lesson 27); read `rec/*` and the rest of `objects/rec/*`; and — what the worker's policy does not need — read `objects/vms/*` and `vms/*`, because the camera's fan-out is found in the VMS worker's heartbeat. Never a recording row, never placement, never anything under `vms/` as a write.

**Added later.** `rec/holds/*` — the VOLUME a recorder took by CAS (М10B Lesson 10). Named by `acl_worker()` and absent here, so on a real cluster a recorder would have been refused the first time it took a declared archive; `test_every_row_the_code_writes_is_granted_too` found it.
