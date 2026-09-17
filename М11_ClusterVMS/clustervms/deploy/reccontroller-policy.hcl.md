# reccontroller-policy.hcl — bound to job reccontroller's workload identity

**Role.** Lesson 2. The only writer of the recorder's placement: `rec/workers/*` (assignments), `rec/placement/*` (with a reason), `rec/slots/*` (redistribution of a released slot). Reads everything (`*`, `objects/*`): the recorders' heartbeats, the resources' heartbeats, the recording rows the console wrote. Never a recording row, never the VMS's.
