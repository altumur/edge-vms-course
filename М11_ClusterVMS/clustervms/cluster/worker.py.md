# worker.py — `ClusterWorker` is М10's `VmsWorker` as an allocation: the slot from `NOMAD_ALLOC_INDEX`, the server and labels from the environment

**Role in the module.** Lesson 2. The worker on a cluster is М10's `VmsWorker` unchanged (see `../../../vmsserver/vms/worker.py.md`): what Nomad hands a process — `NOMAD_ALLOC_INDEX`, `NOMAD_NODE_NAME`, `NOMAD_META_labels`, `NOMAD_ALLOC_ID`, `CAPACITY` — `VmsWorker` already reads from its environment, on a box exactly as in an allocation. This module keeps the name М11's lessons used and the `env=` calling convention so a test can hand it a fake allocation environment. The docstring's line: a worker on a cluster is a worker on a box whose stores happen to be raft. Used by `__main__.worker` and by `tests/conftest.py`.

## Module-level names
- `FakeActuator`, `VmsWorker`, `labels_from_environment`, `slot_from_environment` — re-exported from `vms.worker` (`noqa: F401`): the actuator that records nothing, the worker, and the two environment readers (`w-<NOMAD_ALLOC_INDEX>` as the slot preference; `NOMAD_META_labels` split on commas).

## `class ClusterWorker(VmsWorker)`
A `VmsWorker` whose name is always taken from the environment.

### `__init__(self, vars_, objects, actuator=None, env=None, **kw)`
`super().__init__(None, vars_, objects, actuator or FakeActuator(), env=env, **kw)`. `name=None` means "the environment's": `VmsWorker` calls `claim_slot(prefer=slot_from_environment(env))`, a CAS on `vms/slots/w-<index>` that names the allocation (`NOMAD_ALLOC_ID`) as holder — the index is the preference, the Variable is the proof. `env=None` means `os.environ` (a real allocation); the tests pass `Cluster.env(index, server, alloc)`. `**kw` carries `clock`, `wall`, `capacity`, `lease_ttl`, … through to М10. `server` becomes `NOMAD_NODE_NAME`, `labels` the split `NOMAD_META_labels`, `alloc` the `NOMAD_ALLOC_ID`; `previous_instance`/`previous_hb` are read from the slot's last heartbeat object so a replacement can measure its own failover.

## Notes
- What the tests prove through this class: `w-0`/`w-1` from indexes 0/1 and the server's labels in the heartbeat (`test_the_slot_comes_from_the_allocation_index…`); a new index claims a new slot and a released one is redistributed (`test_nomad_job_scale_out_then_in`); two allocations with one index resolve at the CAS — the second claim wins, the first fences at its next lease pass (`test_two_allocations_with_one_index_resolve_at_the_cas`, Nomad issue #10727); the power pull — a fresh allocation with the same index claims `w-1`, reads the assignment, takes the next epoch per camera and reports `failover_seconds == 48.0` (`test_the_power_pull`).
- `__main__.worker` passes `actuator=None` when GStreamer is absent, which lands on `FakeActuator()` here.
