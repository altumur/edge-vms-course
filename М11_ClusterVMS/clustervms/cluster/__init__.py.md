# __init__.py — the `cluster` package: М11's additions on top of М10's `vmsserver`, and the import path that makes that possible

**Role in the module.** The package docstring is the module map for М11 (ClusterVMS): the same `w2cplatform` contract and the same `vms/` controller, worker and archive resource as М10, *imported* rather than copied, plus the eight modules that a cluster adds (Nomad Variables, the object store, the worker and controller as jobs, the resource job, the cross-server timeline, the directory, the console). The only code is a `sys.path` fix-up so that `import vms` and `import w2cplatform` resolve to М10's package without an install step. Every entry point (`__main__.py`, `tests/conftest.py`, `tests/run.py`) does `import cluster` first for exactly this side effect.

## Module-level names
- `_here` — the directory that contains `cluster/`, i.e. the `clustervms/` module directory.
- The search loop — tries three candidates in order and appends the **first** existing directory to `sys.path`:
  1. `$VMSSERVER_PATH` — set to `/app` by `deploy/Containerfile`, where М10's image already holds `vms/`, `w2cplatform/` and `gstvms/`;
  2. `<clustervms>/vmsserver` — a sibling checkout or symlink beside `cluster/`;
  3. `<notes>/М10_ServerVMS/vmsserver` — the course layout, two directories up from `clustervms/`, which is what the tests use in this tree.
  It **appends** rather than inserts (the comment says why): `clustervms/tests/` must shadow `vmsserver/tests/` when both are importable as `tests`.

## Notes
- The docstring's description of `objectstore.py` ("MinIO / S3 … the heartbeats and the domain's snapshots") is older than the code: on this cluster the default store is Nomad Variables under `objects/…` (see `objectstore.py.md`); S3 is the adapter for a rented or outgrown cluster.
- Nothing here is executed by Nomad directly; it runs whenever any `cluster.*` module is imported, so a process that imports `vms` before `cluster` (nothing in this package does) would fail with `ModuleNotFoundError`.
