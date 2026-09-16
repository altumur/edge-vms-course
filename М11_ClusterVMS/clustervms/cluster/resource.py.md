# resource.py — the resource job is М10's resource process: `cluster_resource = vms.resource.vms_resource`, re-exported

**Role in the module.** Lesson 3. On a cluster the resource is the platform's `w2cplatform.resource.Resource` (see `../../../М10_ServerVMS/vmsserver/w2cplatform/resource.py.md`) with the VMS registered on it — and that is exactly what М10 Lesson 9 built as `python3 -m vms resource` (`vms/resource.py`): `ArchivePolicy` registered as the `rec` hook (the recorder's: manifest repair, media retention by `rec/recordings/<cam>`), an `EventDatabase` attached, `/manifest` and `/segment` plugged into the platform's server. This module has no code of its own any more: it re-exports `vms_resource` under the name М11's lessons used, `cluster_resource`, together with `vms_routes` and the platform's resource vocabulary, so `__main__` and the tests import everything from `cluster.resource`. A `system` job on every server with `meta.archive` (`deploy/resource.nomad.hcl`), serving buckets, taking mirrors from peers, retaining every subsystem's buckets by that subsystem's row, keeping the event database over its own tree, heartbeating under `platform/resources/<server>`.

## Module-level names
- `cluster_resource` — `vms.resource.vms_resource(archive, server, url, vars_, objects, wall=None, peers=None, database=":memory:") -> Resource`.
- `vms_routes` — `vms.resource.vms_routes(archive) -> extra(path, headers)`: `GET /manifest/<cam>`, `GET /segment/<path>` with `Range`.
- `ArchivePolicy`, `ArchiveResource`, `Manifest` — М10's archive (`vms/archive.py.md`); re-exported.
- `PeerClient`, `Resource`, `mirror_settings`, `mirrored_buckets`, `peers_of`, `resources_seen`, `serve` — the platform's resource API, re-exported.

## Notes
- `__main__.resource` builds `cluster_resource(arch, server, url, NomadVariables(), objects, database=EVENTDB)`, serves it with `extra=vms_routes(arch)`, heartbeats, restores, then `res.database.start()`.
- The tests (`test_lesson3_events.py`, `test_lesson3_resources.py`, `test_lesson5_controller.py`) build the same object over directories and call `res.database.rebuild()` / `tail()` by hand.
