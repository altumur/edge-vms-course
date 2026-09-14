# resource.py — the VMS's part of the platform's `resource` job: `ArchivePolicy` registered as the `vms` hook, `/manifest` and `/segment` plugged in

**Role in the module.** Lesson 3. On a cluster the resource is the platform's `psimplatform.resource.Resource` (see `../../../М10_ServerVMS/vmsserver/psimplatform/resource.py.md`): a `system` job on every server with `meta.archive`, serving buckets, taking mirrors from peers, retaining every subsystem's buckets by that subsystem's retention row, heartbeating as `platform/resources/<server>`. Nothing about mirrors, heartbeats or peers is the VMS's — the docstring insists on the names: `platform/resources/<server>/heartbeat`, `platform/mirror`, job `resource`. What this module adds is the VMS's registration: М10's `ArchivePolicy` (repair manifests, close buckets into them, retain media per camera) as the `vms` hook, and two read routes on the resource's HTTP server for the console's timeline and player. Used by `__main__.resource`, `console.py` indirectly (through the resource's URL), and the Lesson 3/5 tests.

## Module-level names
- `ArchivePolicy`, `ArchiveResource`, `Manifest` — М10's archive (`vms/archive.py.md`); re-exported.
- `PeerClient`, `Resource`, `mirror_settings`, `mirrored_buckets`, `peers_of`, `resources_seen`, `serve` — the platform's resource API, re-exported so tests and `__main__` can import the whole resource vocabulary from `cluster.resource`.

## Functions

### `vms_routes(archive) -> extra`
The VMS's reads on the resource, in the `extra(path, headers) -> (status, bytes[, headers]) | None` shape `psimplatform.resource.serve` consults after its own `/buckets`, `/mirrored`, `/events` routes. `root = archive.root`.
- `GET /manifest/<cam>` — `200` and the camera's manifest lines joined verbatim (`Manifest(root, cam)._lines()`, all lines — media and event-bucket entries alike, none filtered). This is what `timeline.ManifestReader.read` fetches.
- `GET /segment/<path>` — the bytes of one segment under the archive root, `Range` honoured: `..` or a missing file → `404`; a `Range: bytes=a-b` header (either end optional) → `206` with `Content-Range: bytes a-b/size` and exactly `b-a+1` bytes; no Range → `200` and the whole file. The console's `/segment/<path>?server=` proxies this and adds `Content-Type`/`Accept-Ranges`.
- anything else → `None` (the platform's 404).

### `cluster_resource(archive, server, url, vars_, objects, wall=None, peers=None) -> Resource`
Builds `Resource(archive.root, server, url, vars_, objects, archive.bucket_seconds, wall or archive.wall, peers)` and `register("vms", ArchivePolicy(archive, vars_))`. `server` is the heartbeat's name and the mirror directory name on peers; `url` is where peers PUT mirrors and the console/index read (`RESOURCE_URL` in the jobspec); `vars_` needs read on `platform/mirror`, `*/retention*` and `vms/cameras/*` and write on `objects/platform/resources/*` (`deploy/resource-policy.hcl`). `peers` is a `PeerClient` or the tests' directory reader.

## Notes
- `test_the_resource_policy_needs_neither_worker_nor_controller`: one `pass_()` on a resource with a stale segment and a file missing behind the manifest returns `{"vms.added": 0, "vms.dropped": 1, "vms.closed": 0, "vms.media_removed": 1, "removed": 0, "enabled": False, "mirrored": 0, "peers": []}` — the hook's report prefixed by its subsystem name, beside the platform's own.
- `ArchivePolicy.pass_` reads `retention_days` from the camera row `vms/cameras/<id>` (default 30) — the VMS keeps media retention per camera, while the platform's own pass retains *event buckets* from `vms/retention/<id>`.
- `/manifest/<cam>` serving unfiltered lines matters for `timeline.ManifestReader`, which parses every line as a `Segment`; see `timeline.py.md`.
