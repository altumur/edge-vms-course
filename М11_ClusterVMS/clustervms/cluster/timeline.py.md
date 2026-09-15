# timeline.py — one camera's timeline across the resources its recording was written into (`rec/<cam>`); the unreachable one named, *unavailable*, never *lost*

**Role in the module.** Lesson 3. A camera that failed over has footage on two servers: the dead one's until the failure, the new one's after. The console asks every resource whose heartbeat reports the camera for its manifest and merges the segments, tagging each with its server and whether its epoch is fenced (older than the current one). A resource whose heartbeat is stale, or that does not answer, is *unreachable*: its ranges are omitted and the answer says so by server name, with a note that they are unavailable until the server returns. The docstring fixes the vocabulary: *unavailable* is a state with a name in it; *lost* is a word this module never prints for footage on a disk. Called by `console.cluster_routes` for `GET /timeline/<id>`; tested by `test_a_timeline_spans_two_resources_and_names_the_unreachable_one` and the HTTP console test.

## `class ManifestReader`
How the console gets a manifest from a resource: HTTP, or a fake for tests (`DirReader` in the tests reads the archive directory and raises `ConnectionError` for a server marked down).

### `__init__(self, timeout=3.0)`
The socket timeout for one manifest fetch — short, because a slow resource must not stall the whole timeline.

### `read(self, url, cam) -> list[Segment]`
`GET <url>/manifest/<cam>` (served by `resource.vms_routes`) and `Segment.from_line` on every non-blank line.

## Functions

### `merged_timeline(resources, reader, cam, t0, t1, current_epoch=None, now=None, lost_after=45.0) -> dict`
`resources` is `resources_seen(objects)` — `{server: heartbeat dict}` with `ts`, `url`, `units` (per subsystem). For every server in sorted order:
- skip it unless `str(cam)` is in `units["rec"]` — footage is the recorder's tree — the platform's heartbeat says which units each subsystem has on that resource;
- if `now - ts > lost_after` (45 s, the same number as Nomad's `disconnect.lost_after`) → `unreachable`, no fetch;
- else fetch; any exception (the heartbeat is fresh but the server is not answering) → `unreachable`;
- keep every segment overlapping `[t0, t1)` as `{start, end, path, epoch, server, fenced}` with `fenced = current_epoch is not None and s.epoch < current_epoch`.
Segments are sorted by `(start, epoch)`. Returns `{"segments": [...], "unreachable": [servers], "note": "ranges on <servers> are unavailable until the server returns — not lost"}` (empty note when nothing is unreachable).

## Notes
- The test walks the three states: both servers fresh → segments `(srv-a, 3, fenced)`, `(srv-a, 3, fenced)`, `(srv-b, 4, live)`; `srv-a` stale after 60 s → only `srv-b`'s segment, `unreachable == ["srv-a"]`, and the note contains both "unavailable until the server returns" and "not lost"; `srv-a` heartbeats again → all three segments, because its manifest came back with its disks and nothing was rebuilt.
- The docstring mentions listing an unreachable resource's ranges "from the last thing known about it — the manifest it served last time, if we cached it"; the code keeps no such cache, so an unreachable server contributes no segments, only its name.
- `ManifestReader.read` applies `Segment.from_line` to **every** line of `/manifest/<cam>`, but `vms_routes` serves the manifest unfiltered and М10's `Manifest` also stores event-bucket lines (`kind: "events"`, no `cam`/`bytes` fields). Once a camera has closed buckets in its manifest, `Segment.from_line` raises `KeyError`, `merged_timeline` catches it and lists the server as unreachable. The tests do not hit this because `DirReader` uses `Manifest.read()`, which filters to `kind == "media"`.
