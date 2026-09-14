# events.py — the event log: JSON-lines buckets per unit per epoch on the resource, for any subsystem

**Role in the module.** Lesson 3. An event here means only "something a worker observed at a time, about a unit it holds the epoch for". The platform fixes the shape and nothing else: a bucket is `<resource root>/<subsystem>/<unit>/e<epoch>/<start>Z.events.jsonl`, holding JSON lines `{t, kind, ...}` for a span of `bucket_seconds` starting at `<start>`; its writer is the worker holding that unit's epoch (one writer per file by construction); its fence is the epoch in the path (a stale instance writes into its own bucket, which is marked afterwards); its index is the manifest beside the unit's buckets (the VMS's, in `vms/archive.py`) and, in М11, `eventindex.py` cluster-wide. `resource.py` walks these paths for retention and mirroring; `console.py` uses `EventLog` for operator marks under `console/<instance>/`; the VMS worker uses it per camera. Nothing here knows what a unit is.

## Module-level names
- `EVENTS` — regex for a bucket filename: `YYYYMMDDTHHMMSSZ.events.jsonl`.
- `EPOCH_DIR` — regex for the epoch directory: `e<digits>`.

## Functions
### `bucket_start(t, bucket_seconds) -> float`
Floors `t` to the start of its bucket span. Buckets roll by the clock, not by anything the subsystem does.

### `_stamp(t) -> str`
UTC `%Y%m%dT%H%M%SZ` for a timestamp — the filename stem.

### `unit_dir(root, subsystem, unit) -> str`
`<root>/<subsystem>/<unit>`.

### `bucket_path(root, subsystem, unit, epoch, start) -> str`
`<root>/<subsystem>/<unit>/e<epoch>/<stamp>.events.jsonl`.

### `parse_bucket(path, root) -> (subsystem, unit, epoch, start) | None`
The inverse of `bucket_path`: relative to `root`, exactly four components, third matching `EPOCH_DIR`, fourth matching `EVENTS`; the start is parsed back to a UTC timestamp. Anything else (a media file, a manifest, a tmp file) is `None`, which is how the walkers below ignore whatever a subsystem keeps beside its buckets.

## `class Bucket` (frozen dataclass)
One bucket as the resource describes it over HTTP and the index stores it.
- `subsystem`, `unit`, `epoch`, `start`, `end` (= start + bucket_seconds), `path` (relative to the resource root), `events` (line count).

### `line(self) -> str`
The bucket as one JSON line with `kind: "events"` — the wire form of `GET /buckets/<sub>/<unit>` and `GET /mirrored/<server>`; `resource.bucket_from_line` parses it back.

## `class EventLog`
What a worker holds per unit it has an epoch for: the writer side.

### `__init__(self, root, subsystem, unit, epoch, bucket_seconds=600)`
Fixes the resource root, the subsystem prefix, the unit (stringified), the epoch this writer holds and the bucket span (10 minutes by default).

### `path_for(self, t) -> str`
The bucket file that time `t` falls in, for this epoch.

### `append(self, t, kind, **fields) -> str`
Writes one JSON line `{t, kind, **fields}` to the bucket for `t`, creating directories, flushing after the write; returns the path. Append-only, one process per file: the epoch in the path guarantees no two live writers share a file.

### `read_bucket(path) -> list[dict]`
All lines of one bucket parsed; a missing file is an empty list.

### `buckets_under(root, subsystem, unit, bucket_seconds) -> list[Bucket]`
Every bucket file for a unit, from the files alone — what repair and the resource's `/buckets` route read. Walks the unit directory, keeps what `parse_bucket` accepts, counts lines, sorts by `(start, epoch)`.

### `subsystems_under(root) -> dict[str, list[str]]`
`{subsystem: [unit, ...]}` present on a resource, from the directory tree — the index's and the resource heartbeat's discovery, with no registry. Hidden directories (`.mirror`) are skipped; a missing root is `{}`. The console test asserts that after one mark the archive root shows `{"console": [<instance>]}` and nothing under `vms/1/`, proving a mark is the console's bucket, not a worker's.

## Notes
- `test_lesson3_archive.py::test_events_are_buckets_on_the_resource_recording_or_not` and `test_lesson4_worker.py::test_the_worker_observes_what_it_holds_recording_or_not` exercise the VMS's use of this file: buckets are written whether or not media is recorded, closed buckets are counted onto the timeline, and a fenced epoch's bucket is marked, not deleted.
- A bucket is "closed" when `end <= now`; only closed buckets are mirrored and indexed (`resource.Resource.closed_buckets`). The open one is the accepted loss on a disk failure.
