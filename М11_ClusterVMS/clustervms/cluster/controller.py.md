# controller.py — `ClusterController` is М10's `VmsController`, by the name М11's lessons use

**Role in the module.** Lesson 5. The controller as a Nomad job is М10's controller unchanged: `ClusterController` is an import alias of `vms.controller.VmsController` (see `../../../М10_ServerVMS/vmsserver/vms/controller.py.md`, and `w2cplatform/spec.py.md` for `SpecController`, where the behaviour actually lives). The docstring records why there is nothing else here: what the first ClusterVMS design added — placement under label constraints, the server named in the placement reason, the snapshot for М12, the measured failover — turned out to be the general behaviour with N = 1 (on one box the labels are empty, the server is the hostname, the snapshot is a cluster of one), so it was moved down into the platform's `SpecController` and the VMS's spec names it. The second paragraph is the design line the jobspecs and tests hold: **no Nomad client** — the controller never places a process, never sets `count`, never retires a slot because a heartbeat went silent. Used by `__main__.controller` (the controller's token), `__main__.console` and `console.py` (the console's token), `directory.py`'s callers, and every Lesson 2–5 test.

## Module-level names
- `ClusterController` — `vms.controller.VmsController`, re-exported (`noqa: F401`). Constructor `(vars_, objects, capacity=50, wall=time.time, cluster=None)`; the methods the cluster tests exercise are all the platform's: `create_camera`/`update_camera`/`delete_camera`/`camera`/`cameras`, `ensure_placed`, `redistribute`, `move`, `place`, `unplace_deleted`, `where`, `placement`, `unplaceable`, `assignment`/`assignments`, `slots`, `workers_seen`, `headroom`, `load`, `failover_seconds`, `publish_snapshot`.
- `Heartbeat` — the platform's heartbeat record (`worker`, `ts`, `status`, `extra`), for the helper below.

## Functions

### `heartbeats(objects, prefix="vms/") -> dict[str, Heartbeat]`
Every worker's last heartbeat, whatever its age — the console's read model, as the docstring says. Lists `objects` under `prefix`, takes every key ending in `/heartbeat` (`vms/w-1/heartbeat`, …), decodes it with `Heartbeat.from_bytes` and keys the result by the heartbeat's own `worker` field (so two instances writing under one slot name collapse to whichever wrote last — the Lesson 4 test's "the live one wrote last"). It is the same function as `w2cplatform.console.heartbeats` with the VMS prefix defaulted; `SpecController.workers_seen(max_age=…)` is the age-filtered version the controller uses for placement.

## Notes
- Two controllers are safe (`test_two_controllers_agree_under_constraints`): forty cameras placed by two instances racing on four threads, each camera in exactly one assignment, because every placement write is a CAS on raft. `count = 1` in `deploy/vmscontroller.nomad.hcl` is therefore economy, not correctness.
- The console holds this same class with a different token; `test_the_acl_from_inside_an_allocation` shows `con.place(2)` raising `Forbidden` while `con.create_camera` succeeds — the class does not know which process it is in.
