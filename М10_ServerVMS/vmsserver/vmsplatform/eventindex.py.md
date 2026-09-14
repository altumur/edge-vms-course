# eventindex.py — the event "database", which is a cache: a SQLite index over every subsystem's buckets on every resource

**Role in the module.** Lesson 3, the cluster-side reader of `events.py`. Events are written by workers into per-unit buckets on their server's resource; cross-unit search needs one index over all of them, and this is it: one per cluster (or per box), a SQLite table it can rebuild entirely by re-reading every resource's closed buckets over the resource's HTTP routes. It discovers subsystems and units from the resource heartbeats' `units` field, so a new subsystem is indexed the pass after it starts writing, with no change here. It knows nothing about what an event means: `cam` is a field an event may carry, indexed if present. Its two properties are the controller's in the form that matters here: it holds nothing it cannot rebuild, and nothing running depends on it. No controller writes events. A failed-over instance says *catching up* until its rebuild is done rather than answering short. `console.SpecConsole` serves `/events` from an instance when one is passed as `index`; none of the 47 tests construct one (it is exercised on the box / in М11).

## Module-level names
None.

## `class ResourceReader`
HTTP against the resource job (`resource.serve`); tests would substitute a directory reader with the same four methods.

### `__init__(self, timeout=3.0)`
### `buckets(self, url, sub, unit) -> list[Bucket]`
`GET <url>/buckets/<sub>/<unit>`, parsed with `bucket_from_line`.
### `events(self, url, b) -> list[dict]`
`GET <url>/events/<b.path>`, one dict per line.
### `mirrored(self, url, server) -> list[Bucket]`
`GET <url>/mirrored/<server>` — a peer's copies of `server`'s buckets.
### `mirrored_events(self, url, server, b) -> list[dict]`
`GET <url>/events/.mirror/<server>/<b.path>`.

## `class EventIndex`
The index. State: a SQLite connection (`check_same_thread=False`, so the console's threaded server may query it), `state` (`"empty"`, `"catching up"`, `"live[; … unreachable][; … from mirror]"`) and `indexed_segments`.

### `__init__(self, reader, path=":memory:", wall=time.time, lost_after=45.0)`
Creates two tables: `seen (server, path)` primary key — which buckets have been ingested, keyed by the real owning server and the original path; and `events (subsystem, unit, cam, epoch, t, kind, server, path, fields)` with indexes on `(cam, t)`, `(subsystem, unit, t)` and `(kind, t)`. `fields` holds the remaining event keys as JSON. `lost_after` is how old a resource heartbeat may be before the resource counts as silent.

### `rebuild(self, resources) -> dict`
From nothing — what a failed-over instance does first: truncate both tables, zero the counter, then `tail(resources, rebuild=True)`.

### `tail(self, resources, rebuild=False) -> dict`
`resources` is `resources_seen()` (`{server: heartbeat}`). Sets `state` to "catching up" on a rebuild. Splits servers into live and silent by `ts`. For a silent server, try `_from_mirror`; if no live peer holds copies, list it as unreachable. For a live server, walk `heartbeat["units"]` and, for each bucket the resource reports, skip it if it has no events or is already in `seen`; otherwise fetch its lines and insert one row each — `cam` is the event's `cam` field, or the unit id itself when the unit is numeric (a numeric unit is its own `cam`; others may name one) — then mark the bucket seen, all in one transaction per bucket. Any exception while talking to a live server (fresh heartbeat, server not answering) marks it unreachable. Ends by setting `state` to "live" plus the unreachable and from-mirror lists, and returns `{added, unreachable, from_mirror, segments}`. Because only closed buckets are reported by the resource and `seen` is keyed per bucket, tailing is idempotent and each bucket is read once.

### `_from_mirror(self, server, live) -> bool`
A silent server's closed buckets, from whichever live peer's heartbeat says `mirrors` includes it. Rows are inserted under the real server and original path — only the source differed — so a later `tail` from the recovered server skips them as seen. Returns False if nobody holds copies; a peer that fails is skipped. Sets `_last_mirror_added` for `tail` to sum. Never used while the resource answers.

### `query(self, t0, t1, cam=None, kind=None, subsystem=None, unit=None, current_epochs=None, limit=1000) -> dict`
Time window plus optional equality filters, ordered by `t`, limited. `current_epochs` is `{(subsystem, unit): epoch}` — fencing is per unit, and only the unit's own subsystem knows its current epoch; the index just compares and sets `fenced: epoch < current`. Each result row is `{subsystem, unit, cam, epoch, t, kind, server, bucket, fenced, **fields}`; the reply is `{events, state}` so a caller can tell a short answer during catch-up from a complete one. The console builds `current_epochs` from `<sub>/epoch/*`.

### `forget(self, server, paths) -> int`
Retention on a resource removed a segment: delete its events and its `seen` row so a re-mirrored copy would not be refused. Returns the count of paths.

## Notes
- The `cam` column exists so a VMS timeline can ask "everything about camera 7 from any subsystem" without the index knowing what a camera is; the console's `/events?cam=` maps onto it.
- Nothing is ever updated in place: rows are inserted per bucket and deleted per bucket, matching the resource's file-level retention.
