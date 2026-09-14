# objectstore.py — the cluster's object store: М10's `ObjectStore` contract over Nomad Variables, plain HTTP, a directory, or S3

**Role in the module.** Lesson 1. `vmsplatform.objects.ObjectStore` (see `../../../М10_ServerVMS/vmsserver/vmsplatform/objects.py.md`) is three calls — `put`, `get`, `list` — holding three small things: worker heartbeats (`vms/<w>/heartbeat`), resource heartbeats (`platform/resources/<server>/heartbeat`) and the snapshot (`vms/snapshot`). The docstring's design decision: on a cluster of this size the implementation is `VariablesObjectStore` — objects as Nomad Variables under `objects/…`. A dozen 10 KB heartbeats every ten seconds is a couple of raft writes a second, not the load the "keep raft small" rule is about, and it removes a whole store (MinIO, its quorum, its credentials) from the cluster. The contract is the point: `vms/` and `vmsplatform/` never know which adapter they hold. `open_store("s3+http://…")` swaps in `s3.py` for a cluster whose heartbeats *are* a raft load, or a rented one (М12 Lesson 8). Footage never goes to any of these. Used by `__main__` (all four jobs, via `open_store(OBJECTS)`) and by the tests (`FsObjectStore`, `VariablesObjectStore`).

## `class ObjectStore(Protocol)`
The contract restated locally: `put(key, data: bytes)`, `get(key) -> bytes | None`, `list(prefix) -> list[str]`. Keys are the platform's slash-separated names, never absolute.

## `class HttpObjectStore`
Anonymous PUT/GET against any endpoint with plain HTTP object semantics (MinIO with a bucket policy, nginx with dav, a proxy in front of presigned URLs). No listing.

### `__init__(self, base_url, timeout=10.0)`
Strips a trailing slash; keeps the socket timeout.

### `put(self, key, data)`
`PUT <base>/<key>` with `Content-Type: application/octet-stream`; anything but 200/201/204 is an `IOError`.

### `get(self, key) -> bytes | None`
`GET <base>/<key>`; 404 → `None`, other HTTP errors propagate.

### `list(self, prefix)`
Raises `NotImplementedError` with the pointer: plain HTTP has no listing, use `s3+http://` for the heartbeat prefix. So this adapter cannot serve a controller (which lists heartbeats) — only a writer.

## `class FsObjectStore`
A directory: the tests, and a bench with a shared mount. Same semantics as М10's `vmsplatform.objects.FsObjectStore`.

### `__init__(self, root)`
Creates `root`.

### `put(self, key, data)`
Writes `<root>/<key>.tmp` and `os.replace`s it over `<root>/<key>` — an object appears whole or not at all (a reader never sees a half-written heartbeat).

### `get(self, key) -> bytes | None`
The file's bytes, or `None` if absent.

### `list(self, prefix) -> list[str]`
Walks the tree, skips `.tmp` leftovers, returns sorted relative keys starting with `prefix`.

## `class VariablesObjectStore`
Objects as Variables: key `vms/w-1/heartbeat` becomes the Variable `objects/vms/w-1/heartbeat` with a single item `{data: <utf-8 text>}`. The store is whatever `Variables` the caller holds, so the ACL comes with the token: a worker's policy grants `objects/vms/*`, a resource's `objects/platform/resources/*`, the controller's `objects/vms/snapshot` (see the `*-policy.hcl` notes).

### `__init__(self, vars_, prefix="objects")`
`vars_` is any `Variables` (`NomadVariables` in a job, `FakeVariables.as_writer(...)` in tests); `prefix` is stripped of slashes.

### `_path(self, key) -> str`
`<prefix>/<key>`; refuses `..` and a leading slash with `ValueError`, so no key can escape the prefix the policy was written for.

### `put(self, key, data)`
`vars.put(path, {"data": data.decode()})` **without** CAS — the comment: the last heartbeat wins, as it should. Objects here are always whole replacements, never read-modify-write. `Forbidden` from the token propagates (the Lesson 1 test proves a worker token cannot write `objects/vms/w-2/heartbeat` without `objects/*`).

### `get(self, key) -> bytes | None`
Reads the Variable and re-encodes `items["data"]`; `None` if the Variable is missing or has no `data` item.

### `list(self, prefix) -> list[str]`
`vars.list("objects/" + prefix)` with the store prefix stripped back off, sorted — so `objects.list("vms/")` returns `["vms/w-1/heartbeat"]` while `vars.list("objects/")` returns `["objects/vms/w-1/heartbeat"]` (the test asserts both).

### `delete(self, key)`
Deletes the Variable; beyond the Protocol, for a bench to purge a stale heartbeat.

## Functions

### `open_store(url) -> ObjectStore`
The URL scheme picks the adapter, as the docstring lists:
- `s3+http://host/bucket?region=r`, `s3+https://…` — `S3ObjectStore(endpoint, bucket, region|"us-east-1")` from `s3.py`, SigV4 with credentials from `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` (on a server those come from a Variable through a template, never from the image). The `s3+` is sliced off before `urlsplit`.
- `http://`, `https://` — `HttpObjectStore` (anonymous, no listing).
- `file:///path` — `FsObjectStore`.
- `variables://<prefix>` — `VariablesObjectStore(NomadVariables(), prefix|"objects")`; the default `OBJECTS=variables://objects` in every jobspec.
- anything else — treated as a bare directory path.

## Notes
- The module docstring's last paragraph ("An S3 adapter with signed requests is a twenty-line boto3 wrapper … not here because the appliance image carries no boto3") predates `s3.py`, which implements SigV4 in the standard library and is wired into `open_store`.
- `test_the_object_store_on_this_cluster_is_variables`: a `Worker` heartbeats through a `VariablesObjectStore`, the controller's `workers_seen()` reads it back, and a token without `objects/*` gets `Forbidden` on `put`.
