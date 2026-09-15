# vmsrecorder-policy.hcl — bound to job vmsrecorder's workload identity

**Role.** Lesson 2. The recorder's version of `vmsworker-policy.hcl`: write `rec/epoch/*` (by CAS when a recording starts), `rec/slots/*` (by CAS when it claims `r-<i>`), `objects/rec/*` (its heartbeat); read `rec/*`; and — what the worker's policy does not need — read `objects/vms/*` and `vms/*`, because the camera's fan-out is found in the VMS worker's heartbeat. Never a recording row, never placement, never anything under `vms/` as a write.
