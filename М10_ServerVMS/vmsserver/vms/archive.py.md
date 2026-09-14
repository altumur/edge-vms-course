# archive.py — the VMS's part of the resource under `vms/<cam>/`: spool → promote → manifest, the camera's event buckets, and the retention policy

**Role in the module.** Lesson 3. The archive is server-bound and has no controller; it has a layout and a policy. The layout, from the docstring:

- `<spool>/vms/<cam>/e<epoch>/<start>Z.mp4` — the open segment, and closed ones not yet promoted (archivesink writes here).
- `<archive>/vms/<cam>/e<epoch>/<start>Z.mp4` — promoted: the resource's media.
- `<archive>/vms/<cam>/e<epoch>/<start>Z.events.jsonl` — the camera's event buckets: the platform's event log (`vmsplatform.events`, see `events.py.md`), written by the worker holding the camera's epoch, recording or not.
- `<archive>/vms/<cam>/manifest.jsonl` — one line per media segment and one per closed event bucket: the index that lives beside the footage.

The archive's unit is a *time span under an epoch*, not a media file: a span may hold media, events, or both; a watched-but-never-recorded camera still has buckets; a camera that went silent has no segment open, and the `silent` event goes into its bucket. The acknowledgement order is М9 Lesson 4's: a closed segment is promoted (renamed into the archive, then a manifest line appended) and the spool copy is gone only after that; a bucket is written in place one flushed line at a time and gets its manifest line when it closes. The manifest is append-only and rebuildable from the files. Retention is a policy per kind: media is the VMS's (`retain`, by `retention_days`, files first then lines); buckets are the platform's (`vmsplatform.resource` deletes files by `vms/retention/<cam>`, and `repair` drops the orphaned lines). The tree is `<subsystem>/<unit>/…`, the platform resource's, so other subsystems' buckets sit on the same server under their own prefix. Used by `gstvms/archivesink.py` (`promote` on fragment-closed), `worker.py` (`event_log`), `console.py` (`Manifest.timeline`, `root`), `__main__` (`closed_in_spool` on worker start; `repair`/`close_buckets`/`retain` in `retain`), and `ArchivePolicy` is what the VMS registers with the platform's `Resource`.

## Module-level names
- `SUB = "vms"` — the subsystem directory under both roots.
- `SEGMENT` — regex for a segment filename `YYYYMMDDTHHMMSSZ.mp4`.
- `EPOCH_DIR` — regex `e<digits>`.

## Functions

### `segment_path(root, cam, epoch, start) -> str`
`<root>/vms/<cam>/e<epoch>/<start as %Y%m%dT%H%M%SZ>.mp4`. archivesink asks for it with the spool root; tests build spool files with it.

### `parse(path, root) -> (cam, epoch, start datetime) | None`
The inverse, relative to `root`: exactly four components, `vms`, a numeric camera, `e<n>`, and a `SEGMENT` name; the start is parsed as UTC. Anything else — a manifest, a bucket, a tmp file, a path outside `vms/` — is `None`, which is how every walker here ignores what it is not looking for. `test_parse_and_paths`.

### `event_log(root, cam, epoch, bucket_seconds=600) -> EventLog`
The camera's event log on this resource: `EventLog(root, "vms", str(cam), epoch, bucket_seconds)` — what the worker holding the camera's epoch writes into. The epoch is in the path, so a stale writer's lines are identifiable afterwards.

## `class Segment` (frozen dataclass)
One media line of the manifest: `cam`, `epoch`, `start`, `end` (unix seconds), `path` (relative to the archive root), `bytes`.

### `line(self) -> str`
JSON with `kind: "media"` and the fields.

### `from_line(cls, line) -> Segment`
The inverse, types coerced.

## Functions (continued)
### `bucket_from_line(line) -> Bucket`
Parses a manifest line of `kind: "events"` (the form `Bucket.line()` writes) back into a platform `Bucket`. Same shape as `vmsplatform.resource.bucket_from_line`.

## `class Manifest`
Per camera, append-only, beside the footage: `<archive>/vms/<cam>/manifest.jsonl`. Two kinds of line, media and events, distinguished by `kind` (a line without one is media, for files written before buckets existed).

### `__init__(self, archive_root, cam)`
Computes `self.path` via `vmsplatform.events.unit_dir`; creates nothing.

### `append(self, entry)`
Creates the directory if needed and appends `entry.line()` — a `Segment` or a `Bucket`.

### `_lines(self) -> list[str]`
Non-blank lines; `[]` if the file does not exist.

### `read(self) -> list[Segment]`
The media lines — what a player needs.

### `buckets(self) -> list[Bucket]`
The closed event buckets — what an index needs.

### `rewrite(self, segs, buckets=None)`
Replaces the file atomically (write `.tmp`, `os.replace`) with the given segments plus the given buckets (or the current bucket lines if `None`), sorted by `(start, epoch)`. Called by `repair` and `retain`.

### `timeline(self, t0, t1, current_epoch=None) -> list[dict]`
Spans overlapping `[t0, t1)`: every media segment as `{start, end, media: path, epoch, events: 0, fenced}`; then every closed bucket — if a media span of the same epoch overlaps it, the bucket's event count is added onto that span (events during a recorded span are counted on it), otherwise the bucket stands alone as `{…, media: None, events: n}` (the camera was watched, not recorded). `fenced` is true when `current_epoch` is given and the span's epoch is older — how the page shows a zombie's footage. Sorted by `(start, epoch)`. `test_timeline_marks_a_fenced_epoch_and_spans_two_resources`: epochs `[3 fenced, 3 fenced, 4]` against `current_epoch=4`; two resources' timelines simply concatenate and sort — the console merges manifests. `test_events_are_buckets…`: `(None, 2, True), (None, 1, True)` for two watched-not-recorded buckets, and the epoch-4 bucket counted onto the epoch-4 segment.

