# retention.py — partitions ahead, drop old months, per-camera windows, the disk-full policy, the orphan sweep

**Role in the module.** Lesson 8, Step 3 — retention that runs while the disk is full. Three rules, in the order a crash makes them matter: (1) drop the index BEFORE unlinking the files — a crash in between leaves orphans (wasteful, recoverable by a scan), the other order leaves index rows pointing at nothing, footage the console offers and cannot play; (2) partitions are created AHEAD of time, because a missing partition is a recording outage; (3) if every camera is inside its window and the disk is still full, something gives, and the recorder SAYS which policy it applied as an event, because a silent drop is indistinguishable from a bug. `db` and `fs` are injected (protocols) so the arithmetic runs in `tests/test_diskfull.py` without Postgres, using `conftest.FakeDb`/`FakeFs`/`LoggingFs`; `Worker.retention()` runs it every `RETENTION_INTERVAL` with `PgStore` and `RealFs`.

## Module-level names
- `log` — `worker.retention`.
- `STOP_RECORDING`, `DEGRADE_RETENTION`, `BY_PRIORITY` — the three policy strings `Settings.disk_full_policy` may hold.

## `class RetentionDb` (Protocol)
The ten async methods of `PgStore` this module needs: `partitions`, `ensure_partition`, `paths_in_partition`, `drop_partition`, `expire_rows`, `oldest_segments`, `delete_segment`, `retention_days`, `indexed_paths_under`, `log_event`. See `store.py.md` for each.

## `class Fs` (Protocol)
`usage(path) -> fraction 0..1`, `unlink(path)`, `walk_files(root) -> [(path, mtime)]`.

## `class RealFs`
### `usage(self, path)` — `statvfs`: `1 - f_bavail/f_blocks` (available-to-unprivileged over total), 0.0 on an empty filesystem.
### `unlink(self, path)` — `os.unlink`, ignoring `FileNotFoundError` (already gone is fine).
### `walk_files(self, root)` — every file under `root` with its mtime, skipping ones that vanish mid-walk.

## `class RetentionReport` (dataclass)
What one pass did, for logging and tests:
- `partitions_created`, `partitions_dropped` — names.
- `files_unlinked` — count. `bytes_freed_by_policy` — sum of `bytes` the policy sacrificed.
- `policy_applied` — the policy string, or `None` when the disk was below the high-water mark.
- `recording_allowed` — `False` when the policy is `stop_recording`, or when a freeing policy ran out of segments; `Worker.retention()` copies it.
- `degraded_cameras` — `camera_id -> start of the newest segment sacrificed` (the camera's new oldest footage).
- `orphans` — files found without an index row.
- `events` — `(kind, camera_id, payload)` to be written at the end of the pass.

## Functions
### `_months(start, n)` — the first days of `n` consecutive months from `start`'s month.

### `ensure_partitions(db, now, ahead, report)`
Collects existing partition names for `segments` and `events`, and for each parent and each of the current month plus `ahead` months calls `db.ensure_partition` where the name `<parent>_YYYY_MM` is absent, recording it. Idempotent (`test_partitions_are_created_ahead`).

### `_unlink_all(fs, paths, report)` — unlink each, count each.

### `enforce_retention(db, fs, settings, now=None, archive_dir=None) -> RetentionReport`
The pass, in order:
0. `ensure_partitions` with `settings.partitions_ahead` — cheap, and its absence is an outage.
1. Read every camera's `retention_days`; with no cameras, return. `longest` is the largest window. Whole `segments` partitions whose upper bound is at or before `now - longest` are dropped: paths remembered first, `drop_partition` (DETACH + DROP, ~5 ms), then the files unlinked. `events` partitions older than the same cutoff are dropped too (no files). `test_whole_partitions_…` asserts from the operation log that the drop precedes the first unlink.
2. Per-camera windows shorter than the longest: `expire_rows` (rows first) then unlink the returned paths (files after).
3. If `fs.usage(archive_dir) >= disk_high_water`, `_apply_policy`.
4. Orphan sweep: every file under `archive_dir` not in `indexed_paths_under(archive_dir)` and older than `2 × segment_seconds` (so no pipeline could still be writing it) is listed in `report.orphans` and an `archive.orphans` event with the count is queued. Reported, never deleted (Lesson 8, exercise 4 — the decision is yours; `test_orphans_are_reported_not_silently_deleted`).
Finally every queued event is written with `db.log_event`, and the report returned.

### `_apply_policy(db, fs, settings, now, archive_dir, report)`
Sets `policy_applied`. `stop_recording`: honour every window, refuse new segments — `recording_allowed = False`, event `retention.stopped` with the usage; nothing is deleted ("footage is evidence and a gap is better than a missing week"). Otherwise free down to `disk_high_water - 0.05`: fetch batches of 50 from `oldest_segments` — oldest first across all cameras for `degrade_retention`, lowest priority then oldest for `by_priority` — and for each, until the target is met, `delete_segment` (index first) then `fs.unlink` (then file), counting bytes and recording the camera's new oldest start. An empty batch means nothing is left to give: `recording_allowed = False`. Then `bytes_freed_by_policy` and a `retention.degraded` event naming the policy, bytes and cameras. Tests: `degrade` keeps recording and gets under 0.90; `by_priority` sacrifices only the car park (priority 10) and only as much as needed; `stop_recording` sacrifices nothing.

## Notes
- The `retention.degraded` event is appended even when the very first batch was empty and nothing was freed; the payload then says `bytes_freed: 0` and no cameras.
- `oldest_segments` is a cross-partition scan (see `store.py.md`); it only runs at or above the high-water mark.
- Dropping `events` partitions by the cameras' longest window ties the event log's lifetime to footage retention; there is no separate setting.
