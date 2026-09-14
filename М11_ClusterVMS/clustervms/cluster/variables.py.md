# variables.py — Nomad Variables over HTTP with the task's own token, and the in-memory fake with the semantics Nomad promises

**Role in the module.** Lesson 1. М10's `psimplatform.variables.Variables` contract (see `../../../М10_ServerVMS/vmsserver/psimplatform/variables.py.md`: `get` returns items and a `ModifyIndex`, `put(..., cas=)` succeeds only when the index still matches, `Conflict` otherwise, `Forbidden` when the token may not write the path) implemented by raft. `NomadVariables` speaks the `/v1/var` API with the workload-identity token Nomad injects as `NOMAD_TOKEN` (`identity { env = true }` in every jobspec). `FakeVariables` is the same contract in memory with the documented semantics — a raft-assigned `ModifyIndex`, `?cas=<index>`, 409 on mismatch, 403 under an ACL — so the 29 tests run in milliseconds without Nomad. Both raise the *platform's* `Conflict`/`Forbidden` (the import comment: one class, so a CAS retry in `psimplatform.epoch.next_epoch` catches ours too). Used by every job in `__main__`, by `objectstore.VariablesObjectStore`, and by `tests/conftest.py`.

## Module-level names
- `Conflict`, `Forbidden` — re-exported from `psimplatform.variables`; `cluster.variables.Conflict is psimplatform.variables.Conflict`.

## `class Variables(Protocol)`
The contract restated: `get(path) -> (items | None, ModifyIndex)`, `put(path, items, cas=None) -> ModifyIndex`, `list(prefix) -> [paths]`, `delete(path, cas=None)`.

## `class NomadVariables`
One namespace, one token, one agent address. Stateless beyond that; safe to construct once per process.

### `__init__(self, addr=None, token=None, namespace="default", timeout=5.0)`
`addr` from `NOMAD_ADDR` (default `http://127.0.0.1:4646` — the local agent, which every jobspec reaches with `network_mode = "host"`); `token` from `NOMAD_TOKEN` (empty string if unset — then every write is a 403 once ACLs are on, `server.hcl`); `namespace` is `default`, the one the policies name.

### `_req(self, method, url, body=None) -> (status, json | None)`
One HTTP call with `X-Nomad-Token` and JSON body. Maps Nomad's replies to the contract: `409` → `Conflict` (with the first 200 bytes of the body), `403` → `Forbidden(url)`, `404` → `(404, None)`; other HTTP errors propagate.

### `get(self, path) -> (dict | None, int)`
`GET /v1/var/<path>?namespace=`; a 404 or empty body is `(None, 0)` — `0` is the CAS value for "create only if absent". Otherwise `(Items, ModifyIndex)`.

### `put(self, path, items, cas=None) -> int`
`PUT /v1/var/<path>?namespace=[&cas=<n>]` with `{"Items": {k: str(v)}}` — Nomad items are strings, so values are stringified here (the fake does the same, which is why the tests compare against `{"epoch": "1"}`). Returns the new `ModifyIndex`.

### `list(self, prefix) -> list[str]`
`GET /v1/vars?prefix=&namespace=` → the `Path` of every match (an empty list on 404 or no body). A token needs `list` on the prefix; the policies grant it where the class needs it (assignments, slots, heartbeats).

### `delete(self, path, cas=None)`
`DELETE /v1/var/<path>?namespace=[&cas=]`; 409/403 map as above.

## `class FakeVariables`
One raft log for the whole cluster, in memory, with a lock. An optional ACL: a writer id may only `put`/`delete` under the prefixes it was granted.

### `__init__(self)`
`_raft_index` starts at 1000 (so a fake index is never confused with a count), `_items: {path: (items, index)}`, `acl: {writer: [prefixes]}`, `writer = None`.

### `as_writer(self, writer, allowed=None) -> FakeVariables`
The same raft seen through one identity — what a task's workload-identity token is under a policy. Returns a shallow copy sharing `_lock`, `_items`, `_raft_index`'s container and `acl` (via `__dict__.copy()`) with `writer` set; when `allowed` is given it registers those prefixes for that writer in the shared `acl`. `conftest.Cluster.worker` uses `as_writer("vmsworker", ["vms/epoch/*", "vms/slots/*"])`; the controller tests use `["vms/*"]`; the console test uses `SPEC.acl_console()`.

### `_acl(self, path)`
If this handle has a writer and any ACL exists: the path must equal a granted entry or match a `prefix*` glob, else `Forbidden("<writer> may not write <path>")`. A handle with `writer=None` (the bare cluster object) bypasses the ACL — the tests' "management token".

### `get(self, path)`
`(copy of items, index)` or `(None, 0)`.

### `put(self, path, items, cas=None)`
ACL check, then under the lock: if `cas` is given and differs from the current index (0 for a missing path) → `Conflict`; otherwise bump the raft index, store stringified items, return the index. This is exactly the promise `next_epoch`'s CAS loop relies on; `test_the_epoch_issuer_under_four_threads_on_the_raft_fake` issues 200 epochs from four threads with no duplicate.

### `list(self, prefix)`
Sorted paths starting with `prefix`.

### `delete(self, path, cas=None)`
ACL check, CAS check, remove, bump the index.

## Notes
- `_raft_index` is an `int`, so `as_writer`'s copied `__dict__` gives each handle its *own* counter after the first increment (`self._raft_index += 1` rebinds on the copy). Handles share `_items` and `_lock`, so CAS still works — a stale index still mismatches — but two handles can hand out the same `ModifyIndex` value for different writes. The tests never compare indexes across handles.
- `test_cas_is_the_same_promise_as_the_files_made` and `test_one_writer_per_prefix_is_an_acl_policy` are the fake's specification; `verify-bench.sh` items 4 and 5 are the same two assertions against real Nomad.