## `class ArchiveResource`
One server's archive: a spool root and an archive root, both created on construction.

### `__init__(self, spool_root, archive_root, bucket_seconds=600, wall=None)`
`wall` defaults to `time.time`; `repair` uses it to tell an open bucket from a closed one.

### `promote(self, spool_path, end=None) -> Segment`
What archivesink calls on `splitmuxsink-fragment-closed`, and what the worker's start-up calls for leftovers. `parse` the spool path (`ValueError` if it is not a segment path), `end` defaults to the file's mtime, then in order: 1. `_move` into the archive at the same relative path (atomic on one filesystem); 2. append the `Segment` line to the camera's manifest. The spool copy disappears as part of step 1 (rename) or last (copy path). `test_promote_is_the_acknowledgement_order`: gone from the spool, present in the archive, one manifest line with `end − start == 600`.

### `close_buckets(self, now, grace_seconds=30.0, bucket_seconds=600) -> list[Bucket]`
For every camera directory: every bucket on disk (`buckets_under`) that is not yet in the manifest, whose span is over (`end <= now`) and whose file has not been touched for `grace_seconds`, gets its manifest line. "The events were durable the moment they were written; this is the index catching up, not an acknowledgement." Idempotent: the second call returns `[]`. Note the default `bucket_seconds` here is the literal 600, not `self.bucket_seconds` — `ArchivePolicy.pass_` passes the instance's explicitly; `__main__.retain` does not.

### `cameras(self) -> list[int]`
Numeric directory names under `<archive>/vms/`, sorted; `[]` if none.

### `_move(src, dest)` (static)
`os.rename`; if that fails (different filesystem) `copy2` to `dest.tmp`, `os.replace` so the file appears whole, then remove the source — the spool copy last.

### `closed_in_spool(self, grace_seconds, now) -> list[str]`
Segment paths in the spool whose mtime is at least `grace_seconds` old: closed by the previous instance but never promoted (it died between close and promote). `__main__.worker` promotes these before starting. The open segment (recent mtime) is not listed — it is the one a kill loses, up to one segment length. `test_kill_mid_segment_open_lost_closed_kept`: six promoted, one late one found and promoted, the open one left; a second call finds nothing.

### `repair(self) -> {"added", "dropped"}`
Make every camera's manifest agree with the files (М11 called it the re-index sweep). Media: walk the camera's directory, add a `Segment` for every segment file no line names (epoch from the path, end from mtime), drop every line whose file is gone. Buckets: every *closed* bucket on disk (`end <= wall()`) is a line, every line without a file is dropped — an open bucket is still being written and is left out. Then `rewrite`. Idempotent: `{added: 0, dropped: 0}` on the second run. `test_manifest_rebuilt_from_the_files_alone`: the manifest deleted, `added: 3` rebuilds it identically; a file removed under a line, `dropped: 1`. `test_events_are_buckets…`: `added: 4` (one segment, three buckets) after the manifest is removed; `dropped: 3` after the platform's `Resource.retain` deleted the bucket files.

### `retain(self, cam, days, now) -> int`
Media retention, the VMS's own: for every media line with `end < now − days·86400`, remove the file (a missing file is fine) and count it; if anything was removed, `rewrite` the manifest with the kept segments (bucket lines untouched). Files first, then lines — the manifest never names a file that is gone for long. Buckets are not this method's: they go by `vms/retention/<cam>` through the platform's resource job, and `repair` drops their lines afterwards. `test_retention_is_a_policy_on_the_resource`: days=8 removes the 1st and 10th, leaves the 19th; `usage()` is 1000 after.

### `usage(self) -> int`
Bytes of every segment file under the archive root (files `parse` recognises — buckets and manifests not counted).

## `class ArchivePolicy`
What the VMS registers with the platform's resource job (`Resource.register("vms", ArchivePolicy(...))`): its own pass over its own part of the tree, run on the resource's timer beside the platform's own bucket retention and mirror.

### `__init__(self, resource, vars_)`
The `ArchiveResource` and a Variables reader (for the camera rows).

### `pass_(self, now) -> dict`
`repair()`, then `close_buckets(now, bucket_seconds=self.res.bucket_seconds)`, then for each camera directory read `vms/cameras/<cam>` and `retain(cam, retention_days or 30, now)`. Returns `{added, dropped, closed, media_removed}`. A camera with no row (deleted rows are marked, not removed, so this is a row that never existed) uses 30 days.

## Notes
- Every path carries the epoch: `e<epoch>` between the camera and the file. That is the fence made visible on disk — a zombie's segment and a zombie's bucket are in their own epoch directory, and the timeline marks them.
- Ordering that matters: `_move` before `Manifest.append` in `promote`; file removal before `rewrite` in `retain`; `close_buckets` only after the span is over *and* the file is quiet for the grace.
- On one box, `ArchivePolicy` is not wired up by `__main__`; `retain` runs the same three steps by hand and then the platform's bucket half (`Resource.retain`) in the same pass. `test_events_are_buckets_on_the_resource_recording_or_not` exercises that half directly with `vms/retention/7 {days: 30}`.
